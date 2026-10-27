"""长期记忆的数据访问层。

和 :class:`rdi_agent.db.repositories.case.Repository` 一样：**不自己 commit**，
事务边界归调用方的 ``session_scope()``。

**这一层只做"取/存"，不做判断**。哪条能记、哪条要丢，
是 :mod:`rdi_agent.memory.merge` 的事 —— 那部分要能脱离数据库单测。

**记忆只有一张表**：``memory_items``（一句短句 + 它的类目）。
类目小结存在 ``children.memory_summaries_json`` 上 ——
它是**派生产物**，由短句归纳而来，不是一句一句攒出来的。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rdi_agent.age import describe_age
from rdi_agent.db.models import (
    Child,
    Interaction,
    MemoryEvent,
    MemoryItem,
    Plan,
    Trial,
)
from rdi_agent.memory.ids import (
    attempt_memory_id,
    content_hash,
    note_memory_id,
)
from rdi_agent.memory.merge import ExistingSnapshot
from rdi_agent.memory.summaries import (
    SummaryItem,
    normalize_summary_items,
)
from rdi_agent.schemas.memory import MemoryCandidate


def _uid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def item_content(row: MemoryItem) -> str:
    """一句短句的正文。

    短句就是一句话，所以正文就是 ``content`` 本身 ——
    没有结构化字段要拼。
    """
    return row.content or ""


def _item_candidate(row: MemoryItem) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=note_memory_id(row.id),
        content=item_content(row),
        status=row.status,
        category=row.category or "other",
        is_preference=bool(row.is_preference),
        inference=bool(row.inference),
        content_hash=row.content_hash or "",
        source_interaction_id=str(row.source_interaction_id or ""),
        source_feedback_id=str(row.source_feedback_id or ""),
        source_turn_id=row.source_turn_id or "",
        source_kind="feedback" if row.source_feedback_id else "interaction",
        source_quote=row.source_quote or "",
        confidence=float(row.confidence or 0.5),
        scope_conditions=[str(x) for x in (row.scope_conditions_json or [])],
    )


class MemoryRepository:
    def __init__(self, session: Any) -> None:
        self.session = session

    # ------------------------------------------------------------ 孩子与锁
    async def get_child(self, child_id: str, family_id: str | None = None) -> Child | None:
        child = await self.session.get(Child, _uid(child_id))
        if child is None:
            return None
        if family_id is not None and str(child.family_id) != str(family_id):
            return None
        return child

    async def lock_child(self, child_id: str) -> Child | None:
        """锁住 ``children`` 那一行，串行化小结 JSON 的读-改-写。

        **为什么必须在事务里**：``memory_summaries_json`` 上没有唯一索引，
        两个并发请求各自读到同一份数组、各自追加、最后一个覆盖前一个 ——
        其中一条小结就这么没了，而且没有任何报错。
        """
        row = (
            await self.session.execute(
                select(Child).where(Child.id == _uid(child_id)).with_for_update()
            )
        ).scalar_one_or_none()
        return row

    # ------------------------------------------------------------ 读取
    async def load_items(
        self, child_id: str, *, limit: int = 200, include_history: bool = False
    ) -> list[MemoryItem]:
        """这个孩子的全部短句。**按时间倒序** —— 最近说的在最前面。"""
        conds = [MemoryItem.child_id == _uid(child_id)]
        if include_history:
            conds.append(MemoryItem.deleted_at.is_(None))
        else:
            conds.append(MemoryItem.status == "active")
            conds.append(MemoryItem.deleted_at.is_(None))
        rows = (
            await self.session.execute(
                select(MemoryItem)
                .where(*conds)
                .order_by(MemoryItem.observed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def load_merging_items(
        self, child_id: str, *, limit: int = 200
    ) -> list[MemoryItem]:
        """**参与类目合并**的短句。

        只有普通短句参与：偏好是硬约束（合并会丢掉强度），
        推断是猜测（合并会变成事实）。

        **没有类目的不参与** —— 老行没类目，让模型给它们补类目
        只会得到一堆编的分类。
        """
        rows = (
            await self.session.execute(
                select(MemoryItem)
                .where(
                    MemoryItem.child_id == _uid(child_id),
                    MemoryItem.status == "active",
                    MemoryItem.deleted_at.is_(None),
                    MemoryItem.category.is_not(None),
                    MemoryItem.is_preference.is_(False),
                    MemoryItem.inference.is_(False),
                )
                .order_by(MemoryItem.observed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def load_preferences(self, child_id: str) -> list[MemoryItem]:
        """家长的偏好（硬约束）。不参与合并，直接进 prompt。"""
        rows = (
            await self.session.execute(
                select(MemoryItem)
                .where(
                    MemoryItem.child_id == _uid(child_id),
                    MemoryItem.status == "active",
                    MemoryItem.deleted_at.is_(None),
                    MemoryItem.is_preference.is_(True),
                )
                .order_by(MemoryItem.observed_at.desc())
            )
        ).scalars().all()
        return list(rows)

    async def load_inferences(self, child_id: str) -> list[MemoryItem]:
        """系统推断出来的短句。进 prompt 时标成"未验证的推测"。"""
        rows = (
            await self.session.execute(
                select(MemoryItem)
                .where(
                    MemoryItem.child_id == _uid(child_id),
                    MemoryItem.status == "active",
                    MemoryItem.deleted_at.is_(None),
                    MemoryItem.inference.is_(True),
                )
                .order_by(MemoryItem.observed_at.desc())
            )
        ).scalars().all()
        return list(rows)

    async def load_forbidden_texts(self, child_id: str) -> list[str]:
        """已作废 / 已删除的短句原文。

        **只给代码用**（``parse_parent_input`` 拿它过滤累积事实），
        **永远不进 prompt** —— 否则家长删掉的隐私会被重新喂回模型。
        """
        rows = (
            await self.session.execute(
                select(MemoryItem.content).where(
                    MemoryItem.child_id == _uid(child_id),
                    (
                        MemoryItem.status.in_(["superseded", "deleted"])
                        | MemoryItem.deleted_at.is_not(None)
                    ),
                )
            )
        ).scalars().all()
        return [str(x) for x in rows if x]

    async def load_attempts(self, child_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Trial, Plan)
                .join(Plan, Plan.id == Trial.plan_id)
                .where(Trial.child_id == _uid(child_id))
                .order_by(Trial.created_at.desc())
                .limit(limit)
            )
        ).all()
        out: list[dict[str, Any]] = []
        for trial, plan in rows:
            title = str((plan.plan_json or {}).get("title") or "")
            out.append(
                {
                    "memory_id": attempt_memory_id(trial.id),
                    "kind": "attempt",
                    "memory_type": "attempt",
                    "content": title or trial.target,
                    "status": trial.status,
                    "created_at": _iso(trial.created_at),
                    "trial_id": str(trial.id),
                }
            )
        return out

    async def load_snapshot(self, child_id: str, source_text: str) -> ExistingSnapshot:
        """合并判定需要的那一份"现有记忆快照"。"""
        rows = await self.load_items(child_id, limit=300)
        return ExistingSnapshot(
            items=[_item_candidate(x) for x in rows],
            source_text=source_text,
        )

    async def get_interaction_id(
        self, conversation_id: str, turn_id: str, role: str = "user"
    ) -> str:
        """这一轮的原始交互 id。找不到返回空串。

        不是幂等键，只是溯源 —— 所以找不到不让调用方失败。
        """
        if not conversation_id or not turn_id:
            return ""
        found = (
            await self.session.execute(
                select(Interaction.id).where(
                    Interaction.conversation_id == conversation_id,
                    Interaction.turn_id == turn_id,
                    Interaction.role == role,
                )
            )
        ).scalar_one_or_none()
        return str(found or "")

    # ------------------------------------------------------------ 写入
    async def update_summary_items(
        self, child: Child, items: list[SummaryItem]
    ) -> None:
        """整份替换小结数组。

        **必须重新赋值而不是原地改 list**：SQLAlchemy 对 JSONB 的原地
        变更是看不见的（它比较的是对象身份），改完不会生成 UPDATE。
        """
        child.memory_summaries_json = [item.to_dict() for item in items]
        child.updated_at = datetime.now(timezone.utc)

    async def insert_item(
        self, *, child_id: str, candidate: MemoryCandidate, status: str = "active"
    ) -> tuple[MemoryItem, bool]:
        values = {
            "id": uuid.uuid4(),
            "child_id": _uid(child_id),
            "conversation_id": candidate.conversation_id or None,
            "content": candidate.content,
            "category": candidate.category or None,
            "is_preference": bool(candidate.is_preference),
            "inference": bool(candidate.inference),
            "scope_conditions_json": list(candidate.scope_conditions),
            "source_kind": "parent_report",
            "confidence": candidate.confidence,
            "content_hash": candidate.content_hash,
            "status": status,
            "source_interaction_id": _opt_uuid(candidate.source_interaction_id),
            "source_feedback_id": _opt_uuid(candidate.source_feedback_id),
            "source_turn_id": candidate.source_turn_id or None,
            "source_quote": candidate.source_quote,
            "supersedes_memory_id": candidate.replaces_memory_id or None,
        }
        stmt = (
            pg_insert(MemoryItem)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(MemoryItem.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = (
                await self.session.execute(
                    select(MemoryItem).where(
                        MemoryItem.child_id == _uid(child_id),
                        MemoryItem.content_hash == candidate.content_hash,
                        MemoryItem.deleted_at.is_(None),
                        MemoryItem.status == "active",
                    )
                )
            ).scalars().first()
            return existing, False  # type: ignore[return-value]
        return await self.session.get(MemoryItem, new_id), True  # type: ignore[return-value]

    async def record_event(
        self,
        *,
        child_id: str,
        memory_table: str,
        memory_id: str,
        event_type: str,
        after: dict[str, Any],
        before: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        confirmed_by_parent: bool = False,
    ) -> bool:
        """写一条审计事件。重放时静默跳过。

        唯一键是 ``(child_id, 会话, turn, content_hash)`` ——
        同一次请求重放两次只留一条，换一轮再写是新的动作，要留痕。
        """
        digest = content_hash(
            "event", f"{memory_table}|{memory_id}|{event_type}|{after}"
        )
        stmt = (
            pg_insert(MemoryEvent)
            .values(
                id=uuid.uuid4(),
                child_id=_uid(child_id),
                memory_table=memory_table,
                memory_id=memory_id,
                event_type=event_type,
                before_json=before or {},
                after_json=after,
                source_conversation_id=conversation_id,
                source_turn_id=turn_id,
                content_hash=digest,
                confirmed_by_parent=confirmed_by_parent,
            )
            .on_conflict_do_nothing()
            .returning(MemoryEvent.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


def _opt_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return _uid(value)
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ 读出来给 API 用
def item_view(row: MemoryItem) -> dict[str, Any]:
    """一条短句的展示口径。

    ``is_preference`` / ``inference`` 两个开关原样带给前端 ——
    家长要能一眼看出"这句是他说的事实""这句是我系统猜的"。
    """
    return {
        "memory_id": note_memory_id(row.id),
        "kind": "note",
        "memory_type": "note",
        "content": item_content(row),
        "status": row.status,
        "confidence": row.confidence,
        "category": row.category,
        "is_preference": bool(row.is_preference),
        "inference": bool(row.inference),
        "scope_conditions": [str(x) for x in (row.scope_conditions_json or [])],
        "source": {
            "kind": "feedback" if row.source_feedback_id else "interaction",
            "interaction_id": str(row.source_interaction_id) if row.source_interaction_id else None,
            "feedback_id": str(row.source_feedback_id) if row.source_feedback_id else None,
            "turn_id": row.source_turn_id,
            "quote": row.source_quote,
        },
        "supersedes_memory_id": row.supersedes_memory_id,
        "superseded_by_memory_id": row.superseded_by_memory_id,
        "observed_at": _iso(row.observed_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def summary_view(item: SummaryItem) -> dict[str, Any]:
    """一条类目小结的展示口径。

    **和 ``item_view`` 的区别**：小结多了 ``derived_from`` ——
    家长要能点开看到"这句话是从哪几句归纳来的"。
    没有它，系统归纳就是个黑盒，家长没法核对也没法纠正。
    """
    return {
        "memory_id": item.id,
        "kind": "summary",
        "memory_type": "summary",
        "content": item.content,
        "status": item.status,
        "confidence_level": item.confidence_level,
        "category": item.category,
        "derived_from": list(item.derived_from),
        "scope_conditions": list(item.scope_conditions),
        # direct = 明细太少、只是把原话复述了一遍（还没做归纳）。
        # 前端据此标"仅一次记录"，提醒家长别把它当特点。
        "source_kind": item.source_kind,
        "source": {
            "kind": "consolidated",
            "interaction_id": item.source_interaction_id or None,
            "feedback_id": None,
            "turn_id": item.source_turn_id or None,
            "quote": item.source_quote,
        },
        "supersedes_memory_id": item.supersedes_memory_id,
        "superseded_by_memory_id": item.superseded_by_memory_id,
        "created_at": item.created_at or None,
        "updated_at": item.updated_at or None,
    }


async def list_memories(
    session: Any,
    *,
    child_id: str,
    family_id: str,
    types: set[str] | None = None,
    include_history: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """一个孩子的全部记忆，按展示口径整理好。"""
    repo = MemoryRepository(session)
    child = await repo.get_child(child_id, family_id)
    if child is None:
        return {}

    wanted = types or {"summary", "note", "attempt"}
    items: list[dict[str, Any]] = []

    if "summary" in wanted:
        statuses = ["active"]
        if include_history:
            statuses += ["superseded", "deleted"]
        for item in normalize_summary_items(child.memory_summaries_json):
            if item.status not in statuses:
                continue
            if not include_history and item.deleted_at:
                continue
            items.append(summary_view(item))

    if "note" in wanted:
        rows = await repo.load_items(
            child_id, limit=limit, include_history=include_history
        )
        items.extend(item_view(r) for r in rows)

    if "attempt" in wanted:
        items.extend(await repo.load_attempts(child_id, limit=5))

    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    age = describe_age(child.birth_date)
    return {
        "child_id": child_id,
        "family_id": str(child.family_id),
        "display_label": child.display_label,
        "birth_date": age.get("birth_date"),
        "age_display": age.get("age_display"),
        "age_band": age.get("age_band") or child.age_band,
        "memories": items[:limit],
        "counts": counts,
        "truncated": len(items) > limit,
    }


__all__ = [
    "MemoryRepository",
    "item_content",
    "item_view",
    "list_memories",
    "summary_view",
]
