"""``children.memory_summaries_json`` 的读写视图。

**它是什么**：每个类目一条"这个孩子目前是什么样"的小结，
由该类目下的多条观察归纳而来。prompt 里跑的观察记忆**只有这些小结** ——
原始观察只留在库里，不参与方案制定。

**为什么和 memory_items 分开放**：那边是**家长说过的一句句短话**，
这里存的是**系统归纳**出来的。混在一列里，代码就得靠字段去猜
这条是家长说的还是系统推的 —— 而这个区分正是记忆层最要紧的那条线
（"推断变事实"是计划里列的头号风险）。

**元素形状**：和 FactItem 一样是对象，带 id / 状态 / 溯源。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from rdi_agent.memory.ids import (
    MIN_CONTENT_CHARS,
    content_hash,
    normalize_content,
    summary_memory_id,
)

SummaryStatus = Literal["active", "superseded", "deleted"]

_LIVE_STATUSES = ("active",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SummaryItem:
    """一条类目小结。"""

    id: str
    category: str = "other"
    content: str = ""
    content_hash: str = ""
    status: str = "active"

    # 由哪些观察归纳而来。**家长点开小结要能看到这个** ——
    # 没有它，归纳就是黑盒。
    derived_from: list[str] = field(default_factory=list)
    # 成立条件（"在幼儿园门口""连续三天以上"）。
    scope_conditions: list[str] = field(default_factory=list)
    confidence_level: str = "medium"
    # 这条小结是怎么来的：
    #   consolidated —— LLM 归纳出来的
    #   direct       —— 明细太少（1-2 条），直接把明细本身当小结
    #
    # **为什么要区分**：只有 consolidated 的小结才参与"该不该重新合并"
    # 的判定。direct 的只是暂存 —— 类目攒到 3 条就该让 LLM 归纳，
    # 如果把 direct 的明细也算成"已合并"，这一轮就永远不会触发归纳。
    source_kind: str = "consolidated"

    source_interaction_id: str = ""
    source_turn_id: str = ""
    source_quote: str = ""
    supersedes_memory_id: str | None = None
    superseded_by_memory_id: str | None = None
    deleted_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_live(self) -> bool:
        return self.status in _LIVE_STATUSES and not self.deleted_at


def _item_from_dict(raw: dict[str, Any]) -> SummaryItem:
    content = str(raw.get("content") or "")
    category = str(raw.get("category") or "other")
    digest = str(raw.get("content_hash") or "") or content_hash(
        "summary", f"{category}\x00{content}"
    )
    return SummaryItem(
        id=str(raw.get("id") or "") or summary_memory_id(category, digest),
        category=category,
        content=content,
        content_hash=digest,
        status=str(raw.get("status") or "active"),
        derived_from=[str(x) for x in (raw.get("derived_from") or [])],
        scope_conditions=[str(x) for x in (raw.get("scope_conditions") or [])],
        confidence_level=str(raw.get("confidence_level") or "medium"),
        source_kind=str(raw.get("source_kind") or "consolidated"),
        source_interaction_id=str(raw.get("source_interaction_id") or ""),
        source_turn_id=str(raw.get("source_turn_id") or ""),
        source_quote=str(raw.get("source_quote") or ""),
        supersedes_memory_id=raw.get("supersedes_memory_id"),
        superseded_by_memory_id=raw.get("superseded_by_memory_id"),
        deleted_at=raw.get("deleted_at"),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def normalize_summary_items(raw: list[Any] | None) -> list[SummaryItem]:
    """把库里的 JSONB 数组读成 ``SummaryItem``。"""
    items: list[SummaryItem] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        item = _item_from_dict(entry)
        if len(normalize_content(item.content)) < MIN_CONTENT_CHARS:
            continue
        items.append(item)
    return items


def active_summaries(items: list[SummaryItem]) -> list[SummaryItem]:
    """活着的（``active``）小结。

    **一个类目只应该有一条活的小结** —— 新的归纳出来会把旧的标
    superseded（见 ``mark_superseded``）。这里不额外按类目去重：
    万一出现两条，那是写入路径的 bug，应该被看见而不是被静默吞掉。
    """
    return [x for x in items if x.is_live]


def live_by_category(items: list[SummaryItem]) -> dict[str, SummaryItem]:
    """类目 -> 活的小结。"""
    return {x.category: x for x in active_summaries(items)}


def next_summary_id(
    items: list[SummaryItem], category: str, digest: str
) -> str:
    """给新小结分配 id。同内容的历史记录存在时加代数后缀。"""
    used = {x.id for x in items}
    generation = 1
    candidate = summary_memory_id(category, digest, generation=generation)
    while candidate in used:
        generation += 1
        candidate = summary_memory_id(category, digest, generation=generation)
    return candidate


def new_summary_item(
    *,
    category: str,
    content: str,
    digest: str,
    existing: list[SummaryItem],
    derived_from: list[str] | None = None,
    scope_conditions: list[str] | None = None,
    confidence_level: str = "medium",
    source_turn_id: str = "",
    supersedes_memory_id: str | None = None,
) -> SummaryItem:
    now = _now()
    return SummaryItem(
        id=next_summary_id(existing, category, digest),
        category=category,
        content=content,
        content_hash=digest,
        status="active",
        derived_from=list(derived_from or []),
        scope_conditions=list(scope_conditions or []),
        confidence_level=confidence_level,
        source_turn_id=source_turn_id,
        supersedes_memory_id=supersedes_memory_id,
        created_at=now,
        updated_at=now,
    )


def mark_superseded(
    items: list[SummaryItem], target_id: str, new_id: str
) -> SummaryItem | None:
    """把旧的小结标成 ``superseded``。

    **明细一条都不删** —— 旧小结只是"不再进 prompt"，
    它归纳自哪些观察、家长后来纠不纠正，都还要能查。
    """
    for item in items:
        if item.id == target_id:
            item.status = "superseded"
            item.superseded_by_memory_id = new_id
            return item
    return None


def mark_deleted(items: list[SummaryItem], target_id: str) -> SummaryItem | None:
    """软删除一条小结。"""
    for item in items:
        if item.id == target_id:
            item.status = "deleted"
            item.deleted_at = _now()
            return item
    return None


__all__ = [
    "SummaryItem",
    "SummaryStatus",
    "active_summaries",
    "live_by_category",
    "mark_deleted",
    "mark_superseded",
    "new_summary_item",
    "next_summary_id",
    "normalize_summary_items",
]
