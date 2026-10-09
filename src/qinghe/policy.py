"""制度库：有效性判定、检索与维护。

分工边界（需求明确）：
- **制度由行政专员维护** → 新增/修订/废止都必须持有 ``policy:write``，
  每次变更写入审计，并保留版本与生效期，不允许直接覆盖历史。
- **业务事实由周敏核对** → 制度库只管"规则"，不记录任何个案事实。

有效性的判定口径：
- 员工问"现在能不能报" → 以 **今天** 为基准找有效制度；
- 员工问"去年 X 月发生的那笔怎么报" → 以 **业务发生日** 为基准，
  这样历史业务不会被新制度错误追溯（跨期新旧制度并存是常态）。
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any, Iterable, Optional

from .audit import DENIED, AuditLog
from .auth import SCOPE_POLICY_WRITE, Authorizer
from .models import (
    DRAFT,
    PUBLISHED,
    REPEALED,
    STATUS_LABELS,
    Clause,
    Policy,
    PolicyHit,
    Principal,
)

# 费用类型识别词。用于把自然语言问题落到某个制度类别上。
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "差旅": ("出差", "差旅", "机票", "高铁", "动车", "住宿", "行程", "外派", "驻场"),
    "招待": ("招待", "宴请", "接待", "请客", "客户吃饭", "商务餐"),
    "采购": ("采购", "办公用品", "耗材", "比价", "请购", "验收", "买电脑", "买设备"),
    "交通补贴": ("交通补贴", "通勤", "里程", "自驾", "油费补贴"),
}


class PolicyMaintenanceError(PermissionError):
    pass


class PolicyLibrary:
    def __init__(self, policies: Iterable[Policy], audit: AuditLog, authorizer: Authorizer,
                 on_change=None, clock=date.today) -> None:
        self._policies: list[Policy] = list(policies)
        self.audit = audit
        self.authorizer = authorizer
        self._on_change = on_change
        self.clock = clock          # 可注入时钟：让"今天"可被测试固定，避免回归随日期漂移

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @property
    def policies(self) -> list[Policy]:
        return list(self._policies)

    def get(self, policy_id: str) -> Optional[Policy]:
        return next((p for p in self._policies if p.policy_id == policy_id), None)

    def list_effective(self, on: Optional[date] = None) -> list[Policy]:
        on = on or self.clock()
        return [p for p in self._policies if p.is_effective(on)]

    def list_not_effective(self, on: Optional[date] = None) -> list[tuple[Policy, str]]:
        """返回 (制度, 失效理由)。用于向用户解释"为什么查不到可依据的制度"。"""
        on = on or self.clock()
        out: list[tuple[Policy, str]] = []
        for p in self._policies:
            ok, reason = p.effectiveness(on)
            if not ok:
                out.append((p, reason))
        return out

    @staticmethod
    def detect_category(text: str) -> Optional[str]:
        best, best_score = None, 0
        for category, words in CATEGORY_KEYWORDS.items():
            score = sum(1 for w in words if w in text)
            if score > best_score:
                best, best_score = category, score
        return best

    def effective_for_category(self, category: str, on: Optional[date] = None) -> list[Policy]:
        on = on or self.clock()
        hits = [p for p in self._policies if p.category == category and p.is_effective(on)]
        # 同一类别可能存在多版本并存（旧版对历史业务仍有效），新版优先
        return sorted(hits, key=lambda p: p.effective_from, reverse=True)

    def search(self, query: str, on: Optional[date] = None,
               category: Optional[str] = None, top_k: int = 3) -> list[PolicyHit]:
        """制度检索。返回结果中既包含命中且有效的制度，也包含命中但当前不可用的制度
        （后者必须向用户说明原因，而不是静默丢弃）。"""
        on = on or self.clock()
        category = category or self.detect_category(query)
        hits: list[PolicyHit] = []

        # 费用类别是硬过滤：一旦确定类别，其他类别的制度不得因"报销"这类通用词被召回，
        # 否则会出现"问采购、答差旅"的串档。
        candidates = [p for p in self._policies if p.category == category] if category else list(self._policies)

        for policy in candidates:
            score = 0.0
            if category and policy.category == category:
                score += 3.0
            for word in (query,):
                if policy.title and policy.title in word:
                    score += 2.0
            for tag_word in self._query_tokens(query):
                if tag_word in policy.search_text():
                    score += 1.0
            if score <= 0:
                continue

            matched = [c for c in policy.clauses if any(t in c.text for t in self._query_tokens(query))] \
                if self._query_tokens(query) else []
            if not matched:
                matched = policy.clauses[:2]

            effective, reason = policy.effectiveness(on)
            hits.append(PolicyHit(policy=policy, matched_clauses=matched,
                                  effective=effective, reason=reason, score=score))
            hits.sort(key=lambda h: (0 if h.effective else 1, -h.score))
        return hits[:top_k]

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        tokens: list[str] = []
        for words in CATEGORY_KEYWORDS.values():
            tokens.extend(w for w in words if w in query)
        for extra in ("报销", "标准", "上限", "时限", "材料", "发票", "审批", "补助", "超标"):
            if extra in query:
                tokens.append(extra)
        return tokens

    # ------------------------------------------------------------------
    # 维护（仅行政专员）
    # ------------------------------------------------------------------

    def _assert_maintainer(self, principal: Principal, target: str) -> None:
        if self.authorizer.can(principal, SCOPE_POLICY_WRITE):
            return
        self.audit.record(principal.user_id, "policy.maintain", target, DENIED,
                          "非行政专员尝试维护制度")
        raise PolicyMaintenanceError(
            "制度由行政专员维护，你的角色无权新增、修订或废止制度。"
            "如发现制度与实际不符，请提交《制度修订申请》给行政专员李静。该次尝试已记录。"
        )

    def publish(self, principal: Principal, *, title: str, category: str, version: str,
                doc_no: str, effective_from: str, effective_to: Optional[str] = None,
                clauses: Optional[list[dict[str, Any]]] = None, material_set: Optional[str] = None,
                summary: str = "", supersedes: Optional[str] = None) -> Policy:
        self._assert_maintainer(principal, f"publish:{title}")
        policy_id = f"QH-POL-{len(self._policies) + 1:04d}"
        policy = Policy(
            policy_id=policy_id,
            title=title,
            doc_no=doc_no,
            version=version,
            status=PUBLISHED,
            effective_from=effective_from,
            effective_to=effective_to,
            category=category,
            owner=principal.user_id,
            updated_at=self.clock().isoformat(),
            summary=summary,
            material_set=material_set or category,
            clauses=[Clause.from_dict(c) for c in (clauses or [])],
            supersedes=supersedes,
        )
        self._policies.append(policy)
        if supersedes and (old := self.get(supersedes)):
            old.status = REPEALED
            old.effective_to = effective_from
        self.audit.record(principal.user_id, "policy.publish", policy_id, "allowed",
                          f"{policy.doc_no} {policy.version} 自 {effective_from} 起生效"
                          + (f"，取代 {supersedes}" if supersedes else ""))
        if self._on_change:
            self._on_change()
        return policy

    def revise(self, principal: Principal, policy_id: str, *, version: str, effective_from: str,
               clauses: Optional[list[dict[str, Any]]] = None,
               summary: Optional[str] = None, effective_to: Optional[str] = None,
               doc_no: Optional[str] = None) -> Policy:
        """修订 = 生成新版本 + 旧版按期终止（保留历史，不覆盖）。"""
        self._assert_maintainer(principal, f"revise:{policy_id}")
        old = self.get(policy_id)
        if old is None:
            raise PolicyMaintenanceError(f"制度 {policy_id} 不存在。")
        before = copy.deepcopy(old.to_dict())

        new = Policy(
            policy_id=f"{policy_id}-{version}",
            title=old.title,
            doc_no=doc_no or old.doc_no,
            version=version,
            status=PUBLISHED,
            effective_from=effective_from,
            effective_to=effective_to,
            category=old.category,
            owner=principal.user_id,
            updated_at=self.clock().isoformat(),
            summary=summary if summary is not None else old.summary,
            material_set=old.material_set,
            clauses=[Clause.from_dict(c) for c in clauses] if clauses else copy.deepcopy(old.clauses),
            supersedes=old.policy_id,
        )
        old.status = REPEALED
        old.effective_to = effective_from
        self._policies.append(new)
        self.audit.record(principal.user_id, "policy.revise", new.policy_id, "allowed",
                          f"{new.doc_no} 由 {before['version']} 修订为 {version}，自 {effective_from} 起生效")
        if self._on_change:
            self._on_change()
        return new

    def repeal(self, principal: Principal, policy_id: str, *, effective_to: str, reason: str) -> Policy:
        self._assert_maintainer(principal, f"repeal:{policy_id}")
        policy = self.get(policy_id)
        if policy is None:
            raise PolicyMaintenanceError(f"制度 {policy_id} 不存在。")
        policy.status = REPEALED
        policy.effective_to = effective_to
        policy.updated_at = self.clock().isoformat()
        self.audit.record(principal.user_id, "policy.repeal", policy_id, "allowed",
                          f"废止于 {effective_to}，原因：{reason}")
        if self._on_change:
            self._on_change()
        return policy

    def maintenance_view(self, principal: Principal) -> str:
        """给行政专员的制度台账视图。"""
        self.authorizer.require(principal, SCOPE_POLICY_WRITE, "policy.ledger")
        today = self.clock()
        lines = [f"制度台账（共 {len(self._policies)} 条，基准日 {today}）"]
        for p in self._policies:
            ok, reason = p.effectiveness(today)
            flag = "生效中" if ok else "不可用"
            lines.append(f"  [{flag}] {p.policy_id}｜{p.citation}｜类别 {p.category}｜维护人 {p.owner}｜{reason}")
        return "\n".join(lines)

    @staticmethod
    def status_label(status: str) -> str:
        return STATUS_LABELS.get(status, status)
