"""关键词路的词表逻辑：切词、高频词统计、区分度词。

单独成文件是因为 :mod:`rdi_agent.rag.keyword_backends` 和
:mod:`rdi_agent.rag.search` 都要用它们，而它们互相引用 ——
不放在一起就会 import 成环。

这个文件**不 import search**，所以没有环。
"""

from __future__ import annotations

import re

from sqlalchemy import text

from rdi_agent.db.engine import session_scope

# tsquery 里能安全出现的词：汉字、字母、数字。
#
# **为什么要剔掉别的**：tsquery 的语法字符（``:`` 权重、``&`` ``|`` ``!``
# 运算符、``(`` ``)`` 分组、``'`` 引号）出现在词里会让解析报错。
# 指南正文里有 ``（一）`` 这类编号、``0~18 月龄`` 这类范围 ——
# 剔掉之后留下 ``18 月龄``，虽然丢了分隔符但仍能命中。
_TSQUERY_SAFE = re.compile(r"[^\w一-鿿]+")


def keyword_terms(query: str) -> list[str]:
    """把关键词查询切成 tsquery 能用的词。

    输入是**已经分词过的**查询串（空格分隔的词），所以这里只是
    把词挑出来、剔掉不安全字符、去重。

    **去掉单字词**：``的`` ``了`` ``和`` 这类虚词没有检索价值 ——
    它们在任何一段中文里都出现，命中它们等于没命中。
    """
    terms: list[str] = []
    for raw in query.split():
        cleaned = _TSQUERY_SAFE.sub("", raw).strip("_")
        if not cleaned:
            continue
        # 单个汉字（或单字母）太弱，跳过
        if len(cleaned) < 2:
            continue
        if cleaned not in terms:
            terms.append(cleaned)
    return terms


# **高频词的门槛**：出现在超过这个比例的块里的词，对排序没有区分度。
#
# **为什么需要它**：中文查询里最高频的词恰恰是最没信息量的那些。
# 实测（扫全部 73 块）：
#
#   儿童 295  行为 288  干预 286  训练 119  能力 119  评估 94 ...
#
# 前三个词出现在**几乎每一块**里 —— 问"孩子有癫痫，还能做行为干预吗"，
# 词表是 ``['孩子', '癫痫', '行为', '干预', '共患病']``，
# 其中"孩子""行为""干预"三个是虚词。命中它们的块（讲挑战性行为、
# 讲缺陷行为）排名虚高，把真正的答案（共存问题和共患病那一节）挤掉了。
#
# 所以排序时**先把高频词剔掉**，只用有区分度的词算命中数。
#
# **但不能把它们从查询里彻底删掉**：它们仍然参与**召回**
# （命中就是命中，扩大候选池），只是不参与**排序**。
# 彻底删的话，"孩子摔东西"这种查询会因为没有区分度词而召不回任何东西。
#
# 门槛取 30% —— 比"儿童/行为/干预"低得多，又不至于把"共患病"
# 这种只出现在一两节的术语误判成高频词。
HIGH_FREQ_RATIO = 0.30

# 高频词表。由 :func:`_load_high_freq_terms` 第一次检索时从库里算出来。
#
# **为什么要从库里算而不是写死**：知识库换了内容，词频分布就变了。
# 写死的话，改了入库内容还要记得改这里 —— 迟早会忘。
_HIGH_FREQ_TERMS: set[str] = set()


async def _load_high_freq_terms() -> set[str]:
    """扫一遍知识库，算出高频词。

    只统计 ``content_normalized``（分词后的形态）—— 和关键词路
    匹配的是同一份文本，口径必须一致。
    """
    async with session_scope() as session:
        total = (
            await session.execute(
                text("SELECT count(*) FROM knowledge_chunks")
            )
        ).scalar_one()
        if not total:
            return set()
        rows = (
            await session.execute(
                text(
                    """
                    SELECT word, count(DISTINCT chunk_id) AS n
                    FROM (
                        SELECT id AS chunk_id,
                               unnest(regexp_split_to_array(content_normalized, ' ')) AS word
                        FROM knowledge_chunks
                    ) t
                    WHERE length(word) > 1
                    GROUP BY word
                    HAVING count(DISTINCT chunk_id) > :threshold
                    """
                ),
                {"threshold": total * HIGH_FREQ_RATIO},
            )
        ).all()
    return {word for word, _ in rows}


async def ensure_high_freq_terms(*, force: bool = False) -> set[str]:
    """确保高频词表已加载（幂等）。

    **为什么在这里加载**：词频是**知识库内容**的属性 ——
    换了内容，分布就变了。所以它不能写死在代码里，也不能在模块
    导入时加载（那时候数据库连接可能还没准备好）。

    **什么时候加载**：第一次检索时。加载之后缓存在模块变量里，
    后续检索直接用 —— 每次检索都扫一遍库太浪费。

    **怎么失效**：重新入库之后词频变了。``force=True`` 可以强制重算
    （入库脚本调它）。或者直接重启进程 —— 检索服务本来就是长驻的。
    """
    global _HIGH_FREQ_TERMS
    if _HIGH_FREQ_TERMS and not force:
        return _HIGH_FREQ_TERMS
    _HIGH_FREQ_TERMS = await _load_high_freq_terms()
    return _HIGH_FREQ_TERMS


def discriminative_terms(terms: list[str]) -> list[str]:
    """去掉高频词，留下有区分度的词。

    **全被高频词占满时怎么办**：返回空列表 —— 调用方要能区分
    "没有区分度词"（排序退化成按 ts_rank）和"有区分度词"。

    退化不是错误：查询全是"孩子""干预"这种词时，本来就没什么可分的，
    按 ts_rank 排已经是能做的最好的事了。
    """
    if not _HIGH_FREQ_TERMS:
        return terms
    kept = [t for t in terms if t not in _HIGH_FREQ_TERMS]
    return kept
