"""引用校验器测试（计划 §9.3 验收点：编造引用必须失败）。

这里测的是**规则**，不是模型。模型编造引用是必然事件，
校验器是唯一拦住它的东西，所以校验器本身必须被钉死。
"""

from __future__ import annotations

from rdi_agent.graph.citations import (
    check_citation,
    validate_card_citations,
    validate_set_citations,
)
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem
from rdi_agent.schemas.recommendation import (
    ParentAction,
    RecommendationCard,
    SourceCitation,
)

REAL_ID = "11111111-1111-1111-1111-111111111111"
FAKE_ID = "99999999-9999-9999-9999-999999999999"


def _item(evidence_id: str = REAL_ID, **kw) -> EvidenceItem:
    base = dict(
        evidence_id=evidence_id,
        source_type="guideline",
        title="Guided Participation",
        section_title="Guided Participation: The Apprentice",
        pdf_page_start=154,
        pdf_page_end=155,
        summary="the guided participation is a way of interacting with the child",
        source_excerpt="the guided participation is a way of interacting with the child "
        "that builds up the child's participation step by step",
        relevance_reason="向量召回第 1 名；相似度 0.6123",
        review_status="unreviewed",
        may_quote_to_user=False,
        similarity=0.6123,
        fts_matched=True,
    )
    base.update(kw)
    return EvidenceItem(**base)


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(
        original_question="孩子怎么学说话",
        case_summary={},
        evidence=list(items),
        evidence_gaps=[],
        retrieval_version="2c-v1",
    )


def _card(**kw) -> RecommendationCard:
    base = dict(
        plan_id="plan_1",
        title="先缩短出门这件事",
        observed_facts=["早上出门会哭"],
        explanation="先让孩子有时间准备好。",
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没有变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=True,
    )
    base.update(kw)
    return RecommendationCard(**base)


def _citation(**kw) -> SourceCitation:
    base = dict(
        source_id=REAL_ID,
        source_type="guideline",
        title="Guided Participation",
        chapter="Guided Participation: The Apprentice",
        pdf_page_start=154,
        pdf_page_end=155,
        claim_supported="书里用 guided participation 描述引导孩子参与的过程",
    )
    base.update(kw)
    return SourceCitation(**base)


# ------------------------------------------------------------------ 编造引用
def test_fabricated_source_id_is_rejected():
    """最关键的一条：source_id 不在证据包里 = 编造引用，必须失败。"""
    card = _card(source_citations=[_citation(source_id=FAKE_ID)])
    errors = validate_card_citations(card, _bundle(_item()))
    assert errors, "编造的 source_id 必须被拦下来"
    assert "编造" in errors[0]


def test_fabricated_citation_in_a_set_is_reported_per_plan():
    card = _card(plan_id="plan_2", source_citations=[_citation(source_id=FAKE_ID)])
    problems = validate_set_citations([card], _bundle(_item()))
    assert "plan_2" in problems
    assert "plan_1" not in problems


# ------------------------------------------------------------------ 页码 / 章节
def test_page_mismatch_is_rejected():
    card = _card(source_citations=[_citation(pdf_page_start=999)])
    errors = validate_card_citations(card, _bundle(_item()))
    assert any("pdf_page_start" in e for e in errors)


def test_chapter_mismatch_is_rejected():
    card = _card(source_citations=[_citation(chapter="Something Else Entirely")])
    errors = validate_card_citations(card, _bundle(_item()))
    assert any("章节" in e for e in errors)


def test_matching_chapter_passes():
    card = _card(source_citations=[_citation()])
    assert validate_card_citations(card, _bundle(_item())) == []


# ------------------------------------------------------------------ 主张支持
def test_english_term_not_in_evidence_is_rejected():
    """主张里引用的英文词必须真的在那条证据里。"""
    card = _card(
        source_citations=[_citation(claim_supported="书里用 scaffolding 来说明搭脚手架")]
    )
    errors = validate_card_citations(card, _bundle(_item()))
    assert any("scaffolding" in e for e in errors)


def test_english_term_in_evidence_passes():
    card = _card(
        source_citations=[_citation(claim_supported="书里用 guided participation 描述引导参与")]
    )
    assert validate_card_citations(card, _bundle(_item())) == []


def test_empty_claim_supported_is_rejected():
    card = _card(source_citations=[_citation(claim_supported="  ")])
    errors = validate_card_citations(card, _bundle(_item()))
    assert any("claim_supported" in e for e in errors)


# ------------------------------------------------------------------ book_summary
def test_book_summary_without_citation_is_rejected():
    """``book_summary`` 没有引用 —— Pydantic 层就拦住了，构造不出这样的卡。

    校验器里还有一条同样的检查，是给绕过 schema 的路径兜底的。
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _card(application_label="source_summary", source_citations=[])


def test_book_summary_with_valid_citation_passes():
    card = _card(application_label="source_summary", source_citations=[_citation()])
    assert validate_card_citations(card, _bundle(_item())) == []


def test_citation_without_any_evidence_is_rejected():
    card = _card(source_citations=[_citation()])
    errors = validate_card_citations(card, _bundle())
    assert errors


# ------------------------------------------------------------------ 细节
def test_check_citation_returns_the_citation():
    result = check_citation(_citation(source_id=FAKE_ID), _bundle(_item()))
    assert not result.ok
    assert result.citation.source_id == FAKE_ID


def test_wrong_source_type_is_rejected():
    card = _card(source_citations=[_citation(source_type="external_research")])
    errors = validate_card_citations(card, _bundle(_item()))
    assert any("source_type" in e for e in errors)
