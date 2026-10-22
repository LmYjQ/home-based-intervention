"""检索评测：跑一遍评测集，输出指标 + 每个 case 的每一步输入输出。

    python -m evals.run_retrieval                    # 人读的报告
    python -m evals.run_retrieval --json out.json    # 机读的完整记录
    python -m evals.run_retrieval --no-rerank        # 关掉重排（对比用）

**报告要能回答"某条为什么排第 7"**，所以每一步的输入输出全留：

    1. 查询改写    → 语义查询 / 关键词查询 / 桥接到的术语
    2. 向量召回    → 命中数 + 前 N 条（名次、页码、片段）
    3. 关键词召回  → 同上
    4. RRF 合并    → 去重后条数 + 前 N 条（两路名次都在）
    5. 重排        → 是否降级 + 降级原因 + 重排后顺序
    6. 父块扩展    → 命中数
    7. 最终证据    → 每条证据的页码、章节、relevance_reason

**指标定义**（写清楚，免得看报告的人自己脑补）：

- ``Recall@5``：前 5 条里**至少有一条**落在期望章节内的比例。
  按章节判而不是按页码 —— 这本书讲一个主题会散在好几章
  （比如"引导者该等多久"横跨 Readiness / Guide / Guiding），
  按页码判会把正确的��果判成错。
- ``MRR``：第一条正确结果排名的倒数，取平均。1.0 = 全部排第一。
- ``页码可定位率``：返回的证据里带 PDF 页码的比例。验收要求 100%。
- ``无命中率**：一条都没召回的比例（过高说明检索不行）。
- ``硬凑率**：无关问题返回了证据的比例。**必须接近 0** ——
  宁可说"书里没有"，也不能给一段不相干的话。

**注意**：默认**关掉重排**。重排要把书里片段发给外部 API，
离线跑评测会重复上传同一批内容；而且重排的收益要单独测
（``--rerank`` 打开，报告会标出用的是哪条路径）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rdi_agent.rag import rerank as rerank_mod  # noqa: E402
from rdi_agent.rag import search as search_mod  # noqa: E402
from rdi_agent.rag.ingest import embeddings  # noqa: E402
from rdi_agent.rag.query_rewrite import rewrite_query  # noqa: E402

CASES_PATH = Path(__file__).parent / "retrieval_cases.jsonl"
SNIPPET_CHARS = 130


@dataclass
class CaseResult:
    case_id: str
    query: str
    category: str
    expected_sections: list[str]
    must_include_terms: list[str]
    notes: str
    # ---- 每一步的输入输出 ----
    step_rewrite: dict[str, Any] = field(default_factory=dict)
    step_vector: dict[str, Any] = field(default_factory=dict)
    step_fts: dict[str, Any] = field(default_factory=dict)
    step_rrf: dict[str, Any] = field(default_factory=dict)
    step_rerank: dict[str, Any] = field(default_factory=dict)
    step_parent: dict[str, Any] = field(default_factory=dict)
    step_final: list[dict[str, Any]] = field(default_factory=list)
    # ---- 指标 ----
    hit_at_5: bool = False
    first_hit_rank: int | None = None
    reciprocal_rank: float = 0.0
    has_page: bool = True
    term_hit: bool = False
    no_hit: bool = False
    is_irrelevant: bool = False
    forced_answer: bool = False  # 无关问题却返回了证据
    error: str = ""


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _cand_to_dict(c: search_mod._Candidate) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "chunk_index": c.chunk_index,
        "section_code": c.section_code,
        "section_title": c.section_title,
        "pdf_pages": [c.pdf_page_start, c.pdf_page_end],
        "vector_rank": c.vector_rank,
        "fts_rank": c.fts_rank,
        "score": round(c.score, 6),
        "snippet": c.snippet[:SNIPPET_CHARS],
    }


async def run_case(
    case: dict[str, Any],
    *,
    top_k: int,
    candidate_k: int,
    use_vector: bool,
    use_rerank: bool,
    expand_parents: bool,
) -> CaseResult:
    result = CaseResult(
        case_id=case["case_id"],
        query=case["query"],
        category=case.get("category", ""),
        expected_sections=case.get("expected_sections", []),
        must_include_terms=case.get("must_include_terms", []),
        notes=case.get("notes", ""),
    )
    result.is_irrelevant = not result.expected_sections

    try:
        # ---- 1. 查询改写 ----
        queries = await rewrite_query(case["query"])
        result.step_rewrite = {
            "input": case["query"],
            "output": {
                "semantic_query": queries.semantic_query,
                "keyword_query": queries.keyword_query,
                "concept_ids": queries.concept_ids,
                "bridged_terms": [
                    f"{b['matched_alias']} -> {b['canonical_name_en']}"
                    for b in queries.bridged_terms
                ],
            },
        }

        # ---- 2. 向量召回 ----
        vector_cands: list[search_mod._Candidate] = []
        if use_vector:
            qv = await embeddings.embed_query(queries.semantic_query)
            vector_cands = await search_mod._vector_candidates(qv, candidate_k, None)
        result.step_vector = {
            "enabled": use_vector,
            "input": {"semantic_query": queries.semantic_query, "top_k": candidate_k},
            "output": {
                "count": len(vector_cands),
                "top": [_cand_to_dict(c) for c in vector_cands[:5]],
            },
        }

        # ---- 3. 关键词召回 ----
        fts_cands = await search_mod._fts_candidates(queries.keyword_query, candidate_k, None)
        result.step_fts = {
            "input": {"keyword_query": queries.keyword_query, "top_k": candidate_k},
            "output": {
                "count": len(fts_cands),
                "top": [_cand_to_dict(c) for c in fts_cands[:5]],
            },
        }

        # ---- 4. RRF 合并 ----
        merged = search_mod._rrf_merge(vector_cands, fts_cands)
        merged = [c for c in merged if not c.is_parent]
        result.step_rrf = {
            "input": {
                "vector_count": len(vector_cands),
                "fts_count": len(fts_cands),
                "formula": "score = sum(1 / (60 + rank))",
            },
            "output": {
                "merged_count": len(merged),
                "top": [_cand_to_dict(c) for c in merged[:5]],
            },
        }

        # ---- 5. 重排 ----
        rerank_scores: dict[str, float] = {}
        if use_rerank and merged:
            head = merged[:20]
            tail = merged[20:]
            rr = await rerank_mod.rerank(
                queries.original_query, [c.snippet for c in head]
            )
            for pos, src_idx in enumerate(rr.order):
                if src_idx < len(head) and rr.scores[src_idx] is not None:
                    rerank_scores[head[src_idx].chunk_id] = rr.scores[src_idx]
            before = [_cand_to_dict(c) for c in head[:5]]
            merged = [head[i] for i in rr.order] + tail
            result.step_rerank = {
                "enabled": True,
                "input": {"query": queries.original_query, "candidates": len(head)},
                "output": {
                    "degraded": rr.degraded,
                    "reason": rr.reason,
                    "before": before,
                    "after": [_cand_to_dict(c) for c in merged[:5]],
                },
            }
        else:
            result.step_rerank = {
                "enabled": False,
                "input": {"query": queries.original_query},
                "output": {
                    "degraded": False,
                    "reason": "本轮评测未启用重排（--no-rerank）",
                    "after": [_cand_to_dict(c) for c in merged[:5]],
                },
            }

        # ---- 6. 父块扩展 ----
        final = merged[:top_k]
        parents: dict[str, dict[str, Any]] = {}
        if expand_parents:
            from rdi_agent.rag.retrieval import _load_parents

            parents = await _load_parents([c.chunk_id for c in final])
        result.step_parent = {
            "enabled": expand_parents,
            "input": {"child_chunk_ids": [c.chunk_id for c in final]},
            "output": {
                "expanded": len(parents),
                "parents": [
                    {
                        "child_chunk_index": next(
                            (c.chunk_index for c in final if c.chunk_id == cid), None
                        ),
                        "pdf_pages": [p["pdf_page_start"], p["pdf_page_end"]],
                        "parent_len": len(p["content"]),
                    }
                    for cid, p in parents.items()
                ],
            },
        }

        # ---- 7. 最终证据 ----
        evidence: list[dict[str, Any]] = []
        for rank, c in enumerate(final, start=1):
            parent = parents.get(c.chunk_id)
            pages = (
                [parent["pdf_page_start"], parent["pdf_page_end"]]
                if parent
                else [c.pdf_page_start, c.pdf_page_end]
            )
            evidence.append(
                {
                    "rank": rank,
                    "chunk_index": c.chunk_index,
                    "section_code": c.section_code,
                    "section_title": c.section_title,
                    "pdf_pages": pages,
                    "has_page": pages[0] is not None,
                    "vector_rank": c.vector_rank,
                    "fts_rank": c.fts_rank,
                    "rrf_score": round(c.score, 6),
                    "rerank_score": rerank_scores.get(c.chunk_id),
                    "review_status": c.review_status,
                    "snippet": c.snippet[:SNIPPET_CHARS],
                    "relevance_reason": (
                        f"向量第{c.vector_rank}名"
                        if c.vector_rank is not None
                        else ""
                    )
                    + (
                        f" / 关键词第{c.fts_rank}名" if c.fts_rank is not None else ""
                    ),
                }
            )
        result.step_final = evidence

        # ---- 指标 ----
        expected = set(result.expected_sections)
        for rank, item in enumerate(evidence, start=1):
            if item["section_code"] in expected:
                if result.first_hit_rank is None:
                    result.first_hit_rank = rank
                if rank <= 5:
                    result.hit_at_5 = True
        result.reciprocal_rank = (
            1.0 / result.first_hit_rank if result.first_hit_rank else 0.0
        )
        result.has_page = all(item["has_page"] for item in evidence) if evidence else True
        result.no_hit = not evidence
        if result.is_irrelevant and evidence:
            result.forced_answer = True
        if result.must_include_terms:
            blob = " ".join(item["snippet"].lower() for item in evidence)
            result.term_hit = any(t.lower() in blob for t in result.must_include_terms)

    except Exception as e:  # noqa: BLE001 - 评测里要看到失败，不要整批挂掉
        result.error = f"{type(e).__name__}: {e}"

    return result


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    relevant = [r for r in results if not r.is_irrelevant]
    irrelevant = [r for r in results if r.is_irrelevant]
    errored = [r for r in results if r.error]

    def pct(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    return {
        "total": len(results),
        "relevant": len(relevant),
        "irrelevant": len(irrelevant),
        "errored": len(errored),
        "recall_at_5": pct(sum(1 for r in relevant if r.hit_at_5), len(relevant)),
        "mrr": round(sum(r.reciprocal_rank for r in relevant) / len(relevant), 4)
        if relevant
        else 0.0,
        "page_locatable_rate": pct(
            sum(1 for r in relevant if r.has_page), len(relevant)
        ),
        "no_hit_rate": pct(sum(1 for r in relevant if r.no_hit), len(relevant)),
        "term_hit_rate": pct(sum(1 for r in relevant if r.term_hit), len(relevant)),
        "forced_answer_rate": pct(
            sum(1 for r in irrelevant if r.forced_answer), len(irrelevant)
        ),
    }


def print_report(results: list[CaseResult], summary: dict[str, Any]) -> None:
    print("=" * 78)
    print("检索评测报告")
    print("=" * 78)
    print(
        f"用例 {summary['total']} 条（相关 {summary['relevant']} / 无关 {summary['irrelevant']}）"
        f"　错误 {summary['errored']}"
    )
    print()
    print(f"  Recall@5        {summary['recall_at_5']:.2%}   (目标 >= 0.80)")
    print(f"  MRR             {summary['mrr']:.4f}")
    print(f"  页码可定位率    {summary['page_locatable_rate']:.2%}   (目标 100%)")
    print(f"  无命中率        {summary['no_hit_rate']:.2%}")
    print(f"  必含术语命中率  {summary['term_hit_rate']:.2%}")
    print(f"  硬凑率(无关题)  {summary['forced_answer_rate']:.2%}   (目标接近 0)")
    print()

    for r in results:
        mark = "OK " if r.hit_at_5 else ("-- " if r.is_irrelevant else "MISS")
        print("-" * 78)
        print(f"[{mark}] {r.case_id}  ({r.category})")
        print(f"  查询：{r.query}")
        if r.error:
            print(f"  !! 错误：{r.error}")
            continue

        rw = r.step_rewrite["output"]
        print(f"  [1] 改写   语义={rw['semantic_query']}")
        print(f"              关键词={rw['keyword_query']}")
        print(
            f"              桥接={rw['bridged_terms'] or '无'}"
            f"  概念={rw['concept_ids'] or '无'}"
        )

        v = r.step_vector["output"]
        print(f"  [2] 向量   {v['count']} 条" + ("" if r.step_vector["enabled"] else "（已关闭）"))
        for i, c in enumerate(v["top"][:3], 1):
            print(f"      {i}. p{c['pdf_pages'][0]} [{c['section_code'][:26]}] {c['snippet'][:60]}")

        f = r.step_fts["output"]
        print(f"  [3] 关键词 {f['count']} 条")
        for i, c in enumerate(f["top"][:3], 1):
            print(f"      {i}. p{c['pdf_pages'][0]} [{c['section_code'][:26]}] {c['snippet'][:60]}")

        m = r.step_rrf["output"]
        print(f"  [4] RRF    合并去重后 {m['merged_count']} 条")
        for i, c in enumerate(m["top"][:3], 1):
            print(
                f"      {i}. score={c['score']:.5f} 向量{c['vector_rank'] or '-'}"
                f"/关键词{c['fts_rank'] or '-'} p{c['pdf_pages'][0]} {c['snippet'][:50]}"
            )

        rr = r.step_rerank["output"]
        print(
            f"  [5] 重排   {'已降级: ' + rr['reason'] if rr.get('degraded') else '正常'}"
            if r.step_rerank.get("enabled")
            else f"  [5] 重排   未启用（{rr.get('reason', '')}）"
        )

        p = r.step_parent["output"]
        print(f"  [6] 父块   扩展 {p['expanded']} 条")

        print(f"  [7] 证据   {len(r.step_final)} 条")
        for e in r.step_final:
            flag = "*" if e["section_code"] in r.expected_sections else " "
            print(
                f"      {flag}{e['rank']}. p{e['pdf_pages'][0]} "
                f"[{e['section_code'][:26]}] {e['relevance_reason']}"
            )

        if r.is_irrelevant:
            print(
                f"  判定：无关问题 —— "
                + ("!! 硬凑了结果（应该返回空）" if r.forced_answer else "正确地没有硬凑")
            )
        else:
            print(
                f"  判定：{'命中' if r.hit_at_5 else '未命中'}"
                f"　首个命中排名={r.first_hit_rank or '-'}"
                f"　RR={r.reciprocal_rank:.3f}"
                f"　期望章节={r.expected_sections}"
            )
    print("=" * 78)


async def main() -> None:
    parser = argparse.ArgumentParser(description="检索评测")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=30)
    parser.add_argument("--no-vector", action="store_true")
    parser.add_argument("--no-rerank", action="store_true", default=True)
    parser.add_argument("--rerank", action="store_true", help="打开重排")
    parser.add_argument("--no-parent", action="store_true")
    parser.add_argument("--json", default=None, help="把完整记录写成 JSON")
    parser.add_argument("--only", default=None, help="只跑某个 case_id")
    args = parser.parse_args()

    use_rerank = args.rerank and not args.no_rerank

    cases = load_cases(Path(args.cases))
    if args.only:
        cases = [c for c in cases if c["case_id"] == args.only]
        if not cases:
            print(f"没有 case_id={args.only} 的用例")
            return

    print(
        f"配置：top_k={args.top_k} candidate_k={args.candidate_k} "
        f"vector={not args.no_vector} rerank={use_rerank} parent={not args.no_parent}",
        file=sys.stderr,
    )

    results: list[CaseResult] = []
    for case in cases:
        results.append(
            await run_case(
                case,
                top_k=args.top_k,
                candidate_k=args.candidate_k,
                use_vector=not args.no_vector,
                use_rerank=use_rerank,
                expand_parents=not args.no_parent,
            )
        )

    summary = summarize(results)
    print_report(results, summary)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "config": {
                        "top_k": args.top_k,
                        "candidate_k": args.candidate_k,
                        "use_vector": not args.no_vector,
                        "use_rerank": use_rerank,
                        "expand_parents": not args.no_parent,
                    },
                    "summary": summary,
                    "cases": [r.__dict__ for r in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n完整记录：{args.json}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
