"""跑通全链路，产出一份**人工可评估**的执行日志。

    python -m tools.run_log

**这个工具做什么**：把"入库 → 检索 → 评测"整条链路跑一遍，把每一步的
输入输出落成一份 Markdown + JSON，人（不是指标）来判断质量。

**为什么要有它**：检索质量这件事，Recall@5 这种数字只能给出方向，
给不出"家长到底会看到什么"。本工具把**每条用例的完整检索轨迹**
（改写 → 向量召回 → 关键词召回 → RRF → 重排 → 最终证据的正文和页码）
原样写出来，人可以直接读"这条证据该不该给家长看"。

**落盘位置**：

- ``data/index/exec_log.md``   —— 人读的（带正文片段）
- ``data/index/exec_log.json`` —— 机读的（全量，含每一步）

**不外发**：这个工具只查库、只做检索和评测。
入库那一步需要向量化（要外发），所以**不在这里跑** ——
入库单独用 ``python -m rdi_agent.rag.ingest.pipeline``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from evals import run_retrieval  # noqa: E402
from rdi_agent.db.engine import session_scope  # noqa: E402

LOG_DIR = Path("data/index")
MD_PATH = LOG_DIR / "exec_log.md"
JSON_PATH = LOG_DIR / "exec_log.json"

# 评测用例的正文片段截多长 —— 人工评估要看得出"这段讲的是什么"，
# 130 字（评测里的 SNIPPET_CHARS）只够看个开头。
LOG_SNIPPET_CHARS = 220


async def collect_db_state() -> dict[str, Any]:
    """库里的实际状态 —— 人要先确认"库是对的"，再看检索。"""
    async with session_scope() as session:
        sources = (
            await session.execute(
                text(
                    """
                    SELECT k.source_id, k.title, k.language, k.version, k.rights_status,
                           k.publication_year, k.authors_json
                    FROM knowledge_sources k
                    ORDER BY k.source_id
                    """
                )
            )
        ).mappings().all()
        sections = (
            await session.execute(
                text(
                    """
                    SELECT s.section_code, s.title_original, s.pdf_page_start, s.pdf_page_end,
                           count(c.id) AS chunks
                    FROM book_sections s
                    LEFT JOIN knowledge_chunks c ON c.section_id = s.id
                    GROUP BY s.id, s.section_code, s.title_original,
                             s.pdf_page_start, s.pdf_page_end
                    ORDER BY s.section_code
                    """
                )
            )
        ).mappings().all()
        counts = (
            await session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM knowledge_chunks) AS chunks,
                      (SELECT count(*) FROM knowledge_chunks WHERE parent_chunk_id IS NULL) AS parents,
                      (SELECT count(*) FROM knowledge_chunks WHERE parent_chunk_id IS NOT NULL) AS children,
                      (SELECT count(*) FROM knowledge_chunks WHERE embedding IS NOT NULL) AS embedded,
                      (SELECT count(*) FROM concept_aliases) AS concepts,
                      (SELECT count(*) FROM concept_aliases WHERE is_explicit_in_source) AS concepts_explicit
                    """
                )
            )
        ).mappings().one()
        # 缺向量的块 —— 必须是 0，否则检索会静默漏召回
        missing = (
            await session.execute(
                text(
                    """
                    SELECT c.chunk_index, s.section_code
                    FROM knowledge_chunks c
                    JOIN book_sections s ON s.id = c.section_id
                    WHERE c.embedding IS NULL
                    ORDER BY c.chunk_index
                    """
                )
            )
        ).mappings().all()

    return {
        "sources": [dict(r) for r in sources],
        "sections": [dict(r) for r in sections],
        "counts": dict(counts),
        "chunks_missing_embedding": [dict(r) for r in missing],
    }


def _fmt_sources(db: dict[str, Any]) -> list[str]:
    lines = ["## 1. 知识库状态", ""]
    lines.append("| source_id | 标题 | 语言 | 版本 | 授权 | 章节 | 块 | 父/子 | 已向量化 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in db["sources"]:
        c = db["counts"]
        lines.append(
            f"| `{s['source_id']}` | {s['title']} | {s['language']} | {s['version']} "
            f"| `{s['rights_status']}` | {len(db['sections'])} "
            f"| {c['chunks']} | {c['parents']}/{c['children']} | {c['embedded']} |"
        )
    lines.append("")
    if db["chunks_missing_embedding"]:
        lines.append(
            f"> ⚠️ 有 {len(db['chunks_missing_embedding'])} 个块没有向量："
            f"{[m['chunk_index'] for m in db['chunks_missing_embedding']]}"
        )
        lines.append("> 缺向量的块**召不回**（向量路直接跳过），必须补齐。")
    else:
        lines.append("> ✅ 所有块都有向量。")
    lines.append("")
    lines.append(f"中文术语表：{db['counts']['concepts']} 条，"
                 f"其中 {db['counts']['concepts_explicit']} 条在指南正文里出现过。")
    lines.append("")
    return lines


def _fmt_sections(db: dict[str, Any]) -> list[str]:
    lines = ["## 2. 章节结构", ""]
    lines.append("| 章节码 | 标题 | 页码 | 块数 |")
    lines.append("|---|---|---|---|")
    for s in db["sections"]:
        lines.append(
            f"| `{s['section_code']}` | {s['title_original']} "
            f"| p{s['pdf_page_start']}–{s['pdf_page_end']} | {s['chunks']} |"
        )
    lines.append("")
    return lines


def _fmt_case(r: run_retrieval.CaseResult) -> list[str]:
    lines: list[str] = []
    if r.is_irrelevant:
        mark = "无关题" if not r.forced_answer else "❌ 硬凑了"
    else:
        mark = "命中" if r.hit_at_5 else "❌ 未命中"
    lines.append(f"### [{mark}] `{r.case_id}` — {r.category}")
    lines.append("")
    lines.append(f"- **查询**：{r.query}")
    lines.append(f"- **期望章节**：{', '.join(f'`{s}`' for s in r.expected_sections) or '（无关题）'}")
    if r.must_include_terms:
        lines.append(f"- **必含术语**：{', '.join(r.must_include_terms)}")
    if r.notes:
        lines.append(f"- **备注**：{r.notes}")
    if r.error:
        lines.append(f"- ❌ **错误**：{r.error}")
        lines.append("")
        return lines

    rw = r.step_rewrite["output"]
    lines.append(f"- **关键词查询**：`{rw['keyword_query']}`")
    lines.append(f"- **术语桥接**：{', '.join(rw['bridged_terms']) or '无'}")
    lines.append(f"- **首个命中排名**：{r.first_hit_rank or '—'}　"
                 f"**RR**：{r.reciprocal_rank:.3f}")
    lines.append("")

    lines.append("**召回的证据（这就是家长会看到的）：**")
    lines.append("")
    if not r.step_final:
        lines.append("（空）")
    else:
        for e in r.step_final:
            pages = e["pdf_pages"]
            pages_txt = (
                f"p{pages[0]}–{pages[1]}" if pages[0] is not None else "无页码"
            )
            src = []
            if e["vector_rank"] is not None:
                src.append(f"向量第{e['vector_rank']}名")
            if e["fts_rank"] is not None:
                src.append(f"关键词第{e['fts_rank']}名")
            if e["similarity"] is not None:
                src.append(f"相似度 {e['similarity']:.4f}")
            usable = "可作依据" if e["usable"] else "仅参考"
            lines.append(
                f"{e['rank']}. **{e['section_code']}** {e['section_title']} "
                f"（{pages_txt}）　`{usable}`　{' / '.join(src)}"
            )
            lines.append("")
            lines.append(f"   > {e['snippet'][:LOG_SNIPPET_CHARS]}")
            lines.append("")
    lines.append("")
    return lines


async def run(cases_path: str, use_rerank: bool) -> dict[str, Any]:
    cases = run_retrieval.load_cases(Path(cases_path))
    results: list[run_retrieval.CaseResult] = []
    for case in cases:
        results.append(
            await run_retrieval.run_case(
                case,
                top_k=5,
                candidate_k=30,
                use_vector=True,
                use_rerank=use_rerank,
                expand_parents=True,
            )
        )
    summary = run_retrieval.summarize(results)
    return {"summary": summary, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="跑通全链路并产出执行日志")
    parser.add_argument("--cases", default=str(run_retrieval.CASES_PATH))
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="打开重排（要发外部 API；默认关掉）",
    )
    args = parser.parse_args()

    started = datetime.now().astimezone()

    print("1/3 读库状态…", file=sys.stderr)
    db = asyncio.run(collect_db_state())

    print("2/3 跑检索评测…", file=sys.stderr)
    outcome = asyncio.run(run(args.cases, args.rerank))
    results = outcome["results"]
    summary = outcome["summary"]

    print("3/3 写日志…", file=sys.stderr)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": started.isoformat(timespec="seconds"),
        "rerank_enabled": args.rerank,
        "db": db,
        "summary": summary,
        "cases": [
            {
                "case_id": r.case_id,
                "query": r.query,
                "category": r.category,
                "expected_sections": r.expected_sections,
                "must_include_terms": r.must_include_terms,
                "notes": r.notes,
                "hit_at_5": r.hit_at_5,
                "first_hit_rank": r.first_hit_rank,
                "reciprocal_rank": r.reciprocal_rank,
                "term_hit": r.term_hit,
                "is_irrelevant": r.is_irrelevant,
                "forced_answer": r.forced_answer,
                "forced_answer_usable": r.forced_answer_usable,
                "error": r.error,
                "step_rewrite": r.step_rewrite,
                "step_vector": r.step_vector,
                "step_fts": r.step_fts,
                "step_rrf": r.step_rrf,
                "step_rerank": r.step_rerank,
                "step_final": r.step_final,
            }
            for r in results
        ],
    }
    JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md: list[str] = []
    md.append("# 检索执行日志（人工评估用）")
    md.append("")
    md.append(f"- 生成时间：{started.isoformat(timespec='seconds')}")
    md.append(f"- 重排：**{'开启' if args.rerank else '关闭'}**")
    md.append(f"- 知识库：`guideline2026`（孤独症谱系障碍儿童干预康复指南 2026 年版）")
    md.append("")
    md.append("> 这份日志是给**人**看的：每条用例都列出了家长实际会看到的证据、"
              "它的章节、页码和正文片段。指标只是参考。")
    md.append("")
    md.append("## 0. 指标汇总")
    md.append("")
    md.append("| 指标 | 值 | 目标 |")
    md.append("|---|---|---|")
    md.append(f"| Recall@5 | {summary['recall_at_5']:.2%} | ≥ 0.80 |")
    md.append(f"| MRR | {summary['mrr']:.4f} | — |")
    md.append(f"| 页码可定位率 | {summary['page_locatable_rate']:.2%} | 100% |")
    md.append(f"| 无命中率 | {summary['no_hit_rate']:.2%} | — |")
    md.append(f"| 必含术语命中率 | {summary['term_hit_rate']:.2%} | — |")
    md.append(f"| 硬凑率（无关题） | {summary['forced_answer_rate']:.2%} | ≈ 0 |")
    md.append(f"| 硬凑率（可作依据） | {summary.get('forced_answer_usable_rate', 0):.2%} | 0 |")
    md.append(f"| 证据可用率 | {summary.get('usable_evidence_rate', 0):.2%} | — |")
    md.append("")
    md.extend(_fmt_sources(db))
    md.extend(_fmt_sections(db))
    md.append("## 3. 逐条用例")
    md.append("")
    md.append("> 每条下面的「召回的证据」就是家长会看到的内容。"
              "请判断：这段讲的是不是家长问的那件事？页码对不对？")
    md.append("")
    for r in results:
        md.extend(_fmt_case(r))

    MD_PATH.write_text("\n".join(md), encoding="utf-8")

    print(f"日志：{MD_PATH}")
    print(f"机读：{JSON_PATH}")
    print(
        f"Recall@5 {summary['recall_at_5']:.2%} | MRR {summary['mrr']:.4f} "
        f"| 硬凑率 {summary['forced_answer_rate']:.2%}"
    )


if __name__ == "__main__":
    main()
