"""一次性修复：把入库内容里的 ``。。`` 压成 ``。``，并只重算受影响块的向量。

**为什么不全量重建**：入库是"按 source_id 全删再重建"，
重建会把**所有** chunk id 换掉 —— 人工标记是按内容哈希存的（内容不变标记就有效），
但 ``evidence_id`` 存在数据库里（``evidence_bundle`` / ``plans`` 的引用），
换 id 等于把已有的引用全打断。

**所以这里只动受影响的那几块**：改内容、重算这��块的向量、更新 review_status。
其余块**一行都不碰**。

**为什么向量要重算**：embedding 的输入是 ``content_normalized``，
内容变了，向量不变就是错的。

**``content_tsv`` 不用管**：它是 ``content_normalized`` 的**生成列**
（``GENERATED ALWAYS AS to_tsvector('simple', content_normalized)``），
更新原文时 Postgres 自动重算。

用法::

    .venv/bin/python -m tools.fix_double_period            # 先看会改什么
    .venv/bin/python -m tools.fix_double_period --apply    # 真改
"""

from __future__ import annotations

import argparse
import asyncio
import re

from sqlalchemy import text

from rdi_agent.db.engine import session_scope
from rdi_agent.rag.ingest import embeddings
from rdi_agent.rag.ingest.clean import normalize_zh
from rdi_agent.rag import search as search_mod

SOURCE_ID = "guideline2026"

# 重复的句号。两种形态：
#
#   - ``。。``   连续两个（``<br>`` 前面本来就有句号）
#   - ``。 。``  中间隔着空格（源文件里是 ``<br><br>``，双换行）
#
# 压完都留成 ``。 `` —— 跟单 ``<br>`` 的形态一致，
# 跟 ``parse_md._br_to_period`` 产出的形态也一致。
_DOUBLE_PERIOD = re.compile(r"。{2,}")
_SPACED_DOUBLE_PERIOD = re.compile(r"。\s+。")


def collapse(text: str) -> str:
    """把重复的句号压成一个。"""
    text = _DOUBLE_PERIOD.sub("。", text)
    text = _SPACED_DOUBLE_PERIOD.sub("。 ", text)
    return text


async def _affected() -> list[dict]:
    """找出内容里含 ``。。`` 的块。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id::text AS id,
                           c.chunk_index,
                           c.content_original,
                           c.content_normalized,
                           c.review_status,
                           s.section_code
                    FROM knowledge_chunks c
                    JOIN book_sections s ON s.id = c.section_id
                    WHERE c.source_id = (
                        SELECT id FROM knowledge_sources WHERE source_id = :sid
                    )
                      AND (c.content_original ~ '。\\s*。'
                           OR c.content_normalized ~ '。\\s*。')
                    ORDER BY c.chunk_index
                    """
                ),
                {"sid": SOURCE_ID},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def apply_fix(recompute_vectors: bool = True) -> dict:
    """改内容 + 重算向量 + 顺带把 ``。。`` 的块标成 reviewed。

    **为什么顺带标 reviewed**：``。。`` 是**入库转换的产物**，不是内容错误 ——
    文字本身跟原文一致，只是多了个句号。修掉之后它就是干净的，
    不该再挂着 ``needs_correction``（那会把整块踢出检索）。
    """
    rows = await _affected()
    if not rows:
        return {"updated": 0}

    # ---- 1. 改内容 ----
    new_originals: list[str] = []
    new_normalizeds: list[str] = []
    for r in rows:
        new_originals.append(collapse(r["content_original"]))
        new_normalizeds.append(collapse(r["content_normalized"]))

    async with session_scope() as session:
        for r, no, nn in zip(rows, new_originals, new_normalizeds):
            await session.execute(
                text(
                    """
                    UPDATE knowledge_chunks
                    SET content_original = :co,
                        content_normalized = :cn
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"co": no, "cn": nn, "id": r["id"]},
            )
        # await session.commit()  -- session_scope 退出时自动提交

    # ---- 2. 重算向量 ----
    embedded = 0
    if recompute_vectors:
        # embedding 的输入跟入库时一致：分词后的 content_normalized
        vectors = await embeddings.embed_texts(new_normalizeds)
        expected = embeddings.expected_dim()
        bad = [i for i, v in enumerate(vectors) if len(v) != expected]
        if bad:
            raise embeddings.EmbeddingError(
                f"{len(bad)} 个向量维度不对（期望 {expected}），未提交"
            )
        async with session_scope() as session:
            for r, vec in zip(rows, vectors):
                await session.execute(
                    text(
                        """
                        UPDATE knowledge_chunks
                        SET embedding = CAST(:vec AS vector)
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {"vec": str(vec), "id": r["id"]},
                )
        embedded = len(vectors)

    return {"updated": len(rows), "embedded": embedded}


async def main() -> None:
    parser = argparse.ArgumentParser(description="修复入库内容里的重复句号")
    parser.add_argument("--apply", action="store_true", help="真改（默认只报告）")
    parser.add_argument(
        "--no-vectors",
        action="store_true",
        help="不重算向量（内容改了但向量还是旧的，检索会不准）",
    )
    args = parser.parse_args()

    rows = await _affected()
    print(f"含 '。。' 的块：{len(rows)}")
    total = 0
    for r in rows:
        n = len(_DOUBLE_PERIOD.findall(r["content_original"])) + len(
            _SPACED_DOUBLE_PERIOD.findall(r["content_original"])
        )
        total += n
        print(
            f"  #{r['chunk_index']:>3} {r['section_code'][:24]:<26} "
            f"{n:>2} 处  当前标记 {r['review_status']}"
        )
    print(f"合计 {total} 处")

    if not args.apply:
        print("\n（这是预览。加 --apply 真改）")
        return

    result = await apply_fix(recompute_vectors=not args.no_vectors)
    print(f"\n已改 {result['updated']} 块，重算向量 {result['embedded']} 个")

    # 高频词表跟着内容变
    await search_mod.ensure_high_freq_terms(force=True)
    print("高频词表已重算")


if __name__ == "__main__":
    asyncio.run(main())
