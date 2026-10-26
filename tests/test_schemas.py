"""契约测试。

这些是计划 §4.3 的硬规则，**必须**由代码强制，不能只靠 prompt 自觉。
所以这里直接测 Pydantic 拒不拒绝。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rdi_agent.schemas import (
    CaseSummary,
    EvidenceBundle,
    EvidenceDecision,
    EvidenceItem,
    ParentAction,
    RecommendationCard,
    SourceCitation,
)


def _card(**overrides) -> dict:
    base = {
        "plan_id": "p1",
        "title": "先做一次简短的告别仪式",
        "observed_facts": ["早晨出门前会哭"],
        "explanation": "书里提到逐步告别可以帮助孩子建立可预测感。",
        "parent_actions": [
            {
                "action": "告别仪式",
                "context": "在门口",
                "steps": ["提前 5 分钟告知", "拥抱 3 次", "挥手离开"],
                "observe": ["孩子是否跟随视线", "哭闹时长"],
                "reduce_or_stop_if": ["孩子持续尖叫超过 5 分钟"],
            }
        ],
        "success_indicators": ["哭闹时间比昨天短"],
        "stop_conditions": ["孩子出现自伤行为"],
        "application_label": "ai_adaptation",
    }
    base.update(overrides)
    return base


class TestRecommendationCard:
    def test_valid_card_passes(self):
        card = RecommendationCard(**_card())
        assert card.plan_id == "p1"
        assert card.parent_actions[0].steps

    def test_empty_success_indicators_rejected(self):
        with pytest.raises(ValidationError, match="观察指标"):
            RecommendationCard(**_card(success_indicators=[]))

    def test_empty_stop_conditions_rejected(self):
        with pytest.raises(ValidationError, match="停止条件"):
            RecommendationCard(**_card(stop_conditions=[]))

    def test_book_summary_without_citation_rejected(self):
        with pytest.raises(ValidationError, match="真实引用"):
            RecommendationCard(**_card(application_label="source_summary"))

    def test_book_summary_with_citation_passes(self):
        card = RecommendationCard(
            **_card(
                application_label="source_summary",
                source_citations=[
                    {
                        "source_id": "guideline2026",
                        "source_type": "guideline",
                        "title": "The RDI Book",
                        "chapter": "Chapter 5",
                        "pdf_page_start": 120,
                        "pdf_page_end": 121,
                        "claim_supported": "逐步告别的原理",
                    }
                ],
            )
        )
        assert len(card.source_citations) == 1

    def test_ai_adaptation_without_citation_allowed(self):
        card = RecommendationCard(**_card(application_label="ai_adaptation"))
        assert card.source_citations == []


class TestCaseSummary:
    def test_no_diagnostic_field(self):
        """结构上禁止诊断：没有 diagnostic_conclusion 这类字段可填。"""
        fields = CaseSummary.model_fields
        assert "diagnostic_conclusion" not in fields
        assert "diagnosis" not in fields
        with pytest.raises(ValidationError):
            CaseSummary(diagnosis="自闭症")


class TestEvidence:
    def _item(self, **overrides) -> dict:
        base = {
            "evidence_id": "e1",
            "source_type": "guideline",
            "title": "The RDI Book",
            "section_title": "Guided Participation",
            "pdf_page_start": 120,
            "pdf_page_end": 121,
            "summary": "共同参与可以建立共同注意力。",
            "relevance_reason": "与孩子在集体活动中难以参与对应。",
            "review_status": "unreviewed",
        }
        base.update(overrides)
        return base

    def test_item_defaults(self):
        item = EvidenceItem(**self._item())
        assert item.may_quote_to_user is False
        assert item.print_page_start is None

    def test_bundle_accepts_empty_evidence(self):
        bundle = EvidenceBundle(
            original_question="孩子在幼儿园哭",
            case_summary={},
            evidence=[],
            retrieval_version="v1",
        )
        assert bundle.evidence == []
        assert bundle.evidence_gaps == []

    def test_decision_vocabulary(self):
        d = EvidenceDecision(evidence_status="direct", high_risk=False)
        assert d.high_risk is False
        with pytest.raises(ValidationError):
            EvidenceDecision(evidence_status="maybe", high_risk=False)

    def test_can_support_field_is_gone(self):
        """``can_support_personalized_advice`` 已删除（2026-10-26）。

        它由模型自由填，和 ``decision`` 经常自相矛盾 ——
        同一份证据两次门控给出不同结论，家长拿到的东西成了掷骰子。
        """
        with pytest.raises(ValidationError):
            EvidenceDecision(
                evidence_status="direct",
                high_risk=False,
                can_support_personalized_advice=True,
            )

    def test_decision_field_is_gone(self):
        """``decision`` 也不再由模型填 —— 改由 ``decide_flow`` 算出来。

        模型填它就等于让模型决定"给不给方案卡"，那正是随机性的来源。
        """
        with pytest.raises(ValidationError):
            EvidenceDecision(
                evidence_status="direct",
                high_risk=False,
                decision="limited_answer",
            )


class TestCitation:
    def test_citation_fields(self):
        c = SourceCitation(
            source_id="guideline2026",
            source_type="guideline",
            title="The RDI Book",
            claim_supported="逐步告别",
        )
        assert c.pdf_page_start is None
        assert c.chapter is None
