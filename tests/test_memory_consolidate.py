"""合并（归纳）的端到端行为（真连 Postgres）。

**测的是"图里跑的 memory 是合并后的"这条要求**：

- 短句不进 prompt，只有类目小结进；
- 3 条以上、跨 2 轮才让 LLM 归纳；
- 明细不足 3 条时也有一条小结（不然这类目在 prompt 里是空的）；
- 家长删掉短句 → 小结重算，不留着他的旧内容；
- 偏好和推断**不参与合并**。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from rdi_agent.db.engine import dispose_engine, get_engine, session_scope
from rdi_agent.db.models import Child, MemoryEvent, MemoryItem
from rdi_agent.db.repositories.case import Repository
from rdi_agent.memory import service
from rdi_agent.memory.ids import note_content_hash
from rdi_agent.memory.summaries import normalize_summary_items
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
    category: str,
    turn_id: str,
    is_preference: bool = False,
    inference: bool = False,
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        category=category,
        is_preference=is_preference,
        inference=inference,
        source_quote=quote,
        source_turn_id=turn_id,
        proposed_decision="auto_commit",
        content_hash=note_content_hash(
            content, is_preference=is_preference, inference=inference
        ),
    )


@pytest.fixture
async def child():
    family_id = uuid.uuid4()
    async with session_scope() as session:
        row = await Repository(session).ensure_child(
            family_id=str(family_id), display_label="合并测试孩子"
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


async def _commit(child_id: str, text_: str, candidates, turn_id: str):
    state = {
        "child_id": child_id,
        "conversation_id": "conv_consolidate",
        "turn_id": turn_id,
        "user_message": text_,
        "memory_candidates": [c.model_dump(mode="json") for c in candidates],
    }
    merged = await service.merge_for_state(state)
    state["memory_update_result"] = merged.model_dump(mode="json")
    return await service.commit_for_state(state)


async def _summaries(child_id: str):
    async with session_scope() as session:
        row = await session.get(Child, uuid.UUID(child_id))
        return normalize_summary_items(row.memory_summaries_json)


class TestConsolidation:
    async def test_single_note_becomes_a_direct_summary(self, child):
        """只有 1 条短句时也要有小结。

        家长第一轮就说了"在幼儿园门口哭" ——
        这时候还没到 3 条，没有 direct 的话这句话永远进不了 prompt，
        而它恰恰是分离场景里最该被用上的信息。
        """
        cid = child["child_id"]
        await _commit(
            cid,
            "在幼儿园门口哭",
            [_note("在幼儿园门口哭", quote="在幼儿园门口哭", category="separation", turn_id="t1")],
            turn_id="t1",
        )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert len(live) == 1
        assert live[0].category == "separation"
        assert live[0].source_kind == "direct"
        assert live[0].derived_from

    async def test_three_notes_from_two_turns_trigger_llm(
        self, child, monkeypatch
    ):
        """3 条 + 跨 2 轮 → 调 LLM 归纳成一条 consolidated。"""
        from rdi_agent import llm
        from rdi_agent.schemas.memory import ConsolidationResult, SummaryDraft

        cid = child["child_id"]
        for i, turn in enumerate(["t1", "t2", "t2"]):
            content = f"第{i}次在幼儿园门口哭"
            await _commit(
                cid,
                content,
                [_note(content, quote=content, category="separation", turn_id=turn)],
                turn_id=turn,
            )

        calls = []

        async def fake_chat_json(**kwargs):
            calls.append(kwargs)
            return (
                ConsolidationResult(
                    summaries=[
                        SummaryDraft(
                            category="separation",
                            content="在幼儿园门口会哭",
                            scope_conditions=["在幼儿园门口交接时"],
                            derived_from=["obs_x"],
                            confidence_level="medium",
                            notes=[],
                        )
                    ],
                    notes=[],
                ),
                llm.LLMTrace(stage="memory_consolidate", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        await service.consolidate(child_id=cid)

        assert calls, "应该调了一次归纳"
        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        # 模型给的 derived_from（obs_x）不真实 → 这一条不该被采用
        assert not any(x.content == "在幼儿园门口会哭" for x in live)

    async def test_model_omitting_a_category_falls_back_to_direct(
        self, child, monkeypatch
    ):
        """模型没给某个类目出小结 → 那条类目退回短句直述。

        不能就这么空着 —— 那个类目的短句就永远进不了 prompt，
        家长说过的话白说了。
        """
        from rdi_agent import llm
        from rdi_agent.schemas.memory import ConsolidationResult

        cid = child["child_id"]
        for i, turn in enumerate(["t1", "t2", "t2"]):
            content = f"第{i}次在幼儿园门口哭"
            await _commit(
                cid,
                content,
                [_note(content, quote=content, category="separation", turn_id=turn)],
                turn_id=turn,
            )

        async def fake_chat_json(**kwargs):
            # 什么类目都没给
            return (
                ConsolidationResult(summaries=[], notes=[]),
                llm.LLMTrace(stage="memory_consolidate", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert len(live) == 1
        assert live[0].category == "separation"
        assert live[0].source_kind == "direct"
        # 覆盖了全部三条短句 —— 没有内容落空
        assert len(live[0].derived_from) == 3

    async def test_llm_failure_falls_back_to_direct(self, child, monkeypatch):
        """模型挂了 → 退回短句直述，不让类目变空白。"""
        from rdi_agent import llm

        cid = child["child_id"]
        for i, turn in enumerate(["t1", "t2", "t2"]):
            content = f"第{i}次在幼儿园门口哭"
            await _commit(
                cid,
                content,
                [_note(content, quote=content, category="separation", turn_id=turn)],
                turn_id=turn,
            )

        async def boom(**kwargs):
            raise RuntimeError("模型挂了")

        monkeypatch.setattr(llm, "chat_json", boom)
        notes = await service.consolidate(child_id=cid)

        assert any("合并失败" in x for x in notes)
        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert len(live) == 1
        assert live[0].source_kind == "direct"
        assert len(live[0].derived_from) == 3

    async def test_notes_do_not_enter_the_prompt(self, child):
        """**核心要求**：原始短句只留在库里，不进 prompt。"""
        cid = child["child_id"]
        content = "在幼儿园门口哭"
        await _commit(
            cid,
            content,
            [_note(content, quote=content, category="separation", turn_id="t1")],
            turn_id="t1",
        )
        await service.consolidate(child_id=cid)

        ctx = await service.load_memory_context(child_id=cid, query="幼儿园")
        memory_context = ctx["memory_context"]
        # 短句不在进 prompt 的那份里 —— 只有小结
        assert memory_context["summaries"]
        assert all(
            s["memory_id"].startswith("summ_")
            for s in memory_context["summaries"]
        )
        # 给 prompt 的 child_context 里也是同一个结构
        assert "summaries" in ctx["child_context_patch"]["memories"]

    async def test_deleting_a_summary_removes_its_notes(self, child):
        """删小结要连它的短句一起删。

        不删的话，下一轮合并会拿这些短句重新归纳出一条 ——
        家长点了删除，结果东西又回来了。
        """
        cid = child["child_id"]
        content = "在幼儿园门口哭"
        await _commit(
            cid,
            content,
            [_note(content, quote=content, category="separation", turn_id="t1")],
            turn_id="t1",
        )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert live

        await service.delete_memory(
            child_id=cid, family_id=child["family_id"], memory_id=live[0].id
        )

        async with session_scope() as session:
            from sqlalchemy import select

            rows = (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.child_id == uuid.UUID(cid)
                    )
                )
            ).scalars().all()
        assert all(r.status == "deleted" for r in rows)

    async def test_consolidation_is_idempotent(self, child):
        """跑两次不能产生两条小结。"""
        cid = child["child_id"]
        content = "在幼儿园门口哭"
        await _commit(
            cid,
            content,
            [_note(content, quote=content, category="separation", turn_id="t1")],
            turn_id="t1",
        )
        await service.consolidate(child_id=cid)
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert len(live) == 1

    async def test_a_new_detail_is_never_orphaned(self, child):
        """**新加的短句必须被小结覆盖**，不能落空。

        这是"原始明细不参与方案制定"这条要求最容易漏的地方：
        短句只留在库里，只有小结进 prompt ——
        所以一条没被小结覆盖的短句，等于家长说过的一句话白说了。

        触发条件只按"新增几条"判的话，这条会一直落空：
        既没被旧小结覆盖，又不够触发重算。
        """
        cid = child["child_id"]
        for i, turn in enumerate(["t1", "t2"]):
            content = f"第{i}次在幼儿园门口哭"
            await _commit(
                cid,
                content,
                [_note(content, quote=content, category="separation", turn_id=turn)],
                turn_id=turn,
            )
        await service.consolidate(child_id=cid)

        # 第三条来了 —— 只有一条新增
        content = "第3次在幼儿园门口哭"
        await _commit(
            cid,
            content,
            [_note(content, quote=content, category="separation", turn_id="t3")],
            turn_id="t3",
        )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert len(live) == 1
        # 小结必须覆盖三条短句
        assert len(live[0].derived_from) == 3

        # 而且它得真的进 prompt
        ctx = await service.load_memory_context(child_id=cid, query="幼儿园")
        assert ctx["memory_context"]["summaries"]

    async def test_deleting_a_detail_triggers_recompute(self, child):
        """家长删掉一条短句 → 小结重算，不再引用它。"""
        cid = child["child_id"]
        for i, turn in enumerate(["t1", "t2", "t3"]):
            content = f"第{i}次在幼儿园门口哭"
            await _commit(
                cid,
                content,
                [_note(content, quote=content, category="separation", turn_id=turn)],
                turn_id=turn,
            )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert live and len(live[0].derived_from) == 3

        # 家长删掉其中一条短句
        from sqlalchemy import select

        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.child_id == uuid.UUID(cid)
                    )
                )
            ).scalars().all()
            victim = rows[0]
            obs_id = f"mi_{victim.id.hex}"
            victim.status = "deleted"
            victim.deleted_at = datetime.now(timezone.utc)
            await session.flush()

        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        live = [x for x in items if x.is_live]
        assert live
        assert obs_id not in live[0].derived_from
        assert len(live[0].derived_from) == 2


class TestNonMergingKinds:
    async def test_preferences_are_not_merged(self, child):
        """偏好是硬约束 —— 合并会丢掉"不希望"的强度，所以不参与。"""
        cid = child["child_id"]
        for turn in ("t1", "t2", "t3"):
            await _commit(
                cid,
                "我不希望他被批评",
                [
                    _note(
                        "我不希望他被批评",
                        quote="我不希望他被批评",
                        category="family",
                        turn_id=turn,
                        is_preference=True,
                    )
                ],
                turn_id=turn,
            )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        assert not [x for x in items if x.is_live]

        ctx = await service.load_memory_context(child_id=cid, query="批评")
        assert ctx["memory_context"]["preferences"] == ["我不希望他被批评"]

    async def test_inferences_are_not_merged(self, child):
        """推断是猜测 —— 合并会把它变成事实，所以不参与。"""
        cid = child["child_id"]
        for turn in ("t1", "t2", "t3"):
            await _commit(
                cid,
                "他最近不太想去幼儿园",
                [
                    _note(
                        "可能是分离焦虑",
                        quote="",
                        category="separation",
                        turn_id=turn,
                        inference=True,
                    )
                ],
                turn_id=turn,
            )
        await service.consolidate(child_id=cid)

        items = await _summaries(cid)
        assert not [x for x in items if x.is_live]

        ctx = await service.load_memory_context(child_id=cid, query="幼儿园")
        assert [x["content"] for x in ctx["memory_context"]["inferences"]] == [
            "可能是分离焦虑"
        ]
