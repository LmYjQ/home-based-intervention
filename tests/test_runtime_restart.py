"""重启恢复（阶段 5）：Checkpointer 在进程重启后仍然能接上。

只测一条最有代表性的路径：**反馈追问**。
它同时覆盖：
- ``AsyncPostgresSaver`` 写进独立 schema；
- ``feedback-{trial_id}-{turn_id}`` 线程的确定性；
- ``shutdown_graph`` 真的把 saver 关掉（关不掉的话下面的 setup 会撞连接）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from rdi_agent import llm
from rdi_agent.api import conversations as api
from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Feedback, Plan, Trial
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.runtime import (
    config_for,
    feedback_thread_id,
    get_graph,
    setup_graph,
    shutdown_graph,
)
from rdi_agent.schemas.conversation import (
    FollowupQuestion,
    FollowupQuestions,
    NextStep,
)
from rdi_agent.schemas.feedback import FeedbackAnalysis, FeedbackSubmission
from rdi_agent.schemas.memory import MemoryExtractionResult
from rdi_agent.schemas.recommendation import (
    ExplanationPart,
    ParentAction,
    RecommendationCard,
)


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
    state = {"decision": "followup"}

    async def fake_chat_json(**kwargs):
        model_cls = kwargs["model_cls"]
        stage = kwargs.get("stage", "")
        if model_cls is FeedbackAnalysis:
            payload = FeedbackAnalysis(
                actual_execution="做了，但中途停了",
                observed_changes=["哭的时间短了一点"],
                open_questions=["是谁送园那天的？"],
                confidence_note="单次观察",
            )
        elif model_cls is NextStep:
            payload = NextStep(
                decision=state["decision"],
                acknowledgment="谢谢你试了。",
                next_steps=["继续观察"],
            )
        elif model_cls is FollowupQuestions:
            payload = FollowupQuestions(
                questions=[FollowupQuestion(question="是谁送园那天的？", options=["妈妈", "奶奶"])]
            )
        elif model_cls is MemoryExtractionResult:
            payload = MemoryExtractionResult(candidates=[], notes=[])
        else:
            raise AssertionError(f"没预置答案：{stage}")
        return payload, llm.LLMTrace(stage=stage, source="fake")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return state


def _card() -> RecommendationCard:
    return RecommendationCard(
        plan_id="plan_1",
        title="先把出门变短",
        observed_facts=["早上出门哭"],
        explanation_parts=[ExplanationPart(text="缩短难受时长。")],
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停"],
        application_label="ai_adaptation",
        uncertainty_notes=["原因还不确定"],
        recommended=True,
    )


async def _seed_trial() -> tuple[str, str, str]:
    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label="重启测试孩子", birth_date="2021-06")
    )
    cid = conv["conversation_id"]
    async with session_scope() as session:
        repo = Repository(session)
        await repo.save_candidate_plans(
            conversation_id=cid,
            family_id=conv["family_id"],
            child_id=conv["child_id"],
            cards=[_card()],
            evidence_bundle_id=None,
        )
        _, trial = await repo.select_plan(
            conversation_id=cid,
            plan_id="plan_1",
            turn_id="select_1",
            child_id=conv["child_id"],
            target="先把出门变短",
        )
    return cid, str(trial.id), "fb_restart"


async def _cleanup(cid: str, trial_id: str) -> None:
    async with session_scope() as session:
        for row in (
            await session.execute(select(Feedback).where(Feedback.trial_id == trial_id))
        ).scalars().all():
            await session.delete(row)
        trial = await session.get(Trial, trial_id)
        if trial is not None:
            await session.delete(trial)
        for row in (
            await session.execute(select(Plan).where(Plan.conversation_id == cid))
        ).scalars().all():
            await session.delete(row)


@pytest.mark.asyncio
async def test_feedback_followup_survives_restart(graph, fake_llm):
    cid, trial_id, turn_id = await _seed_trial()
    try:
        first = await api.submit_feedback(
            trial_id,
            FeedbackSubmission(
                trial_id=trial_id, turn_id=turn_id, attempted=True,
                implementation_notes="做了，但中途停了",
            ),
        )
        assert first["status"] == "awaiting_feedback_followup"

        # 模拟进程重启：关掉 saver，重新 setup
        await shutdown_graph()
        await setup_graph()

        # checkpoint 还在，pending interrupt 也还在
        snapshot = await get_graph().aget_state(
            config_for(feedback_thread_id(trial_id, turn_id))
        )
        assert snapshot.tasks, "重启后 checkpoint 应该还在"

        fake_llm["decision"] = "continue"
        resumed = await api.resume_feedback(
            trial_id,
            api.FeedbackFollowupRequest(
                turn_id=turn_id,
                answers=[
                    api.FollowupAnswer(
                        question=first["questions"][0]["question"],
                        answer="妈妈",
                    )
                ],
            ),
        )
        assert resumed["status"] == "completed"

        async with session_scope() as session:
            rows = (
                await session.execute(select(Feedback).where(Feedback.trial_id == trial_id))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].completed_at is not None
    finally:
        await _cleanup(cid, trial_id)
