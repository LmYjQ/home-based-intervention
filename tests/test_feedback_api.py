"""反馈入口的 API 集成测试（阶段 4）。

覆盖四件事：

1. 提交反馈后 trial 的执行状态落库；
2. 重试同一个 ``(trial_id, turn_id)`` 不再跑 LLM（读缓存）；
3. 反馈不足时停在 interrupt，resume 后继续到收尾；
4. 整个流程只产生一条 feedback。

要跑它：先起 Postgres；不可用时跳过而不是失败。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from rdi_agent import llm
from rdi_agent.api import conversations as api
from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Feedback, Plan, Trial
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.runtime import setup_graph, shutdown_graph
from rdi_agent.progress import emit as progress_emit
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
def feedback_llm(monkeypatch):
    """可变的假模型：``state["decision"]`` 控制 next_step 走哪条路。"""
    state = {"decision": "continue", "calls": []}

    async def fake_chat_json(**kwargs):
        model_cls = kwargs["model_cls"]
        stage = kwargs.get("stage", "")
        state["calls"].append(stage)
        # 替身也要打进度点 —— 真的 ``chat_json`` 在每次调用开头
        # 会 emit 一次。不打的话，"流式端点推了进度"这条就测不到。
        progress_emit(stage)
        if model_cls is FeedbackAnalysis:
            payload = FeedbackAnalysis(
                actual_execution="提前十分钟出门，但在门口还是抱了五分钟",
                observed_changes=["哭的时间短了一点"],
                execution_difficulties=["老人送的时候做不到"],
                execution_barriers=["执行人不同，流程不一致"],
                relation_to_plan="这一次比上次短，但还是一次观察",
                confidence_note="单次观察，不能判断方案有效",
            )
        elif model_cls is NextStep:
            payload = NextStep(
                decision=state["decision"],
                decision_reason="按当前信息决定下一步",
                acknowledgment="谢谢你试了，也把卡住的地方记下来了。",
                next_steps=["保留提前十分钟", "把门口告别缩短到两分钟"],
                observe_this_week=["哭的持续时间", "谁送园时差异最大"],
            )
        elif model_cls is FollowupQuestions:
            payload = FollowupQuestions(
                questions=[
                    FollowupQuestion(
                        question="那天从出门到进园，哭的时间大概多久？",
                        options=["不到五分钟", "五到十分钟", "十分钟以上"],
                        why_asking="需要知道变化幅度",
                    )
                ]
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
        title="先把出门这件事变短",
        observed_facts=["4 岁，早上出门哭"],
        explanation_parts=[ExplanationPart(text="先把难受的时长缩短。")],
        parent_actions=[
            ParentAction(action="提前十分钟出门", context="早上", steps=["和老师约好"])
        ],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=True,
    )


async def _seed_trial() -> tuple[str, str, str]:
    """建会话 + 方案卡 + trial，返回 (conversation_id, trial_id, turn_id)。"""
    conv = await api.create_conversation(
        api.CreateConversationRequest(
            display_label="反馈测试孩子", birth_date="2021-06"
        )
    )
    conversation_id = conv["conversation_id"]
    async with session_scope() as session:
        repo = Repository(session)
        await repo.save_candidate_plans(
            conversation_id=conversation_id,
            family_id=conv["family_id"],
            child_id=conv["child_id"],
            cards=[_card()],
            evidence_bundle_id=None,
        )
        _, trial = await repo.select_plan(
            conversation_id=conversation_id,
            plan_id="plan_1",
            turn_id="select_1",
            child_id=conv["child_id"],
            target="先把出门这件事变短",
        )
    assert trial is not None
    return conversation_id, str(trial.id), "fb_1"


def _submission(trial_id: str, turn_id: str) -> FeedbackSubmission:
    return FeedbackSubmission(
        trial_id=trial_id,
        turn_id=turn_id,
        attempted=True,
        implementation_notes="提前十分钟出门，但在门口还是抱了五分钟",
        parent_observations=["哭的时间短了一点"],
        difficulties=["老人送的时候做不到"],
    )


async def _collect(resp) -> list[dict]:
    """把 StreamingResponse 的内容读成事件列表。"""
    events: list[dict] = []
    async for chunk in resp.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for frame in text.split("\n\n"):
            if not frame.strip():
                continue
            name = None
            data_lines: list[str] = []
            for line in frame.split("\n"):
                if line.startswith("event:"):
                    name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
            if name is None or not data_lines:
                continue
            events.append({"event": name, "data": json.loads("\n".join(data_lines))})
    return events


@pytest.mark.asyncio
async def test_feedback_stream_emits_progress_then_result(
    db, graph, feedback_llm
):
    """反馈的流式端点：先推进度，最后推结果。

    **为什么要这条流**：反馈链上有 3 次 LLM 调用，每次超时 120 秒、
    重试 3 次 —— 最坏 6 分钟。非流式的时候家长面对的是一个不动的
    按钮，只能猜"是卡了还是在算"。
    """
    conversation_id, trial_id, turn_id = await _seed_trial()
    try:
        resp = await api.submit_feedback_stream(
            trial_id, _submission(trial_id, turn_id)
        )
        events = await _collect(resp)

        names = [e["event"] for e in events]
        assert names[-1] == "result", f"最后一个事件应该是 result，实际 {names}"
        assert "error" not in names, f"不该有 error 事件：{events}"

        # 进度要有 —— 至少得有"正在分析你的反馈"这一步
        stages = [e["data"]["stage"] for e in events if e["event"] == "progress"]
        assert "feedback_analyze" in stages, f"没有分析阶段的进度：{stages}"
        # 进度标签要给家长看的中文，不是英文 stage 名
        labels = [
            e["data"]["label"] for e in events if e["event"] == "progress"
        ]
        assert all(" " not in lbl or not lbl.isascii() for lbl in labels)

        result = events[-1]["data"]
        assert result["status"]
        assert result["analysis"]
    finally:
        await _cleanup(conversation_id, trial_id)


@pytest.mark.asyncio
async def test_feedback_stream_result_matches_non_streaming(
    db, graph, feedback_llm
):
    """流式和非流式必须产出**一样的结果**。

    **为什么这是最要紧的一条**：手测台的价值就在于"看到的就是真的"。
    两条路跑不同逻辑的话，手测看到的东西就不能代表线上。
    """
    cid_a, trial_a, turn_a = await _seed_trial()
    cid_b, trial_b, turn_b = await _seed_trial()
    try:
        streamed = await _collect(
            await api.submit_feedback_stream(
                trial_a, _submission(trial_a, turn_a)
            )
        )
        plain = await api.submit_feedback(
            trial_b, _submission(trial_b, turn_b)
        )

        stream_result = streamed[-1]["data"]
        for payload in (stream_result, plain):
            payload.pop("turn_id", None)
            payload.pop("trial_id", None)

        assert stream_result == plain, (
            "流式和非流式的结果不一致 —— 两条路跑的不是同一套逻辑"
        )
    finally:
        await _cleanup(cid_a, trial_a)
        await _cleanup(cid_b, trial_b)


async def _cleanup(conversation_id: str, trial_id: str) -> None:
    async with session_scope() as session:
        feedbacks = (
            await session.execute(select(Feedback).where(Feedback.trial_id == trial_id))
        ).scalars().all()
        for row in feedbacks:
            await session.delete(row)
        trial = await session.get(Trial, trial_id)
        if trial is not None:
            await session.delete(trial)
        plans = (
            await session.execute(
                select(Plan).where(Plan.conversation_id == conversation_id)
            )
        ).scalars().all()
        for row in plans:
            await session.delete(row)


@pytest.mark.asyncio
async def test_feedback_updates_trial_and_caches_on_retry(
    graph, feedback_llm
):
    cid, trial_id, turn_id = await _seed_trial()
    try:
        result = await api.submit_feedback(trial_id, _submission(trial_id, turn_id))
        assert result["status"] == "completed"
        assert result["trial_status"] == "attempted"
        assert result["analysis"]["actual_execution"]
        assert result["next_step"]["decision"] == "continue"

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Feedback).where(
                        Feedback.trial_id == trial_id,
                        Feedback.turn_id == turn_id,
                    )
                )
            ).scalars().one()
            assert row.completed_at is not None
            trial = await session.get(Trial, trial_id)
            assert trial.status == "attempted"
            assert trial.ended_at is not None
            assert trial.plan_version == 1

        calls_before = len(feedback_llm["calls"])

        # 同一个 (trial_id, turn_id) 重试：命中 completed_at 缓存，不再调 LLM
        retry = await api.submit_feedback(trial_id, _submission(trial_id, turn_id))
        assert retry["status"] == "completed"
        assert len(feedback_llm["calls"]) == calls_before

        async with session_scope() as session:
            count = len(
                (
                    await session.execute(
                        select(Feedback).where(Feedback.trial_id == trial_id)
                    )
                ).scalars().all()
            )
        assert count == 1
    finally:
        await _cleanup(cid, trial_id)


@pytest.mark.asyncio
async def test_feedback_followup_interrupt_and_resume(graph, feedback_llm):
    cid, trial_id, turn_id = await _seed_trial()
    feedback_llm["decision"] = "followup"
    try:
        first = await api.submit_feedback(trial_id, _submission(trial_id, turn_id))
        assert first["status"] == "awaiting_feedback_followup"
        assert first["questions"], "反馈不足时应该生成追问"
        assert first["trial_status"] == "attempted"

        # 家长答完 → 继续到收尾
        feedback_llm["decision"] = "continue"
        second = await api.resume_feedback(
            trial_id,
            api.FeedbackFollowupRequest(
                turn_id=turn_id,
                answers=[
                    api.FollowupAnswer(
                        question=first["questions"][0]["question"],
                        answer="不到五分钟",
                    )
                ],
            ),
        )
        assert second["status"] == "completed"
        assert second["next_step"]["decision"] == "continue"

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Feedback).where(
                        Feedback.trial_id == trial_id,
                        Feedback.turn_id == turn_id,
                    )
                )
            ).scalars().one()
            assert row.completed_at is not None
            assert row.followup_rounds == 1
            assert row.followup_answers_json
    finally:
        await _cleanup(cid, trial_id)
