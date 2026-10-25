"""``children.confirmed_facts_json`` 的读写视图。

**为什么事实不像观察/假设那样单独一张表**：计划 §10.2 把 L1（稳定档案）
放在 ``children`` 上 —— 它是一份"当前怎么看这个孩子"的快照，
条数很少（几十条上限），不需要按时间查全量历史。

**代价**：JSONB 数组上建不了唯一索引，去重只能靠应用层 + 行锁。
阶段 3 接受这个代价（每轮写入本来就在一个事务里），
真到了需要从库里强约束的那天，再拆出 ``memory_facts`` 表。

**元素形状**：对象（不再是裸字符串），因为每条事实都要有 id、
状态、原话引用。旧数据里的字符串照常读 —— 升版前存进去的还躺在那儿。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from rdi_agent.memory.ids import (
    MIN_CONTENT_CHARS,
    content_hash,
    fact_memory_id,
    normalize_content,
)

FactStatus = Literal["confirmed", "pending_confirmation", "superseded", "deleted"]

_LIVE_STATUSES = ("confirmed",)
_FORBIDDEN_STATUSES = ("superseded", "deleted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FactItem:
    """一条 L1 记忆（事实或偏好）。"""

    id: str
    memory_type: str = "fact"  # fact | preference
    content: str = ""
    content_hash: str = ""
    status: str = "confirmed"

    source_interaction_id: str = ""
    source_feedback_id: str = ""
    source_turn_id: str = ""
    source_kind: str = "interaction"
    source_quote: str = ""
    confidence: float = 0.8

    scope_conditions: list[str] = field(default_factory=list)
    supersedes_memory_id: str | None = None
    superseded_by_memory_id: str | None = None
    deleted_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_live(self) -> bool:
        """还能不能进 prompt。"""
        return self.status in _LIVE_STATUSES and not self.deleted_at


def _item_from_dict(raw: dict[str, Any]) -> FactItem:
    content = str(raw.get("content") or "")
    memory_type = str(raw.get("memory_type") or "fact")
    if memory_type not in ("fact", "preference"):
        memory_type = "fact"
    digest = str(raw.get("content_hash") or "") or content_hash(memory_type, content)
    return FactItem(
        id=str(raw.get("id") or "") or fact_memory_id(memory_type, digest),
        memory_type=memory_type,
        content=content,
        content_hash=digest,
        status=str(raw.get("status") or "confirmed"),
        source_interaction_id=str(raw.get("source_interaction_id") or ""),
        source_feedback_id=str(raw.get("source_feedback_id") or ""),
        source_turn_id=str(raw.get("source_turn_id") or ""),
        source_kind=str(raw.get("source_kind") or "interaction"),
        source_quote=str(raw.get("source_quote") or ""),
        confidence=float(raw.get("confidence") or 0.8),
        scope_conditions=[str(x) for x in (raw.get("scope_conditions") or [])],
        supersedes_memory_id=raw.get("supersedes_memory_id"),
        superseded_by_memory_id=raw.get("superseded_by_memory_id"),
        deleted_at=raw.get("deleted_at"),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def normalize_fact_items(raw: list[Any] | None) -> list[FactItem]:
    """把库里的 JSONB 数组读成 ``FactItem``。

    **兼容裸字符串**：阶段 3 之前 ``confirmed_facts_json`` 存的是
    ``["孩子 4 岁", ...]``。这类条目没有溯源信息，读的时候补一个
    由内容哈希推出来的稳定 id —— 同一个字符串每次读到的 id 都一样，
    否则前端刷新一次就换一个 id，纠正按钮立刻失效。
    """
    items: list[FactItem] = []
    for entry in raw or []:
        if isinstance(entry, str):
            text = entry.strip()
            if len(normalize_content(text)) < MIN_CONTENT_CHARS:
                continue
            digest = content_hash("fact", text)
            items.append(
                FactItem(
                    id=fact_memory_id("fact", digest),
                    memory_type="fact",
                    content=text,
                    content_hash=digest,
                    status="confirmed",
                    source_kind="legacy",
                    source_quote="",
                    created_at="",
                    updated_at="",
                )
            )
        elif isinstance(entry, dict):
            item = _item_from_dict(entry)
            if len(normalize_content(item.content)) < MIN_CONTENT_CHARS:
                continue
            items.append(item)
    return items


def active_facts(items: list[FactItem]) -> list[FactItem]:
    return [x for x in items if x.is_live]


def pending_facts(items: list[FactItem]) -> list[FactItem]:
    return [
        x
        for x in items
        if x.status == "pending_confirmation" and not x.deleted_at
    ]


def forbidden_fact_texts(items: list[FactItem]) -> list[str]:
    """已作废 / 已删除的事实原文。

    **只给代码用**（``parse_parent_input`` 拿它过滤累积事实），
    **永远不进 prompt** —— 否则家长删掉的隐私会被重新喂回模型。
    """
    return [
        x.content
        for x in items
        if x.status in _FORBIDDEN_STATUSES or x.deleted_at
    ]


def next_fact_id(
    items: list[FactItem], memory_type: str, digest: str
) -> str:
    """给新事实分配 id。同内容的历史记录存在时加代数后缀。"""
    used = {x.id for x in items}
    generation = 1
    candidate = fact_memory_id(memory_type, digest, generation=generation)
    while candidate in used:
        generation += 1
        candidate = fact_memory_id(memory_type, digest, generation=generation)
    return candidate


def new_fact_item(
    *,
    memory_type: str,
    content: str,
    digest: str,
    existing: list[FactItem],
    status: str = "confirmed",
    source_interaction_id: str = "",
    source_feedback_id: str = "",
    source_turn_id: str = "",
    source_kind: str = "interaction",
    source_quote: str = "",
    confidence: float = 0.8,
    scope_conditions: list[str] | None = None,
    supersedes_memory_id: str | None = None,
) -> FactItem:
    now = _now()
    return FactItem(
        id=next_fact_id(existing, memory_type, digest),
        memory_type=memory_type,
        content=content,
        content_hash=digest,
        status=status,
        source_interaction_id=source_interaction_id,
        source_feedback_id=source_feedback_id,
        source_turn_id=source_turn_id,
        source_kind=source_kind,
        source_quote=source_quote,
        confidence=confidence,
        scope_conditions=list(scope_conditions or []),
        supersedes_memory_id=supersedes_memory_id,
        created_at=now,
        updated_at=now,
    )
