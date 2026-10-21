"""Repository 层的幂等性测试（计划 §8.5 最后一条：重试不会重复创建）。

这些用例真连 Postgres，不可用时整组跳过 —— 在没有数据库的机器上
跑测试套件不该变红，但跑起来就该真的验一遍。

幂等不是靠"先查后写"，是靠唯一约束在数据库层挡住。
所以这里的做法是：**同一个键写两次，断言第二次没有产生新记录**。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from rdi_agent.db.engine import dispose_engine, session_scope
from rdi_agent.db.models import Child, Feedback, Interaction, Plan, Trial
from rdi_agent.db.repositories.case import Repository
from rdi_agent.schemas.feedback import ChildResponse, FeedbackSubmission
from rdi_agent.schemas.recommendation import ParentAction, RecommendationCard


def _needs_db() -> bool:
    import asyncio

    from sqlalchemy import text

    from rdi_agent.db.engine import get_engine

    async def probe() -> bool:
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    return asyncio.run(probe())


pytestmark = pytest.mark.skipif(not _needs_db(), reason="本地没有 Postgres")


def _card(plan_id: str = "plan_1", recommended: bool = True) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["早上出门会哭"],
        explanation="缩短难受时长。",
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下"],
        application_label="ai_adaptation",
        recommended=recommended,
    )


@pytest.fixture
async def cleanup():
    """每组用例前后删干净，避免上一轮失败留下的数据干扰下一轮。

    ``repo`` fixture 会往这个 dict 里塞 conversation_id 供这里清理用。
    """
    tags: dict = {}
    yield tags
    if tags:
        async with session_scope() as session:
            conv = tags["conversation_id"]
            trials = (
                await session.execute(
                    select(Trial).where(Trial.conversation_id == conv)
                )
            ).scalars().all()
            for t in trials:
                await session.execute(
                    Feedback.__table__.delete().where(Feedback.trial_id == t.id)
                )
            await session.execute(Trial.__table__.delete().where(Trial.conversation_id == conv))
            await session.execute(Plan.__table__.delete().where(Plan.conversation_id == conv))
            await session.execute(
                Interaction.__table__.delete().where(Interaction.conversation_id == conv)
            )
        # children 每次 UUID 都不同，留着也只是噪音；统一删掉 label 前缀匹配的
        await session.execute(
            Child.__table__.delete().where(
                Child.display_label.in_(["测试孩子", "换选孩子", "反馈孩子"])
            )
        )
    await dispose_engine()


@pytest.fixture
async def repo(cleanup):
    """给一组用例共用的 conversation_id。不持有 session ——
    每个用例自己开 session_scope，避免用例之间共用连接。"""
    yield {"conversation_id": f"test_conv_{uuid.uuid4().hex[:10]}"}


@pytest.mark.asyncio
async def test_duplicate_interaction_is_ignored(repo):
    """同一个 (conversation_id, turn_id, role) 写两次只留一条。"""
    tags = repo
    cid = tags["conversation_id"]
    family = str(uuid.uuid4())

    async with session_scope() as s:
        repo2 = Repository(s)
        first = await repo2.record_interaction(
            conversation_id=cid, thread_id=cid, turn_id="t1",
            role="user", content="孩子 4 岁", family_id=family,
        )
        second = await repo2.record_interaction(
            conversation_id=cid, thread_id=cid, turn_id="t1",
            role="user", content="孩子 4 岁", family_id=family,
        )

    assert first is True, "第一次应该写进去"
    assert second is False, "重复的应该被唯一约束挡掉"

    async with session_scope() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(Interaction)
                .where(Interaction.conversation_id == cid)
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_selecting_same_plan_twice_creates_one_trial(repo):
    """重发 select-plan 不会产生第二条尝试。"""
    tags = repo
    cid = tags["conversation_id"]
    family = str(uuid.uuid4())

    async with session_scope() as s:
        repo2 = Repository(s)
        child = await repo2.ensure_child(family_id=family, display_label="测试孩子")
        child_id = str(child.id)
        await repo2.save_candidate_plans(
            conversation_id=cid, family_id=family, child_id=child_id,
            cards=[_card("plan_1", True), _card("plan_2", False)],
        )
        _p1, t1 = await repo2.select_plan(
            conversation_id=cid, plan_id="plan_1", turn_id="t1",
            child_id=child_id, target="方案 plan_1",
        )
        _p2, t2 = await repo2.select_plan(
            conversation_id=cid, plan_id="plan_1", turn_id="t2",
            child_id=child_id, target="方案 plan_1",
        )
        assert t1 is not None and t2 is not None
        assert str(t1.id) == str(t2.id), "第二次应该拿到同一个 trial"

    async with session_scope() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(Trial).where(Trial.conversation_id == cid)
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_switching_plan_archives_the_previous_one(repo):
    """换了选择，上一张标 archived，不能两边都是 selected。"""
    tags = repo
    cid = tags["conversation_id"]
    family = str(uuid.uuid4())

    async with session_scope() as s:
        repo2 = Repository(s)
        child = await repo2.ensure_child(family_id=family, display_label="换选孩子")
        child_id = str(child.id)
        await repo2.save_candidate_plans(
            conversation_id=cid, family_id=family, child_id=child_id,
            cards=[_card("plan_1", True), _card("plan_2", False)],
        )
        await repo2.select_plan(
            conversation_id=cid, plan_id="plan_1", turn_id="t1",
            child_id=child_id, target="方案 1",
        )
        await repo2.select_plan(
            conversation_id=cid, plan_id="plan_2", turn_id="t2",
            child_id=child_id, target="方案 2",
        )
        rows = (
            await s.execute(select(Plan).where(Plan.conversation_id == cid))
        ).scalars().all()
        statuses = {(p.plan_json or {}).get("plan_id"): p.status for p in rows}

    assert statuses["plan_1"] == "archived", "换选后上一张不该还是 selected"
    assert statuses["plan_2"] == "selected"


@pytest.mark.asyncio
async def test_duplicate_feedback_is_ignored(repo):
    """同一个 (trial_id, turn_id) 提交两次只留一条。"""
    tags = repo
    cid = tags["conversation_id"]
    family = str(uuid.uuid4())

    async with session_scope() as s:
        repo2 = Repository(s)
        child = await repo2.ensure_child(family_id=family, display_label="反馈孩子")
        child_id = str(child.id)
        await repo2.save_candidate_plans(
            conversation_id=cid, family_id=family, child_id=child_id, cards=[_card()]
        )
        _plan, trial = await repo2.select_plan(
            conversation_id=cid, plan_id="plan_1", turn_id="t1",
            child_id=child_id, target="方案 1",
        )
        trial_id = str(trial.id)
        submission = FeedbackSubmission(
            trial_id=trial_id, turn_id="fb1", attempted=True,
            implementation_notes="试了三天",
            child_response=ChildResponse(behavior="第三天自己走进去了"),
            parent_observations=["哭的时间变短"],
        )
        _row1, created1 = await repo2.save_feedback(trial_id=trial_id, submission=submission)
        _row2, created2 = await repo2.save_feedback(trial_id=trial_id, submission=submission)

    assert created1 is True
    assert created2 is False, "重复提交不该新建"

    async with session_scope() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(Feedback).where(Feedback.trial_id == trial_id)
            )
        ).scalar_one()
    assert n == 1