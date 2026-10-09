"""单据：发起报销单、查询流转状态。

授权模型（需求："进入报销流程后，扩展为经授权查询本人单据状态"）：

- **功能级授权**：账号必须被开通 ``ticket:read:self`` 才能使用状态查询，
  有单据 ≠ 有权查（外包/临时账号默认不开通）；
- **对象级授权**：只允许查本人单据；他人代查必须有本人在系统内出具的、
  未过期的委派记录（原型里体现为 ``delegations``）；
- **例外**：单据指定的业务事实核对责任人（周敏）可访问该单据，留痕记录理由。

业务事实的核对**不在智能体能力范围内**：见 ``record_facts_verification``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .audit import AuditLog
from .auth import (
    SCOPE_FACTS_VERIFY,
    SCOPE_REIMBURSE_SUBMIT,
    SCOPE_TICKET_READ_ANY,
    SCOPE_TICKET_READ_SELF,
    AuthError,
    Authorizer,
)
from .models import Principal, Ticket, TicketEvent, money

# 节点 → 下一步提示。真实系统可从流程引擎取，原型固化为规则。
NEXT_STEP: dict[str, str] = {
    "草稿": "材料齐备后提交，单据将进入部门审核。",
    "部门审核": "等待部门负责人审批。若超过 3 个工作日未处理，可提醒部门负责人。",
    "财务复核": "等待财务复核。票据真伪、事由真实性由周敏核对，智能助手不判定。",
    "出纳付款": "已通过复核，等待出纳付款。",
    "归档": "流程已结束。",
    "已入账": "流程已结束，款项已入账。",
    "已退回": "单据被退回，请按退回意见补充材料后重新提交。",
}

SUBMIT_DEADLINE_DAYS = 30  # 与差旅办法 5.1 一致；真实系统应读取制度字段


@dataclass
class StatusReport:
    ticket: Ticket
    access_reason: str
    next_step: str
    timing_note: str
    material_gap: Optional[str] = None

    def render(self) -> str:
        lines = [self.ticket.render_progress()]
        lines.append(f"访问依据：{self.access_reason}")
        lines.append(f"下一步：{self.next_step}")
        if self.timing_note:
            lines.append(f"时效提示：{self.timing_note}")
        return "\n".join(lines)


class TicketService:
    def __init__(self, repo, authorizer: Authorizer, audit: AuditLog,
                 clock=date.today) -> None:
        self.repo = repo
        self.authorizer = authorizer
        self.audit = audit
        self.clock = clock

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------

    def _require_ticket_read(self, principal: Principal, target: str) -> None:
        if self.authorizer.can(principal, SCOPE_TICKET_READ_SELF) or \
                self.authorizer.can(principal, SCOPE_TICKET_READ_ANY):
            return
        self.audit.record(principal.user_id, "ticket.status.read", target, "denied",
                          "账号未开通单据状态查询功能")
        raise AuthError(
            "你的账号尚未开通「查询本人单据状态」功能。"
            "报销已由系统正常受理，如需查询进度请联系财务周敏或你的部门对接人。该次尝试已记录。"
        )

    def _must_own(self, principal: Principal, ticket: Ticket) -> str:
        reason = self.authorizer.ticket_access_reason(principal, ticket)
        if reason is None:
            self.authorizer.assert_ticket_access(principal, ticket)  # 触发拒绝留痕与抛错
        return reason or "本人单据"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def accessible_tickets(self, principal: Principal) -> list[Ticket]:
        """当前身份可查看的单据集合（用于"我有哪些单据"）。"""
        if not (self.authorizer.can(principal, SCOPE_TICKET_READ_SELF)
                or self.authorizer.can(principal, SCOPE_TICKET_READ_ANY)):
            return []
        return [t for t in self.repo.tickets
                if self.authorizer.ticket_access_reason(principal, t) is not None]

    def status(self, principal: Principal, doc_no: str) -> StatusReport:
        self._require_ticket_read(principal, doc_no)
        ticket = self.repo.ticket(doc_no)
        if ticket is None:
            raise LookupError(f"未找到单据 {doc_no}，请核对单号。")
        reason = self._must_own(principal, ticket)
        return StatusReport(
            ticket=ticket,
            access_reason=reason,
            next_step=NEXT_STEP.get(ticket.current_node, "请联系财务确认当前节点。"),
            timing_note=self._timing_note(ticket),
            material_gap=self._material_gap(ticket),
        )

    def list_mine(self, principal: Principal) -> list[Ticket]:
        self._require_ticket_read(principal, "ticket.list")
        if not self.authorizer.can(principal, SCOPE_TICKET_READ_SELF):
            return []
        mine = self.repo.tickets_of(principal.user_id)
        self.audit.record(principal.user_id, "ticket.list", principal.user_id, "allowed",
                          f"返回 {len(mine)} 张单据")
        return mine

    def review_queue(self, principal: Principal) -> list[Ticket]:
        """待核对队列（仅业务事实核对责任人）。队列里不出现他人单据明细。"""
        self.authorizer.require(principal, SCOPE_TICKET_READ_ANY, "ticket.review_queue")
        queue = [t for t in self.repo.tickets if t.reviewer == principal.user_id and not t.facts_verified]
        self.audit.record(principal.user_id, "ticket.review_queue", principal.user_id, "allowed",
                          f"返回 {len(queue)} 张待核对单据")
        return queue

    def _timing_note(self, ticket: Ticket) -> str:
        try:
            incurred = date.fromisoformat(ticket.incurred_on)
        except ValueError:
            return ""
        days = (self.clock() - incurred).days
        if ticket.current_node in ("草稿",) and days > SUBMIT_DEADLINE_DAYS:
            return (f"业务发生日距今 {days} 天，已超过 {SUBMIT_DEADLINE_DAYS} 日提交时限，"
                    "需补《逾期情况说明》并经部门负责人签批。")
        if days > SUBMIT_DEADLINE_DAYS:
            return f"业务发生日距今 {days} 天（已提交，流程正常）。"
        return f"业务发生日距今 {days} 天，在 {SUBMIT_DEADLINE_DAYS} 日时限内。"

    @staticmethod
    def _material_gap(ticket: Ticket) -> Optional[str]:
        if not ticket.materials_submitted:
            return "系统内未见材料清单，请确认是否已上传附件。"
        return None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create(self, principal: Principal, *, category: str, amount: float, incurred_on: str,
               policy_ref: Optional[str], materials: list[str], summary: str = "") -> Ticket:
        self.authorizer.require(principal, SCOPE_REIMBURSE_SUBMIT, "ticket.create")
        if amount <= 0:
            raise ValueError("申报金额必须大于 0。")
        today = self.clock().isoformat()
        doc_no = self.repo.next_doc_no(today)
        ticket = Ticket(
            doc_no=doc_no,
            subject=principal.user_id,          # 只能为自己发起，不接受代他人发起
            category=category,
            amount=float(amount),
            incurred_on=incurred_on,
            status="草稿",
            current_node="草稿",
            facts_verified=False,
            reviewer="R3001",                    # 业务事实核对人固定为周敏
            policy_ref=policy_ref,
            materials_submitted=list(materials),
            created_at=today,
            history=[TicketEvent(at=today, node="草稿", actor=principal.user_id,
                                 action="由青禾报销助手创建报销单草稿")],
        )
        self.repo.tickets.append(ticket)
        self.repo.save_tickets()
        self.audit.record(principal.user_id, "ticket.create", doc_no, "allowed",
                          f"{category} {money(amount)}，依据 {policy_ref or '未指定'}，摘要：{summary}")
        return ticket

    def submit(self, principal: Principal, doc_no: str) -> Ticket:
        self.authorizer.require(principal, SCOPE_REIMBURSE_SUBMIT, "ticket.submit")
        ticket = self.repo.ticket(doc_no)
        if ticket is None:
            raise LookupError(f"未找到单据 {doc_no}。")
        self._must_own(principal, ticket)
        if ticket.current_node != "草稿":
            raise ValueError(f"单据 {doc_no} 当前为「{ticket.status}」，不可重复提交。")
        today = self.clock().isoformat()
        ticket.status = "部门审核中"
        ticket.current_node = "部门审核"
        ticket.history.append(TicketEvent(at=today, node="部门审核", actor=principal.user_id,
                                          action="提交报销单，进入部门审核"))
        self.repo.save_tickets()
        self.audit.record(principal.user_id, "ticket.submit", doc_no, "allowed",
                          "由青禾报销助手提交，进入部门审核")
        return ticket

    # ------------------------------------------------------------------
    # 业务事实：智能体明确不执行
    # ------------------------------------------------------------------

    def record_facts_verification(self, principal: Principal, doc_no: str,
                                  verified: bool, note: str = "") -> None:
        """本方法**故意不实现**，用于固化边界：
        业务事实（票据真伪、事由真实性、金额是否合规）由周敏在财务系统核对。"""
        self.audit.record(principal.user_id, "facts.verify", doc_no, "denied",
                          "智能体不执行事实核对，已引导至财务系统")
        raise AuthError(
            "业务事实（发票真伪、行程真实性、金额是否合规）由周敏在财务系统中核对，"
            "智能助手不判定、不代签，也不会给出「能不能报」的结论。"
            "我可以在你提交前帮你核对材料是否齐全、金额是否超出制度标准。"
        )
