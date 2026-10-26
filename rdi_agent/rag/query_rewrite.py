"""查询改写：把家长的话变成能检索中文指南的查询。

**做法**：三件事，各管一段路。

1. **语义路**（向量）：原话直接交给 bge-m3。多语言模型，原话必须保留 ——
   改写掉的恰恰是最有信息量的孩子描述。
2. **关键词路**（FTS）：原话**分词**后 + 从 ``concept_aliases`` 表把
   中文别名桥接成指南里的规范术语，合成 OR 查询。
3. **概念 ID**：命中的概念记下来，检索侧可以据此加权。

**为什么关键词路必须分词**：``content_tsv`` 是
``to_tsvector('simple', content_normalized)`` 生成的，``simple`` 配置
按**空格**分词。入库时内容已经 jieba 分过词，所以查询这边也要分 ——
不然查询是一片连写的中文，库里是一片连写的中文，两边都是单个 lexeme，
匹配不上。

**桥接的方向**：家长说"发脾气"，指南写"挑战性行为"。知识库和用户输入
都是中文，所以桥接是**中文口语 -> 中文规范术语**，不再是跨语言桥接。

**只用已确认事实**：家长说的原话是事实；``CaseSummary`` 里的
``observed_facts`` 是抽取出的事实。**待验证的假设不进检索词** ——
把假设写进查询，等于用假设去检索，再用检索结果"验证"假设，循环论证。

**这一层是纯规则 + 数据库查询，不调 LLM。** 原因：评测集要可复现。
同一批查询跑两次必须得到同样的检索词，否则 Recall 的变化说不清是
检索变好了还是改写随机了。

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
from rdi_agent.rag.ingest.clean import segment_zh


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
    """把查询里的中文别名桥接成知识库里的规范术语。

    **只桥 ``is_explicit_in_source`` 的概念** —— 也就是规范名在知识库
    正文里真的出现过的。没出现过的词桥过去只会把关键词查询带偏：
    库里没有这个词，匹配到的必然是别的东西。

    匹配用 ``ILIKE '%别名%'``（子串），因为家长说的是"发脾气"，
    术语表里存的是"发脾气"——但也可能是"孩子发脾气怎么办"，
    子串匹配能覆盖这种说法。

    精确匹配（等号）反而会漏掉"孩子现在不太会配合"这种句式。

    排序按别名长度降序：先匹配长的，避免"哭"这种短别名抢走
    "哭闹"该匹配的机会。
    """
    sql = """
        SELECT c.concept_id,
               c.canonical_name,
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
        # 最长的匹配别名优先（"挑战性行为" 胜过 "行为"）
        matched.sort(key=len, reverse=True)
        hits.append(
            {
                "concept_id": row["concept_id"],
                "canonical_name": row["canonical_name"],
                "matched_alias": matched[0],
                "alias_len": len(matched[0]),
            }
        )
    hits.sort(key=lambda h: h["alias_len"], reverse=True)
    return hits


# **定义意图**：家长在问"这个词是什么意思"。
#
# **为什么要单独识别**：问"挑战性行为指什么"和问"孩子发脾气怎么办"
# 需要的**不是同一段内容** —— 前者要定义（指南的"名词解释"章节），
# 后者要干预方法。
#
# 向量检索分不开这两者："挑战性行为指什么" 和讲挑战性行为的那些段
# 语义上非常像（实测前 4 名全是讲挑战性行为**内容**的块，
# 没有一条是**定义**）。关键词路也分不开 —— "挑战性行为"这个词
# 在定义块和内容块里都出现。
#
# 所以要在**查询改写**这一层识别意图，把"名词解释"这个章节
# 显式加进关键词查询 —— 让定义块在关键词路上排前面。
#
# **这是规则不是模型**：评测集要可复现，同一条查询每次改写结果一样。
DEFINITION_INTENT_PATTERNS = (
    "什么意思", "什么意思", "是指什么", "指什么", "指的是什么",
    "什么叫", "是什么", "定义", "含义", "概念",
    "名词解释", "解释一下", "到底指",
)

# 意图命中时优先指向的章节（中文指南的"六、名词解释"）。
DEFINITION_SECTION_CODE = "016_名词解释"

# 意图命中时额外加进关键词查询的词。
#
# "名词解释"是**章节标题**，正文里没有这几个字（正文是
# "（一）缺陷性行为。相对于正常儿童…"），所以直接加进查询词
# 命中不了 —— 章节标题不在 ``content_tsv`` 里。
#
# 真正能命中的是正文里的**定义句式**。
#
# 选词的标准是**区分度**，不是"像定义"：实测（扫全库 73 块）
# "相对 于" 只出现在 4 个块里 —— 2 个在名词解释（016），
# 2 个在局限兴趣那节（005）。别的块一个都没有。
#
# 早先试过"是指""即""称为"，但它们要么被 jieba 拆得更碎
# （"是指" -> "是 指"），要么在别的块里也常见，区分度不够。
#
# **这些词要按入库时的分词形态写**：入库侧也是 jieba 分词，
# "相对于"会变成 "相对 于"。查询侧用同一个分词器，
# 两边形态一致才能匹配上。
DEFINITION_BOOST_TERMS = ("相对 于",)


def _has_definition_intent(query: str) -> bool:
    """查询是不是在问定义。

    **要排除否定式**：家长说"我不知道发脾气指什么"也是问定义，
    但家长说"不要跟我讲这些定义"就��是。这里只做正向匹配 ——
    否定式的误判代价是"多召回一点定义"，不大；漏判的代价是
    "完全召不回定义"，更大。
    """
    return any(p in query for p in DEFINITION_INTENT_PATTERNS)


async def rewrite_query(
    query: str,
    *,
    source_id: str | None = None,
    extra_filters: dict[str, str] | None = None,
) -> RetrievalQueries:
    """把一句家长的话改写成检索参数。

    ``source_id`` 给了就限定在这个来源里检索 —— 知识库将来可能不止
    一个来源，混检会让"引用哪份文档哪一页"变得说不清。
    """
    original = query.strip()
    hits = await _bridge_concepts(original)

    # 关键词查询 = 原话分词 + 桥接到的规范术语。
    #
    # **为什么要分词**：``content_tsv`` 用的是 ``to_tsvector('simple', ...)``，
    # ``simple`` 按空格分词 —— 库里存的是 jieba 分好词的文本，
    # 查询这边不分词就匹配不上（一片连写的中文在库里是一个 lexeme，
    # 查询也是一个，整段对整段）。
    #
    # **规范术语要带上**：家长说"发脾气"，指南里写"挑战性行为"。
    # 只用原话分词，关键词路命中的是"发脾气"这个词（指南里根本没这个词），
    # 桥接之后才能命中"挑战性行为"那一段。
    keyword_terms = [
        *segment_zh(original).split(),
        *[h["canonical_name"] for h in hits],
    ]
    # 定义意图：额外加"定义句式"词，让名词解释章节的块在关键词路排前面。
    if _has_definition_intent(original):
        keyword_terms.extend(DEFINITION_BOOST_TERMS)
    keyword_query = " ".join(dict.fromkeys(t for t in keyword_terms if t))

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




# 计划 §5.4：查询改写只能使用**已确认事实**。
#
# CaseSummary 里哪些字段是事实、哪些不是：
#   observed_facts / parent_goals / context / already_tried —— 家长报告的事实
#   retrieval_concepts —— 模型抽的"线索"，**不是事实**
#   missing_information —— 缺口，不是事实
#
# **为什么丢掉 retrieval_concepts**：把假设写进检索词，等于用假设去检索，
# 再用检索结果"验证"假设，循环论证。而且 2B 报告 §5.5 记录了术语桥接的
# 另一个问题：``greeting`` 在书里只出现 6 块，且是比喻义（"大脑向世界打招呼"），
# 桥过去捞回来的是噪声。线索要用，得先解决语义核实。
FACT_FIELDS = ("observed_facts", "parent_goals")

# 查询文本的长度上限。bge-m3 有 8192 token 的输入上限，但查询越短通常越准，
# 而且这里是要给家长看的依据 —— 抓得越准越好。
MAX_QUERY_CHARS = 500
MAX_QUERY_ITEMS = 6


def build_query_from_case_summary(
    case_summary: dict[str, Any] | None,
    user_message: str = "",
) -> str:
    """用**已确认的事实**拼出检索查询。

    纯规则，不调 LLM —— 同一份 case_summary 必须永远得到同一个查询，
    否则评测的 Recall 变化说不清是检索变好了还是改写随机了。

    ``user_message`` 只在一条事实都没有时兜底（冷启动的第一轮）。
    """
    summary = case_summary or {}
    parts: list[str] = []
    for field in FACT_FIELDS:
        for value in summary.get(field) or []:
            text = str(value).strip()
            if text:
                parts.append(text)
    if not parts:
        text = (user_message or "").strip()
        if text:
            parts.append(text)
    if not parts:
        return ""
    return "；".join(parts[:MAX_QUERY_ITEMS])[:MAX_QUERY_CHARS]


def main() -> None:
    parser = argparse.ArgumentParser(description="查询改写")
    parser.add_argument("query", help="原始查询（中文）")
    parser.add_argument("--source-id", default=None)
    args = parser.parse_args()

    result = asyncio.run(rewrite_query(args.query, source_id=args.source_id))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
