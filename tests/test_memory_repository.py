"""长期记忆的写库行为（真连 Postgres，没有库就跳过）。

**测的是计划 §10.7 的验收**，不是实现细节：

- 同一内容写两次只有一条（重试/恢复不重复）
- 纠正之后旧的那条不再进 prompt
- 删除之后列表和上下文都没有它
- 偏好和推断照写，但**不参与合并**
- 跨家庭读不到别人的记忆
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Child, MemoryEvent, MemoryItem
from rdi_agent.db.repositories.case import Repository
from rdi_agent.db.repositories.memory import list_memories
from rdi_agent.memory import service
from rdi_agent.memory.ids import note_content_hash
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


def _note(
    content: str,
    *,
    quote: str,
    category: str = "other",
    decision: str = "auto_commit",
    replaces: str = "",
    is_preference: bool = False,
    inference: bool = False,
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        category=category,
        is_preference=is_preference,
        inference=inference,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        replaces_memory_id=replaces,
        content_hash=note_content_hash(
            content, is_preference=is_preference, inference=inference
        ),
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
        for model in (MemoryEvent, MemoryItem):
            await session.execute(
                model.__table__.delete().where(model.child_id == uid)
            )
        await session.execute(Child.__table__.delete().where(Child.id == uid))
    await dispose_engine()


async def _rows(child_id: str) -> list[MemoryItem]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.child_id == uuid.UUID(child_id)
                    )
                )
            ).scalars().all()
        )


async def _context(child_id: str) -> dict:
    """进 prompt 的那份记忆上下文。"""
    ctx = await service.load_memory_context(child_id=child_id, query="")
    return ctx["memory_context"]


class TestNotes:
    async def test_same_note_twice_is_stored_once(self, child):
        cid = child["child_id"]
        text_ = "孩子今年五岁了"
        await _commit(cid, text_, [_note("孩子 5 岁", quote="孩子今年五岁了")])
        await _commit(
            cid,
            text_,
            [_note("孩子 5 岁", quote="孩子今年五岁了")],
            turn_id="t2",
        )
        rows = await _rows(cid)
        assert len(rows) == 1

    async def test_correction_supersedes_the_old_value(self, child):
        cid = child["child_id"]
        await _commit(cid, "孩子今年四岁了", [_note("孩子 4 岁", quote="孩子今年四岁了")])
        rows = await _rows(cid)
        old_id = f"mi_{rows[0].id.hex}"

        result = await _commit(
            cid,
            "不是四岁，是五岁",
            [_note("孩子 5 岁", quote="不是四岁，是五岁", replaces=old_id)],
            turn_id="t2",
        )
        assert result.superseded_facts == ["孩子 4 岁"]

        rows = await _rows(cid)
        statuses = {r.content: r.status for r in rows}
        assert statuses["孩子 4 岁"] == "superseded"
        assert statuses["孩子 5 岁"] == "active"

    async def test_delete_hides_memory_everywhere(self, child):
        cid = child["child_id"]
        await _commit(cid, "孩子今年五岁了", [_note("孩子 5 岁", quote="孩子今年五岁了")])
        rows = await _rows(cid)
        memory_id = f"mi_{rows[0].id.hex}"

        await service.delete_memory(
            child_id=cid, family_id=child["family_id"], memory_id=memory_id
        )
        ctx = await _context(cid)
        assert not ctx["summaries"]
        assert not ctx["preferences"]
        assert not ctx["inferences"]

        async with session_scope() as session:
            listed = await list_memories(
                session, child_id=cid, family_id=child["family_id"]
            )
        assert listed["memories"] == []

    async def test_events_are_not_duplicated_on_retry(self, child):
        cid = child["child_id"]
        candidate = _note("孩子 5 岁", quote="孩子今年五岁了")
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

    async def test_preference_is_stored_but_not_merged(self, child):
        """偏好是硬约束：照写，但**不进类目小结**。"""
        cid = child["child_id"]
        await _commit(
            cid,
            "我不希望他被批评",
            [_note("我不希望他被批评", quote="我不希望他被批评", is_preference=True)],
        )
        ctx = await _context(cid)
        assert ctx["preferences"] == ["我不希望他被批评"]
        assert not ctx["summaries"]

    async def test_inference_is_stored_but_not_merged(self, child):
        """推断照写、标出来，但**不进类目小结** —— 合并会把猜测变成事实。"""
        cid = child["child_id"]
        await _commit(
            cid,
            "他最近不太想去幼儿园",
            [_note("可能是分离焦虑", quote="", inference=True)],
        )
        ctx = await _context(cid)
        assert [x["content"] for x in ctx["inferences"]] == ["可能是分离焦虑"]
        assert not ctx["summaries"]


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
                            content="孩子 5 岁",
                            category="other",
                            is_preference=False,
                            inference=False,
                            source_quote="孩子今年五岁了",
                            confidence=0.9,
                            proposed_decision="auto_commit",
                            reason="家长明确说了",
                            replaces_memory_id="",
                            scope_conditions=[],
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
            content="孩子 5 岁",
            category="other",
            is_preference=False,
            inference=False,
            source_quote="孩子今年五岁了",
            confidence=0.9,
            proposed_decision="auto_commit",
            reason="家长明确说了",
            replaces_memory_id="",
            scope_conditions=[],
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
        rows = await _rows(cid)
        assert [r.content for r in rows] == ["孩子 5 岁"]
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
        await _commit(cid, "孩子今年五岁了", [_note("孩子 5 岁", quote="孩子今年五岁了")])
        rows = await _rows(cid)
        assert (
            await service.delete_memory(
                child_id=cid,
                family_id=str(uuid.uuid4()),
                memory_id=f"mi_{rows[0].id.hex}",
            )
            is None
        )
