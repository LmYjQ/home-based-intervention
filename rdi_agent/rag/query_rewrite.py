"""查询改写：把家长的话变成能检索英文书的查询。

**问题**：家长说中文，书是英文的。`content_tsv` 用的是 `english` 配置，
中文词进 `websearch_to_tsquery('english', ...)` 一个都匹配不上。

**做法**：三件事，各管一段路。

1. **语义路**（向量）：原话直接交给 bge-m3。多语言模型能中英跨语言对齐，
   所以**原话必须保留** —— 改写掉的恰恰是最有信息量的孩子描述。
2. **关键词路**（FTS）：从 ``concept_aliases`` 表把中文别名桥接成
   英文规范名，和原话一起 OR 起来。
3. **概念 ID**：命中的概念记下来，检索侧可以据此加权。

**只用已确认事实**：家长说的原话是事实；``CaseSummary`` 里的
``observed_facts`` 是抽取出的事实。**待验证的假设不进检索词** ——
把假设写进查询，等于用假设去检索，再用检索结果"验证"假设，循环论证。

**这一层是纯规则 + 数据库查询，不调 LLM。** 原因：评测集要可复现。
同一批 25 条查询跑两次必须得到同样的检索词，否则 Recall 的变化说不清是
检索变好了还是改写随机了。LLM 改写留给 2C（那时需要的是自然语言多样性，
不是可复现性）。

    python -m rdi_agent.rag.query_rewrite "孩子不敢跟人打招呼"
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text

from rdi_agent.db.engine import session_scope


@dataclass
class RetrievalQueries:
    """一次检索要用的全部查询参数。

    对应计划 §5.4 的 ``RetrievalQueries``。
    """

    original_query: str
    semantic_query: str
    keyword_query: str
    concept_ids: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)
    # 桥接过程的可观测信息：桥到了哪些概念、从哪个别名桥过去的
    bridged_terms: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _bridge_concepts(query: str) -> list[dict[str, Any]]:
    """把查询里的中文别名桥接成英文规范名。

    **只桥 ``is_explicit_in_source`` 的概念** —— 也就是英文规范名在书里
    真的出现过的。库里留着一批历史概念（``affective perspective``、
    ``engagement cycle``、``functional play``…），它们的英文在本书里
    一次都没出现过，桥过去只会把 FTS 查询带偏：书里没有这个词，
    匹配到的必然是别的东西。

    匹配用 ``ILIKE '%别名%'``（子串），因为家长说的是"引导式参与"，
    术语表里存的是"引导式参与"——但也可能是"做引导式参与练习"，
    子串匹配能覆盖这种说法。

    精确匹配（等号）反而会漏掉"孩子现在不太会引导式参与"这种句式。

    排序按别名长度降序：先匹配长的，避免"引导"这种短别名抢走
    "引导式参与"该匹配的机会。
    """
    sql = """
        SELECT c.concept_id,
               c.canonical_name_en,
               c.aliases_zh_json
        FROM concept_aliases c
        WHERE c.is_explicit_in_source
          AND EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(c.aliases_zh_json) AS alias
            WHERE :q ILIKE '%' || alias || '%'
          )
    """
    async with session_scope() as session:
        rows = (await session.execute(text(sql), {"q": query})).mappings().all()

    hits: list[dict[str, Any]] = []
    for row in rows:
        aliases = row["aliases_zh_json"] or []
        matched = [a for a in aliases if a and a in query]
        if not matched:
            continue
        # 最长的匹配别名优先（"引导式参与" 胜过 "引导"）
        matched.sort(key=len, reverse=True)
        hits.append(
            {
                "concept_id": row["concept_id"],
                "canonical_name_en": row["canonical_name_en"],
                "matched_alias": matched[0],
                "alias_len": len(matched[0]),
            }
        )
    hits.sort(key=lambda h: h["alias_len"], reverse=True)
    return hits


async def rewrite_query(
    query: str,
    *,
    source_id: str | None = None,
    extra_filters: dict[str, str] | None = None,
) -> RetrievalQueries:
    """把一句家长的话改写成检索参数。

    ``source_id`` 给了就限定在这个来源里检索 —— 知识库将来不止一本书，
    跨书混检会让"引用哪本书哪一页"变得说不清。
    """
    original = query.strip()
    hits = await _bridge_concepts(original)

    # 关键词查询 = 原话 + 桥接到的英文术语，用 OR 串起来。
    # 原话保留：FTS 走 english 配置，中文部分匹配不上但无害，
    # 而英文原话（家长自己说了英文术语的情况）能匹配上。
    keyword_terms = [original, *[h["canonical_name_en"] for h in hits]]
    keyword_query = " OR ".join(dict.fromkeys(t for t in keyword_terms if t))

    filters: dict[str, str] = {}
    if source_id:
        filters["source_id"] = source_id
    if extra_filters:
        filters.update(extra_filters)

    return RetrievalQueries(
        original_query=original,
        # 语义路用**原话**：bge-m3 能跨语言对齐，改写反而丢信息
        semantic_query=original,
        keyword_query=keyword_query,
        concept_ids=[h["concept_id"] for h in hits],
        filters=filters,
        bridged_terms=hits,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="查询改写")
    parser.add_argument("query", help="原始查询（中文）")
    parser.add_argument("--source-id", default=None)
    args = parser.parse_args()

    result = asyncio.run(rewrite_query(args.query, source_id=args.source_id))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
