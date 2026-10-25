"""从一堆记忆里挑"跟这次问题有关"的那几条。

**为什么不能全塞进 prompt**：一个家庭攒上几十条观察之后，
prompt 里塞满上周、上个月跟这次无关的记录 —— 模型要么被带偏，
要么干脆被挤掉真正相关的几条。计划 §10.4 明确要求
"只取与当前问题相关的记忆，并限制 token 预算"。

**为什么不用 ``rag/terms.py``**：那个是给 Postgres tsquery 用的，
契约是"输入已经分好词的查询串"，而且会丢掉单字词（对 FTS 是对的，
对记忆相关性判断是错的）。这里直接复用人库时的 jieba 分词
（``rag.ingest.clean.segment_zh``），按普通词重合度算。

**这是粗筛不是语义匹配**：词重合度为零不代表无关。
所以排序只是"相关度优先"，最后的截断还要**保留时间最近的若干条** ——
不能因为今天的问题碰巧一个词都对不上，就把记忆全丢了。
"""

from __future__ import annotations

from typing import Any, Callable

from rdi_agent.rag.ingest.clean import segment_zh

# 这些词在中文里几乎每句都有，重合了也说明不了相关性。
_MEMORY_STOPWORDS = frozenset(
    {
        "孩子", "家长", "我们", "你们", "他们", "今天", "昨天", "明天",
        "现在", "时候", "问题", "情况", "可能", "就是", "还是", "这个",
        "那个", "然后", "因为", "所以", "但是", "如果", "一个", "一下",
        "没有", "什么", "怎么", "可以", "这样", "那样",
    }
)


def tokens(text: str) -> set[str]:
    """分词 + 去停用词 + 去单字。返回小写集合。"""
    if not text:
        return set()
    out: set[str] = set()
    for raw in segment_zh(text).split():
        token = raw.strip().casefold()
        if len(token) < 2:
            continue
        if token in _MEMORY_STOPWORDS:
            continue
        out.add(token)
    return out


def overlap_score(query_tokens: set[str], text: str) -> float:
    """重合词数占查询词数的比例，0–1。"""
    if not query_tokens:
        return 0.0
    hit = query_tokens & tokens(text)
    return len(hit) / len(query_tokens)


def rank_by_relevance(
    query: str,
    items: list[dict[str, Any]],
    *,
    text_of: Callable[[dict[str, Any]], str] | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    """按与 ``query`` 的词重合度排序，取前 ``limit`` 条。

    **稳定排序**：同分的保持调用方给的顺序（调用方按时间倒序查出来的），
    于是"同样相关时新的优先"是自然结果，不需要额外算时间权重。
    """
    if limit <= 0:
        return []
    if not items:
        return []
    getter = text_of or (lambda item: str(item.get("content") or ""))
    query_tokens = tokens(query)
    if not query_tokens:
        return items[:limit]

    scored = [
        (overlap_score(query_tokens, getter(item)), index, item)
        for index, item in enumerate(items)
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in scored[:limit]]


def take_by_budget(
    items: list[dict[str, Any]],
    *,
    text_of: Callable[[dict[str, Any]], str] | None = None,
    max_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    """按字符预算截断。返回 ``(保留的条目, 是否发生截断)``。

    **按字符而不是 token 估算**：中文里 1 token ≈ 1–1.5 字，
    用字符数当上限是保守估计 —— 宁可少放一点，也不要让 prompt 超长
    之后被服务端截断（被截断的往往是最后那几条，也就是最相关的）。
    """
    getter = text_of or (lambda item: str(item.get("content") or ""))
    kept: list[dict[str, Any]] = []
    used = 0
    for item in items:
        length = len(getter(item))
        if kept and used + length > max_chars:
            return kept, True
        kept.append(item)
        used += length
    return kept, False
