"""检索管线：查询改写 -> 双路召回 -> RRF -> 重排 -> 父块扩展 -> 证据包。

对应计划 §5.4 的 ``retrieve_evidence``。

    from rdi_agent.rag.retrieval import retrieve_evidence
    bundle = await retrieve_evidence("孩子不敢跟人打招呼")

**每一步都可单独跳过并观察**（评测集要靠这个定位"是哪一步出的问题"）：

- 关向量：``use_vector=False``（看关键词路单独能召回多少）
- 关重排：``use_rerank=False``（看 RRF 单独排得怎么样）
- 降级可见：``EvidenceBundle.retrieval_version`` 和每条证据的
  ``relevance_reason`` 都会写明当前是否降级

**过滤条件**（计划 §5.4 明确要求）至少包含 ``source_id``、
``review_status != needs_correction``、``rights_status``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text

from rdi_agent.db.engine import session_scope
from rdi_agent.rag import rerank as rerank_mod
from rdi_agent.rag import search as search_mod
from rdi_agent.rag.ingest import embeddings
from rdi_agent.rag.query_rewrite import RetrievalQueries, rewrite_query
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem

# 检索管线版本。改了什么就改它 —— 评测报告靠它区分"结果变了是因为
# 改了检索逻辑，还是因为重新入库了"。
#
# 2c-v1：加了向量距离下限 + 排除参考文献/脚注（见 search.py），
#         并把管线拆成 recall_candidates / build_evidence_bundle。
RETRIEVAL_VERSION = "2c-v1"

# 计划 §5.4 的初始配置
DEFAULT_CANDIDATE_K = 30
DEFAULT_RERANK_CANDIDATES = 20
DEFAULT_FINAL_EVIDENCE = 8


@dataclass
class RecallOutcome:
    """双路召回的结果（未重排）。

    图里 ``retrieve_knowledge`` 节点用它，``rerank_and_expand`` 节点接着处理。
    """

    vector_candidates: list[search_mod._Candidate] = field(default_factory=list)
    fts_candidates: list[search_mod._Candidate] = field(default_factory=list)
    merged: list[search_mod._Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class RetrievalOutcome:
    """一次检索的完整过程记录。

    评测要能回答"为什么这条排第 7"，所以过程全留痕。
    """

    queries: RetrievalQueries
    hits: list[search_mod.SearchHit]
    rerank_degraded: bool = False
    rerank_reason: str = ""
    parent_expanded: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queries": self.queries.to_dict(),
            "hits": [h.to_dict() for h in self.hits],
            "rerank_degraded": self.rerank_degraded,
            "rerank_reason": self.rerank_reason,
            "parent_expanded": self.parent_expanded,
            "notes": self.notes,
        }


async def _load_parents(chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    """按命中子块取父块内容（1 层，不无限扩散）。

    父块是"可读性"：命中子块是半句话，父块才是完整语义，
    引用给家长看时要引父块。
    """
    if not chunk_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT p.id::text AS id,
                           p.content_original,
                           p.pdf_page_start,
                           p.pdf_page_end,
                           c.id::text AS child_id
                    FROM knowledge_chunks c
                    JOIN knowledge_chunks p ON p.id = c.parent_chunk_id
                    WHERE c.id = ANY(CAST(:ids AS uuid[]))
                    """
                ),
                {"ids": list(chunk_ids)},
            )
        ).mappings().all()
    return {
        row["child_id"]: {
            "parent_id": row["id"],
            "content": row["content_original"],
            "pdf_page_start": row["pdf_page_start"],
            "pdf_page_end": row["pdf_page_end"],
        }
        for row in rows
    }


def _relevance_reason(
    hit: search_mod.SearchHit,
    queries: RetrievalQueries,
    *,
    rerank_score: float | None,
    degraded: bool,
) -> str:
    """给证据写一句"为什么相关"。

    计划 §9.2 验收："每条证据都有 ``relevance_reason``，
    不能只有相似度分数"。所以要说清**哪一路召回的、桥到了什么词**。
    """
    parts: list[str] = []
    if hit.vector_rank is not None:
        parts.append(f"向量召回第 {hit.vector_rank} 名")
    if hit.fts_rank is not None:
        parts.append(f"关键词召回第 {hit.fts_rank} 名")
    if not parts:
        parts.append("召回来源不明")
    if hit.similarity is not None:
        parts.append(f"相似度 {hit.similarity:.4f}")
    if rerank_score is not None:
        parts.append(f"重排分 {rerank_score:.4f}")
    elif degraded:
        parts.append("重排已降级，未参与排序")
    if queries.bridged_terms:
        bridged = "、".join(
            f"{b['matched_alias']}->{b['canonical_name']}"
            for b in queries.bridged_terms[:3]
        )
        parts.append(f"术语桥接 {bridged}")
    return "；".join(parts)


async def recall_candidates(
    queries: RetrievalQueries,
    *,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    use_vector: bool = True,
    kind: str = "child",
) -> RecallOutcome:
    """双路召回 + RRF 合并。

    图里拆成两个节点：``retrieve_knowledge`` 调这个，
    ``rerank_and_expand`` 接着处理 ``RecallOutcome.merged``。

    ``kind`` 默认只回子块：子块是召回单元，父块是它的上下文。
    父块和它的子块同时出现在结果里，等于同一段话显示两遍。
    """
    vector_hits: list[search_mod._Candidate] = []
    if use_vector:
        query_vector = await embeddings.embed_query(queries.semantic_query)
        vector_hits = await search_mod._vector_candidates(
            query_vector, candidate_k, None
        )
    await search_mod.ensure_high_freq_terms()
    fts_hits = await search_mod._fts_candidates(
        queries.keyword_query, candidate_k, None
    )

    merged = search_mod._rrf_merge(vector_hits, fts_hits)
    if kind == "child":
        merged = [c for c in merged if not c.is_parent]
    elif kind == "parent":
        merged = [c for c in merged if c.is_parent]

    notes = [
        f"向量路 {len(vector_hits)} 条，关键词路 {len(fts_hits)} 条",
        f"RRF 合并去重后 {len(merged)} 条",
    ]
    return RecallOutcome(
        vector_candidates=vector_hits,
        fts_candidates=fts_hits,
        merged=merged,
        notes=notes,
    )


@dataclass
class EvidenceBuildResult:
    """``build_evidence_bundle`` 的返回值。

    证据包本身是给下游用的；hits / 重排状态是给评测和 trace 用的。
    """

    bundle: EvidenceBundle
    hits: list[search_mod.SearchHit] = field(default_factory=list)
    rerank_degraded: bool = False
    rerank_reason: str = ""
    parent_expanded: int = 0
    notes: list[str] = field(default_factory=list)


async def build_evidence_bundle(
    candidates: list[search_mod._Candidate],
    queries: RetrievalQueries,
    *,
    original_question: str,
    case_summary: dict[str, Any] | None = None,
    top_k: int = DEFAULT_FINAL_EVIDENCE,
    use_rerank: bool = True,
    expand_parents: bool = True,
) -> EvidenceBuildResult:
    """重排 -> 截断 -> 父块扩展 -> 组装证据包。

    每条证据都带 ``relevance_reason``、章节、PDF 页码。
    """
    notes: list[str] = []
    merged = list(candidates)

    # ---- 重排 ----
    rerank_degraded = False
    rerank_reason = ""
    rerank_scores: dict[str, float | None] = {}
    if use_rerank and merged:
        head = merged[:DEFAULT_RERANK_CANDIDATES]
        tail = merged[DEFAULT_RERANK_CANDIDATES:]
        result = await rerank_mod.rerank(
            queries.original_query,
            [c.snippet for c in head],
        )
        rerank_degraded = result.degraded
        rerank_reason = result.reason
        for pos, src_idx in enumerate(result.order):
            if src_idx < len(head):
                rerank_scores[head[src_idx].chunk_id] = result.scores[src_idx]
        reordered = [head[i] for i in result.order]
        merged = reordered + tail
        notes.append(
            f"重排 {len(head)} 条"
            + ("（已降级：%s）" % result.reason if result.degraded else "")
        )
    else:
        notes.append("未启用重排")

    merged = merged[:top_k]

    # ---- 组装成 SearchHit ----
    hits: list[search_mod.SearchHit] = []
    for candidate in merged:
        hits.append(
            search_mod.SearchHit(
                chunk_id=candidate.chunk_id,
                chunk_index=candidate.chunk_index,
                section_code=candidate.section_code,
                section_title=candidate.section_title,
                pdf_page_start=candidate.pdf_page_start,
                pdf_page_end=candidate.pdf_page_end,
                snippet=candidate.snippet,
                score=round(candidate.score, 6),
                vector_rank=candidate.vector_rank,
                fts_rank=candidate.fts_rank,
                is_parent=candidate.is_parent,
                knowledge_type=candidate.knowledge_type,
                review_status=candidate.review_status,
                similarity=candidate.similarity,
            )
        )

    # ---- 父块扩展 ----
    parents: dict[str, dict[str, Any]] = {}
    if expand_parents:
        parents = await _load_parents([h.chunk_id for h in hits])
        notes.append(f"父块扩展命中 {len(parents)} 条")

    evidence: list[EvidenceItem] = []
    for hit in hits:
        parent = parents.get(hit.chunk_id)
        excerpt = parent["content"] if parent else hit.snippet
        page_start = parent["pdf_page_start"] if parent else hit.pdf_page_start
        page_end = parent["pdf_page_end"] if parent else hit.pdf_page_end
        evidence.append(
            EvidenceItem(
                evidence_id=hit.chunk_id,
                source_type="guideline",
                title=hit.section_title,
                section_title=hit.section_title,
                pdf_page_start=page_start,
                pdf_page_end=page_end,
                # print_page_* 一律空：calibre 重排时把原书页码丢了，猜不得
                print_page_start=None,
                print_page_end=None,
                summary=hit.snippet,
                source_excerpt=excerpt,
                relevance_reason=_relevance_reason(
                    hit,
                    queries,
                    rerank_score=rerank_scores.get(hit.chunk_id),
                    degraded=rerank_degraded,
                ),
                review_status=hit.review_status,
                # 版权未核实：只给摘要 + 页码，不给大段原文
                may_quote_to_user=False,
                similarity=hit.similarity,
                fts_matched=hit.fts_rank is not None,
            )
        )

    gaps: list[str] = []
    if not evidence:
        gaps.append("知识库中没有找到相关片段")
    if rerank_degraded:
        gaps.append(f"重排已降级：{rerank_reason}")

    bundle = EvidenceBundle(
        original_question=original_question,
        case_summary=case_summary or {},
        evidence=evidence,
        evidence_gaps=gaps,
        retrieval_version=RETRIEVAL_VERSION,
    )
    return EvidenceBuildResult(
        bundle=bundle,
        hits=hits,
        rerank_degraded=rerank_degraded,
        rerank_reason=rerank_reason,
        parent_expanded=len(parents),
        notes=notes,
    )


async def retrieve(
    query: str,
    *,
    source_id: str | None = None,
    top_k: int = DEFAULT_FINAL_EVIDENCE,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    use_vector: bool = True,
    use_rerank: bool = True,
    expand_parents: bool = True,
    kind: str = "child",
) -> RetrievalOutcome:
    """跑完整检索管线，返回命中 + 过程记录。

    这是 ``recall_candidates`` + ``build_evidence_bundle`` 的便捷串法。
    评测脚本和 CLI 用它；图里两个节点分别调那两个函数。
    """
    queries = await rewrite_query(query, source_id=source_id)
    recall = await recall_candidates(
        queries,
        candidate_k=candidate_k,
        use_vector=use_vector,
        kind=kind,
    )
    built = await build_evidence_bundle(
        recall.merged,
        queries,
        original_question=query,
        top_k=top_k,
        use_rerank=use_rerank,
        expand_parents=expand_parents,
    )
    return RetrievalOutcome(
        queries=queries,
        hits=built.hits,
        rerank_degraded=built.rerank_degraded,
        rerank_reason=built.rerank_reason,
        parent_expanded=built.parent_expanded,
        notes=recall.notes + built.notes,
    )


async def retrieve_evidence(
    query: str,
    *,
    source_id: str | None = None,
    case_summary: dict[str, Any] | None = None,
    top_k: int = DEFAULT_FINAL_EVIDENCE,
    use_vector: bool = True,
    use_rerank: bool = True,
    expand_parents: bool = True,
) -> EvidenceBundle:
    """跑检索并组装成证据包。

    每条证据都带 ``relevance_reason``、章节、PDF 页码。
    """
    queries = await rewrite_query(query, source_id=source_id)
    recall = await recall_candidates(
        queries, use_vector=use_vector, kind="child"
    )
    built = await build_evidence_bundle(
        recall.merged,
        queries,
        original_question=query,
        case_summary=case_summary,
        top_k=top_k,
        use_rerank=use_rerank,
        expand_parents=expand_parents,
    )
    return built.bundle


async def main() -> None:
    parser = argparse.ArgumentParser(description="检索管线")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=DEFAULT_FINAL_EVIDENCE)
    parser.add_argument("--no-vector", action="store_true")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--no-parent", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    outcome = await retrieve(
        args.query,
        top_k=args.top_k,
        use_vector=not args.no_vector,
        use_rerank=not args.no_rerank,
        expand_parents=not args.no_parent,
    )

    if args.json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"查询：{outcome.queries.original_query}")
    print(f"关键词查询：{outcome.queries.keyword_query}")
    print(f"桥接术语：{[b['canonical_name'] for b in outcome.queries.bridged_terms]}")
    for note in outcome.notes:
        print(f"  · {note}")
    print()
    for i, hit in enumerate(outcome.hits, start=1):
        print(
            f"[{i}] {hit.section_title} (p{hit.pdf_page_start}-{hit.pdf_page_end})"
            f" 分数 {hit.score:.5f}"
            f" 向量 {hit.vector_rank or '-'} / 关键词 {hit.fts_rank or '-'}"
        )
        print(f"    {hit.snippet[:160]}")


if __name__ == "__main__":
    asyncio.run(main())
