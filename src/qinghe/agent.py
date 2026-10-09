"""对话编排：意图路由 → 工具调用 → 带引用的答复。

平台移植说明
------------
- ``SYSTEM_PROMPT`` 是给大模型看的系统提示，把职责与**边界**写死在里面；
- ``RuleBasedPlanner`` 是无需大模型即可运行的确定性路由（原型默认，保证可测可跑）；
- 接大模型时实现同签名的 ``Planner``，让它输出 ``{"intent":..., "slots":{...}}``，
  其余流程、鉴权、留痕逻辑完全复用——**权限判断永远不在模型侧**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional, Protocol

from .auth import AuthError
from .material import MaterialCatalog
from .models import PolicyHit, Principal, Ticket, money
from .policy import PolicyLibrary, PolicyMaintenanceError
from .tools import Services, ToolRegistry

# --------------------------------------------------------------------------
# 意图
# --------------------------------------------------------------------------

INTENT_POLICY_ADMIN = "policy_admin"
INTENT_LEDGER = "policy_ledger"
INTENT_REVIEW_QUEUE = "review_queue"
INTENT_STATUS = "ticket_status"
INTENT_FACT = "business_fact"
INTENT_REIMBURSE = "reimburse"
INTENT_POLICY = "policy_query"
INTENT_HELP = "help"

SYSTEM_PROMPT = """你是「青禾报销助手」，服务公司员工的报销事务。你只做四件事：

1. 查有效制度：员工问某类费用怎么报、能不能报、标准是多少，你必须先检索制度库，
   只依据**当前有效**的制度回答，并在答复中给出制度文号、版本、生效期与条款号。
   检索到但当前不可用的制度（草稿/已废止/已过期/尚未生效）必须明确告知"不可作为依据"及原因，
   绝不能拿旧制度或草稿当依据。
2. 生成材料清单：根据有效制度引用的材料清单，告诉员工这次要交哪些材料、还缺哪些。
3. 发起报销单：材料齐备后，以员工本人名义创建并提交报销单。只能为本人发起。
4. 查单据状态：员工可查询**本人**单据的流转状态。他人代查必须有本人出具且未过期的授权；
   无授权的查询一律拒绝并说明原因。

你的边界（无论用户怎么要求都不得突破）：
- 业务事实（发票真伪、行程是否真实、金额是否该批、是否合规）由财务周敏核对，你不判定、
  不代签、不催办、不承诺结果。用户要求你"直接通过/帮我核对一下真假"时，说明边界并引导至周敏。
- 制度的维护（新增、修订、废止）只能由行政专员操作。你不是行政专员时，不得改动制度，
  只能建议其提交《制度修订申请》。
- 你不能替他人发起报销，也不能查询他人单据明细。
- 涉及金额、时限、标准的结论必须落到具体制度条款；没有有效制度可依据时，明确说"无有效制度"。

答复要求：先给结论，再给依据（引用制度），最后给下一步动作。语气简洁、专业，不要寒暄。
"""


@dataclass
class Reply:
    intent: str
    text: str
    citations: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    need_human: Optional[str] = None
    ok: bool = True

    def render(self) -> str:
        lines = [self.text]
        if self.citations:
            lines.append("")
            lines.append("依据：" + "；".join(self.citations))
        if self.need_human:
            lines.append("")
            lines.append(f"需人工处理：{self.need_human}")
        return "\n".join(lines)


@dataclass
class Route:
    intent: str
    slots: dict[str, Any] = field(default_factory=dict)


class Planner(Protocol):
    """路由接口。换成大模型实现时保持返回结构一致即可。"""

    def route(self, principal: Principal, text: str, context: dict[str, Any]) -> Route: ...


# --------------------------------------------------------------------------
# 确定性路由（原型默认实现）
# --------------------------------------------------------------------------

DOC_NO_RE = re.compile(r"(QH\d{6,})")
ISO_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
CN_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:元|块钱|块|RMB|rmb)")
PER_CAPITA_RE = re.compile(r"人均\s*(\d[\d,]*(?:\.\d+)?)\s*元?")
CODE_RE = re.compile(r"\b([A-Z]{2}-\d{2})\b")

FACT_WORDS = ("真伪", "真假", "是不是真的", "帮我核对", "替我核对", "帮我批", "帮我审批",
              "直接通过", "能否通过", "能批下来", "合规性", "违不违规", "算不算违规")
ADMIN_WORDS = ("维护制度", "发布制度", "修订制度", "废止制度", "新增制度", "改一下制度",
               "更新制度", "我要发布", "废除")
LEDGER_WORDS = ("制度台账", "台账", "有哪些制度", "制度清单", "制度列表")
QUEUE_WORDS = ("待我核对", "待核对", "核对队列", "我的待办")
STATUS_WORDS = ("到哪了", "进度", "状态", "审核到", "走到哪", "查一下单", "报销到")
SUBMIT_WORDS = ("我要报销", "报销一下", "提交报销", "发起报销", "开始报销", "帮我报销", "提交材料")
HELP_WORDS = ("怎么用", "你能做什么", "帮助", "使用说明")


class RuleBasedPlanner:
    def route(self, principal: Principal, text: str, context: dict[str, Any]) -> Route:
        slots: dict[str, Any] = {}
        if m := DOC_NO_RE.search(text):
            slots["doc_no"] = m.group(1)
        if m := ISO_DATE_RE.search(text):
            slots["business_date"] = m.group(1)
        elif m := CN_DATE_RE.search(text):
            slots["business_date"] = date(date.today().year, int(m.group(1)), int(m.group(2))).isoformat()
        elif "今天" in text:
            slots["business_date"] = date.today().isoformat()
        elif "昨天" in text:
            slots["business_date"] = (date.today() - timedelta(days=1)).isoformat()
        per_capita_match = PER_CAPITA_RE.search(text)
        if per_capita_match:
            slots["per_capita"] = float(per_capita_match.group(1).replace(",", ""))
        if m := AMOUNT_RE.search(text):
            # "人均 260 元"是标准判断依据，不是申报金额，两者不能混为一谈
            if not (per_capita_match and per_capita_match.group(1) == m.group(1)):
                slots["amount"] = float(m.group(1).replace(",", ""))
        codes = CODE_RE.findall(text)
        if codes:
            slots["materials"] = codes

        category = PolicyLibrary.detect_category(text)
        if category:
            slots["category"] = category

        if any(w in text for w in ADMIN_WORDS):
            return Route(INTENT_POLICY_ADMIN, slots)
        if any(w in text for w in LEDGER_WORDS):
            return Route(INTENT_LEDGER, slots)
        if any(w in text for w in QUEUE_WORDS):
            return Route(INTENT_REVIEW_QUEUE, slots)
        if "doc_no" in slots or any(w in text for w in STATUS_WORDS):
            return Route(INTENT_STATUS, slots)
        if any(w in text for w in FACT_WORDS):
            return Route(INTENT_FACT, slots)
        if any(w in text for w in SUBMIT_WORDS) or (text.strip() in ("提交", "确认提交", "提交吧")
                                                    and context.get("flow_ready")):
            return Route(INTENT_REIMBURSE, slots)
        if any(w in text for w in HELP_WORDS):
            return Route(INTENT_HELP, slots)
        return Route(INTENT_POLICY, slots)


# --------------------------------------------------------------------------
# 智能体
# --------------------------------------------------------------------------

NO_POLICY_HINT = (
    "没有查到可作为依据的有效制度，我不能凭印象给你标准或结论。\n"
    "可以这样做：① 联系行政专员李静确认制度是否已发布/修订；"
    "② 若确有紧急业务，先与财务周敏确认临时处理口径，事后再补制度。"
)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class QingheAgent:
    def __init__(self, services: Services, registry: ToolRegistry,
                 planner: Optional[Planner] = None) -> None:
        self.s = services
        self.tools = registry
        self.planner = planner or RuleBasedPlanner()
        self._flows: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------

    def handle(self, principal: Principal, text: str) -> Reply:
        flow = self._flows.setdefault(principal.user_id, {})
        context = {"flow_ready": bool(flow.get("ready"))}
        try:
            route = self.planner.route(principal, text, context)
            handler = {
                INTENT_POLICY: self._policy_flow,
                INTENT_REIMBURSE: self._policy_flow,
                INTENT_STATUS: self._status,
                INTENT_FACT: self._fact_boundary,
                INTENT_POLICY_ADMIN: self._policy_admin,
                INTENT_LEDGER: self._ledger,
                INTENT_REVIEW_QUEUE: self._review_queue,
                INTENT_HELP: self._help,
            }[route.intent]
            reply = handler(principal, text, route.slots, flow)
        except AuthError as exc:
            reply = Reply(intent="denied", text=str(exc), ok=False,
                          need_human="如需办理该操作，请联系对应责任人。")
        except PolicyMaintenanceError as exc:
            reply = Reply(intent="denied", text=str(exc), ok=False,
                          need_human="制度修订请提交《制度修订申请》给行政专员李静。")
        except LookupError as exc:
            reply = Reply(intent="error", text=str(exc), ok=False)
        return reply

    # ------------------------------------------------------------------
    # 主流程：查有效制度 → 材料清单 → 发起报销
    # ------------------------------------------------------------------

    def _policy_flow(self, principal: Principal, text: str, slots: dict[str, Any],
                     flow: dict[str, Any], wants_submit: Optional[bool] = None) -> Reply:
        wants_submit = wants_submit if wants_submit is not None else (
            slots.get("amount") is not None or bool(flow.get("ready")) or "报销" in text)
        prev_category = flow.get("category")
        new_category = slots.get("category")
        if new_category and prev_category and new_category != prev_category:
            # 费用类别切换 = 新话题：上一轮的金额、日期、材料不得带进本轮
            flow.clear()
        for key, value in slots.items():
            flow[key] = value

        category = flow.get("category")
        business_date = (slots.get("business_date") or flow.get("business_date")
                         or self.s.clock().isoformat())
        flow["business_date"] = business_date

        if not category:
            return Reply(
                intent=INTENT_POLICY,
                text="好的，先明确费用类别我才能找对制度。这次是哪一类费用？\n"
                     "可选项：差旅 / 招待 / 采购 / 交通补贴。",
            )

        result = self.tools.call("search_policy", principal,
                                 query=text, business_date=business_date, category=category)
        hits: list[PolicyHit] = result.payload["effective"]
        unavailable = result.payload["unavailable"]

        if not hits:
            lines = [f"类别「{category}」（业务发生日 {business_date}）{NO_POLICY_HINT}"]
            if unavailable:
                lines.append("")
                lines.append("已检索到相关制度，但当前不可用：")
                lines.extend(f"  - {p.citation}：{reason}" for p, reason in unavailable)
            else:
                lines.append("")
                lines.append("制度库中也没有该类别的注册记录，属于制度缺位，建议行政专员尽快立项。")
            return Reply(intent=INTENT_POLICY, text="\n".join(lines), tool_calls=["search_policy"],
                         need_human="行政专员李静（制度维护）；财务周敏（临时处理口径）")

        policy = hits[0].policy
        flow["policy_ref"] = f"{policy.policy_id}@{policy.version}"

        blocks: list[str] = [f"【第一步·有效制度】"]
        blocks.append(f"适用：{policy.citation}")
        blocks.append(f"效力：{hits[0].reason}")
        if hits[0].matched_clauses:
            blocks.append("相关条款：")
            blocks.extend(f"  {c.render()}" for c in hits[0].matched_clauses)

        checklist_result = self.tools.call(
            "get_material_checklist", principal, category=category, business_date=business_date,
            amount=flow.get("amount"), per_capita=flow.get("per_capita"),
            submitted_codes=flow.get("materials") or [],
        )
        blocks.append("")
        blocks.append("【第二步·材料清单】")
        if checklist_result.ok:
            checklist = checklist_result.payload["checklist"]
            blocks.append(MaterialCatalog.render_result(checklist, flow.get("amount")))
            flow["missing"] = [i.code for i in checklist.missing_required]
        else:
            blocks.append(checklist_result.error)

        citations = _dedupe(result.citations + checklist_result.citations)

        # 第三步：发起报销
        blocks.append("")
        blocks.append("【第三步·提交】")
        missing = flow.get("missing") or []
        if not checklist_result.ok:
            blocks.append("材料要求未确定，暂不能发起报销单。")
            return Reply(intent=INTENT_POLICY, text="\n".join(blocks), citations=citations,
                         tool_calls=["search_policy", "get_material_checklist"],
                         need_human="行政专员李静（补齐制度）")
        if flow.get("amount") is None:
            blocks.append("请告诉我申报金额，我据此复核材料条件项（如是否达比价门槛、是否超标），"
                          "然后为你生成报销单。")
            flow["ready"] = False
            return Reply(intent=INTENT_POLICY, text="\n".join(blocks), citations=citations,
                         tool_calls=["search_policy", "get_material_checklist"])
        if missing:
            blocks.append("还缺 " + "、".join(missing) + "，补齐后回复「提交」即可进入流程。")
            flow["ready"] = False
            return Reply(intent=INTENT_POLICY, text="\n".join(blocks), citations=citations,
                         tool_calls=["search_policy", "get_material_checklist"])
        if not wants_submit:
            blocks.append("材料齐备。回复「提交」我就按以上依据发起报销单。")
            flow["ready"] = True
            return Reply(intent=INTENT_POLICY, text="\n".join(blocks), citations=citations,
                         tool_calls=["search_policy", "get_material_checklist"])

        draft = self.tools.call("create_reimbursement_draft", principal, category=category,
                                amount=flow["amount"], business_date=business_date,
                                policy_ref=flow.get("policy_ref"),
                                materials=flow.get("materials") or [])
        if not draft.ok:
            blocks.append(draft.error)
            return Reply(intent=INTENT_REIMBURSE, text="\n".join(blocks), ok=False,
                         citations=citations, tool_calls=["create_reimbursement_draft"])
        ticket: Ticket = draft.payload
        submitted = self.tools.call("submit_reimbursement", principal, doc_no=ticket.doc_no)
        flow["ready"] = False
        flow["last_doc_no"] = ticket.doc_no
        blocks.append(f"已提交，单据号 {ticket.doc_no}，当前节点：{submitted.payload.current_node}。")
        blocks.append("业务事实（发票真伪、事由真实性、金额是否合规）由周敏核对，"
                      "任何一方都不会因为智能助手提交就跳过核对。")
        blocks.append(f"之后你可以直接问我「{ticket.doc_no} 到哪了」查进度。")
        return Reply(intent=INTENT_REIMBURSE, text="\n".join(blocks), citations=citations,
                     tool_calls=["search_policy", "get_material_checklist",
                                 "create_reimbursement_draft", "submit_reimbursement"])

    # ------------------------------------------------------------------
    # 其余意图
    # ------------------------------------------------------------------

    def _status(self, principal: Principal, text: str, slots: dict[str, Any],
                flow: dict[str, Any]) -> Reply:
        doc_no = slots.get("doc_no") or flow.get("last_doc_no")
        if not doc_no:
            listed = self.tools.call("list_my_reimbursements", principal)
            if not listed.ok:
                return Reply(intent=INTENT_STATUS, text=listed.error, ok=False)
            tickets: list[Ticket] = listed.payload
            if not tickets:
                return Reply(intent=INTENT_STATUS,
                             text="你当前没有可查询的单据。若刚提交，请提供单据号，例如 QH2026090001。")
            lines = ["你当前可查询的单据："]
            lines.extend(f"  - {t.doc_no}｜{t.category}｜{money(t.amount)}｜{t.status}"
                         f"（{t.current_node}）" for t in tickets)
            lines.append("回复单据号即可查看详细进度。")
            return Reply(intent=INTENT_STATUS, text="\n".join(lines),
                         tool_calls=["list_my_reimbursements"])

        result = self.tools.call("my_reimbursement_status", principal, doc_no=doc_no)
        if not result.ok:
            return Reply(intent=INTENT_STATUS, text=result.error, ok=False,
                         tool_calls=["my_reimbursement_status"],
                         need_human="如认为授权有误，请联系行政/财务核查权限")
        return Reply(intent=INTENT_STATUS, text=result.text,
                     tool_calls=["my_reimbursement_status"])

    def _fact_boundary(self, principal: Principal, text: str, slots: dict[str, Any],
                       flow: dict[str, Any]) -> Reply:
        return Reply(
            intent=INTENT_FACT,
            text="这个我不能替你判断。业务事实——发票真伪、行程是否真实、金额该不该批、是否合规——"
                 "只有财务周敏核对后才有结论，我既不判定也不代签。\n"
                 "我能做的是把依据和材料准备到位：如果你告诉我费用类别、发生日期和金额，"
                 "我可以给出适用的有效制度、标准上限和材料清单，让核对更顺。",
            need_human="财务周敏（业务事实核对）",
        )

    def _policy_admin(self, principal: Principal, text: str, slots: dict[str, Any],
                      flow: dict[str, Any]) -> Reply:
        from .auth import SCOPE_POLICY_WRITE
        if not self.s.authorizer.can(principal, SCOPE_POLICY_WRITE):
            self.s.authorizer.require(principal, SCOPE_POLICY_WRITE, "policy.maintain")
        return Reply(
            intent=INTENT_POLICY_ADMIN,
            text="制度维护请按以下方式办理（当前原型不通过对话直接改制度，避免误改）：\n"
                 "  - 新增/发布：调用工具 maintain_policy(action=publish, ...)\n"
                 "  - 修订：maintain_policy(action=revise, policy_id=..., version=...)，"
                 "旧版自动按期终止并保留历史\n"
                 "  - 废止：maintain_policy(action=repeal, policy_id=..., effective_to=..., reason=...)\n"
                 "每次变更都会写入审计，并带生效期，员工端只会看到生效中的版本。",
            tool_calls=["maintain_policy"],
        )

    def _ledger(self, principal: Principal, text: str, slots: dict[str, Any],
                flow: dict[str, Any]) -> Reply:
        result = self.tools.call("policy_ledger", principal)
        if not result.ok:
            return Reply(intent=INTENT_LEDGER, text=result.error, ok=False,
                         tool_calls=["policy_ledger"])
        return Reply(intent=INTENT_LEDGER, text=result.text, tool_calls=["policy_ledger"])

    def _review_queue(self, principal: Principal, text: str, slots: dict[str, Any],
                      flow: dict[str, Any]) -> Reply:
        result = self.tools.call("my_review_queue", principal)
        if not result.ok:
            return Reply(intent=INTENT_REVIEW_QUEUE, text=result.error, ok=False,
                         tool_calls=["my_review_queue"])
        queue: list[Ticket] = result.payload
        lines = [f"待你核对业务事实的单据 {len(queue)} 张："]
        lines.extend(f"  - {t.doc_no}｜{t.category}｜{money(t.amount)}｜业务发生日 {t.incurred_on}"
                     f"｜{t.current_node}" for t in queue)
        lines.append("核对结论请在财务系统录入；智能助手只提示队列，不代签结论。")
        return Reply(intent=INTENT_REVIEW_QUEUE, text="\n".join(lines),
                     tool_calls=["my_review_queue"])

    def _help(self, principal: Principal, text: str, slots: dict[str, Any],
              flow: dict[str, Any]) -> Reply:
        from .auth import SCOPE_LABELS, effective_scopes
        mine = effective_scopes(principal)
        allowed = "、".join(SCOPE_LABELS[s] for s in sorted(mine) if s in SCOPE_LABELS) or "无"
        return Reply(
            intent=INTENT_HELP,
            text="我能做四件事：① 查有效制度（带文号和生效期）② 按制度列出材料清单和缺项 "
                 "③ 以你本人名义发起并提交报销单 ④ 查询你本人单据的流转状态。\n"
                 "我不做：判断发票真伪或金额是否该批（由周敏核对）、维护制度（由行政专员维护）、"
                 "查询他人单据。\n"
                 f"你当前身份：{principal.name}（{'、'.join(principal.user.role_labels)}），"
                 f"已开通：{allowed}。",
        )
