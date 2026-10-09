"""青禾报销助手 · 回归测试。

测试分四组，对应需求的四条主线与三条边界：
A 制度有效性（只依据有效制度）
B 材料清单（按制度生成、缺项可查）
C 单据状态（经授权、仅限本人）
D 分工边界（制度归行政专员、事实归周敏）

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qinghe.agent import (  # noqa: E402
    INTENT_FACT,
    INTENT_LEDGER,
    INTENT_REIMBURSE,
    INTENT_REVIEW_QUEUE,
    INTENT_STATUS,
)
from qinghe.auth import AuthError  # noqa: E402
from qinghe.cli import Harness, scratch_dir  # noqa: E402
from qinghe.policy import PolicyMaintenanceError  # noqa: E402

DATA = ROOT / "data"
# 固定基准日：样例票据的业务发生日都是 2026-09 前后，若用真实当天，
# 超过 30 天后会触发"逾期说明"条件项，回归结果会随日历漂移。
TODAY = date(2026, 10, 9)


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = scratch_dir("test-")
        data_dir = self.tmp / "data"
        shutil.copytree(DATA, data_dir)
        self.h = Harness(data_dir, audit_path=self.tmp / "audit.jsonl", clock=lambda: TODAY)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# ==========================================================================
# A 制度有效性
# ==========================================================================


class TestPolicyEffectiveness(BaseCase):
    def test_only_effective_policy_is_usable_today(self):
        hits = self.h.policy.search("出差住宿标准", on=TODAY)
        effective = [h.policy.version for h in hits if h.effective]
        self.assertIn("V3.0", effective)
        self.assertNotIn("V2.0", effective, "已废止的旧版不得作为当前依据")

    def test_repealed_policy_is_reported_with_reason(self):
        reasons = {p.policy_id: r for p, r in self.h.policy.list_not_effective(TODAY)}
        self.assertIn("QH-CW-2022-007", reasons)
        self.assertIn("已废止", reasons["QH-CW-2022-007"])

    def test_draft_policy_never_applies(self):
        effective = {p.policy_id for p in self.h.policy.list_effective(TODAY)}
        self.assertNotIn("QH-CW-2026-004", effective, "草稿制度不得生效")
        policy = self.h.policy.get("QH-CW-2026-004")
        ok, reason = policy.effectiveness(TODAY)
        self.assertFalse(ok)
        self.assertIn("草稿", reason)

    def test_expired_policy_never_applies(self):
        policy = self.h.policy.get("QH-CW-2023-015")
        ok, reason = policy.effectiveness(TODAY)
        self.assertFalse(ok)
        self.assertIn("已过有效期", reason)

    def test_historical_business_date_uses_the_then_version(self):
        """2024 年发生的业务，应依据当时有效的 V2.0，而不是尚未生效的 V3.0。"""
        on = date(2024, 6, 1)
        versions = {p.version: p.effectiveness(on) for p in self.h.policy.effective_for_category("差旅", on)}
        self.assertIn("V2.0", versions)
        self.assertTrue(versions["V2.0"][0])
        self.assertNotIn("V3.0", versions)

    def test_new_version_starts_after_effective_date(self):
        on = date(2026, 1, 1)
        effective = {p.version for p in self.h.policy.effective_for_category("差旅", on)}
        self.assertEqual(effective, {"V3.0"})


# ==========================================================================
# B 材料清单
# ==========================================================================


class TestMaterialChecklist(BaseCase):
    def test_required_items_detected_as_missing(self):
        result = self.h.catalog.evaluate("差旅", ["TR-01"], {"incurred_on": TODAY.isoformat()})
        missing = {i.code for i in result.missing_required}
        self.assertIn("TR-02", missing)
        self.assertIn("TR-04", missing)
        self.assertFalse(result.complete)

    def test_conditional_item_appears_only_when_threshold_met(self):
        big = self.h.catalog.evaluate("采购", [], {"amount": 2500})
        small = self.h.catalog.evaluate("采购", [], {"amount": 500})
        self.assertIn("PR-04", {i.code for i in big.conditional})
        self.assertNotIn("PR-04", {i.code for i in small.conditional})

    def test_overdue_travel_requires_explanation(self):
        old = (TODAY - timedelta(days=60)).isoformat()
        recent = (TODAY - timedelta(days=5)).isoformat()
        self.assertIn("TR-05", {i.code for i in self.h.catalog.evaluate("差旅", [], {"incurred_on": old}).conditional})
        self.assertNotIn("TR-05", {i.code for i in self.h.catalog.evaluate("差旅", [], {"incurred_on": recent}).conditional})

    def test_unknown_fact_leaves_conditional_unresolved(self):
        """人均金额未知时不得猜测结论，只能标为待确认。"""
        result = self.h.catalog.evaluate("招待", ["EN-01", "EN-02", "EN-03"], {})
        self.assertEqual([i.code for i in result.unresolved_conditional], ["EN-04"])


# ==========================================================================
# C 单据状态：经授权、仅限本人
# ==========================================================================


class TestTicketAccess(BaseCase):
    def test_owner_can_query_own_ticket(self):
        report = self.h.tickets.status(self.h.principal("E1001"), "QH2026090001")
        self.assertEqual(report.access_reason, "本人单据")
        self.assertIn("财务复核", report.render())

    def test_other_employee_ticket_is_denied_and_audited(self):
        with self.assertRaises(AuthError):
            self.h.tickets.status(self.h.principal("E1001"), "QH2026090033")
        denied = [e for e in self.h.audit.denied() if e.action == "ticket.status.read"]
        self.assertTrue(denied, "越权尝试必须留痕")
        self.assertIn("E1003", denied[-1].detail)

    def test_explicit_delegation_allows_proxy_query(self):
        report = self.h.tickets.status(self.h.principal("E1001"), "QH2026090042")
        self.assertIn("授权代查", report.access_reason)

    def test_account_without_feature_is_denied(self):
        """有单据不等于有权查：外包账号未开通报销查询功能。"""
        with self.assertRaises(AuthError) as ctx:
            self.h.tickets.status(self.h.principal("E1003"), "QH2026090033")
        self.assertIn("尚未开通", str(ctx.exception))

    def test_non_ticket_role_is_denied(self):
        """行政专员可维护制度，但无权查看员工单据。"""
        with self.assertRaises(AuthError):
            self.h.tickets.status(self.h.principal("A2001"), "QH2026090001")

    def test_reviewer_sees_queue_not_arbitrary_tickets(self):
        queue = self.h.tickets.review_queue(self.h.principal("R3001"))
        self.assertTrue(all(t.reviewer == "R3001" for t in queue))
        self.assertTrue(all(not t.facts_verified for t in queue))

    def test_agent_cannot_verify_business_facts(self):
        with self.assertRaises(AuthError) as ctx:
            self.h.tickets.record_facts_verification(self.h.principal("R3001"), "QH2026090001", True)
        self.assertIn("周敏", str(ctx.exception))


# ==========================================================================
# D 分工边界：制度归行政专员、事实归周敏
# ==========================================================================


class TestSeparationOfDuties(BaseCase):
    def test_employee_cannot_maintain_policy(self):
        with self.assertRaises(PolicyMaintenanceError):
            self.h.policy.publish(self.h.principal("E1001"), title="差旅新规", category="差旅",
                                  version="V1.0", doc_no="青禾财字〔2026〕099号",
                                  effective_from="2026-11-01")

    def test_admin_can_revise_and_history_is_kept(self):
        admin = self.h.principal("A2001")
        before = self.h.policy.get("QH-CW-2025-011")
        new = self.h.policy.revise(admin, "QH-CW-2025-011", version="V4.0", effective_from="2026-11-01",
                                   clauses=[{"no": "4.1", "text": "住宿费上限调整为一线城市 700 元。"}])
        self.assertEqual(new.version, "V4.0")
        self.assertEqual(before.status, "repealed")
        self.assertEqual(before.effective_to, "2026-11-01", "旧版按期终止，不追溯")
        # 修订生效前，原版本仍是唯一依据
        self.assertTrue(before.effectiveness(date(2026, 10, 20))[0])
        self.assertFalse(before.effectiveness(date(2026, 11, 2))[0])
        self.assertTrue(new.effectiveness(date(2026, 11, 2))[0])
        self.assertIn("policy.revise", [e.action for e in self.h.audit.events])

    def test_fact_question_is_routed_to_human(self):
        reply = self.h.agent.handle(self.h.principal("E1001"), "这张发票是真的吗，能不能直接通过")
        self.assertEqual(reply.intent, INTENT_FACT)
        self.assertIsNotNone(reply.need_human)
        self.assertIn("周敏", reply.need_human)


# ==========================================================================
# E 端到端对话
# ==========================================================================


class TestConversationFlow(BaseCase):
    def test_full_reimbursement_flow(self):
        reply = self.h.agent.handle(
            self.h.principal("E1001"),
            "我要报销上海出差的费用，9月12日，3860元，材料 TR-01 TR-02 TR-03 TR-04",
        )
        self.assertEqual(reply.intent, INTENT_REIMBURSE)
        self.assertIn("青禾财字〔2025〕011号", reply.text, "答复必须给出制度引用")
        self.assertIn("依据：", reply.render())
        self.assertIn("由周敏核对", reply.text)
        doc_no = self.h.repo.tickets[-1].doc_no
        self.assertEqual(self.h.repo.tickets[-1].subject, "E1001", "只能为本人发起")

        status = self.h.agent.handle(self.h.principal("E1001"), f"{doc_no} 到哪了")
        self.assertEqual(status.intent, INTENT_STATUS)
        self.assertIn(doc_no, status.text)
        self.assertIn("部门审核", status.text)

    def test_missing_materials_block_submission(self):
        before = len(self.h.repo.tickets)
        reply = self.h.agent.handle(self.h.principal("E1001"), "报销差旅 9月12日 3860元")
        self.assertEqual(len(self.h.repo.tickets), before, "缺材料时不得创建单据")
        self.assertIn("还缺", reply.text)
        self.assertIn("TR-01", reply.text)

    def test_no_effective_policy_stops_the_flow(self):
        reply = self.h.agent.handle(self.h.principal("E1001"), "疫情防控期间交通补贴还能报吗")
        self.assertIn("没有查到可作为依据的有效制度", reply.text)
        self.assertIn("已过有效期", reply.text)
        self.assertIsNotNone(reply.need_human)

    def test_draft_policy_would_block_purchase_reimbursement(self):
        """采购只有草稿制度 → 员工端必须得到"无有效制度"而不是草稿里的标准。"""
        reply = self.h.agent.handle(self.h.principal("E1001"), "我要报销采购办公用品 2000元")
        self.assertIn("没有查到可作为依据的有效制度", reply.text)
        self.assertIn("草稿", reply.text)

    def test_ledger_requires_admin_role(self):
        denied = self.h.agent.handle(self.h.principal("E1001"), "制度台账")
        self.assertFalse(denied.ok)
        allowed = self.h.agent.handle(self.h.principal("A2001"), "制度台账")
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.intent, INTENT_LEDGER)
        self.assertIn("QH-CW-2025-011", allowed.text)

    def test_reviewer_queue_via_agent(self):
        reply = self.h.agent.handle(self.h.principal("R3001"), "待我核对的单据有哪些")
        self.assertEqual(reply.intent, INTENT_REVIEW_QUEUE)
        self.assertIn("待你核对业务事实的单据", reply.text)
        self.assertIn("不代签结论", reply.text)

    def test_citations_are_deduplicated(self):
        reply = self.h.agent.handle(self.h.principal("E1001"),
                                    "报销差旅 9月12日 3860元 TR-01 TR-02 TR-03 TR-04")
        self.assertEqual(len(reply.citations), len(set(reply.citations)))
        self.assertEqual(len(reply.citations), 1)

    def test_per_capita_is_not_treated_as_claim_amount(self):
        """人均标准是判断依据，不能被当成申报金额，也不能因此猜出"超标"结论。"""
        reply = self.h.agent.handle(self.h.principal("E1001"), "招待客户吃饭人均 260 元怎么报")
        self.assertIn("EN-04", reply.text)
        self.assertIn("申报金额", reply.text)  # 仍在等用户给出申报金额
        self.assertIn("人均超过 200 元", reply.text)

    def test_business_date_does_not_leak_across_categories(self):
        """上一轮差旅的发生日与金额不得污染下一轮其他类别的判定。"""
        self.h.agent.handle(self.h.principal("E1001"), "报销差旅 9月12日 3860元")
        reply = self.h.agent.handle(self.h.principal("E1001"), "疫情防控期间交通补贴还能报吗")
        self.assertIn(f"业务发生日 {TODAY}", reply.text)
        self.assertNotIn("¥3,860.00", reply.text)

    def test_switching_category_resets_slots(self):
        self.h.agent.handle(self.h.principal("E1001"), "报销差旅 9月12日 3860元")
        reply = self.h.agent.handle(self.h.principal("E1001"), "招待客户吃饭人均 260 元怎么报")
        self.assertNotIn("¥3,860.00", reply.text, "切换到招待后不得沿用差旅金额")
        self.assertNotIn("TR-", reply.text, "切换到招待后不得沿用差旅材料")


if __name__ == "__main__":
    unittest.main(verbosity=2)
