"""长期记忆的写库行为（真连 Postgres，没有库就跳过）。

**测的是计划 §10.7 的验收**，不是实现细节：

- 同一内容写两次只有一条（重试/恢复不重复）
- 纠正之后 ``child_context`` 只出新值（旧值不再进 prompt）
- 删除之后列表和上下文都没有它
- 反例让假设 ``weakened`` 且置信度降一档
- 跨家庭读不到别人的记忆
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Child, Hypothesis, MemoryEvent, Observation
from rdi_agent.db.repositories.case import Repository
from rdi_agent.db.repositories.memory import list_memories
from rdi_agent.memory import service
from rdi_agent.memory.ids import content_hash
from rdi_agent.schemas.memory import MemoryCandidate


def _needs_db() -> bool:
    async def probe() -> bool:
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    return asyncio.run(probe())


pytestmark = pytest.mark.skipif(not _needs_db(), reason="本地没有 Postgres")


def _fact(content: str, *, quote: str, decision: str = "auto_commit", replaces: str = "") -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="fact",
        content=content,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        replaces_memory_id=replaces,
        content_hash=content_hash("fact", content),
    )


def _episode(
    content: str, *, quote: str, contradiction_ids: list[str] | None = None
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="episode",
        content=content,
        source_quote=quote,
        observable_behavior="抱着腿不说话",
        setting="家门口",
        contradicts_memory_ids=list(contradiction_ids or []),
        proposed_decision="auto_commit",
        content_hash=content_hash("episode", content),
    )


def _hypothesis(content: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="hypothesis",
        content=content,
        source_quote="",
        inference=True,
        proposed_decision="needs_confirmation",
        scope_conditions=["早上出门时"],
        content_hash=content_hash("hypothesis", content),
    )


async def _commit(
    child_id: str,
    text_: str,
    candidates: list[MemoryCandidate],
    *,
    turn_id: str = "t1",
    conversation_id: str = "conv_mem_test",
):
    state = {
        "child_id": child_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "user_message": text_,
        "memory_candidates": [c.model_dump(mode="json") for c in candidates],
    }
    merged = await service.merge_for_state(state)
    state["memory_update_result"] = merged.model_dump(mode="json")
    return await service.commit_for_state(state)


@pytest.fixture
async def child():
    """一个测试孩子。用完连同记忆一起删干净。"""
    family_id = uuid.uuid4()
    async with session_scope() as session:
        row = await Repository(session).ensure_child(
            family_id=str(family_id), display_label="记忆测试孩子"
        )
        child_id = str(row.id)
    yield {"child_id": child_id, "family_id": str(family_id)}
    async with session_scope() as session:
        uid = uuid.UUID(child_id)
        for model in (MemoryEvent, Observation, Hypothesis):
            await session.execute(
                model.__table__.delete().where(model.child_id == uid)
            )
        await session.execute(Child.__table__.delete().where(Child.id == uid))
    await dispose_engine()


async def _context(child_id: str) -> dict:
    async with session_scope() as session:
        return await Repository(session).child_context(child_id)


class TestFacts:
    async def test_same_fact_twice_is_stored_once(self, child):
        cid = child["child_id"]
        text_ = "孩子今年五岁了"
        await _commit(cid, text_, [_fact("孩子 5 岁", quote="孩子今年五岁了")])
        await _commit(
            cid,
            text_,
            [_fact("孩子 5 岁", quote="孩子今年五岁了")],
            turn_id="t2",
        )
        ctx = await _context(cid)
        assert ctx["confirmed_facts"] == ["孩子 5 岁"]

    async def test_correction_replaces_the_value_in_context(self, child):
        cid = child["child_id"]
        await _commit(cid, "孩子今年四岁了", [_fact("孩子 4 岁", quote="孩子今年四岁了")])

        async with session_scope() as session:
            from rdi_agent.db.repositories.memory import MemoryRepository

            items = await MemoryRepository(session).load_fact_items(cid)
        old_id = items[0].id

        result = await _commit(
            cid,
            "不是四岁，是五岁",
            [_fact("孩子 5 岁", quote="不是四岁，是五岁", replaces=old_id)],
            turn_id="t2",
        )
        assert result.superseded_facts == ["孩子 4 岁"]

        ctx = await _context(cid)
        assert ctx["confirmed_facts"] == ["孩子 5 岁"]

    async def test_delete_hides_memory_everywhere(self, child):
        cid = child["child_id"]
        await _commit(cid, "孩子今年五岁了", [_fact("孩子 5 岁", quote="孩子今年五岁了")])
        async with session_scope() as session:
            from rdi_agent.db.repositories.memory import MemoryRepository

            items = await MemoryRepository(session).load_fact_items(cid)

        await service.delete_memory(
            child_id=cid, family_id=child["family_id"], memory_id=items[0].id
        )
        ctx = await _context(cid)
        assert ctx["confirmed_facts"] == []

        async with session_scope() as session:
            listed = await list_memories(
                session, child_id=cid, family_id=child["family_id"]
            )
        assert listed["memories"] == []

    async def test_events_are_not_duplicated_on_retry(self, child):
        cid = child["child_id"]
        candidate = _fact("孩子 5 岁", quote="孩子今年五岁了")
        await _commit(cid, "孩子今年五岁了", [candidate], turn_id="same")
        await _commit(cid, "孩子今年五岁了", [candidate], turn_id="same")

        async with session_scope() as session:
            count = (
                await session.execute(
                    select(MemoryEvent).where(
                        MemoryEvent.child_id == uuid.UUID(cid),
                        MemoryEvent.source_turn_id == "same",
                    )
                )
            ).scalars().all()
        assert len(count) == 1


class TestObservations:
    async def test_duplicate_observation_is_stored_once(self, child):
        cid = child["child_id"]
        candidate = _episode("早上出门抱着腿不说话", quote="早上出门一直抱着我的腿")
        await _commit(cid, "早上出门一直抱着我的腿", [candidate])
        await _commit(
            cid, "早上出门一直抱着我的腿", [candidate], turn_id="t2"
        )
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Observation).where(
                        Observation.child_id == uuid.UUID(cid)
                    )
                )
            ).scalars().all()
        assert len(rows) == 1

    async def test_counter_example_weakens_hypothesis(self, child):
        """验收第 7 条：反例要真的削弱假设，不能只是记一笔。"""
        cid = child["child_id"]
        await _commit(
            cid,
            "每天早上出门一直抱着我的腿",
            [_hypothesis("可能在和妈妈分开时会紧张")],
        )
        async with session_scope() as session:
            hyp = (
                await session.execute(
                    select(Hypothesis).where(
                        Hypothesis.child_id == uuid.UUID(cid)
                    )
                )
            ).scalars().one()
        hyp_id = f"hyp_{hyp.id.hex}"

        await _commit(
            cid,
            "今天出门他主动跟我挥手了",
            [_episode("今天出门主动挥手告别", quote="今天出门他主动跟我挥手了", contradiction_ids=[hyp_id])],
            turn_id="t2",
        )

        async with session_scope() as session:
            updated = await session.get(Hypothesis, hyp.id)
        assert updated is not None
        assert updated.status == "weakened"
        assert updated.confidence_level == "low"
        # 反例列表里存的是**观察**的 id（哪一次观察推翻了这个假设）
        ids = updated.contradicting_observation_ids_json or []
        assert len(ids) == 1 and ids[0].startswith("obs_")


class TestExtraction:
    async def test_draft_is_enriched_with_provenance(self, child, monkeypatch):
        """模型只负责给内容，溯源/哈希由代码补 —— 它不知道自己在哪一轮。"""
        from rdi_agent import llm
        from rdi_agent.schemas.memory import (
            MemoryCandidateDraft,
            MemoryExtractionResult,
        )

        async def fake_chat_json(**kwargs):
            return (
                MemoryExtractionResult(
                    candidates=[
                        MemoryCandidateDraft(
                            memory_type="fact",
                            content="孩子 5 岁",
                            source_quote="孩子今年五岁了",
                            confidence=0.9,
                            inference=False,
                            proposed_decision="auto_commit",
                            reason="家长明确说了",
                            replaces_memory_id="",
                            contradicts_memory_ids=[],
                            scope_conditions=[],
                            setting="",
                            antecedent="",
                            observable_behavior="",
                            adult_response="",
                            child_response="",
                            outcome="",
                            is_attempt=False,
                        )
                    ],
                    notes=[],
                ),
                llm.LLMTrace(stage="memory_extract", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        candidates, _notes = await service.extract_candidates(
            child_id=child["child_id"],
            conversation_id="conv_x",
            turn_id="t9",
            source_kind="interaction",
            parent_text="孩子今年五岁了",
        )
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.source_turn_id == "t9"
        assert candidate.source_kind == "interaction"
        assert candidate.content_hash
        assert candidate.memory_id == ""


class TestGraphIntegration:
    async def test_full_turn_writes_memory(self, child, monkeypatch):
        """从图入口跑一轮，记忆要真的落库。

        **和上面的用例不重复**：那些直接调 service，测的是写库；
        这个走完整张图（load_context → 抽取 → 合并 → 提交 → 解析 → 判断），
        测的是**接线**。节点之间传的 key 写错了，只有这里能发现。
        """
        from langgraph.checkpoint.memory import InMemorySaver

        from rdi_agent import llm
        from rdi_agent.graph.builder import build_graph
        from rdi_agent.schemas.case import CaseSummary
        from rdi_agent.schemas.conversation import (
            FollowupQuestion,
            FollowupQuestions,
            SufficiencyDecision,
        )
        from rdi_agent.schemas.memory import (
            MemoryCandidateDraft,
            MemoryExtractionResult,
        )

        draft = MemoryCandidateDraft(
            memory_type="fact",
            content="孩子 5 岁",
            source_quote="孩子今年五岁了",
            confidence=0.9,
            inference=False,
            proposed_decision="auto_commit",
            reason="家长明确说了",
            replaces_memory_id="",
            contradicts_memory_ids=[],
            scope_conditions=[],
            setting="",
            antecedent="",
            observable_behavior="",
            adult_response="",
            child_response="",
            outcome="",
            is_attempt=False,
        )

        async def fake_chat_json(**kwargs):
            model_cls = kwargs["model_cls"]
            if model_cls is MemoryExtractionResult:
                payload = MemoryExtractionResult(candidates=[draft], notes=[])
            elif model_cls is CaseSummary:
                payload = CaseSummary(observed_facts=["孩子今年五岁了"])
            elif model_cls is SufficiencyDecision:
                payload = SufficiencyDecision(
                    requires_followup=True,
                    reasons=["还缺场景"],
                    priority_questions=["早上出门时具体会怎样？"],
                )
            elif model_cls is FollowupQuestions:
                payload = FollowupQuestions(
                    questions=[FollowupQuestion(question="早上出门时具体会怎样？")]
                )
            else:
                raise AssertionError(f"没预置答案：{kwargs.get('stage')}")
            return payload, llm.LLMTrace(
                stage=kwargs.get("stage", ""), model="fake", source="fake"
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)

        graph = build_graph(checkpointer=InMemorySaver())
        cid = child["child_id"]
        result = await graph.ainvoke(
            {
                "entrypoint": "chat",
                "conversation_id": "conv_graph_test",
                "thread_id": "conv_graph_test",
                "family_id": child["family_id"],
                "child_id": cid,
                "turn_id": "t1",
                "user_message": "孩子今年五岁了",
            },
            config={"configurable": {"thread_id": "conv_graph_test"}},
        )

        # 停在追问中断上 —— 这一轮的记忆应该已经写完了
        assert result.get("__interrupt__")
        ctx = await _context(cid)
        assert "孩子 5 岁" in ctx["confirmed_facts"]
        assert result["memory_update_result"]["created"]


class TestIsolation:
    async def test_other_family_cannot_list(self, child):
        async with session_scope() as session:
            result = await list_memories(
                session,
                child_id=child["child_id"],
                family_id=str(uuid.uuid4()),
            )
        assert result == {}

    async def test_other_family_cannot_delete(self, child):
        cid = child["child_id"]
        await _commit(cid, "孩子今年五岁了", [_fact("孩子 5 岁", quote="孩子今年五岁了")])
        async with session_scope() as session:
            from rdi_agent.db.repositories.memory import MemoryRepository

            items = await MemoryRepository(session).load_fact_items(cid)
        assert (
            await service.delete_memory(
                child_id=cid,
                family_id=str(uuid.uuid4()),
                memory_id=items[0].id,
            )
            is None
        )
