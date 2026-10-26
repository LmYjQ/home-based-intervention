"""方案修订接口的集成测试（需要 Postgres + Checkpointer）。

覆盖计划 §9 的验收点里能自动判的部分：

- 补充新情况能拿到第 2 版卡，家长没有被重新提问；
- 新卡沿用上一版同一个证据包；
- 新卡卡号带版本前缀，不和旧卡撞车；
- 选新卡建的是新 trial，旧 trial 还在；
- 同一个 turn_id 重复提交不产生第二条分支；
- 补充风险信号时不出卡，改走专业支持。

检索链被替身顶掉（不连 embedding），其余走真实图 + 真实 Postgres。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from rdi_agent import llm
from rdi_agent.api import conversations as api
from rdi_agent.api import revision as rev
from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Plan, Trial
from rdi_agent.graph.nodes import retrieval_nodes
from rdi_agent.graph.runtime import setup_graph, shutdown_graph
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    LimitedAnswer,
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

EVIDENCE_ID = "22222222-2222-2222-2222-222222222222"


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
                "pdf_page_start": 3,
                "pdf_page_end": 4,
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


def _card(plan_id: str, recommended: bool) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["4 岁，早上出门哭"],
        explanation_parts=[ExplanationPart(text="先把难受的时长缩短。")],
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=recommended,
    )


@pytest.fixture
def no_retrieval(monkeypatch):
    """检索链替身；顺带记录有没有被调用（修订时不该再调）。"""
    calls = {"rewrite": 0, "retrieve": 0, "rerank": 0}

    async def fake_rewrite(state):
        calls["rewrite"] += 1
        return {
            "retrieval_queries": {
                "original_query": state.get("user_message", ""),
                "semantic_query": state.get("user_message", ""),
                "keyword_query": state.get("user_message", ""),
            }
        }

    async def fake_retrieve(state):
        calls["retrieve"] += 1
        return {"retrieval_candidates": []}

    async def fake_rerank(state):
        calls["rerank"] += 1
        return {"evidence_bundle": _bundle()}

    monkeypatch.setattr(retrieval_nodes, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(retrieval_nodes, "retrieve_knowledge", fake_retrieve)
    monkeypatch.setattr(retrieval_nodes, "rerank_and_expand", fake_rerank)
    return calls


@pytest.fixture
async def db():
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        await dispose_engine()
        pytest.skip(f"Postgres 不可用，跳过集成测试：{type(e).__name__}: {e}")
    yield
    await dispose_engine()


@pytest.fixture
async def graph(db):
    await setup_graph()
    try:
        yield
    finally:
        await shutdown_graph()


@pytest.fixture
def fake_llm(monkeypatch):
    state = {"calls": []}

    async def fake_chat_json(**kwargs):
        model_cls = kwargs["model_cls"]
        stage = kwargs.get("stage", "")
        state["calls"].append(stage)
        if model_cls is CaseSummary:
            payload = CaseSummary(observed_facts=["4 岁，早上出门哭"])
        elif model_cls is SufficiencyDecision:
            payload = SufficiencyDecision(requires_followup=False)
        elif model_cls is EvidenceDecision:
            payload = EvidenceDecision(evidence_status="direct", high_risk=False)
        elif model_cls is RecommendationSet:
            payload = RecommendationSet(
                plans=[_card("plan_1", True), _card("plan_2", False)]
            )
        elif model_cls is SafetyReviewCheck:
            payload = SafetyReviewCheck(passed=True, severity="none", concerns=[])
        elif model_cls is LimitedAnswer:
            payload = LimitedAnswer(
                answer="你描述的情况建议尽快找专业的人面对面评估。",
                low_risk_actions=["保证安全，有人陪着"],
                observe=["记录下来"],
                when_to_seek_professional_help=["出现自伤或伤人时立刻就医"],
                knowledge_gap_note="这一轮先不给行为调整建议。",
            )
        elif model_cls is MemoryExtractionResult:
            payload = MemoryExtractionResult(candidates=[], notes=[])
        else:
            raise AssertionError(f"没预置答案：{stage}")
        return payload, llm.LLMTrace(stage=stage, source="fake")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return state


async def _seed_trial() -> tuple[str, str]:
    """走完一轮聊天并选一张卡，返回 (conversation_id, trial_id)。"""
    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label="修订测试孩子", birth_date="2021-06")
    )
    cid = conv["conversation_id"]
    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    assert res["status"] == "awaiting_plan_selection", res
    sel = await api.select_plan(
        cid, api.SelectPlanRequest(plan_id="plan_1", turn_id="t-select")
    )
    assert sel["trial_id"]
    return cid, sel["trial_id"]


@pytest.mark.asyncio
async def test_revision_reuses_evidence_and_versions_plan_ids(
    no_retrieval, graph, fake_llm
):
    cid, trial_id = await _seed_trial()
    calls_after_v1 = list(fake_llm["calls"])
    retrieval_after_v1 = dict(no_retrieval)

    res = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(
            revision_note="家里没人帮忙接送，只能晚上做",
            turn_id="rev_1",
        ),
    )

    assert res["status"] == "awaiting_plan_selection"
    assert res["plan_version"] == 2
    current = [p for p in res["plans"] if p.get("is_current_version")]
    archived = [p for p in res["plans"] if not p.get("is_current_version")]
    assert {p["plan_id"] for p in current} == {"r2_plan_1", "r2_plan_2"}, (
        "第 2 版卡号必须带 r2_ 前缀，否则会撞上旧卡"
    )
    assert {p["plan_id"] for p in archived} == {"plan_1", "plan_2"}, (
        "旧版本要保留，并在界面上标成已归档"
    )

    # 没有重新问家长、没有重新检索
    assert "intake_parse" not in fake_llm["calls"][len(calls_after_v1):]
    assert "sufficiency" not in fake_llm["calls"][len(calls_after_v1):]
    assert no_retrieval == retrieval_after_v1, "修订不该重跑检索链"

    # 新卡挂的是同一个证据包
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Plan)
                .where(Plan.conversation_id == cid)
                .order_by(Plan.plan_version)
            )
        ).scalars().all()
    by_version: dict[int, set[str]] = {}
    for row in rows:
        by_version.setdefault(row.plan_version, set()).add(str(row.evidence_bundle_id))
    assert by_version[1] == by_version[2] and by_version[1] != {"None"}, (
        f"新版没有沿用上一版的证据包：{by_version}"
    )

    # ---- 幂等：同一个 turn_id 再来一次，不再调 LLM、不多出方案 ----
    calls_before_retry = len(fake_llm["calls"])
    again = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(
            revision_note="家里没人帮忙接送，只能晚上做",
            turn_id="rev_1",
        ),
    )
    assert again["status"] == "awaiting_plan_selection"
    assert len(fake_llm["calls"]) == calls_before_retry, "重复提交不该再跑一遍模型"
    async with session_scope() as session:
        same_turn = (
            await session.execute(
                select(Plan).where(
                    Plan.conversation_id == cid, Plan.source_turn_id == "rev_1"
                )
            )
        ).scalars().all()
    assert len(same_turn) == 2, "同一个修订 turn_id 只能产生一版方案"

    # ---- 选新卡建新尝试，旧尝试还在 ----
    sel = await api.select_plan(
        cid, api.SelectPlanRequest(plan_id="r2_plan_1", turn_id="t-select-v2")
    )
    assert sel["trial_id"] and sel["trial_id"] != trial_id
    async with session_scope() as session:
        old_trial = await session.get(Trial, trial_id)
        new_trial = await session.get(Trial, sel["trial_id"])
    assert old_trial is not None, "旧尝试不能因为修订被删掉"
    assert new_trial is not None
    assert new_trial.plan_version == 2


@pytest.mark.asyncio
async def test_second_revision_chains_from_latest_version(
    no_retrieval, graph, fake_llm
):
    """v1→v2→v3 要形成一条链，不能每次都回到 v1 互相覆盖。"""
    cid, trial_id = await _seed_trial()

    first = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(revision_note="只能晚上做", turn_id="rev_1"),
    )
    assert first["plan_version"] == 2

    second = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(revision_note="晚上也做不了，只能周末", turn_id="rev_2"),
    )
    assert second["plan_version"] == 3, "第二次修订应该基于最新一版继续分叉"
    current = [p for p in second["plans"] if p.get("is_current_version")]
    assert {p["plan_id"] for p in current} == {"r3_plan_1", "r3_plan_2"}
    archived_versions = {
        p.get("plan_version") for p in second["plans"] if not p.get("is_current_version")
    }
    assert archived_versions == {1, 2}, "前两版都要保留为已归档"

    # 第三次修订用的仍是同一批依据
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Plan).where(Plan.conversation_id == cid)
            )
        ).scalars().all()
    bundle_ids = {str(r.evidence_bundle_id) for r in rows}
    assert len(bundle_ids) == 1, (
        f"各版本不该换证据包：{[str(r.evidence_bundle_id) for r in rows]}"
    )


@pytest.mark.asyncio
async def test_revision_new_risk_signal_skips_cards(no_retrieval, graph, fake_llm):
    cid, trial_id = await _seed_trial()
    calls_before = len(fake_llm["calls"])

    res = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(
            revision_note="孩子最近开始打人，还会推倒小朋友",
            turn_id="rev_risk",
        ),
    )

    assert res["status"] == "needs_professional_support", res
    assert res["plans"] == [], "风险分支不能出卡"
    assert res["evidence_decision"]["high_risk"] is True
    assert "build_limited_answer" in fake_llm["calls"][calls_before:]
    assert "build_recommendation" not in fake_llm["calls"][calls_before:]

    # 风险分支也不落新方案
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Plan).where(
                    Plan.conversation_id == cid, Plan.source_turn_id == "rev_risk"
                )
            )
        ).scalars().all()
    assert rows == []

    # 重试同一个 turn_id 同样不再跑模型（交互记录充当幂等标记）
    calls_after = len(fake_llm["calls"])
    again = await rev.revise_plan(
        trial_id,
        rev.ReviseRequest(
            revision_note="孩子最近开始打人，还会推倒小朋友",
            turn_id="rev_risk",
        ),
    )
    assert again["status"] == "needs_professional_support"
    assert len(fake_llm["calls"]) == calls_after
