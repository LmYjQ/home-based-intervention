"""安全审查的**逐卡丢弃**（2026-10-26）。

**这个文件测的是一条真实的线上事故**：

三张建议卡里，一张的标题写成
「尝试忽略孩子的哭闹，让孩子逐渐适应出门过程」，
命中安全规则 ``ignore_distress``（"忽视明显不适"）。
之前是**整组卡连坐丢掉** —— 另外两张无辜的也没了，
家长只拿到一段有限回答，前端还显示"证据不够"（更误导）。

规矩：
1. 命中的卡单独丢，其余几张照常给；
2. 全被命中才退到有限回答；
3. 丢掉的正是推荐卡时，剩下的要补一个推荐位。
"""

from __future__ import annotations

import pytest

from rdi_agent import llm
from rdi_agent.graph.nodes import retrieval_nodes
from rdi_agent.graph.state import CoachState
from rdi_agent.schemas.conversation import SafetyReviewCheck
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem
from rdi_agent.schemas.recommendation import ParentAction, RecommendationCard

EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"


def _item() -> EvidenceItem:
    return EvidenceItem(
        evidence_id=EVIDENCE_ID,
        source_type="guideline",
        title="针对挑战性行为的干预",
        section_title="（四）针对挑战性行为的干预",
        pdf_page_start=7,
        pdf_page_end=8,
        summary="挑战性行为干预应坚持预防为主，通过功能分析明确行为的功能",
        source_excerpt="挑战性行为干预应坚持预防为主，通过功能分析明确行为的功能。",
        relevance_reason="向量召回第 1 名",
        review_status="unreviewed",
        may_quote_to_user=False,
        similarity=0.61,
        fts_matched=True,
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        original_question="孩子早上出门就哭",
        case_summary={},
        evidence=[_item()],
        evidence_gaps=[],
        retrieval_version="2c-v1",
    )


def _card(plan_id: str = "plan_1", **kw) -> RecommendationCard:
    base = dict(
        plan_id=plan_id,
        title="先缩短出门这件事",
        observed_facts=["早上出门会哭"],
        explanation="先让孩子有时间准备好。",
        parent_actions=[
            ParentAction(
                action="提前十分钟出门",
                context="早上",
                steps=["出门前先陪他看一会儿要带的东西"],
                observe=["他有没有主动拿东西"],
                reduce_or_stop_if=["他明显抗拒就停下来"],
            )
        ],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没有变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=False,
    )
    base.update(kw)
    return RecommendationCard(**base)


def _bad_card() -> RecommendationCard:
    """命中 ignore_distress 的卡：忽略 + 哭闹。"""
    return _card(
        plan_id="plan_1",
        title="尝试忽略孩子的哭闹，让孩子逐渐适应出门过程",
        explanation="先不回应他的哭闹。",
        recommended=True,
    )


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """LLM 复核一律放行 —— 这里只测规则层怎么处置被点名的卡。"""

    async def fake(**kwargs):
        return (
            SafetyReviewCheck(passed=True, severity="none", concerns=[]),
            llm.LLMTrace(stage="safety_review", model="fake", source="fake"),
        )

    monkeypatch.setattr(llm, "chat_json", fake)


def _state(cards: list[RecommendationCard]) -> CoachState:
    return {
        "candidate_plans": [c.model_dump(mode="json") for c in cards],
        "evidence_bundle": _bundle().model_dump(mode="json"),
        "final_answer": "",
    }


# ------------------------------------------------------------------ 逐卡丢弃
@pytest.mark.asyncio
async def test_flagged_card_is_dropped_others_survive():
    """命中的那张丢掉，剩下的照常。"""
    good = _card(plan_id="plan_2", title="出门前先玩一个小游戏")
    out = await retrieval_nodes.safety_review(_state([_bad_card(), good]))

    assert out["safety_dropped_plans"] == ["plan_1"]
    kept = out["candidate_plans"]
    assert len(kept) == 1
    assert kept[0]["plan_id"] == "plan_2"
    assert out["safety_review"]["result"] == "pass"


@pytest.mark.asyncio
async def test_dropped_recommendation_gets_replaced():
    """被丢掉的正好是推荐卡，剩下的要补一个推荐位。"""
    good = _card(plan_id="plan_2", title="出门前先玩一个小游戏")
    out = await retrieval_nodes.safety_review(_state([_bad_card(), good]))
    kept = out["candidate_plans"]
    assert any(c["recommended"] for c in kept)


@pytest.mark.asyncio
async def test_clean_cards_are_untouched():
    """没有卡命中时，什么都不该动。"""
    cards = [_card(plan_id="plan_1"), _card(plan_id="plan_2")]
    out = await retrieval_nodes.safety_review(_state(cards))
    assert "safety_dropped_plans" not in out
    assert "candidate_plans" not in out
    assert out["safety_review"]["result"] == "pass"


# ------------------------------------------------------------------ 全被拦
@pytest.mark.asyncio
async def test_all_flagged_records_every_dropped_id():
    """全被拦下时要记清楚是哪几张 —— 前端要靠它说真话。"""
    out = await retrieval_nodes.safety_review(_state([_bad_card()]))
    assert out["safety_dropped_plans"] == ["plan_1"]
    # 一张不剩时不能把被拦的卡又写回 state
    assert "candidate_plans" not in out
    assert out["safety_review"]["result"] == "blocked"


# ------------------------------------------------------------------ API 状态
def test_status_distinguishes_safety_blocked_from_insufficient_evidence():
    """有限回答有两种原因，状态要分开报。

    实测踩过的坑：卡全被安全审查拦下，状态却报"证据不够" ——
    家长和调试的人都会被引到完全错误的方向。
    """
    from rdi_agent.api.conversations import _status_of

    blocked = _status_of(None, {"answer_mode": "limited", "safety_dropped_plans": ["plan_1"]})
    insufficient = _status_of(None, {"answer_mode": "limited"})
    assert blocked == "safety_blocked"
    assert insufficient == "limited_answer"


def test_payload_carries_safety_review_details():
    """前端要能说清"少了几张、为什么少"，payload 里必须带上。"""
    from rdi_agent.api.conversations import _turn_payload

    payload = _turn_payload(
        "conv",
        {
            "answer_mode": "limited",
            "safety_dropped_plans": ["plan_1"],
            "safety_review": {
                "result": "blocked",
                "rules_hit": ["ignore_distress"],
                "findings": ["[plan_1] 出现了忽视明显不适的建议。"],
            },
        },
    )
    assert payload["status"] == "safety_blocked"
    assert payload["safety_review"]["dropped_plans"] == ["plan_1"]
    assert payload["safety_review"]["rules_hit"] == ["ignore_distress"]
