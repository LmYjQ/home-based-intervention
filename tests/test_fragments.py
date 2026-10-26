"""片段级引用的测试（方案 A）。

卡级引用只能回答"这张卡依据了哪几条"，回答不了"**这句话**依据的是哪一条"。
说明拆成 ``explanation_parts`` 之后，每段带自己的 ``evidence_ids``，
这里钉住三件事：

1. 片段引用的证据必须在本次证据包里（编造的引用必须失败）
2. 片段里的指南术语必须出现在**它引用的那条**证据里
3. 短编号（E1/E2）会被翻译回真实 id —— 翻译之后校验器只认真实 id
"""

from __future__ import annotations

from rdi_agent.graph.citations import (
    build_evidence_catalogue,
    translate_short_ids,
    validate_card_fragments,
    validate_cards,
)
from rdi_agent.graph.nodes.retrieval_nodes import render_answer
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem
from rdi_agent.schemas.recommendation import (
    ExplanationPart,
    ParentAction,
    RecommendationCard,
    SourceCitation,
)

REAL_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
FAKE_ID = "99999999-9999-9999-9999-999999999999"


def _item(evidence_id: str = REAL_ID, **kw) -> EvidenceItem:
    base = dict(
        evidence_id=evidence_id,
        source_type="guideline",
        title="针对缺陷性行为",
        section_title="004 针对缺陷性行为",
        pdf_page_start=7,
        pdf_page_end=8,
        summary="可以用消退的方法减少孩子的哭闹",
        source_excerpt="消退是通过不再回应来减少行为出现的方法。",
        relevance_reason="关键词路第 1 名",
        review_status="unreviewed",
        may_quote_to_user=False,
        similarity=0.61,
        fts_matched=True,
    )
    base.update(kw)
    return EvidenceItem(**base)


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(
        original_question="孩子不愿意去幼儿园",
        case_summary={},
        evidence=list(items),
        evidence_gaps=[],
        retrieval_version="frag-v1",
    )


def _card(**kw) -> RecommendationCard:
    base = dict(
        plan_id="plan_1",
        title="先把出门这件事变短",
        observed_facts=["早上出门会哭"],
        explanation_parts=[ExplanationPart(text="先让孩子有时间准备好。")],
        parent_actions=[
            ParentAction(action="提前十分钟出门", context="早上"),
        ],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没有变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=True,
    )
    base.update(kw)
    return RecommendationCard(**base)


# ------------------------------------------------------------------ 片段校验
def test_unknown_evidence_id_in_fragment_is_rejected():
    """片段引用了不在证据包里的 id —— 和编造引用一样，必须失败。"""
    card = _card(
        explanation_parts=[ExplanationPart(text="先做点什么。", evidence_ids=[FAKE_ID])]
    )
    errors = validate_card_fragments(card, _bundle(_item()))
    assert errors
    assert any("不在本次证据包里" in e for e in errors)


def test_term_not_in_cited_evidence_is_rejected():
    """片段提到"消退"，但它引用的证据里没有这个词 —— 挂错证据或编造。"""
    card = _card(
        application_label="source_summary",
        source_citations=[
            SourceCitation(
                source_id=OTHER_ID,
                source_type="guideline",
                title="别的章节",
                claim_supported="随便一条",
            )
        ],
        explanation_parts=[
            ExplanationPart(text="可以用消退来减少哭闹。", evidence_ids=[OTHER_ID])
        ],
    )
    errors = validate_card_fragments(card, _bundle(_item(REAL_ID), _item(OTHER_ID, summary="别的话", source_excerpt="别的话")))
    assert any("消退" in e for e in errors)


def test_term_in_cited_evidence_passes():
    card = _card(
        application_label="source_summary",
        source_citations=[
            SourceCitation(
                source_id=REAL_ID,
                source_type="guideline",
                title="针对缺陷性行为",
                claim_supported="消退",
            )
        ],
        explanation_parts=[
            ExplanationPart(text="可以用消退来减少哭闹。", evidence_ids=[REAL_ID])
        ],
    )
    assert validate_card_fragments(card, _bundle(_item())) == []


def test_source_summary_fragment_without_evidence_is_rejected():
    """标了"指南依据"，却有一段提到指南术语却不指出是哪一段 —— 家长核不了。"""
    card = _card(
        application_label="source_summary",
        source_citations=[
            SourceCitation(
                source_id=REAL_ID,
                source_type="guideline",
                title="针对缺陷性行为",
                claim_supported="消退",
            )
        ],
        explanation_parts=[ExplanationPart(text="可以用消退来减少哭闹。", evidence_ids=[])],
    )
    errors = validate_card_fragments(card, _bundle(_item()))
    assert any("没有挂任何证据" in e for e in errors)


def test_ai_adaptation_fragment_without_evidence_is_allowed():
    """应用转化的卡本来就是在讲"我们怎么用"，不挂证据是正常的。"""
    card = _card(
        application_label="ai_adaptation",
        explanation_parts=[ExplanationPart(text="可以用消退来减少哭闹。", evidence_ids=[])],
    )
    assert validate_card_fragments(card, _bundle(_item())) == []


def test_action_evidence_ids_are_checked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="用正强化",
                context="出门前",
                evidence_ids=[FAKE_ID],
            )
        ]
    )
    errors = validate_card_fragments(card, _bundle(_item()))
    assert any("不在本次证据包里" in e for e in errors)


def test_validate_cards_covers_both_levels():
    """总入口要把卡级和片段级的问题都报出来。"""
    card = _card(
        application_label="source_summary",
        source_citations=[
            SourceCitation(
                source_id=FAKE_ID,
                source_type="guideline",
                title="编的",
                claim_supported="消退",
            )
        ],
        explanation_parts=[
            ExplanationPart(text="可以用消退来减少哭闹。", evidence_ids=[FAKE_ID])
        ],
    )
    problems = validate_cards([card], _bundle(_item()))
    assert "plan_1" in problems
    assert len(problems["plan_1"]) >= 2  # 卡级一条 + 片段级一条


# ------------------------------------------------------------------ 短编号
def test_catalogue_numbers_evidence_in_order():
    evidence = [
        {"evidence_id": REAL_ID, "section_title": "第四章", "summary": "摘要一", "pdf_page_start": 7, "pdf_page_end": 8, "source_type": "guideline", "review_status": "unreviewed"},
        {"evidence_id": OTHER_ID, "section_title": "第五章", "summary": "摘要二", "pdf_page_start": 9, "pdf_page_end": 9, "source_type": "guideline", "review_status": "unreviewed"},
    ]
    text, mapping = build_evidence_catalogue(evidence)
    assert mapping == {"E1": REAL_ID, "E2": OTHER_ID}
    assert "E1" in text and "E2" in text
    # 清单里不该出现 UUID —— 模型只需要记编号
    assert REAL_ID not in text


def test_short_ids_are_translated_back():
    card = _card(
        explanation_parts=[ExplanationPart(text="可以做点什么。", evidence_ids=["E1"])],
        parent_actions=[ParentAction(action="做一下", context="早上", evidence_ids=["E2"])],
        source_citations=[
            SourceCitation(
                source_id="E1",
                source_type="guideline",
                title="第四章",
                claim_supported="随便",
            )
        ],
    )
    count = translate_short_ids([card], {"E1": REAL_ID, "E2": OTHER_ID})
    assert count == 3
    assert card.explanation_parts[0].evidence_ids == [REAL_ID]
    assert card.parent_actions[0].evidence_ids == [OTHER_ID]
    assert card.source_citations[0].source_id == REAL_ID


def test_unknown_short_id_is_left_alone():
    """编号不存在时原样留着 —— 交给校验器报错，别在这里悄悄吞掉。"""
    card = _card(
        explanation_parts=[ExplanationPart(text="可以做点什么。", evidence_ids=["E9"])]
    )
    translate_short_ids([card], {"E1": REAL_ID})
    assert card.explanation_parts[0].evidence_ids == ["E9"]


# ------------------------------------------------------------------ 渲染
def test_render_marks_each_fragment_with_its_evidence():
    """片段后面要挂编号，回答末尾要有对应的编号清单。"""
    card = _card(
        explanation_parts=[
            ExplanationPart(text="消退可以减少哭闹。", evidence_ids=[REAL_ID]),
            ExplanationPart(text="我们建议先从十分钟开始。", evidence_ids=[]),
        ],
        source_citations=[
            SourceCitation(
                source_id=REAL_ID,
                source_type="guideline",
                title="004 针对缺陷性行为",
                chapter="004 针对缺陷性行为",
                pdf_page_start=7,
                pdf_page_end=8,
                claim_supported="消退",
            )
        ],
    )
    state = {
        "candidate_plans": [card.model_dump(mode="json")],
        "evidence_bundle": _bundle(_item()).model_dump(mode="json"),
        "safety_review": {"result": "pass"},
    }
    out = render_answer(state)
    text = out["final_answer"]
    assert "消退可以减少哭闹。[1]" in text
    # 没有依据的那段不挂编号
    assert "我们建议先从十分钟开始。\n" in text
    # 末尾的编号清单
    assert "[1] 《004 针对缺陷性行为》，PDF 第 7-8 页" in text


def test_render_without_evidence_says_so():
    card = _card(
        explanation_parts=[ExplanationPart(text="我们建议先从十分钟开始。", evidence_ids=[])],
    )
    state = {
        "candidate_plans": [card.model_dump(mode="json")],
        "evidence_bundle": _bundle().model_dump(mode="json"),
        "safety_review": {"result": "pass"},
    }
    text = render_answer(state)["final_answer"]
    assert "没有直接引用指南里的原文" in text


# ------------------------------------------------------------------ 契约
def test_legacy_explanation_string_is_still_readable():
    """数据库里已经落了一批老卡（只有一整段 explanation）—— 不能读不出来。"""
    card = RecommendationCard.model_validate(
        {
            "plan_id": "plan_1",
            "title": "标题",
            "observed_facts": [],
            "explanation": "老形状的一段说明",
            "parent_actions": [],
            "success_indicators": ["a"],
            "stop_conditions": ["b"],
            "application_label": "ai_adaptation",
        }
    )
    assert card.explanation_parts[0].text == "老形状的一段说明"
    assert card.explanation_text == "老形状的一段说明"


def test_schema_requires_explanation_parts():
    """说明必须给 —— 不给的话卡片只剩动作清单。"""
    schema = RecommendationCard.model_json_schema()
    assert "explanation_parts" in schema["required"]
    assert "explanation_parts" in schema["properties"]
