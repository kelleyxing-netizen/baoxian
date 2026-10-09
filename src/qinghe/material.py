"""材料清单：把"制度要求"翻译成"这次要交什么"。

智能体在这里只做两件机械的事：
1. 按制度引用的 ``material_set`` 取出清单；
2. 用已知可机械判定的条件（如"业务发生日距今是否超过 30 天"）决定条件项是否成立。

凡是要判断**业务事实**才成立的条件（是否超标、是否三家比价、票据是否真实），
智能体一律不下结论，只列为"待确认"，由员工补材料、由周敏核对。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

from .models import ChecklistResult, MaterialItem, MaterialSet, money


class MaterialCatalog:
    def __init__(self, sets: dict[str, dict[str, Any]], clock=date.today) -> None:
        self._sets = {k: MaterialSet.from_dict(v) for k, v in sets.items()}
        self.clock = clock

    def get(self, set_id: str) -> Optional[MaterialSet]:
        return self._sets.get(set_id)

    def all_sets(self) -> list[MaterialSet]:
        return list(self._sets.values())

    # ------------------------------------------------------------------
    # 条件项判定：只允许"可机械判定"的规则，其余返回 None（待确认）
    # ------------------------------------------------------------------

    @staticmethod
    def _condition_rule(code: str, context: dict[str, Any]) -> Optional[bool]:
        incurred_on = context.get("incurred_on")
        today = context.get("today") or date.today()
        amount = context.get("amount")
        per_capita = context.get("per_capita")

        if code == "TR-05":  # 差旅逾期说明：超过 30 个自然日
            if not incurred_on:
                return None
            return (today - date.fromisoformat(incurred_on)).days > 30
        if code == "PR-04":  # 采购三家比价：单笔 2000 元以上
            if amount is None:
                return None
            return amount >= 2000
        if code == "EN-04":  # 招待超标同意：人均超 200 元
            if per_capita is None:
                return None
            return per_capita > 200
        return None

    def evaluate(self, set_id: str, submitted_codes: list[str],
                 context: Optional[dict[str, Any]] = None) -> ChecklistResult:
        material_set = self.get(set_id)
        if material_set is None:
            return ChecklistResult(set_id=set_id, set_name=f"未维护的材料集（{set_id}）",
                                   required=[], conditional=[], provided=[],
                                   missing_required=[], unresolved_conditional=[])
        context = dict(context or {})
        context.setdefault("today", self.clock())
        submitted = set(submitted_codes)

        required: list[MaterialItem] = []
        conditional: list[MaterialItem] = []
        provided: list[MaterialItem] = []
        unresolved: list[MaterialItem] = []

        for item in material_set.items:
            if item.required:
                required.append(item)
            else:
                applies = self._condition_rule(item.code, context)
                if applies is False:
                    continue  # 条件不成立，本次不需要
                conditional.append(item)
                if applies is None and item.code not in submitted:
                    unresolved.append(item)
            if item.code in submitted:
                provided.append(item)

        missing = [i for i in required if i.code not in submitted]
        return ChecklistResult(
            set_id=set_id,
            set_name=material_set.name,
            required=required,
            conditional=conditional,
            provided=provided,
            missing_required=missing,
            unresolved_conditional=unresolved,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def render_result(result: ChecklistResult, amount: Optional[float] = None) -> str:
        lines = [f"依据制度引用的材料清单：{result.set_name}"]
        if amount is not None:
            lines.append(f"申报金额：{money(amount)}")
        for item in result.required:
            mark = "[已提交]" if item.code in {p.code for p in result.provided} else "[未提交]"
            note = f"（{item.note}）" if item.note else ""
            lines.append(f"  {mark} {item.label}{note}")
        for item in result.conditional:
            mark = "[已提交]" if item.code in {p.code for p in result.provided} else "[条件项待确认]"
            lines.append(f"  {mark} {item.label}｜适用条件：{item.condition}")
        if result.missing_required:
            lines.append("缺项：" + "、".join(i.label for i in result.missing_required))
        if result.unresolved_conditional:
            lines.append("待确认条件项：" + "、".join(i.label for i in result.unresolved_conditional))
        if result.complete and not result.unresolved_conditional:
            lines.append("材料齐备，可发起报销单。")
        return "\n".join(lines)
