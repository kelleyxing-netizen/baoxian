"""工具契约（平台无关）。

这里定义的是**智能体能做什么**的完整清单。落地到任何平台时，
把 ``TOOL_SPECS`` 逐条翻译成该平台的插件/工具定义即可，
``ToolRegistry`` 里已有的实现可直接作为后端函数。

三条设计规则：
1. 每个工具标注所需权限范围 ``scope``，由 ``Authorizer`` 统一校验，
   权限判断不写在提示词里，避免"提示词注入即越权"；
2. 工具描述里显式写明"不做什么"，尤其是业务事实类判断；
3. **没有**任何工具可以代填/代签业务事实核对结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from .audit import AuditLog
from .auth import (
    SCOPE_POLICY_READ,
    SCOPE_POLICY_WRITE,
    SCOPE_REIMBURSE_SUBMIT,
    SCOPE_TICKET_READ_ANY,
    SCOPE_TICKET_READ_SELF,
    Authorizer,
)
from .material import MaterialCatalog
from .models import Policy, Principal
from .policy import PolicyLibrary
from .ticket import TicketService

# --------------------------------------------------------------------------
# 工具声明
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    scope: Optional[str]
    parameters: dict[str, Any]
    returns: str
    writes: bool = False
    boundary: str = ""      # 明确的"不做什么"

    def to_platform_dict(self) -> dict[str, Any]:
        """平台中立的工具声明，可直接改写为 OpenAI tools / 扣子插件 / Dify 工具。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": self.parameters,
                           "required": [k for k, v in self.parameters.items() if v.get("required")],
                           "additionalProperties": False},
        }


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search_policy",
        description="按费用类别或自然语言问题检索**当前有效**的制度条款，返回制度文号、版本、生效期与条款原文。"
                    "同时返回命中的但当前不可用的制度及原因（草稿/废止/过期/未生效）。",
        scope=SCOPE_POLICY_READ,
        parameters={
            "query": {"type": "string", "description": "用户的问题或关键词", "required": True},
            "business_date": {"type": "string", "description": "业务发生日 YYYY-MM-DD；缺省按今天判定有效性"},
            "category": {"type": "string", "description": "费用类别：差旅/招待/采购/交通补贴"},
        },
        returns="命中的有效制度（含条款与引用）+ 命中但不可用的制度及原因",
    ),
    ToolSpec(
        name="get_material_checklist",
        description="根据有效制度引用的材料清单，生成本次报销需要的材料，并比对已提交材料、列出缺项。"
                    "条件项仅在能机械判定时自动判定（如是否逾期、单笔金额是否达门槛）。",
        scope=SCOPE_POLICY_READ,
        parameters={
            "category": {"type": "string", "description": "费用类别", "required": True},
            "business_date": {"type": "string", "description": "业务发生日 YYYY-MM-DD"},
            "amount": {"type": "number", "description": "申报金额"},
            "per_capita": {"type": "number", "description": "招待人均金额"},
            "submitted_codes": {"type": "array", "items": {"type": "string"},
                                "description": "已提交的材料编码"},
        },
        returns="材料清单、缺项、待确认条件项",
        boundary="不判断材料内容是否真实、是否合规",
    ),
    ToolSpec(
        name="create_reimbursement_draft",
        description="以当前登录人名义创建报销单草稿（草稿状态，尚未进入流程）。只允许为本人发起。",
        scope=SCOPE_REIMBURSE_SUBMIT,
        parameters={
            "category": {"type": "string", "required": True},
            "amount": {"type": "number", "required": True},
            "business_date": {"type": "string", "description": "业务发生日 YYYY-MM-DD", "required": True},
            "policy_ref": {"type": "string", "description": "依据的制度版本，如 QH-CW-2025-011@V3.0"},
            "materials": {"type": "array", "items": {"type": "string"}, "description": "已提交材料编码"},
        },
        returns="单据号与草稿状态",
        writes=True,
    ),
    ToolSpec(
        name="submit_reimbursement",
        description="把本人的报销单草稿提交进流程，单据进入部门审核。",
        scope=SCOPE_REIMBURSE_SUBMIT,
        parameters={"doc_no": {"type": "string", "required": True}},
        returns="提交后的状态与当前节点",
        writes=True,
    ),
    ToolSpec(
        name="my_reimbursement_status",
        description="查询**本人**单据的流转状态、当前节点、历史记录与下一步提示。"
                    "仅限本人；他人代查需本人在系统内出具的未过期授权。",
        scope=SCOPE_TICKET_READ_SELF,
        parameters={"doc_no": {"type": "string", "description": "单据号，如 QH2026090001", "required": True}},
        returns="状态、当前节点、流转记录、下一步、时效提示",
        boundary="不下结论、不催办、不修改单据；越权查询会被拒绝并留痕",
    ),
    ToolSpec(
        name="list_my_reimbursements",
        description="列出当前身份可查看的单据（本人单据 + 他人明确授权代查的单据）。",
        scope=SCOPE_TICKET_READ_SELF,
        parameters={},
        returns="单据列表",
    ),
    ToolSpec(
        name="my_review_queue",
        description="业务事实核对责任人查看待自己核对的单据队列（只返回单号、类别、金额、节点）。",
        scope=SCOPE_TICKET_READ_ANY,
        parameters={},
        returns="待核对队列",
        boundary="队列仅用于提示，核对结论必须在财务系统中录入",
    ),
    ToolSpec(
        name="maintain_policy",
        description="新增/发布、修订或废止制度。仅行政专员可用，变更写入审计并保留版本历史。",
        scope=SCOPE_POLICY_WRITE,
        parameters={
            "action": {"type": "string", "enum": ["publish", "revise", "repeal"], "required": True},
            "title": {"type": "string"},
            "category": {"type": "string"},
            "version": {"type": "string"},
            "doc_no": {"type": "string"},
            "effective_from": {"type": "string"},
            "effective_to": {"type": "string"},
            "policy_id": {"type": "string", "description": "修订/废止时的制度 ID"},
            "reason": {"type": "string"},
        },
        returns="变更后的制度版本",
        writes=True,
    ),
    ToolSpec(
        name="policy_ledger",
        description="查看制度台账：每条制度的当前效力、版本、维护人与失效原因。仅行政专员可用。",
        scope=SCOPE_POLICY_WRITE,
        parameters={},
        returns="制度台账文本",
    ),
]

# 明确不提供的工具（写进文档与测试，避免被"顺手"加上）
NOT_PROVIDED = {
    "verify_business_facts": "业务事实核对（票据真伪、事由真实性、金额是否合规）——由周敏在财务系统完成",
    "approve_ticket": "审批或推进单据流程——由部门负责人、财务在各自系统完成",
    "query_others_ticket": "查询他人单据明细——仅本人或被授权人，核对责任人只能看队列",
}


# --------------------------------------------------------------------------
# 工具实现绑定
# --------------------------------------------------------------------------


@dataclass
class Services:
    policy: PolicyLibrary
    catalog: MaterialCatalog
    tickets: TicketService
    authorizer: Authorizer
    audit: AuditLog
    repo: Any = None
    clock: Callable[[], date] = date.today


@dataclass
class ToolResult:
    tool: str
    ok: bool
    payload: Any = None
    text: str = ""
    citations: list[str] = field(default_factory=list)
    error: str = ""


class ToolRegistry:
    def __init__(self, services: Services) -> None:
        self.s = services
        self._handlers: dict[str, Callable[..., ToolResult]] = {
            "search_policy": self._search_policy,
            "get_material_checklist": self._get_material_checklist,
            "create_reimbursement_draft": self._create_draft,
            "submit_reimbursement": self._submit,
            "my_reimbursement_status": self._status,
            "list_my_reimbursements": self._list_mine,
            "my_review_queue": self._review_queue,
            "maintain_policy": self._maintain_policy,
            "policy_ledger": self._ledger,
        }

    @property
    def specs(self) -> list[ToolSpec]:
        return TOOL_SPECS

    def spec(self, name: str) -> Optional[ToolSpec]:
        return next((s for s in TOOL_SPECS if s.name == name), None)

    def call(self, name: str, principal: Principal, **kwargs: Any) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"未注册的工具：{name}")
        spec = self.spec(name)
        if spec and spec.scope:
            self.s.authorizer.require(principal, spec.scope, f"tool:{name}")
        try:
            return handler(principal, **kwargs)
        except (PermissionError, LookupError, ValueError) as exc:
            return ToolResult(tool=name, ok=False, error=str(exc), text=str(exc))

    # -- 制度 --------------------------------------------------------------

    def _search_policy(self, principal: Principal, query: str,
                       business_date: Optional[str] = None,
                       category: Optional[str] = None) -> ToolResult:
        on = date.fromisoformat(business_date) if business_date else self.s.clock()
        hits = self.s.policy.search(query, on=on, category=category)
        effective = [h for h in hits if h.effective]
        unavailable = [(p, r) for p, r in self.s.policy.list_not_effective(on)
                       if any(p.policy_id == h.policy.policy_id for h in hits)] if not effective else []
        self.s.audit.record(principal.user_id, "policy.search", query, "allowed",
                            f"基准日 {on}，有效命中 {len(effective)} 条")
        return ToolResult(
            tool="search_policy", ok=True,
            payload={"effective": effective, "unavailable": unavailable, "on": on.isoformat()},
            citations=[h.citation for h in effective],
        )

    def _get_material_checklist(self, principal: Principal, category: str,
                                business_date: Optional[str] = None, amount: Optional[float] = None,
                                per_capita: Optional[float] = None,
                                submitted_codes: Optional[list[str]] = None) -> ToolResult:
        on = date.fromisoformat(business_date) if business_date else self.s.clock()
        policies = self.s.policy.effective_for_category(category, on)
        if not policies:
            return ToolResult(tool="get_material_checklist", ok=False,
                              error=f"类别「{category}」在 {on} 没有有效制度，无法确定材料要求。")
        policy = policies[0]
        result = self.s.catalog.evaluate(
            policy.material_set,
            submitted_codes or [],
            {"incurred_on": business_date, "amount": amount, "per_capita": per_capita,
             "today": self.s.clock()},
        )
        self.s.audit.record(principal.user_id, "material.checklist", category, "allowed",
                            f"依据 {policy.citation}")
        return ToolResult(tool="get_material_checklist", ok=True,
                          payload={"policy": policy, "checklist": result},
                          citations=[policy.citation])

    # -- 单据 --------------------------------------------------------------

    def _create_draft(self, principal: Principal, category: str, amount: float,
                      business_date: str, policy_ref: Optional[str] = None,
                      materials: Optional[list[str]] = None) -> ToolResult:
        ticket = self.s.tickets.create(principal, category=category, amount=float(amount),
                                       incurred_on=business_date, policy_ref=policy_ref,
                                       materials=materials or [])
        return ToolResult(tool="create_reimbursement_draft", ok=True, payload=ticket,
                          text=f"已创建报销单草稿：{ticket.doc_no}")

    def _submit(self, principal: Principal, doc_no: str) -> ToolResult:
        ticket = self.s.tickets.submit(principal, doc_no)
        return ToolResult(tool="submit_reimbursement", ok=True, payload=ticket,
                          text=f"单据 {ticket.doc_no} 已提交，当前节点：{ticket.current_node}")

    def _status(self, principal: Principal, doc_no: str) -> ToolResult:
        report = self.s.tickets.status(principal, doc_no)
        return ToolResult(tool="my_reimbursement_status", ok=True, payload=report, text=report.render())

    def _list_mine(self, principal: Principal) -> ToolResult:
        tickets = self.s.tickets.list_mine(principal)
        return ToolResult(tool="list_my_reimbursements", ok=True, payload=tickets)

    def _review_queue(self, principal: Principal) -> ToolResult:
        queue = self.s.tickets.review_queue(principal)
        return ToolResult(tool="my_review_queue", ok=True, payload=queue)

    # -- 制度维护 ----------------------------------------------------------

    def _maintain_policy(self, principal: Principal, action: str, **kwargs: Any) -> ToolResult:
        if action == "publish":
            policy = self.s.policy.publish(
                principal,
                title=kwargs.get("title", "未命名制度"), category=kwargs.get("category", "其他"),
                version=kwargs.get("version", "V1.0"), doc_no=kwargs.get("doc_no", "待编号"),
                effective_from=kwargs.get("effective_from", self.s.clock().isoformat()),
                effective_to=kwargs.get("effective_to"),
                clauses=kwargs.get("clauses"),
                summary=kwargs.get("summary", ""),
                supersedes=kwargs.get("policy_id"),
            )
        elif action == "revise":
            policy = self.s.policy.revise(
                principal, kwargs["policy_id"], version=kwargs.get("version", "V1.1"),
                effective_from=kwargs.get("effective_from", self.s.clock().isoformat()),
                clauses=kwargs.get("clauses"), summary=kwargs.get("summary"),
                effective_to=kwargs.get("effective_to"), doc_no=kwargs.get("doc_no"),
            )
        elif action == "repeal":
            policy = self.s.policy.repeal(
                principal, kwargs["policy_id"], effective_to=kwargs.get("effective_to",
                                                                       self.s.clock().isoformat()),
                reason=kwargs.get("reason", "未注明"),
            )
        else:
            return ToolResult(tool="maintain_policy", ok=False, error=f"不支持的 action：{action}")
        return ToolResult(tool="maintain_policy", ok=True, payload=policy,
                          text=f"已处理：{policy.citation}（{Policy.__name__} 状态 {policy.status}）")

    def _ledger(self, principal: Principal) -> ToolResult:
        text = self.s.policy.maintenance_view(principal)
        return ToolResult(tool="policy_ledger", ok=True, text=text)
