"""记忆管理接口 + 会话复用孩子（真连 Postgres，没有库就跳过）。

对应计划 §10.7 的"家长可以查看、纠正、删除记忆"，
以及验收第 1 条需要的"同一个孩子换一个会话"。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from rdi_agent.api import conversations as conv_api
from rdi_agent.api import memory as mem_api
from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Child, Hypothesis, MemoryEvent, Observation
from rdi_agent.db.repositories.case import Repository
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


def _fact(content: str, quote: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="fact",
        content=content,
        source_quote=quote,
        proposed_decision="auto_commit",
        content_hash=content_hash("fact", content),
    )


async def _seed(child_id: str, text_: str, candidates) -> None:
    state = {
        "child_id": child_id,
        "conversation_id": "conv_api_test",
        "turn_id": "t1",
        "user_message": text_,
        "memory_candidates": [c.model_dump(mode="json") for c in candidates],
    }
    merged = await service.merge_for_state(state)
    state["memory_update_result"] = merged.model_dump(mode="json")
    await service.commit_for_state(state)


@pytest.fixture
async def child():
    family_id = uuid.uuid4()
    async with session_scope() as session:
        row = await Repository(session).ensure_child(
            family_id=str(family_id), display_label="接口测试孩子"
        )
        child_id = str(row.id)
    yield {"child_id": child_id, "family_id": str(family_id)}
    async with session_scope() as session:
        uid = uuid.UUID(child_id)
        for model in (MemoryEvent, Observation, Hypothesis):
            await session.execute(model.__table__.delete().where(model.child_id == uid))
        await session.execute(Child.__table__.delete().where(Child.id == uid))
    await dispose_engine()


class TestList:
    async def test_list_returns_seeded_fact(self, child):
        await _seed(child["child_id"], "孩子今年五岁了", [_fact("孩子 5 岁", "孩子今年五岁了")])
        result = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        contents = [m["content"] for m in result["memories"]]
        assert "孩子 5 岁" in contents
        assert result["counts"]["fact"] == 1
        assert result["memories"][0]["source"]["quote"] == "孩子今年五岁了"

    async def test_wrong_family_is_not_found(self, child):
        with pytest.raises(HTTPException) as exc:
            await mem_api.list_memories(
                child_id=child["child_id"], family_id=str(uuid.uuid4())
            )
        assert exc.value.status_code == 404


class TestMutate:
    async def test_confirm_pending_fact(self, child):
        await _seed(
            child["child_id"],
            "他说不想上幼儿园",
            [
                MemoryCandidate(
                    memory_type="fact",
                    content="孩子最近不想上幼儿园",
                    source_quote="他说不想上幼儿园",
                    proposed_decision="needs_confirmation",
                    content_hash=content_hash("fact", "孩子最近不想上幼儿园"),
                )
            ],
        )
        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        pending = [m for m in listed["memories"] if m["status"] == "pending_confirmation"]
        assert pending

        result = await mem_api.confirm_memory(
            child_id=child["child_id"],
            memory_id=pending[0]["memory_id"],
            req=mem_api.MemoryActionRequest(family_id=child["family_id"]),
        )
        assert result["memory"]["status"] == "confirmed"

    async def test_correct_then_old_value_is_gone(self, child):
        await _seed(child["child_id"], "孩子今年五岁了", [_fact("孩子 5 岁", "孩子今年五岁了")])
        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        memory_id = listed["memories"][0]["memory_id"]

        result = await mem_api.correct_memory(
            child_id=child["child_id"],
            memory_id=memory_id,
            req=mem_api.CorrectMemoryRequest(
                family_id=child["family_id"], content="孩子 6 岁"
            ),
        )
        assert result["memory"]["content"] == "孩子 6 岁"
        assert result["memory"]["supersedes_memory_id"] == memory_id

        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        assert [m["content"] for m in listed["memories"]] == ["孩子 6 岁"]

        history = await mem_api.list_memories(
            child_id=child["child_id"],
            family_id=child["family_id"],
            include_history=True,
        )
        assert "孩子 5 岁" in [m["content"] for m in history["memories"]]

    async def test_correct_observation_keeps_history(self, child):
        await _seed(
            child["child_id"],
            "早上出门一直抱着我的腿",
            [
                MemoryCandidate(
                    memory_type="episode",
                    content="早上出门抱着腿不说话",
                    source_quote="早上出门一直抱着我的腿",
                    observable_behavior="抱着腿不说话",
                    setting="家门口",
                    proposed_decision="auto_commit",
                    content_hash=content_hash("episode", "早上出门抱着腿不说话"),
                )
            ],
        )
        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        observation = next(m for m in listed["memories"] if m["kind"] == "observation")

        result = await mem_api.correct_memory(
            child_id=child["child_id"],
            memory_id=observation["memory_id"],
            req=mem_api.CorrectMemoryRequest(
                family_id=child["family_id"], content="出门时站在门口不动"
            ),
        )
        # 展示出来的 content 是结构化字段拼的（还带着原来的场景 "家门口"），
        # 所以这里只断言纠正后的行为文本在里面。
        assert "出门时站在门口不动" in result["memory"]["content"]
        assert result["memory"]["supersedes_memory_id"] == observation["memory_id"]

        history = await mem_api.list_memories(
            child_id=child["child_id"],
            family_id=child["family_id"],
            include_history=True,
        )
        superseded = [
            m for m in history["memories"] if m["status"] == "superseded"
        ]
        assert superseded and superseded[0]["superseded_by_memory_id"]

    async def test_delete_is_idempotent(self, child):
        await _seed(child["child_id"], "孩子今年五岁了", [_fact("孩子 5 岁", "孩子今年五岁了")])
        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        memory_id = listed["memories"][0]["memory_id"]

        for _ in range(2):
            result = await mem_api.delete_memory(
                child_id=child["child_id"],
                memory_id=memory_id,
                req=mem_api.MemoryActionRequest(family_id=child["family_id"]),
            )
            assert result["memory"]["status"] == "deleted"

        listed = await mem_api.list_memories(
            child_id=child["child_id"], family_id=child["family_id"]
        )
        assert listed["memories"] == []


class TestChildReuse:
    async def test_create_conversation_can_reuse_child(self, child):
        conv = await conv_api.create_conversation(
            conv_api.CreateConversationRequest(child_id=child["child_id"])
        )
        assert conv["child_id"] == child["child_id"]
        assert conv["family_id"] == child["family_id"]
        assert conv["conversation_id"].split(".")[1] == child["child_id"]

    async def test_family_mismatch_is_forbidden(self, child):
        with pytest.raises(HTTPException) as exc:
            await conv_api.create_conversation(
                conv_api.CreateConversationRequest(
                    child_id=child["child_id"], family_id=str(uuid.uuid4())
                )
            )
        assert exc.value.status_code == 403

    async def test_unknown_child_is_not_found(self):
        with pytest.raises(HTTPException) as exc:
            await conv_api.create_conversation(
                conv_api.CreateConversationRequest(child_id=str(uuid.uuid4()))
            )
        assert exc.value.status_code == 404
