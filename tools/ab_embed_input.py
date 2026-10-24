"""A/B 实验：向量化该用「jieba 分词文本」还是「自然文本」。

**要验证的怀疑**：入库时向量是按 ``content_normalized`` 算的，而中文路径的
``content_normalized`` 是 **jieba 分词后**的文本
（``（ 一 ） 早期干预 。 早期干预 可以 显著 ...``）。
bge-m3 是在**自然中文**上训的，给它分好词、带空格的文本，
可能拿不到它训练时见过的分布 —— 向量路的精度因此打折。

**为什么必须测**：中文的同义表述对不上（"一周" vs "每周"、"哪里" vs "场地"），
关键词路精确匹配召不回，只能靠向量路兜。
如果分词文本���在拖累向量路，换成自然文本就能一次性解掉这一类失败。

**怎么测**：同一批块，两种输入各算一遍向量，然后**只走向量路**，
看期望章节排到第几名。分词文本仍然是 FTS 需要的（``content_tsv`` 生成列
读的就是 ``content_normalized``），所以这里只是**向量**换输入，FTS 不动。

    python -m tools.ab_embed_input

**会外发知识库正文到外部 embedding API** —— 用户已批准（2026-10-23）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import text

from rdi_agent.db.engine import session_scope
from rdi_agent.rag.ingest import embeddings

CASES_PATH = Path("evals/retrieval_cases.jsonl")


async def _load_chunks() -> list[dict]:
    """取所有子块（父块只是上下文，不参与召回）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id::text AS chunk_id,
                           c.content_original,
                           c.content_normalized,
                           s.section_code
                    FROM knowledge_chunks c
                    JOIN book_sections s ON s.id = c.section_id
                    WHERE c.parent_chunk_id IS NOT NULL
                    """
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _natural(text: str) -> str:
    """自然文本：原文压掉换行，其余不动。

    **不能直接用 ``content_original``**：表格行之间是 ``\\n\\n``，
    向量化时那些换行会带来无意义的段落边界。
    """
    return re.sub(r"\s+", " ", text).strip()


def _cos(a: list[float], b: list[float]) -> float:
    """bge-m3 输出已归一化，点积即余弦。"""
    return sum(x * y for x, y in zip(a, b))


async def main() -> None:
    parser = argparse.ArgumentParser(description="向量化输入形态 A/B")
    parser.add_argument("--cases", default=str(CASES_PATH))
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.cases).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in cases if c.get("expected_sections")]

    chunks = await _load_chunks()
    print(f"块数 {len(chunks)}，用例 {len(cases)}")

    natural = [_natural(c["content_original"]) for c in chunks]
    segmented = [c["content_normalized"] for c in chunks]

    # 两种向量算一次落盘 —— 重跑对比不用再外发一遍语料。
    cache_path = Path("data/index/ab_embed_input_cache.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        # 缓存必须对得上当前语料 —— 块顺序变了就不能复用
        if cached.get("chunk_ids") == [c["chunk_id"] for c in chunks]:
            cache = cached
            print("用缓存里的向量（没有重新外发）")
    if "seg" not in cache:
        print("算向量（分词文本）…")
        cache["seg"] = await embeddings.embed_texts(segmented)
    if "nat" not in cache:
        print("算向量（自然文本）…")
        cache["nat"] = await embeddings.embed_texts(natural)
    cache["chunk_ids"] = [c["chunk_id"] for c in chunks]
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    vec_seg = cache["seg"]
    vec_nat = cache["nat"]

    results: dict[str, list[int]] = {}
    for tag, vectors in (("seg", vec_seg), ("nat", vec_nat)):
        ranks: list[int] = []
        for case in cases:
            qv = await embeddings.embed_query(case["query"])
            scored = sorted(
                ((_cos(qv, v), c["section_code"]) for v, c in zip(vectors, chunks)),
                reverse=True,
            )
            rank = 99
            expected = set(case["expected_sections"])
            for i, (_, section) in enumerate(scored, 1):
                if section in expected:
                    rank = i
                    break
            ranks.append(rank)
        results[tag] = ranks

    print()
    for tag in ("seg", "nat"):
        ranks = results[tag]
        recall = sum(1 for r in ranks if r <= 5) / len(ranks)
        mrr = sum(1 / r if r < 99 else 0 for r in ranks) / len(ranks)
        print(f"{tag:4s} 向量路 recall@5={recall:.4f}  MRR={mrr:.4f}")

    print("\n逐条对比（只看有变化的）：")
    for case, rs, rn in zip(cases, results["seg"], results["nat"]):
        if rs == rn:
            continue
        better = "自然更好" if rn < rs else "分词更好"
        print(f"  {case['case_id']:42s} seg={rs:3d}  nat={rn:3d}  {better}")

    # ---- 全链路：向量 + 关键词 + RRF 合并 ----
    #
    # **只看向量路是不够的**：线上是两路合并，关键词路会盖掉向量路的
    # 差别。真正要回答的是"换成自然文本之后，家长看到的排序有没有变好"。
    #
    # 关键词路**完全不变**（``content_tsv`` 生成列读的是
    # ``content_normalized``，分词文本仍然是 FTS 需要的）。
    from rdi_agent.rag import search as search_mod
    from rdi_agent.rag.query_rewrite import rewrite_query

    await search_mod.ensure_high_freq_terms()
    print("\n=== 全链路（向量 + 关键词 + RRF）===")
    for tag, vectors in (("seg", vec_seg), ("nat", vec_nat)):
        ranks = []
        for case in cases:
            queries = await rewrite_query(case["query"])
            qv = await embeddings.embed_query(queries.semantic_query)
            fts = await search_mod._fts_candidates(queries.keyword_query, 30, None)
            # 用外部算好的向量代替数据库里的向量，复现向量路的排序
            scored = sorted(
                ((_cos(qv, v), i) for i, (v, c) in enumerate(zip(vectors, chunks))),
                reverse=True,
            )
            vector_hits = []
            for rank, (sim, idx) in enumerate(scored, 1):
                c = chunks[idx]
                if (1 - sim) > search_mod.MAX_VECTOR_DISTANCE:
                    continue
                vector_hits.append(
                    search_mod._Candidate(
                        chunk_id=c["chunk_id"],
                        chunk_index=0,
                        section_code=c["section_code"],
                        section_title=c["section_code"],
                        snippet="",
                        score=0.0,
                        similarity=sim,
                        is_parent=False,
                        knowledge_type="paragraph",
                        review_status="unreviewed",
                        pdf_page_start=None,
                        pdf_page_end=None,
                    )
                )
                vector_hits[-1].vector_rank = rank
            merged = search_mod._rrf_merge(vector_hits, fts)
            merged = [c for c in merged if not c.is_parent]
            expected = set(case["expected_sections"])
            rank = 99
            for i, c in enumerate(merged[:5], 1):
                if c.section_code in expected:
                    rank = i
                    break
            ranks.append(rank)
        recall = sum(1 for r in ranks if r <= 5) / len(ranks)
        mrr = sum(1 / r if r < 99 else 0 for r in ranks) / len(ranks)
        print(f"{tag:4s} 全链路 recall@5={recall:.4f}  MRR={mrr:.4f}")
        results[tag + "_full"] = ranks


if __name__ == "__main__":
    asyncio.run(main())
