"""检索 CLI：查书里的哪一段。

    python -m rdi_agent.rag.search "引导式参与" --top-k 8

**输出必须能翻回去核对**：每条结果都给章节、PDF 页码、原文片段。
"书里说"这种话如果没有页码，出了问题没人能查。

**两路召回 + RRF 合并**：

- 向量：抓语义相近的段落（"孩子不敢跟人打招呼" 能命中 "social departure"）
- 关键词：抓用词精确的段落（专有名词、术语、原文引用）

两路分数不可比（余弦距离 vs ts_rank），所以用 RRF 合并：只看名次，
不看分数。``score = sum(1 / (60 + rank))``。

向量路要调 embedding 接口；``--no-vector`` 可以只跑关键词路。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text

from rdi_agent.db.engine import session_scope
from rdi_agent.rag.ingest import embeddings

# RRF 的平滑常数。60 是原论文的经验值，对我们这个量级（几百条候选）够用。
RRF_K = 60

# 召回过滤（计划 §5.4）：标了 needs_correction 的片段不进召回。
# 两路 SQL 共用这一句 —— 漏了其中一路，坏片段还是会从另一路冒出来。
REVIEW_FILTER = "c.review_status <> 'needs_correction'"


@dataclass
class SearchHit:
    chunk_id: str
    chunk_index: int
    section_code: str
    section_title: str
    pdf_page_start: int
    pdf_page_end: int
    snippet: str
    score: float
    vector_rank: int | None = None
    fts_rank: int | None = None
    is_parent: bool = False
    knowledge_type: str = ""
    review_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Candidate:
    chunk_id: str
    chunk_index: int
    section_code: str
    section_title: str
    pdf_page_start: int
    pdf_page_end: int
    snippet: str
    is_parent: bool
    knowledge_type: str
    review_status: str
    vector_rank: int | None = None
    fts_rank: int | None = None
    score: float = 0.0


_SNIPPET_LEN = 220


def _snippet(text_value: str) -> str:
    text_value = " ".join(text_value.split())
    if len(text_value) <= _SNIPPET_LEN:
        return text_value
    return text_value[:_SNIPPET_LEN].rstrip() + "…"


# 两路召回共用的列。写两遍就容易改漏一处 —— 上次加 review_status 过滤
# 就差点只加到一路。
_SELECT_COLUMNS = """
        SELECT c.id, c.chunk_index, s.section_code, s.title_original,
               c.pdf_page_start, c.pdf_page_end, c.content_original,
               (c.parent_chunk_id IS NULL) AS is_parent,
               c.knowledge_type, c.review_status
        FROM knowledge_chunks c
        JOIN book_sections s ON s.id = c.section_id
"""


def _row_to_candidate(row: Any) -> _Candidate:
    return _Candidate(
        chunk_id=str(row["id"]),
        chunk_index=row["chunk_index"],
        section_code=row["section_code"],
        section_title=row["title_original"],
        pdf_page_start=row["pdf_page_start"],
        pdf_page_end=row["pdf_page_end"],
        snippet=_snippet(row["content_original"]),
        is_parent=bool(row["is_parent"]),
        knowledge_type=row["knowledge_type"],
        review_status=row["review_status"],
    )


async def _run_candidate_query(
    sql: str, params: dict[str, Any]
) -> list[_Candidate]:
    """跑一路召回的 SQL，返回候选。

    两路（向量 / 关键词）共用：列一样、过滤一样、行转候选的逻辑一样。
    """
    async with session_scope() as session:
        rows = (
            await session.execute(text(sql), params)
        ).mappings().all()
    return [_row_to_candidate(row) for row in rows]


async def _vector_candidates(
    query_vector: list[float], top_k: int, section_code: str | None
) -> list[_Candidate]:
    sql = (
        _SELECT_COLUMNS
        + """
        WHERE c.embedding IS NOT NULL
          AND """
        + REVIEW_FILTER
    )
    params: dict[str, Any] = {"qv": str(query_vector)}
    if section_code:
        sql += " AND s.section_code = :section_code"
        params["section_code"] = section_code
    sql += " ORDER BY c.embedding <=> :qv LIMIT :k"
    params["k"] = top_k

    out = await _run_candidate_query(sql, params)
    for rank, candidate in enumerate(out, start=1):
        candidate.vector_rank = rank
    return out


async def expand_with_concepts(query: str) -> list[str]:
    """中文查询桥接到书里的英文术语。

    家长说"引导式参与"，书里写 "guided participation"。术语表就是干这个的。

    **桥接逻辑只有一份**，在 :mod:`rdi_agent.rag.query_rewrite` 里。
    这里保留这个函数是为了兼容旧调用方，但内部转发过去 ——
    曾经这里和 query_rewrite 各有一份实现，两份的过滤规则还不一样
    （这里没看 ``is_explicit_in_source``，会把书里不存在的词也桥进去）。
    """
    from rdi_agent.rag.query_rewrite import _bridge_concepts

    hits = await _bridge_concepts(query)
    return [h["canonical_name_en"] for h in hits]


async def _fts_candidates(
    query: str, top_k: int, section_code: str | None
) -> list[_Candidate]:
    """关键词召回。

    ``query`` 是**已经改写好的**关键词查询（``query_rewrite`` 负责桥接）。
    这里**不再自己桥一次** —— 曾经两处各桥一遍，结果是同一个查询被桥接
    两次，而且两处的过滤规则还不一样（旧的没看 ``is_explicit_in_source``，
    会把书里不存在的词也塞进去）。
    """
    sql = (
        _SELECT_COLUMNS
        + """
        WHERE c.content_tsv @@ websearch_to_tsquery('english', :tsquery)
          AND """
        + REVIEW_FILTER
    )
    params: dict[str, Any] = {"tsquery": query}
    if section_code:
        sql += " AND s.section_code = :section_code"
        params["section_code"] = section_code
    sql += " ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :tsquery)) DESC LIMIT :k"
    params["k"] = top_k

    out = await _run_candidate_query(sql, params)
    for rank, candidate in enumerate(out, start=1):
        candidate.fts_rank = rank
    return out


def _rrf_merge(
    vector: list[_Candidate], fts: list[_Candidate]
) -> list[_Candidate]:
    """RRF：只看名次，不看分数。两路分数本来就不可比。

    两路都命中的块要把两边的名次都记下来。注意 ``setdefault`` 返回的是
    **先放进去的那份**（向量路的），所以名次要显式写回去 —— 只累加分数
    的话，FTS 路的 ``fts_rank`` 会跟着被丢弃的那份对象一起丢掉，
    排查召回时就看不出这条是哪路捞上来的。
    """
    merged: dict[str, _Candidate] = {}
    for candidate in vector:
        kept = merged.setdefault(candidate.chunk_id, candidate)
        kept.vector_rank = candidate.vector_rank
        kept.score += 1.0 / (RRF_K + (candidate.vector_rank or 0))
    for candidate in fts:
        kept = merged.setdefault(candidate.chunk_id, candidate)
        kept.fts_rank = candidate.fts_rank
        kept.score += 1.0 / (RRF_K + (candidate.fts_rank or 0))
    return sorted(merged.values(), key=lambda c: c.score, reverse=True)


async def search(
    query: str,
    *,
    top_k: int = 8,
    candidate_k: int = 30,
    section_code: str | None = None,
    use_vector: bool = True,
    kind: str = "child",
) -> list[SearchHit]:
    """检索。返回按 RRF 分数排好的结果。

    ``kind`` 默认只回子块：子块是召回单元，父块是它的上下文。父块和它的
    子块同时出现在结果里，等于同一段话显示两遍。
    """
    # 查询改写只做一次：语义路用原话，关键词路用桥接后的
    from rdi_agent.rag.query_rewrite import rewrite_query

    queries = await rewrite_query(query)
    vector_hits: list[_Candidate] = []
    if use_vector:
        query_vector = await embeddings.embed_query(queries.semantic_query)
        vector_hits = await _vector_candidates(query_vector, candidate_k, section_code)
    fts_hits = await _fts_candidates(queries.keyword_query, candidate_k, section_code)

    merged = _rrf_merge(vector_hits, fts_hits)
    if kind == "child":
        merged = [c for c in merged if not c.is_parent]
    elif kind == "parent":
        merged = [c for c in merged if c.is_parent]
    merged = merged[:top_k]
    return [
        SearchHit(
            chunk_id=c.chunk_id,
            chunk_index=c.chunk_index,
            section_code=c.section_code,
            section_title=c.section_title,
            pdf_page_start=c.pdf_page_start,
            pdf_page_end=c.pdf_page_end,
            snippet=c.snippet,
            score=round(c.score, 6),
            vector_rank=c.vector_rank,
            fts_rank=c.fts_rank,
            is_parent=c.is_parent,
            knowledge_type=c.knowledge_type,
            review_status=c.review_status,
        )
        for c in merged
    ]


def _print_hits(query: str, hits: list[SearchHit]) -> None:
    print(f"\n查询：{query}")
    if not hits:
        print("（没有命中）")
        return
    for i, hit in enumerate(hits, start=1):
        tag = "父块" if hit.is_parent else "子块"
        print(
            f"\n[{i}] {hit.section_title}  "
            f"(p{hit.pdf_page_start}-{hit.pdf_page_end}, {tag}, {hit.knowledge_type})"
        )
        print(f"    分数 {hit.score:.5f}"
              f"  向量名次 {hit.vector_rank or '-'}"
              f" / 关键词名次 {hit.fts_rank or '-'}")
        print(f"    {hit.snippet}")


def main() -> None:
    parser = argparse.ArgumentParser(description="检索 RDI Book")
    parser.add_argument("query", help="查询词")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--candidate-k", type=int, default=30, help="每路召回多少条")
    parser.add_argument("--section", default=None, help="只看某个 section_code")
    parser.add_argument("--no-vector", action="store_true", help="只跑关键词路")
    parser.add_argument(
        "--kind",
        choices=("child", "parent", "any"),
        default="child",
        help="只回子块（默认）/ 只回父块 / 都要",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    hits = asyncio.run(
        search(
            args.query,
            top_k=args.top_k,
            candidate_k=args.candidate_k,
            section_code=args.section,
            use_vector=not args.no_vector,
            kind=args.kind,
        )
    )
    if args.json:
        print(json.dumps([h.to_dict() for h in hits], ensure_ascii=False, indent=2))
    else:
        _print_hits(args.query, hits)


if __name__ == "__main__":
    main()
