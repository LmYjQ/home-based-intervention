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
                      AND source_id = :sid
                    ORDER BY score DESC
                    """
                ),
                {"q": "guided participation", "sid": source_id},
            )
        ).all()
    # 只查本测试自己插的那条 —— 库里还有真实入库的书（1049 块），
    # 不限定 source_id 的话 "guided participation" 会命中上百条
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


async def test_review_marks_survive_rebuild(db):
    """人工标记在入库重建后必须还在。

    这条是"标记机制"的核心保证：``write_to_db`` 是先删后建的重建流程，
    标记如果只在库里，重建一次就全没了。所以标记存文件（按内容哈希），
    重建时回填。

    用自己的测试 source，不碰真实入库的那本书。
    """
    from rdi_agent.rag.ingest import chunk as chunker
    from rdi_agent.rag.ingest import review, sections
    from rdi_agent.db.models import KnowledgeSource
    from rdi_agent.rag.ingest.pipeline import write_to_db

    source_id = "test_review_source_tmp"
    source = KnowledgeSource(
        source_id=source_id,
        source_type="book",
        title="Test Review Source",
        language="en",
        rights_status="internal_review_only",
    )
    section_map = sections.SectionMap(
        source_id=source_id,
        pdf="test.pdf",
        total_pages=10,
        sections=[
            sections.Section(
                section_code="000_test",
                title="Test Chapter",
                toc_level=2,
                pdf_page_start=1,
                pdf_page_end=5,
                order=0,
            )
        ],
    )
    chunks = [
        chunker.Chunk(
            content_original="alpha content here",
            content_normalized="alpha content here",
            pdf_page_start=1,
            pdf_page_end=1,
            chunk_index=0,
            parent_index=0,
            is_parent=True,
            section_code="000_test",
        ),
        chunker.Chunk(
            content_original="beta content here",
            content_normalized="beta content here",
            pdf_page_start=2,
            pdf_page_end=2,
            chunk_index=1,
            parent_index=0,
            is_parent=False,
            section_code="000_test",
            parent_chunk_index=0,
        ),
    ]

    # 标记第一个块为 reviewed，第二个不标
    marks = {0: "reviewed"}
    await write_to_db(
        source=source, section_map=section_map, chunks=chunks, vectors=None, marks=marks
    )

    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT chunk_index, review_status
                    FROM knowledge_chunks c
                    JOIN knowledge_sources s ON s.id = c.source_id
                    WHERE s.source_id = :sid ORDER BY chunk_index
                    """
                ),
                {"sid": source_id},
            )
        ).all()
    assert rows == [(0, "reviewed"), (1, "unreviewed")]

    # 重建一次（先删后建），标记必须还在
    source2 = KnowledgeSource(
        source_id=source_id,
        source_type="book",
        title="Test Review Source",
        language="en",
        rights_status="internal_review_only",
    )
    await write_to_db(
        source=source2,
        section_map=section_map,
        chunks=chunks,
        vectors=None,
        marks=marks,
    )
    async with session_scope() as session:
        rows2 = (
            await session.execute(
                text(
                    """
                    SELECT chunk_index, review_status
                    FROM knowledge_chunks c
                    JOIN knowledge_sources s ON s.id = c.source_id
                    WHERE s.source_id = :sid ORDER BY chunk_index
                    """
                ),
                {"sid": source_id},
            )
        ).all()
    assert rows2 == [(0, "reviewed"), (1, "unreviewed")]

    # 清理。顺序有讲究：先 chunk，再 section，最后 source ——
    # 反了会撞外键（book_sections 也引用 knowledge_sources）。
    async with session_scope() as session:
        await session.execute(
            text(
                """
                DELETE FROM knowledge_chunks WHERE source_id IN (
                    SELECT id FROM knowledge_sources WHERE source_id = :sid
                )
                """
            ),
            {"sid": source_id},
        )
        await session.execute(
            text(
                """
                DELETE FROM book_sections WHERE source_id IN (
                    SELECT id FROM knowledge_sources WHERE source_id = :sid
                )
                """
            ),
            {"sid": source_id},
        )
        await session.execute(
            text("DELETE FROM knowledge_sources WHERE source_id = :sid"), {"sid": source_id}
        )
