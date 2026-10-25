"""检索 CLI：查知识库里的哪一段。

    python -m rdi_agent.rag.search "挑战性行为" --top-k 8

**输出必须能翻回去核对**：每条结果都给章节、PDF 页码、原文片段。
"指南里说"这种话如果没有页码，出了问题没人能查。

**两路召回 + RRF 合并**：

- 向量：抓语义相近的段落（"孩子摔东西尖叫" 能命中 "挑战性行为的干预"）
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
from rdi_agent.rag import keyword_backends
from rdi_agent.rag.filters import (
    CONTENT_FILTER,
    REVIEW_FILTER,
    SELECT_COLUMNS,
    select_columns,
)
from rdi_agent.rag.ingest import embeddings

# 高频词表现在住在 :mod:`rdi_agent.rag.terms`（关键词后端也要用）。
# 这里 re-export ``ensure_high_freq_terms`` 和 ``_keyword_terms``，
# 是因为调用方（``rag/retrieval.py``、``evals/run_retrieval.py``、
# ``evals/calibrate_threshold.py``、``ingest/pipeline.py``）一贯用
# ``search_mod.ensure_high_freq_terms()`` 调用 ——
# 换了调用点就多一处「评测和线上走的不是同一套」的风险。
from rdi_agent.rag.terms import ensure_high_freq_terms  # noqa: F401
from rdi_agent.rag.terms import keyword_terms as _keyword_terms  # noqa: F401

# RRF 的平滑常数。60 是原论文的经验值，对我们这个量级（几百条候选）够用。
RRF_K = 60

# 召回过滤和共用列现在住在 :mod:`rdi_agent.rag.filters` ——
# 关键词后端（``keyword_backends.py``）也要用它们，
# 放这里会 import 成环。
# 这里仍然 re-export 一份，是为了让 ``search.REVIEW_FILTER`` 这种
# 写法继续能用（测试和历史文档都这么引）。
__all_filters__ = (REVIEW_FILTER, CONTENT_FILTER, SELECT_COLUMNS)

# 向量路的余弦距离上限（越小越严）。
#
# **为什么需要它**：向量检索永远能返回"最像的"若干条，哪怕完全不相关。
# ``ts_rank_cd`` 有天然下限（不匹配就是 0），向量路没有。
#
# **0.52 是标定出来的**，不是拍脑袋：``evals/calibrate_threshold.py``
# 跑了 37 条中文评测用例（34 相关 + 3 无关）的 top-30 向量距离。
#
# **中文的距离分布和英文书时代完全不同**，所以三个阈值全部重新标定过：
#
#   - 相关用例的最近邻：0.2876 ~ 0.4691
#   - 无关用例的最近邻：0.5675 ~ 0.6071
#
# 分界带在 0.47~0.57 之间，**0.52 落在带子中间** ——
# 比它严会砍掉相关用例的期望块（0.52 时砍 5 块，再严砍得更多），
# 比它松会把无关题放进来。
#
# **标定表**（相关仍命中章节 / 漏判相关）：
#   0.46 → 29 / 0     0.50 → 32 / 0     0.52 → 34 / 0
#   0.54 → 34 / 0     0.56 → 34 / 0
# 0.52 是"相关用例全部命中"和"砍掉块最少"的交点。
MAX_VECTOR_DISTANCE = 0.52

# 合并之后的一道**严**门槛：没有关键词路佐证的向量块，距离超过这个就丢。
#
# **为什么要有这一层**：向量路放宽之后，无关题（红烧肉、天气）
# 又会被捞回 4-5 条。区分它们靠的是**第二个信号** ——
# 关键词路有没有命中精确术语：
#
#   - 红烧肉：关键词路 0 条，最近邻 0.6071 > 0.52 → 丢
#   - 挑战性行为：关键词路 25 条，期望块距离 0.4378 但有关键词佐证 → 留
#
# 也就是说，一条块要留下来，**要么**它离得很近，
# **要么**关键词路也捞到了它（精确术语命中）。
#
# **和 MAX_VECTOR_DISTANCE 同值（0.52）**：中文的关键词路判别力很强 ——
# 34 条相关用例**全部**有关键词命中（10~30 条），3 条无关用例**全部**
# 是 0 条。这个信号单独就能分开相关和无关，所以两处门槛可以重合。
STRICT_VECTOR_DISTANCE = 0.52

# 门控层判"这条证据能否当作直接依据"的距离上限。
#
# 和上面两个阈值的区别，这是**第三层**，从宽到严：
#
#   MAX_VECTOR_DISTANCE  = 0.52  召回：向量路最多捞到多远的块
#   STRICT_VECTOR_DISTANCE = 0.52 合并：没有关键词佐证的块，最多允许到多近
#   DIRECT_MAX_DISTANCE  = 0.50 采信：门控认它当"直接依据"的门槛
#
# 落在 0.50–0.52 的块：检索层留、门控层不算直接依据。
#
# **DIRECT 比前两个严一档（0.50）**：门控要回答的是"这条能不能**拿来
# 支撑一条给这个孩子的建议**"，比"能不能召回"更保守 ——
# 0.50 之下才认。相关用例的最近邻最大 0.4691，所以 0.50 不会伤正常召回。
DIRECT_MAX_DISTANCE = 0.50


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
    # 向量路的余弦相似度（1 - 距离）。FTS 命中没经过向量路时为 None。
    similarity: float | None = None

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
    # 向量路的余弦相似度（1 - 距离）。FTS 命中没经过向量路时为 None。
    similarity: float | None = None


_SNIPPET_LEN = 220


def _snippet(text_value: str) -> str:
    text_value = " ".join(text_value.split())
    if len(text_value) <= _SNIPPET_LEN:
        return text_value
    return text_value[:_SNIPPET_LEN].rstrip() + "…"


def _row_to_candidate(row: Any) -> _Candidate:
    similarity = row.get("similarity")
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
        similarity=round(float(similarity), 4) if similarity is not None else None,
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
    query_vector: list[float],
    top_k: int,
    section_code: str | None,
    max_distance: float | None = MAX_VECTOR_DISTANCE,
) -> list[_Candidate]:
    """向量召回。

    ``max_distance`` 是余弦距离上限。给 None 表示不过滤（标定时用）。
    距离在 SQL 里算出来，**顺便 SELECT 回来** —— 不取回来就没法观测、
    也没法在门控层判断"这条能不能采信"。
    """
    sql = (
        select_columns(
            "               1 - (c.embedding <=> CAST(:qv AS vector)) AS similarity"
        )
        + """
        WHERE c.embedding IS NOT NULL
          AND """
        + REVIEW_FILTER
        + CONTENT_FILTER
    )
    params: dict[str, Any] = {"qv": str(query_vector)}
    if section_code:
        sql += " AND s.section_code = :section_code"
        params["section_code"] = section_code
    if max_distance is not None:
        sql += " AND (c.embedding <=> CAST(:qv AS vector)) <= :max_distance"
        params["max_distance"] = max_distance
    sql += " ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :k"
    params["k"] = top_k

    out = await _run_candidate_query(sql, params)
    for rank, candidate in enumerate(out, start=1):
        candidate.vector_rank = rank
    return out


async def expand_with_concepts(query: str) -> list[str]:
    """中文查询桥接到知识库里的规范术语。

    家长说"发脾气"，指南里写"挑战性行为"。术语表就是干这个的。

    **桥接逻辑只有一份**，在 :mod:`rdi_agent.rag.query_rewrite` 里。
    这里保留这个函数是为了兼容旧调用方，但内部转发过去 ——
    曾经这里和 query_rewrite 各有一份实现，两份的过滤规则还不一样
    （这里没看 ``is_explicit_in_source``，会把知识库里不存在的词也桥进去）。
    """
    from rdi_agent.rag.query_rewrite import _bridge_concepts

    hits = await _bridge_concepts(query)
    return [h["canonical_name"] for h in hits]


async def _fts_candidates(
    query: str,
    top_k: int,
    section_code: str | None,
    *,
    backend_name: str | None = None,
) -> list[_Candidate]:
    """关键词召回。**走指定的后端**（默认取配置里的那个）。

    ``query`` 是**已经改写好的**关键词查询（``query_rewrite`` 负责桥接和分词）。
    这里**不再自己桥一次** —— 曾经两处各桥一遍，结果是同一个查询被桥接
    两次，而且两处的过滤规则还不一样（旧的没看 ``is_explicit_in_source``，
    会把知识库里不存在的词也塞进去）。

    具体怎么查由 :mod:`rdi_agent.rag.keyword_backends` 里的后端决定 ——
    换后端不用改这个函数。

    ``backend_name`` 显式传时**覆盖配置**。CLI 用它做「同一查询换后端对比」，
    不用改环境变量、不用重启进程。
    """
    backend = keyword_backends.get_backend(backend_name or _configured_backend())
    built = await backend.build_query(
        query, top_k=top_k, section_code=section_code
    )
    if built is None:
        return []
    sql, params = built
    out = await _run_candidate_query(sql, params)
    for rank, candidate in enumerate(out, start=1):
        candidate.fts_rank = rank
    return out


def _configured_backend() -> str:
    """配置里指定的关键词后端名。

    **为什么每次都读配置而不是模块加载时取一次**：测试里会改配置
    （``reset_settings``），缓存了就测不到切换效果。
    配置本身是 ``pydantic-settings`` 的单例，读取开销可以忽略。
    """
    from rdi_agent.config import get_settings

    return get_settings().keyword_backend


def _rrf_merge(
    vector: list[_Candidate], fts: list[_Candidate]
) -> list[_Candidate]:
    """RRF：只看名次，不看分数。两路分数本来就不可比。

    两路都命中的块要把两边的名次都记下来。注意 ``setdefault`` 返回的是
    **先放进去的那份**（向量路的），所以名次要显式写回去 —— 只累加分数
    的话，FTS 路的 ``fts_rank`` 会跟着被丢弃的那份对象一起丢掉，
    排查召回时就看不出这条是哪路捞上来的。

    合并完再过一道**严门槛**（``STRICT_VECTOR_DISTANCE``）：
    没有关键词路佐证的向量块，距离太远就丢。理由见那个常量的注释。
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
    ranked = sorted(merged.values(), key=lambda c: c.score, reverse=True)
    return [c for c in ranked if _passes_strict_gate(c)]


def _passes_strict_gate(candidate: _Candidate) -> bool:
    """严门槛：这条块留下来吗？

    留下当且仅当满足其一：

    1. 关键词路也捞到了它（精确术语命中）—— 距离再远也是有人佐证的；
    2. 它只走了关键词路（``similarity`` 是 None，向量路没给它打分）；
    3. 它离得够近（余弦距离 ≤ ``STRICT_VECTOR_DISTANCE``）。

    反过来：**只有向量路捞到、距离又超过严门槛**的块，丢掉 ——
    那就是"向量检索硬凑出来的"，正是 2B 报告 §5.1 说的硬凑。
    """
    if candidate.fts_rank is not None:
        return True
    if candidate.similarity is None:
        return True
    return (1.0 - candidate.similarity) <= STRICT_VECTOR_DISTANCE


async def search(
    query: str,
    *,
    top_k: int = 8,
    candidate_k: int = 30,
    section_code: str | None = None,
    use_vector: bool = True,
    kind: str = "child",
    keyword_backend: str | None = None,
) -> list[SearchHit]:
    """检索。返回按 RRF 分数排好的结果。

    ``kind`` 默认只回子块：子块是召回单元，父块是它的上下文。父块和它的
    子块同时出现在结果里，等于同一段话显示两遍。

    ``keyword_backend`` 显式传时覆盖配置里的后端名。
    """
    # 查询改写只做一次：语义路用原话，关键词路用桥接后的
    from rdi_agent.rag.query_rewrite import rewrite_query

    queries = await rewrite_query(query)
    vector_hits: list[_Candidate] = []
    if use_vector:
        query_vector = await embeddings.embed_query(queries.semantic_query)
        vector_hits = await _vector_candidates(query_vector, candidate_k, section_code)
    fts_hits = await _fts_candidates(
        queries.keyword_query,
        candidate_k,
        section_code,
        backend_name=keyword_backend,
    )

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
            similarity=c.similarity,
        )
        for c in merged
    ]




async def load_candidates_by_ids(chunk_ids: list[str]) -> list[_Candidate]:
    """按 id 把候选重新读出来（带完整原文）。

    **为什么要这么绕**：图里 ``retrieve_knowledge`` 节点往 state 里只放
    精简候选（chunk_id + 分数 + 页码 + 一小段摘要）—— 计划 §4.2 明确说
    "原文片段只放 evidence_id 或摘要，避免 State 膨胀和日志泄露儿童信息"。
    但下一棒 ``rerank_and_expand`` 需要完整片段来重排、需要父块来引用。

    所以这里按 id 回库取全文。id 索引，很快，也不违反"state 里不放原文"的约束。
    """
    if not chunk_ids:
        return []
    sql = (
        SELECT_COLUMNS
        + """
        WHERE c.id = ANY(CAST(:ids AS uuid[]))
        """
    )
    async with session_scope() as session:
        rows = (
            await session.execute(text(sql), {"ids": list(chunk_ids)})
        ).mappings().all()
    return [_row_to_candidate(row) for row in rows]


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
    parser.add_argument(
        "--keyword-backend",
        default=None,
        help=(
            "关键词路的后端（默认取配置 settings.keyword_backend）。"
            f"可选：{'、'.join(keyword_backends.available_backends())}"
        ),
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
            keyword_backend=args.keyword_backend,
        )
    )
    if args.json:
        print(json.dumps([h.to_dict() for h in hits], ensure_ascii=False, indent=2))
    else:
        _print_hits(args.query, hits)


if __name__ == "__main__":
    main()
