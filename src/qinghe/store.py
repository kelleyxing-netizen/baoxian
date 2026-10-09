"""数据访问层（可替换为真实业务系统接口）。

原型用 JSON 仓储，真实落地时直接换成：
- 制度库 → 行政制度管理系统 / 知识库 API
- 单据   → 报销系统（费控）单据接口
- 用户   → 统一身份认证 / HR 主数据
接口签名保持不变即可，上层 agent / tools 不需要改动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Delegation, Policy, Ticket, User


class Repository:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.users: list[User] = []
        self.delegations: list[Delegation] = []
        self.policies: list[Policy] = []
        self.material_sets: dict[str, dict[str, Any]] = {}
        self.tickets: list[Ticket] = []

    @classmethod
    def load(cls, root: Path | str) -> "Repository":
        repo = cls(root)
        users_raw = cls._read(repo.root / "users.json")
        repo.users = [User.from_dict(u) for u in users_raw.get("users", [])]
        repo.delegations = [Delegation.from_dict(d) for d in users_raw.get("delegations", [])]
        policies_raw = cls._read(repo.root / "policies.json")
        repo.policies = [Policy.from_dict(p) for p in policies_raw.get("policies", [])]
        materials_raw = cls._read(repo.root / "materials.json")
        repo.material_sets = materials_raw.get("sets", {})
        tickets_raw = cls._read(repo.root / "tickets.json")
        repo.tickets = [Ticket.from_dict(t) for t in tickets_raw.get("tickets", [])]
        return repo

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # -- 写回（原型用；真实系统走各自 API） --------------------------------

    def save_policies(self) -> None:
        path = self.root / "policies.json"
        payload = {"policies": [p.to_dict() for p in self.policies]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_tickets(self) -> None:
        path = self.root / "tickets.json"
        payload = {"tickets": [t.to_dict() for t in self.tickets]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 查询辅助 ----------------------------------------------------------

    def user(self, user_id: str) -> User | None:
        return next((u for u in self.users if u.user_id == user_id), None)

    def delegations_of(self, user_id: str) -> list[Delegation]:
        return [d for d in self.delegations if d.grantee == user_id or d.grantor == user_id]

    def ticket(self, doc_no: str) -> Ticket | None:
        return next((t for t in self.tickets if t.doc_no == doc_no), None)

    def tickets_of(self, user_id: str) -> list[Ticket]:
        return [t for t in self.tickets if t.subject == user_id]

    def next_doc_no(self, on: str) -> str:
        """生成单号：QH + 年月 + 4 位流水。真实系统由费控系统分配。"""
        prefix = f"QH{on[0:4]}{on[5:7]}"
        seq = [t.doc_no for t in self.tickets if t.doc_no.startswith(prefix)]
        return f"{prefix}{len(seq) + 1:04d}"
