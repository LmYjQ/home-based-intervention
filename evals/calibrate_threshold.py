"""相似度阈值标定（一次性工具，但保留在仓库里当回归基线）。

**为什么需要它**：2B 评测量出"硬凑率 100%"（问"红烧肉怎么做"也返回 5 条书里的段落）。
根因是向量路没有相似度下限 —— ``ORDER BY c.embedding <=> :qv`` 永远能排出个"最像的"，
哪怕完全不相关。2B 报告里只有 7 个查询点，样本太少，阈值定不下来。

这个脚本把 27 条评测用例全跑一遍，输出：

- 每条用例的 top-N 向量距离 + 章节 + 是否落在期望章节
- 相关用例：期望章节块的**最小距离**（这个距离以下的块被砍掉就伤召回）
- 无关用例：**最小距离**（这个距离以上的块留着就是硬凑）
- 一张阈值对照表：每个候选阈值会"砍掉几个期望块 / 拦下几个无关题"

    python -m evals.calibrate_threshold
    python -m evals.calibrate_threshold --top-n 30 --json out.json

**注意**：会外发查询到 embedding API（评测本来就这么干）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rdi_agent.db.engine import session_scope  # noqa: E402
from rdi_agent.rag.ingest import embeddings  # noqa: E402
from rdi_agent.rag.query_rewrite import rewrite_query  # noqa: E402

CASES_PATH = Path(__file__).parent / "retrieval_cases.jsonl"

# 标定阶段**不套**检索过滤（reference/footnote 排除、review_status），
# 否则测的是"过滤之后还剩什么"，标不出阈值。要标的是纯向量距离。
_SQL = """
    SELECT c.id::text AS chunk_id,
           s.section_code,
           c.pdf_page_start,
           c.content_original,
           c.embedding <=> CAST(:qv AS vector) AS distance
    FROM knowledge_chunks c
    JOIN book_sections s ON s.id = c.section_id
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> CAST(:qv AS vector)
    LIMIT :k
"""


_FTS_SQL = """
    SELECT c.id::text AS chunk_id,
           s.section_code,
           ts_rank_cd(c.content_tsv, to_tsquery('simple', :tsquery)) AS rank
    FROM knowledge_chunks c
    JOIN book_sections s ON s.id = c.section_id
    WHERE c.content_tsv @@ to_tsquery('simple', :tsquery)
    ORDER BY rank DESC
    LIMIT :k
"""


async def _fts_hits(keyword_query: str, top_n: int) -> list[dict[str, Any]]:
    """关键词路命中。

    **查询串在这里要重新拼成 OR 的 tsquery**：``keyword_query`` 已经是
    空格分隔的分词结果，但 ``to_tsquery`` 收到多个词时是 **AND** 语义 ——
    关键词路要的是"命中任一术语"，所以显式用 ``|`` 连接。
    """
    from rdi_agent.rag.search import _keyword_terms

    terms = _keyword_terms(keyword_query)
    if not terms:
        return []
    tsquery = " | ".join(f"'{t}'" for t in terms)
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(_FTS_SQL), {"tsquery": tsquery, "k": top_n}
            )
        ).mappings().all()
    return [
        {
            "chunk_id": r["chunk_id"],
            "section_code": r["section_code"],
            "rank": round(float(r["rank"]), 6),
        }
        for r in rows
    ]


async def _distances(query: str, top_n: int) -> list[dict[str, Any]]:
    qv = await embeddings.embed_query(query)
    async with session_scope() as session:
        rows = (
            await session.execute(text(_SQL), {"qv": str(qv), "k": top_n})
        ).mappings().all()
    return [
        {
            "chunk_id": r["chunk_id"],
            "section_code": r["section_code"],
            "pdf_page_start": r["pdf_page_start"],
            "distance": round(float(r["distance"]), 4),
            "snippet": " ".join((r["content_original"] or "").split())[:100],
        }
        for r in rows
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description="相似度阈值标定")
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    per_case: list[dict[str, Any]] = []
    for case in cases:
        queries = await rewrite_query(case["query"])
        rows = await _distances(queries.semantic_query, args.top_n)
        fts = await _fts_hits(queries.keyword_query, args.top_n)
        expected = set(case.get("expected_sections") or [])
        is_irrelevant = not expected
        for row in rows:
            row["in_expected"] = row["section_code"] in expected
        fts_sections = sorted({h["section_code"] for h in fts})
        per_case.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "category": case.get("category", ""),
                "is_irrelevant": is_irrelevant,
                "expected_sections": sorted(expected),
                "bridged_terms": [b["canonical_name"] for b in queries.bridged_terms],
                "fts_count": len(fts),
                "fts_sections": fts_sections,
                "fts_hit_expected": bool(set(fts_sections) & expected),
                "rows": rows,
            }
        )
        mark = "无关" if is_irrelevant else "相关"
        best = rows[0]["distance"] if rows else None
        hit = next((r["distance"] for r in rows if r["in_expected"]), None)
        print(
            f"[{mark}] {case['case_id']:<24} 最近邻={best} 期望块最小={hit} "
            f"FTS={len(fts)}条(命中期望章节={bool(set(fts_sections) & expected)})",
            file=sys.stderr,
        )

    # ---- 汇总 ----
    relevant = [c for c in per_case if not c["is_irrelevant"]]
    irrelevant = [c for c in per_case if c["is_irrelevant"]]
    # 相关用例里，期望章节块在 top-N 内的最小距离 —— 阈值高于它就会伤召回
    expected_distances = [
        r["distance"] for c in relevant for r in c["rows"] if r["in_expected"]
    ]
    # 无关用例里，所有块里最小的距离 —— 阈值高于它就是硬凑
    irrelevant_distances = [r["distance"] for c in irrelevant for r in c["rows"]]

    print("\n" + "=" * 78)
    print("标定汇总")
    print("=" * 78)
    print(f"相关用例 {len(relevant)} 条，无关用例 {len(irrelevant)} 条")
    if expected_distances:
        print(
            f"期望章节块距离：最小 {min(expected_distances):.4f} / "
            f"最大 {max(expected_distances):.4f} / 共 {len(expected_distances)} 块"
        )
    if irrelevant_distances:
        print(
            f"无关用例块距离：最小 {min(irrelevant_distances):.4f} / "
            f"最大 {max(irrelevant_distances):.4f} / 共 {len(irrelevant_distances)} 块"
        )

    print("\n阈值对照表（阈值 = 余弦距离上限，越小越严）：")
    print(
        f"{'阈值':>8} {'砍掉期望块':>12} {'拦下无关题':>12} "
        f"{'相关仍命中章节':>16} {'漏判相关(会硬凑)':>18}"
    )
    table: list[dict[str, Any]] = []
    for threshold in [round(0.34 + 0.02 * i, 2) for i in range(12)]:
        cut_expected = sum(1 for d in expected_distances if d > threshold)
        blocked = sum(
            1
            for c in irrelevant
            if all(r["distance"] > threshold for r in c["rows"])
        )
        still_hit = sum(
            1
            for c in relevant
            if any(r["in_expected"] and r["distance"] <= threshold for r in c["rows"])
        )
        # 组合规则：FTS 路无命中 且 向量最近邻超阈值 → 判为无关
        false_block = sum(
            1
            for c in relevant
            if c["fts_count"] == 0
            and c["rows"]
            and c["rows"][0]["distance"] > threshold
        )
        print(
            f"{threshold:>8.2f} {cut_expected:>12} {blocked:>12} "
            f"{still_hit:>16} {false_block:>18}"
        )
        table.append(
            {
                "threshold": threshold,
                "cut_expected_chunks": cut_expected,
                "blocked_irrelevant_cases": blocked,
                "relevant_cases_still_hitting": still_hit,
                "false_block_with_fts_rule": false_block,
            }
        )

    print("\n逐条明细（只看信号够不够判无关）：")
    print(f"{'case_id':<26} {'最近邻':>8} {'FTS条数':>8} {'桥接术语':>24} {'判无关?'}")
    for c in per_case:
        nearest = c["rows"][0]["distance"] if c["rows"] else None
        bridged = ",".join(c["bridged_terms"][:2]) or "-"
        judged_irrelevant = c["fts_count"] == 0 and nearest is not None and nearest > 0.50
        flag = "是" if judged_irrelevant else "否"
        print(
            f"{c['case_id']:<26} {nearest:>8} {c['fts_count']:>8} {bridged:>24} {flag:>6}"
            + ("  ←无关题，判对" if c["is_irrelevant"] and judged_irrelevant else "")
            + ("  ←相关题，误判!" if not c["is_irrelevant"] and judged_irrelevant else "")
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "top_n": args.top_n,
                    "cases": per_case,
                    "summary": {
                        "expected_chunk_distances": expected_distances,
                        "irrelevant_chunk_distances": irrelevant_distances,
                    },
                    "threshold_table": table,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n完整记录：{args.json}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
