"""方案修订的纯函数与回溯点测试。

这里不连 Postgres：事实合并、风险复查、卡号前缀都是纯函数；
回溯点定位用 InMemorySaver 跑一遍真图，验证写进 state 的
``fork_checkpoint_id`` 确实是"生成建议卡之前"那个 checkpoint。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from rdi_agent import llm
from rdi_agent.api.revision import _risk_decision
from rdi_agent.graph.builder import build_graph
from rdi_agent.graph.facts_merge import merge_parent_supplement
from rdi_agent.graph.nodes import chat_nodes, memory_nodes, retrieval_nodes, write_nodes
from rdi_agent.graph.nodes.chat_nodes import apply_plan_id_prefix, plan_prefix
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    RecommendationSet,
    SafetyReviewCheck,
    SufficiencyDecision,
)
from rdi_agent.schemas.evidence import EvidenceDecision
from rdi_agent.schemas.memory import MemoryExtractionResult
from rdi_agent.schemas.recommendation import (
    ExplanationPart,
    ParentAction,
    RecommendationCard,
)

CID = "00000000-0000-0000-0000-00000000000b"
EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"


def _card(plan_id: str, recommended: bool = False) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["早上出门会哭"],
        explanation_parts=[ExplanationPart(text="先把难受的时长缩短。")],
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=recommended,
    )


# --------------------------------------------------------------- 事实合并
def test_supplement_keeps_old_facts_and_adds_new_one():
    previous = {
        "observed_facts": ["孩子 4 岁", "早上出门会哭"],
        "already_tried": ["提前十分钟出门"],
        "context": {"场景": "幼儿园门口"},
        "risk_signals": [],
    }
    merged, meta = merge_parent_supplement(
        previous, "家里没人帮忙接送，只能晚上做", []
    )

    facts = merged["observed_facts"]
    assert "孩子 4 岁" in facts, "旧事实不能丢"
    assert "早上出门会哭" in facts
    assert any("只能晚上做" in f for f in facts), (
        "新情况必须由代码兜底进事实清单，不能依赖模型抽取"
    )
    assert merged["already_tried"] == ["提前十分钟出门"]
    assert meta["added"] is True


def test_supplement_filters_forbidden_facts_unless_restated():
    previous = {
        "observed_facts": ["已经换过老师", "孩子 4 岁"],
        "already_tried": ["已经换过老师", "提前出门"],
        "risk_signals": [],
    }

    merged, meta = merge_parent_supplement(previous, "只能晚上做", ["已经换过老师"])
    assert "已经换过老师" not in merged["observed_facts"], (
        "家长已经作废的旧事实不能从修订入口漏回去"
    )
    assert "孩子 4 岁" in merged["observed_facts"]
    assert meta["dropped"] >= 1

    # 家长这轮又亲口说了 —— 以最新说法为准，保留。
    merged2, meta2 = merge_parent_supplement(
        previous, "已经换过老师这件事先不说", ["已经换过老师"]
    )
    assert "已经换过老师" in merged2["observed_facts"]
    assert "已经换过老师" in meta2["restated"]


# --------------------------------------------------------------- 风险复查
def test_new_aggression_signal_forces_professional_support():
    decision = _risk_decision("孩子最近开始打人，还会推倒小朋友", {})
    assert decision is not None, "补充的攻击行为必须触发风险复查"
    assert decision["high_risk"] is True
    assert decision["decision"] == "professional_support"


def test_ordinary_crying_is_not_risk():
    assert _risk_decision("最近在幼儿园门口哭得更久了", {}) is None, (
        "普通哭闹/分离焦虑不是风险信号，不能把家长吓去就医"
    )


# --------------------------------------------------------------- 卡号前缀
def test_plan_id_prefix_keeps_v1_and_prefixes_later_versions():
    assert plan_prefix(1) == ""
    assert plan_prefix(2) == "r2_"

    cards = [_card("plan_1", True), _card("plan_2")]
    apply_plan_id_prefix(cards, 1)
    assert [c.plan_id for c in cards] == ["plan_1", "plan_2"], "第 1 版保持原样"

    apply_plan_id_prefix(cards, 2)
    assert [c.plan_id for c in cards] == ["r2_plan_1", "r2_plan_2"], (
        "第 2 版必须换号，否则 save_candidate_plans 会当成旧卡直接跳过"
    )

    # 模型已经自己带了前缀 / 写了奇怪 id，也要收敛到当前版本。
    odd = [_card("r2_plan_9"), _card("foo")]
    apply_plan_id_prefix(odd, 3)
    assert [c.plan_id for c in odd] == ["r3_plan_9", "r3_plan_2"]


# --------------------------------------------------------------- 回溯点
def _bundle() -> dict:
    return {
        "original_question": "孩子 4 岁",
        "case_summary": {},
        "evidence": [
            {
                "evidence_id": EVIDENCE_ID,
                "source_type": "guideline",
                "title": "测试指南",
                "section_title": "测试章节",
                "pdf_page_start": 1,
                "pdf_page_end": 2,
                "print_page_start": None,
                "print_page_end": None,
                "summary": "先把难受的时长缩短。",
                "source_excerpt": "先把难受的时长缩短。",
                "relevance_reason": "测试",
                "limitations": [],
                "review_status": "unreviewed",
                "may_quote_to_user": False,
                "similarity": 0.9,
                "fts_matched": True,
            }
        ],
        "evidence_gaps": [],
        "retrieval_version": "test",
    }


@pytest.mark.asyncio
async def test_fork_checkpoint_points_before_build_recommendation(monkeypatch):
    """写进 state 的回溯点必须正好是"下一步生成建议卡"的 checkpoint。"""
    async def no_mem_extract(state):
        return {"memory_candidates": [], "memory_notes": []}

    async def no_mem_merge(state):
        return {}

    async def no_mem_commit(state):
        return {}

    async def fake_load(**kwargs):
        return {"child_context_patch": {}, "memory_context": {}, "forbidden_facts": []}

    async def fake_rewrite(state):
        return {
            "retrieval_queries": {
                "original_query": state.get("user_message", ""),
                "semantic_query": state.get("user_message", ""),
                "keyword_query": state.get("user_message", ""),
            }
        }

    async def fake_retrieve(state):
        return {"retrieval_candidates": []}

    async def fake_rerank(state):
        return {"evidence_bundle": _bundle()}

    async def fake_persist_bundle(state):
        return {"evidence_bundle_db_id": "bundle_fake"}

    async def fake_persist_plans(state):
        return {"plan_db_ids": {}}

    async def fake_chat_json(**kwargs):
        model_cls = kwargs["model_cls"]
        if model_cls is CaseSummary:
            payload = CaseSummary(observed_facts=["4 岁，早上出门哭"])
        elif model_cls is SufficiencyDecision:
            payload = SufficiencyDecision(requires_followup=False)
        elif model_cls is EvidenceDecision:
            payload = EvidenceDecision(evidence_status="direct", high_risk=False)
        elif model_cls is RecommendationSet:
            payload = RecommendationSet(plans=[_card("plan_1", True)])
        elif model_cls is SafetyReviewCheck:
            payload = SafetyReviewCheck(passed=True, severity="none", concerns=[])
        elif model_cls is MemoryExtractionResult:
            payload = MemoryExtractionResult(candidates=[], notes=[])
        else:
            raise AssertionError(f"没预置答案：{kwargs.get('stage')}")
        return payload, llm.LLMTrace(stage=kwargs.get("stage", ""), source="fake")

    monkeypatch.setattr(memory_nodes, "extract_memories", no_mem_extract)
    monkeypatch.setattr(memory_nodes, "merge_memories", no_mem_merge)
    monkeypatch.setattr(memory_nodes, "commit_memory", no_mem_commit)
    monkeypatch.setattr(chat_nodes.memory_service, "load_memory_context", fake_load)
    monkeypatch.setattr(retrieval_nodes, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(retrieval_nodes, "retrieve_knowledge", fake_retrieve)
    monkeypatch.setattr(retrieval_nodes, "rerank_and_expand", fake_rerank)
    monkeypatch.setattr(write_nodes, "persist_evidence_bundle", fake_persist_bundle)
    monkeypatch.setattr(write_nodes, "persist_candidate_plans", fake_persist_plans)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)

    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": CID}}
    result = await graph.ainvoke(
        {
            "entrypoint": "chat",
            "conversation_id": CID,
            "thread_id": CID,
            "family_id": "fam",
            "child_id": "child",
            "turn_id": "t1",
            "user_message": "孩子 4 岁，早上出门哭",
        },
        config=config,
    )

    fork_id = result.get("fork_checkpoint_id")
    assert fork_id, "build_recommendation 没记下回溯点"
    snapshot = await graph.aget_state(
        {"configurable": {"thread_id": CID, "checkpoint_id": fork_id}}
    )
    assert "build_recommendation" in (snapshot.next or ()), (
        f"回溯点定位错了，next={snapshot.next}"
    )
    assert (snapshot.values or {}).get("evidence_bundle"), (
        "回溯点必须已经拿到证据包，否则修订会重新检索"
    )
    assert "case_summary" in (snapshot.values or {})
