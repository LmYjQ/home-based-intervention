"""数据库集成测试。

这些用例要真连 Postgres。数据库不可用时**跳过**而不是失败 ——
CI 或别人的机器上没有 db 是正常的，不该让整套测试挂掉。

要跑它们：先 ``docker compose -f deploy/docker-compose.yml up -d db``，
再 ``pytest -q tests/test_db_integration.py``。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from rdi_agent.db.engine import dispose_engine, get_engine, session_scope


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


async def test_pgvector_extension_available(db):
    async with get_engine().connect() as conn:
        ver = (
            await conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            )
        ).scalar_one()
    assert ver


async def test_generated_tsv_column_and_gin_index(db):
    """content_tsv 必须是生成列，GIN 索引必须真的在。"""
    async with get_engine().connect() as conn:
        col = (
            await conn.execute(
                text(
                    """
                    SELECT is_generated FROM information_schema.columns
                    WHERE table_name='knowledge_chunks' AND column_name='content_tsv'
                    """
                )
            )
        ).scalar_one()
        idx = (
            await conn.execute(
                text(
                    """
                    SELECT indexdef FROM pg_indexes
                    WHERE tablename='knowledge_chunks' AND indexname='idx_chunks_tsv'
                    """
                )
            )
        ).scalar_one()
    assert col == "ALWAYS"
    assert "gin" in idx.lower()


async def test_hnsw_embedding_index_exists(db):
    async with get_engine().connect() as conn:
        idx = (
            await conn.execute(
                text(
                    """
                    SELECT indexdef FROM pg_indexes
                    WHERE tablename='knowledge_chunks' AND indexname='idx_chunks_embedding'
                    """
                )
            )
        ).scalar_one()
    assert "hnsw" in idx.lower()
    assert "vector_cosine_ops" in idx


async def test_fulltext_search_roundtrip(db):
    """写入一个块，用内置全文检索查回来。"""
    source_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    # source_id 是唯一列，先清掉上一次可能残留的（测试中途失败会留下）。
    # 先删 chunk 再删 source，顺序反了会撞外键。
    async with session_scope() as session:
        await session.execute(
            text(
                """
                DELETE FROM knowledge_chunks
                WHERE source_id IN (
                    SELECT id FROM knowledge_sources WHERE source_id = 'test_source_tmp'
                )
                """
            )
        )
        await session.execute(
            text("DELETE FROM knowledge_sources WHERE source_id = 'test_source_tmp'")
        )
    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO knowledge_sources (id, source_id, source_type, title)
                VALUES (:id, 'test_source_tmp', 'book', 'Test Source')
                """
            ),
            {"id": source_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO knowledge_chunks
                    (id, source_id, content_original, content_normalized, knowledge_type)
                VALUES (:id, :sid, :c, :c, 'principle')
                """
            ),
            {
                "id": chunk_id,
                "sid": source_id,
                "c": "Guided participation builds joint attention over time.",
            },
        )

    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id,
                           ts_rank_cd(content_tsv, websearch_to_tsquery('english', :q)) AS score
                    FROM knowledge_chunks
                    WHERE content_tsv @@ websearch_to_tsquery('english', :q)
                    ORDER BY score DESC
                    """
                ),
                {"q": "guided participation"},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == chunk_id
    assert float(rows[0][1]) > 0

    # 清理
    async with session_scope() as session:
        await session.execute(
            text("DELETE FROM knowledge_chunks WHERE id = :id"), {"id": chunk_id}
        )
        await session.execute(
            text("DELETE FROM knowledge_sources WHERE id = :id"), {"id": source_id}
        )


async def test_interactions_idempotency_constraint(db):
    """同一 (conversation_id, turn_id, role) 只能写一次。"""
    from sqlalchemy.exc import IntegrityError

    fam = uuid.uuid4()
    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO interactions
                    (id, family_id, conversation_id, thread_id, turn_id, role, content)
                VALUES (:id, :fam, 'conv_test', 'conv_test', 'turn_1', 'user', 'hi')
                """
            ),
            {"id": uuid.uuid4(), "fam": fam},
        )

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO interactions
                        (id, family_id, conversation_id, thread_id, turn_id, role, content)
                    VALUES (:id, :fam, 'conv_test', 'conv_test', 'turn_1', 'user', 'hi again')
                    """
                ),
                {"id": uuid.uuid4(), "fam": fam},
            )

    async with session_scope() as session:
        await session.execute(
            text("DELETE FROM interactions WHERE family_id = :fam"), {"fam": fam}
        )
