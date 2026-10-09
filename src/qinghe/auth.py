"""身份、角色与授权。

核心原则：**智能体不提权**。

- 智能体的能力上限是 ``AGENT_MAX_SCOPES``，任何人都不能通过对话获得超出上限的动作；
- 调用者的实际权限 = 其角色自带范围 ∩ 智能体能力上限，再加上他人对自己的显式委派；
- 查询单据时必须同时满足"有 ``ticket:read:self`` 范围内的权限"和"对象是本人（或有委派）"，
  两个条件缺一不可，只有前者会退化成"能查所有人"。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from .audit import DENIED, AuditLog
from .models import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_EMPLOYEE,
    ROLE_REVIEWER,
    Delegation,
    Principal,
    Ticket,
    User,
)

# --------------------------------------------------------------------------
# 权限范围
# --------------------------------------------------------------------------

SCOPE_POLICY_READ = "policy:read"
SCOPE_POLICY_WRITE = "policy:write"
SCOPE_MATERIAL_READ = "material:read"
SCOPE_MATERIAL_WRITE = "material:write"
SCOPE_REIMBURSE_SUBMIT = "reimburse:submit"
SCOPE_TICKET_READ_SELF = "ticket:read:self"
SCOPE_TICKET_READ_ANY = "ticket:read:any"
SCOPE_TICKET_REVIEW = "ticket:review"
SCOPE_FACTS_VERIFY = "facts:verify"

SCOPE_LABELS = {
    SCOPE_POLICY_READ: "查询有效制度",
    SCOPE_POLICY_WRITE: "维护制度（新增/修订/废止）",
    SCOPE_MATERIAL_READ: "查询材料要求",
    SCOPE_MATERIAL_WRITE: "维护材料清单",
    SCOPE_REIMBURSE_SUBMIT: "发起报销单",
    SCOPE_TICKET_READ_SELF: "查询本人单据状态",
    SCOPE_TICKET_READ_ANY: "查询他人单据（仅财务核对责任人）",
    SCOPE_TICKET_REVIEW: "推进单据流程",
    SCOPE_FACTS_VERIFY: "核对业务事实",
}

ROLE_SCOPES: dict[str, set[str]] = {
    ROLE_EMPLOYEE: {
        SCOPE_POLICY_READ,
        SCOPE_MATERIAL_READ,
        SCOPE_REIMBURSE_SUBMIT,
        SCOPE_TICKET_READ_SELF,
    },
    ROLE_ADMIN: {
        SCOPE_POLICY_READ,
        SCOPE_POLICY_WRITE,
        SCOPE_MATERIAL_READ,
        SCOPE_MATERIAL_WRITE,
    },
    ROLE_REVIEWER: {
        SCOPE_POLICY_READ,
        SCOPE_MATERIAL_READ,
        SCOPE_TICKET_READ_ANY,
        SCOPE_TICKET_REVIEW,
        SCOPE_FACTS_VERIFY,
    },
    ROLE_AGENT: {
        SCOPE_POLICY_READ,
        SCOPE_MATERIAL_READ,
        SCOPE_REIMBURSE_SUBMIT,
        SCOPE_TICKET_READ_SELF,
        SCOPE_TICKET_READ_ANY,
    },
}

# 智能体能力上限：注意里面**没有** facts:verify 与 ticket:review。
# 业务事实只能由周敏在财务系统核对、流程只能由审批人在各自系统推进，
# 智能体对业务事实"可读不可判"、对流程"可查不可推"。
# 制度维护（policy:write / material:write）**在**上限内，因为行政专员本就是制度责任人，
# 助手只是他的一个操作渠道，仍然受角色校验与审计约束。
AGENT_MAX_SCOPES: set[str] = {
    SCOPE_POLICY_READ,
    SCOPE_POLICY_WRITE,
    SCOPE_MATERIAL_READ,
    SCOPE_MATERIAL_WRITE,
    SCOPE_REIMBURSE_SUBMIT,
    SCOPE_TICKET_READ_SELF,
    SCOPE_TICKET_READ_ANY,  # 仅解锁"待我核对"队列，不解锁他人单据明细
}


class AuthError(PermissionError):
    """鉴权失败。调用方应把 message 原样回给用户，并已写入审计。"""


def base_scopes(roles: Iterable[str]) -> set[str]:
    scopes: set[str] = set()
    for role in roles:
        scopes |= ROLE_SCOPES.get(role, set())
    return scopes


def effective_scopes(principal: Principal, on: Optional[date] = None) -> set[str]:
    """调用者在智能体里真正可用的权限范围。"""
    on = on or date.today()
    scopes = base_scopes(principal.roles) & AGENT_MAX_SCOPES
    for delegation in principal.delegations:
        if delegation.grantee != principal.user_id:
            continue
        for scope in delegation.scopes:
            if scope not in AGENT_MAX_SCOPES:
                continue
            if delegation.expires_on is None or on <= date.fromisoformat(delegation.expires_on):
                scopes.add(scope)
    return scopes


class Authorizer:
    def __init__(self, audit: AuditLog, allow_reviewer_access: bool = True,
                 clock=date.today) -> None:
        self.audit = audit
        self.allow_reviewer_access = allow_reviewer_access
        self.clock = clock

    # -- 通用 scope 校验 ---------------------------------------------------

    def require(self, principal: Principal, scope: str, target: str = "") -> None:
        if scope in effective_scopes(principal):
            return
        self.audit.record(
            principal.user_id,
            "auth.require",
            target or scope,
            DENIED,
            f"缺少权限 {scope}（{SCOPE_LABELS.get(scope, scope)}）",
        )
        raise AuthError(
            f"你的角色（{'、'.join(principal.user.role_labels) or '未分配'}）没有「{SCOPE_LABELS.get(scope, scope)}」权限，"
            "该操作已记录。如确需办理，请联系对应责任人。"
        )

    def can(self, principal: Principal, scope: str) -> bool:
        return scope in effective_scopes(principal)

    # -- 单据访问 ----------------------------------------------------------

    def ticket_access_reason(self, principal: Principal, ticket: Ticket, on: Optional[date] = None) -> Optional[str]:
        """返回可访问的理由；None 表示无权访问。"""
        on = on or self.clock()
        if principal.user_id == ticket.subject:
            return "本人单据"
        for delegation in principal.delegations:
            if delegation.covers(SCOPE_TICKET_READ_SELF, ticket.subject, on) and delegation.grantee == principal.user_id:
                return f"经 {ticket.subject} 授权代查（{delegation.granted_at} 起，{delegation.expires_on or '长期'}）"
        if (
            self.allow_reviewer_access
            and principal.has_role(ROLE_REVIEWER)
            and ticket.reviewer == principal.user_id
        ):
            return "本人为该单据的业务事实核对责任人"
        return None

    def assert_ticket_access(self, principal: Principal, ticket: Ticket) -> str:
        reason = self.ticket_access_reason(principal, ticket)
        if reason is None:
            self.audit.record(
                principal.user_id,
                "ticket.status.read",
                ticket.doc_no,
                DENIED,
                f"越权尝试：单据属于 {ticket.subject}",
            )
            raise AuthError(
                f"单据 {ticket.doc_no} 不属于你，无法查询。"
                "单据状态只能由本人查询；他人代查需要本人在系统内出具授权。该次尝试已记录。"
            )
        self.audit.record(principal.user_id, "ticket.status.read", ticket.doc_no, "allowed", reason)
        return reason


def build_principal(user: User, delegations: Iterable[Delegation]) -> Principal:
    return Principal(user=user, delegations=tuple(delegations))
