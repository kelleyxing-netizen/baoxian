"""领域模型。

三条硬约束体现在数据模型上：

1. 制度是"规则知识"，有版本、有效期和维护责任人（行政专员），
   有效性由 ``status`` 与 ``effective_from/to`` 共同决定，不靠人工记忆。
2. 业务事实（票据真伪、事由真实性、金额该不该批）不是模型能判定的字段，
   单据上只有 ``facts_verified`` 标记 + 固定核对人 ``reviewer``，由周敏填写。
3. 单据必有数据主体 ``subject``，状态查询围绕它做授权判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

# --------------------------------------------------------------------------
# 角色
# --------------------------------------------------------------------------

ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin_specialist"
ROLE_REVIEWER = "reviewer"
ROLE_AGENT = "agent"
ROLE_CONTRACTOR = "contractor"

ROLE_LABELS = {
    ROLE_EMPLOYEE: "员工",
    ROLE_ADMIN: "行政专员（制度维护）",
    ROLE_REVIEWER: "财务核对人（周敏）",
    ROLE_AGENT: "智能体服务账号",
    ROLE_CONTRACTOR: "外包/临时账号（未开通报销查询）",
}


def parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def money(amount: float) -> str:
    return f"¥{amount:,.2f}"


# --------------------------------------------------------------------------
# 人与授权
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    dept: str
    roles: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "User":
        return cls(
            user_id=raw["user_id"],
            name=raw["name"],
            dept=raw.get("dept", ""),
            roles=tuple(raw.get("roles", ())),
        )

    @property
    def role_labels(self) -> list[str]:
        return [ROLE_LABELS.get(r, r) for r in self.roles]


@dataclass(frozen=True)
class Delegation:
    """一次显式授权：grantor 允许 grantee 以其名义查询单据。"""

    grantor: str
    grantee: str
    scopes: tuple[str, ...]
    granted_at: str
    expires_on: Optional[str]
    reason: str
    approved_by: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Delegation":
        return cls(
            grantor=raw["grantor"],
            grantee=raw["grantee"],
            scopes=tuple(raw.get("scopes", ())),
            granted_at=raw["granted_at"],
            expires_on=raw.get("expires_on"),
            reason=raw.get("reason", ""),
            approved_by=raw.get("approved_by", ""),
        )

    def covers(self, scope: str, scope_owner: str, on: date) -> bool:
        """grantee 是否在 on 日、就 scope_owner 的单据取得 scope 授权。"""
        if self.grantor != scope_owner:
            return False
        if scope not in self.scopes:
            return False
        expiry = parse_date(self.expires_on)
        return expiry is None or on <= expiry


@dataclass(frozen=True)
class Principal:
    """当前调用者身份。智能体所有动作都以调用者身份执行，不做身份提权。"""

    user: User
    delegations: tuple[Delegation, ...] = ()

    @property
    def user_id(self) -> str:
        return self.user.user_id

    @property
    def name(self) -> str:
        return self.user.name

    @property
    def roles(self) -> tuple[str, ...]:
        return self.user.roles

    def has_role(self, role: str) -> bool:
        return role in self.user.roles


# --------------------------------------------------------------------------
# 制度
# --------------------------------------------------------------------------

DRAFT = "draft"
PUBLISHED = "published"
REPEALED = "repealed"

STATUS_LABELS = {DRAFT: "草稿", PUBLISHED: "已发布", REPEALED: "已废止"}


@dataclass
class Clause:
    no: str
    text: str
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Clause":
        return cls(no=raw["no"], text=raw["text"], tags=tuple(raw.get("tags", ())))

    def render(self) -> str:
        return f"{self.no} {self.text}"


@dataclass
class Policy:
    policy_id: str
    title: str
    doc_no: str
    version: str
    status: str
    effective_from: str
    effective_to: Optional[str]
    category: str
    owner: str
    updated_at: str
    summary: str
    material_set: str
    clauses: list[Clause] = field(default_factory=list)
    supersedes: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Policy":
        return cls(
            policy_id=raw["policy_id"],
            title=raw["title"],
            doc_no=raw["doc_no"],
            version=raw["version"],
            status=raw["status"],
            effective_from=raw["effective_from"],
            effective_to=raw.get("effective_to"),
            category=raw["category"],
            owner=raw["owner"],
            updated_at=raw["updated_at"],
            summary=raw.get("summary", ""),
            material_set=raw.get("material_set", raw["category"]),
            clauses=[Clause.from_dict(c) for c in raw.get("clauses", [])],
            supersedes=raw.get("supersedes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "title": self.title,
            "doc_no": self.doc_no,
            "version": self.version,
            "status": self.status,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "category": self.category,
            "owner": self.owner,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "material_set": self.material_set,
            "supersedes": self.supersedes,
            "clauses": [{"no": c.no, "text": c.text, "tags": list(c.tags)} for c in self.clauses],
        }

    # -- 有效性 -------------------------------------------------------------

    def effectiveness(self, on: date) -> tuple[bool, str]:
        """在 on 日（业务发生日或今日）是否有效，并说明理由。

        注意"已废止"与"当时有效"并不矛盾：`effective_to` 界定了制度的效力区间，
        废止只是宣布该区间不再延续。因此对发生在区间内的历史业务，
        该版本仍是正确依据（且通常只能用它，新版尚未生效）。
        """
        if self.status == DRAFT:
            return False, f"草稿状态，尚未发布（拟 {self.effective_from} 起生效）"
        start = parse_date(self.effective_from)
        end = parse_date(self.effective_to)
        if start and on < start:
            return False, f"尚未生效，自 {self.effective_from} 起适用"
        if end and on > end:
            note = "已废止" if self.status == REPEALED else "已过有效期"
            return False, f"{note}，效力止于 {self.effective_to}"
        window = f"{self.effective_from} 起" + (f"至 {self.effective_to}" if self.effective_to else "长期有效")
        if self.status == REPEALED:
            return True, f"{self.version} 在业务发生日有效（{window}，现已废止）"
        return True, f"{self.version} 有效（{window}）"

    def is_effective(self, on: date) -> bool:
        return self.effectiveness(on)[0]

    @property
    def citation(self) -> str:
        """引用标识。所有涉及制度的答复都必须带它。"""
        return f"{self.title}（{self.doc_no}，{self.version}）"

    def clause(self, no: str) -> Optional[Clause]:
        return next((c for c in self.clauses if c.no == no), None)

    def search_text(self) -> str:
        parts = [self.title, self.category, self.summary, self.doc_no]
        parts.extend(c.text for c in self.clauses)
        return " ".join(parts)


@dataclass
class PolicyHit:
    """一次制度检索命中。"""

    policy: Policy
    matched_clauses: list[Clause]
    effective: bool
    reason: str
    score: float

    @property
    def citation(self) -> str:
        return self.policy.citation


# --------------------------------------------------------------------------
# 材料
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialItem:
    code: str
    name: str
    required: bool
    condition: Optional[str]
    note: Optional[str]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MaterialItem":
        return cls(
            code=raw["code"],
            name=raw["name"],
            required=bool(raw.get("required", False)),
            condition=raw.get("condition"),
            note=raw.get("note"),
        )

    @property
    def label(self) -> str:
        return f"[{self.code}] {self.name}"


@dataclass
class MaterialSet:
    set_id: str
    name: str
    items: list[MaterialItem]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MaterialSet":
        return cls(
            set_id=raw["set_id"],
            name=raw["name"],
            items=[MaterialItem.from_dict(i) for i in raw.get("items", [])],
        )


@dataclass
class ChecklistResult:
    """材料核对结果。智能体只做"清单 vs 已提交"的机械比对，不判断材料内容真实性。"""

    set_id: str
    set_name: str
    required: list[MaterialItem]
    conditional: list[MaterialItem]
    provided: list[MaterialItem]
    missing_required: list[MaterialItem]
    unresolved_conditional: list[MaterialItem]

    @property
    def complete(self) -> bool:
        return not self.missing_required

    def render(self) -> str:
        lines = [f"{self.set_name}（共 {len(self.required) + len(self.conditional)} 项）"]
        for item in self.required:
            mark = "已提交" if item in self.provided else "缺失"
            lines.append(f"  - {item.label}：{mark}")
        for item in self.conditional:
            mark = "已提交" if item in self.provided else "待确认"
            lines.append(f"  - {item.label}：{mark}（条件：{item.condition}）")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 单据
# --------------------------------------------------------------------------


@dataclass
class TicketEvent:
    at: str
    node: str
    actor: str
    action: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TicketEvent":
        return cls(at=raw["at"], node=raw["node"], actor=raw["actor"], action=raw["action"])

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at, "node": self.node, "actor": self.actor, "action": self.action}


@dataclass
class Ticket:
    doc_no: str
    subject: str
    category: str
    amount: float
    incurred_on: str
    status: str
    current_node: str
    facts_verified: bool
    reviewer: str
    policy_ref: Optional[str]
    materials_submitted: list[str]
    created_at: str
    history: list[TicketEvent] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Ticket":
        return cls(
            doc_no=raw["doc_no"],
            subject=raw["subject"],
            category=raw["category"],
            amount=float(raw["amount"]),
            incurred_on=raw["incurred_on"],
            status=raw["status"],
            current_node=raw["current_node"],
            facts_verified=bool(raw.get("facts_verified", False)),
            reviewer=raw.get("reviewer", "R3001"),
            policy_ref=raw.get("policy_ref"),
            materials_submitted=list(raw.get("materials_submitted", [])),
            created_at=raw["created_at"],
            history=[TicketEvent.from_dict(e) for e in raw.get("history", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_no": self.doc_no,
            "subject": self.subject,
            "category": self.category,
            "amount": self.amount,
            "incurred_on": self.incurred_on,
            "status": self.status,
            "current_node": self.current_node,
            "facts_verified": self.facts_verified,
            "reviewer": self.reviewer,
            "policy_ref": self.policy_ref,
            "materials_submitted": list(self.materials_submitted),
            "created_at": self.created_at,
            "history": [e.to_dict() for e in self.history],
        }

    def render_progress(self) -> str:
        lines = [
            f"单据 {self.doc_no}｜{self.category}｜{money(self.amount)}｜业务发生日 {self.incurred_on}",
            f"当前状态：{self.status}（节点：{self.current_node}）",
        ]
        if self.history:
            lines.append("流转记录：")
            lines.extend(f"  {e.at} {e.node} · {e.action}" for e in self.history)
        else:
            lines.append("流转记录：暂无（单据尚未进入流程）")
        lines.append("业务事实核对：已完成" if self.facts_verified else "业务事实核对：待周敏核对（智能助手不作判定）")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


@dataclass
class AuditEvent:
    ts: str
    actor: str
    action: str
    target: str
    result: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "result": self.result,
            "detail": self.detail,
        }
