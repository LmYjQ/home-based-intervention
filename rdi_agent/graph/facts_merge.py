"""事实合并的纯函数。

**为什么单独抽出来**：首版聊天入口和修订接口都要做同一件事 ——
把家长新说的话并进 ``case_summary.observed_facts``，同时把已经作废 /
删除的旧事实剔掉。规则只能有一份，否则两条入口迟早会不一致：
聊天入口过滤了、修订入口没过滤，家长删掉的信息就从修订版漏回去。

这里不连库、不调模型。作废清单由调用方从记忆服务读出来传进来。
"""

from __future__ import annotations

from typing import Any

from rdi_agent.memory.ids import normalize_content


def forbidden_terms(values: list[str] | None) -> list[str]:
    """把作废事实归一化，方便和 case_summary 里的转述做包含判断。"""
    return [
        normalized
        for normalized in (normalize_content(x) for x in (values or []))
        if normalized
    ]


def forbidden_hit(fact: str, forbidden: list[str]) -> str:
    """这条事实命中了哪条已作废的内容。没命中返回空串。

    只看"旧内容是这条事实的全部或一部分" —— 反过来会误杀
    （删掉"孩子 4 岁，喜欢恐龙"之后，"喜欢恐龙"仍然有效）。
    """
    normalized = normalize_content(fact)
    if not normalized:
        return ""
    for old in forbidden:
        if old and (normalized == old or old in normalized):
            return old
    return ""


def restated_terms(message: str, forbidden: list[str]) -> set[str]:
    """家长这一轮原话里又提到了哪些已作废的内容。

    家长最新的话说了算：他这轮又说了，就是当前事实。
    """
    normalized = normalize_content(message)
    if not normalized:
        return set()
    return {old for old in forbidden if old and old in normalized}


def filter_forbidden_texts(
    values: list[Any],
    forbidden: list[str],
    restated: set[str],
) -> tuple[list[str], int]:
    """把作废内容从一批文本里剔掉。返回 (留下的, 剔掉几条)。"""
    kept: list[str] = []
    dropped = 0
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        hit = forbidden_hit(text, forbidden)
        if hit and hit not in restated:
            dropped += 1
            continue
        if text not in kept:
            kept.append(text)
    return kept, dropped


def merge_parent_supplement(
    previous_summary: dict[str, Any] | None,
    supplement: str,
    forbidden: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把家长补充的新情况并进已累积的事实清单。

    **不依赖模型抽取**：新情况原样作为一条"家长补充"事实加进去，
    保证它一定出现在生成建议卡的输入里。旧事实一条不丢，
    已作废/已删除的旧事实照旧被过滤。

    返回 ``(新的 case_summary, 合并说明)``。合并说明给 trace 和接口响应用。
    """
    previous = dict(previous_summary or {})
    terms = forbidden_terms(forbidden)
    text = str(supplement or "").strip()
    restated = restated_terms(text, terms)
    dropped = 0

    facts, dropped_facts = filter_forbidden_texts(
        list(previous.get("observed_facts") or []), terms, restated
    )
    dropped += dropped_facts
    if text:
        line = f"家长补充：{text}"
        if line not in facts:
            facts.append(line)
    previous["observed_facts"] = facts

    for field in ("already_tried", "missing_information", "retrieval_concepts"):
        values, dropped_n = filter_forbidden_texts(
            list(previous.get(field) or []), terms, restated
        )
        previous[field] = values
        dropped += dropped_n

    filtered_context: dict[str, str] = {}
    for key, value in (previous.get("context") or {}).items():
        hit = forbidden_hit(str(value), terms)
        if hit and hit not in restated:
            dropped += 1
            continue
        filtered_context[str(key)] = value
    previous["context"] = filtered_context

    # risk_signals 不在这里过滤 —— 风险信号绝不能丢。
    previous["risk_signals"] = list(previous.get("risk_signals") or [])

    return previous, {
        "dropped": dropped,
        "restated": sorted(restated),
        "added": bool(text),
    }
