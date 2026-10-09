"""可运行演示 / 交互命令行。

用法：
    python run.py                # 交互对话（在 data/ 的临时副本上运行，不改动样例数据）
    python run.py demo           # 跑完整剧本，展示四条工作流与三条边界
    python run.py --data ./data  # 指定数据目录（会写回 tickets.json）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qinghe.agent import RuleBasedPlanner, QingheAgent  # noqa: E402
from qinghe.audit import AuditLog  # noqa: E402
from qinghe.auth import Authorizer, build_principal  # noqa: E402
from qinghe.material import MaterialCatalog  # noqa: E402
from qinghe.models import Principal  # noqa: E402
from qinghe.policy import PolicyLibrary  # noqa: E402
from qinghe.store import Repository  # noqa: E402
from qinghe.ticket import TicketService  # noqa: E402
from qinghe.tools import NOT_PROVIDED, TOOL_SPECS, Services, ToolRegistry  # noqa: E402

DEFAULT_DATA = PROJECT_ROOT / "data"
TMP_ROOT = PROJECT_ROOT / ".tmp"


def scratch_dir(prefix: str) -> Path:
    """在工作区内创建临时目录。

    注意：不使用 ``tempfile.mkdtemp``，它在 Windows 上会创建 0o700（不继承权限）的目录，
    在受限令牌下会导致该目录内的后续写入被拒绝。这里用默认权限创建，继承父目录 ACL。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(64):
        path = TMP_ROOT / f"{prefix}{uuid.uuid4().hex[:10]}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError("无法创建临时目录")


class Harness:
    def __init__(self, data_dir: Path, audit_path: Path | None = None,
                 clock: Callable[[], date] = date.today) -> None:
        self.clock = clock
        self.repo = Repository.load(data_dir)
        self.audit = AuditLog(audit_path)
        self.authorizer = Authorizer(self.audit, clock=clock)
        self.policy = PolicyLibrary(self.repo.policies, self.audit, self.authorizer,
                                    on_change=self.repo.save_policies, clock=clock)
        self.catalog = MaterialCatalog(self.repo.material_sets, clock=clock)
        self.tickets = TicketService(self.repo, self.authorizer, self.audit, clock=clock)
        self.services = Services(policy=self.policy, catalog=self.catalog,
                                 tickets=self.tickets, authorizer=self.authorizer,
                                 audit=self.audit, repo=self.repo, clock=clock)
        self.registry = ToolRegistry(self.services)
        self.agent = QingheAgent(self.services, self.registry, RuleBasedPlanner())
        self._principals: dict[str, Principal] = {}

    def principal(self, user_id: str) -> Principal:
        if user_id not in self._principals:
            user = self.repo.user(user_id)
            if user is None:
                raise SystemExit(f"未知用户 {user_id}")
            self._principals[user_id] = build_principal(user, self.repo.delegations_of(user_id))
        return self._principals[user_id]

    def ask(self, user_id: str, text: str) -> str:
        principal = self.principal(user_id)
        reply = self.agent.handle(principal, text)
        return reply.render()


# --------------------------------------------------------------------------
# 剧本
# --------------------------------------------------------------------------

SCRIPT: list[tuple[str, str]] = [
    ("E1001", "我要报销上海出差的费用，9月12日，3860元，材料 TR-01 TR-02 TR-03 TR-04"),
    ("E1001", "QH2026090001 到哪了"),
    ("E1001", "帮我查一下李四的单据 QH2026090042 到哪了"),
    ("E1003", "我的单据 QH2026090033 到哪了"),
    ("A2001", "QH2026090001 到哪了"),
    ("A2001", "制度台账"),
    ("A2001", "我要维护制度，把差旅住宿上限改成 700"),
    ("R3001", "待我核对的单据有哪些"),
    ("R3001", "这张发票是真的吗，能不能直接通过"),
    ("E1001", "疫情防控期间交通补贴还能报吗"),
    ("E1001", "招待客户吃饭人均 260 元怎么报"),
    ("E1001", "你能做什么"),
]

INTRO = """青禾报销助手 · 原型演示
====================================================================
员工自助查有效制度 -> 按制度生成材料清单 -> 发起报销单 -> 经授权查本人单据状态
制度由行政专员维护；业务事实由周敏核对，智能体不判定、不代签
===================================================================="""


def run_demo(harness: Harness) -> None:
    print(INTRO)
    print()
    print("【已注册的工具契约】")
    for spec in TOOL_SPECS:
        flag = "写" if spec.writes else "读"
        print(f"  [{flag}] {spec.name:<28} 权限：{spec.scope}")
    print()
    print("【明确不提供的动作】")
    for name, why in NOT_PROVIDED.items():
        print(f"  x {name:<24} {why}")
    print()

    current = None
    for user_id, text in SCRIPT:
        user = harness.repo.user(user_id)
        if user_id != current:
            current = user_id
            print("-" * 68)
            print(f"当前身份：{user.name}（{user.user_id}／{'、'.join(user.role_labels)}）")
            print("-" * 68)
        print(f"用户提问：{text}")
        print(harness.ask(user_id, text))
        print()


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------


def run_interactive(harness: Harness) -> None:
    print(INTRO)
    print("指令：/users 用户列表　/user E1001 切换身份　/audit 查看审计　/quit 退出")
    print()
    current = "E1001"
    while True:
        user = harness.repo.user(current)
        try:
            text = input(f"[{user.name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/users":
            for u in harness.repo.users:
                print(f"  {u.user_id}  {u.name}  {'、'.join(u.role_labels)}")
            continue
        if text.startswith("/user "):
            candidate = text.split(maxsplit=1)[1].strip()
            if harness.repo.user(candidate):
                current = candidate
                print(f"已切换到 {harness.repo.user(candidate).name}")
            else:
                print("未找到该用户")
            continue
        if text == "/audit":
            for e in harness.audit.events[-20:]:
                print(f"  {e.ts} {e.actor} {e.action} {e.target} [{e.result}] {e.detail}")
            continue
        if text == "/tools":
            for spec in TOOL_SPECS:
                print(f"  {spec.name}（{spec.scope}）")
            continue
        print()
        print(harness.ask(current, text))
        print()


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="青禾报销助手原型")
    parser.add_argument("mode", nargs="?", default="chat", choices=["chat", "demo"])
    parser.add_argument("--data", type=Path, default=None,
                        help="数据目录；缺省使用 data/ 的临时副本，避免污染样例数据")
    args = parser.parse_args(argv)

    temp_dir: Path | None = None
    if args.data:
        data_dir = args.data
    else:
        temp_dir = scratch_dir("data-")
        shutil.copytree(DEFAULT_DATA, temp_dir, dirs_exist_ok=True)
        data_dir = temp_dir

    harness = Harness(data_dir, audit_path=scratch_dir("audit-") / "audit.jsonl")
    try:
        if args.mode == "demo":
            run_demo(harness)
        else:
            run_interactive(harness)
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
