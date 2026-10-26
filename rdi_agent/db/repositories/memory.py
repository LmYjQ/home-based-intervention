"""长期记忆的数据访问层。

和 :class:`rdi_agent.db.repositories.case.Repository` 一样：**不自己 commit**，
事务边界归调用方的 ``session_scope()``。

**这一层只做"取/存"，不做判断**。哪条能自动记、哪条要问家长，
是 :mod:`rdi_agent.memory.merge` 的事 —— 那部分要能脱离数据库单测。

**事实为什么单独走 JSON**：见 :mod:`rdi_agent.memory.facts` ——
计划 §10.2 把 L1 放在 ``children`` 上。代价是它没有数据库级唯一约束，
所以写入前必须 :meth:`lock_child` 拿行锁，否则两个并发请求会各写一份。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rdi_agent.db.models import (
    Child,
    Hypothesis,
    Interaction,
    MemoryEvent,
    Observation,
    Plan,
    Trial,
)
from rdi_agent.memory.facts import (
    FactItem,
    active_facts,
    forbidden_fact_texts,
    new_fact_item,
    normalize_fact_items,
    pending_facts,
)
from rdi_agent.memory.ids import (
    attempt_memory_id,
    content_hash,
    fact_memory_id,
    hypothesis_memory_id,
    observation_memory_id,
)
from rdi_agent.memory.merge import ExistingSnapshot
from rdi_agent.schemas.memory import MemoryCandidate


def _uid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def observation_content(row: Observation) -> str:
    """把结构化观察拼成一句能读的话。

    列表展示、prompt 背景、去重哈希都用它 —— 三处必须是同一份文本，
    否则"同一条观察"在不同地方算出不同哈希，去重就形同虚设。
    """
    parts: list[str] = []
    if row.setting:
        parts.append(f"在{row.setting}")
    if row.antecedent:
        parts.append(f"当时{row.antecedent}")
    if row.observable_behavior:
        parts.append(row.observable_behavior)
    if row.adult_response:
        parts.append(f"大人{row.adult_response}")
    if row.child_response:
        parts.append(f"孩子{row.child_response}")
    if row.outcome:
        parts.append(f"结果{row.outcome}")
    return "；".join(parts)


_CONFIDENCE_TO_LEVEL = ((0.7, "high"), (0.4, "medium"))
_LEVEL_TO_CONFIDENCE = {"high": 0.8, "medium": 0.55, "low": 0.3}


def confidence_level(value: float) -> str:
    for threshold, level in _CONFIDENCE_TO_LEVEL:
        if value >= threshold:
            return level
    return "low"


def _observation_candidate(row: Observation) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=observation_memory_id(row.id),
        memory_type="episode",
        content=observation_content(row),
        status=row.status,
        content_hash=row.content_hash or "",
        source_interaction_id=str(row.source_interaction_id or ""),
        source_feedback_id=str(row.source_feedback_id or ""),
        source_turn_id=row.source_turn_id or "",
        source_kind="feedback" if row.source_feedback_id else "interaction",
        source_quote=row.source_quote or "",
        confidence=float(row.confidence or 0.5),
        setting=row.setting or "",
        antecedent=row.antecedent or "",
        observable_behavior=row.observable_behavior or "",
        adult_response=row.adult_response or "",
        child_response=row.child_response or "",
        outcome=row.outcome or "",
        is_attempt=bool(row.is_attempt),
    )


def _hypothesis_candidate(row: Hypothesis) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=hypothesis_memory_id(row.id),
        memory_type="hypothesis",
        content=row.hypothesis_text,
        status=row.status,
        content_hash=row.content_hash or "",
        source_interaction_id=str(row.source_interaction_id or ""),
        source_feedback_id=str(row.source_feedback_id or ""),
        source_turn_id=row.source_turn_id or "",
        source_kind="feedback" if row.source_feedback_id else "interaction",
        source_quote=row.source_quote or "",
        confidence=_LEVEL_TO_CONFIDENCE.get(row.confidence_level, 0.3),
        scope_conditions=[str(x) for x in (row.scope_conditions_json or [])],
    )


def fact_to_candidate(item: FactItem) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=item.id,
        memory_type=item.memory_type,  # type: ignore[arg-type]
        content=item.content,
        status=item.status,
        content_hash=item.content_hash,
        source_interaction_id=item.source_interaction_id,
        source_feedback_id=item.source_feedback_id,
        source_turn_id=item.source_turn_id,
        source_kind=item.source_kind,  # type: ignore[arg-type]
        source_quote=item.source_quote,
        confidence=item.confidence,
        scope_conditions=list(item.scope_conditions),
        replaces_memory_id=item.supersedes_memory_id or "",
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
        """锁住 ``children`` 那一行，串行化事实 JSON 的读-改-写。

        **为什么必须在事务里**：``confirmed_facts_json`` 上没有唯一索引，
        两个并发请求各自读到同一份数组、各自追加、最后一个覆盖前一个 ——
        其中一条记忆就这么没了，而且没有任何报错。
        """
        row = (
            await self.session.execute(
                select(Child).where(Child.id == _uid(child_id)).with_for_update()
            )
        ).scalar_one_or_none()
        return row

    # ------------------------------------------------------------ 读取
    async def load_fact_items(self, child_id: str) -> list[FactItem]:
        child = await self.session.get(Child, _uid(child_id))
        if child is None:
            return []
        return normalize_fact_items(child.confirmed_facts_json)

    async def load_observations(
        self, child_id: str, *, limit: int = 20, include_pending: bool = True
    ) -> list[Observation]:
        statuses = ["active"] + (["pending_confirmation"] if include_pending else [])
        rows = (
            await self.session.execute(
                select(Observation)
                .where(
                    Observation.child_id == _uid(child_id),
                    Observation.status.in_(statuses),
                    Observation.deleted_at.is_(None),
                )
                .order_by(Observation.observed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def load_hypotheses(
        self, child_id: str, *, limit: int = 20
    ) -> list[Hypothesis]:
        rows = (
            await self.session.execute(
                select(Hypothesis)
                .where(
                    Hypothesis.child_id == _uid(child_id),
                    Hypothesis.status.in_(
                        ["candidate", "active", "weakened"]
                    ),
                    Hypothesis.deleted_at.is_(None),
                )
                .order_by(Hypothesis.updated_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

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
        facts = await self.load_fact_items(child_id)
        observations = await self.load_observations(child_id, limit=30)
        hypotheses = await self.load_hypotheses(child_id, limit=30)
        return ExistingSnapshot(
            facts=[
                fact_to_candidate(x)
                for x in facts
                if x.status in ("confirmed", "pending_confirmation")
            ],
            hypotheses=[_hypothesis_candidate(x) for x in hypotheses],
            observations=[_observation_candidate(x) for x in observations],
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
    async def update_fact_items(self, child: Child, items: list[FactItem]) -> None:
        """整份替换事实数组。

        **必须重新赋值而不是原地改 list**：SQLAlchemy 对 JSONB 的原地
        变更是看不见的（它比较的是对象身份），改完不会生成 UPDATE。
        """
        child.confirmed_facts_json = [item.to_dict() for item in items]
        child.updated_at = datetime.now(timezone.utc)

    async def insert_observation(
        self, *, child_id: str, candidate: MemoryCandidate, status: str
    ) -> tuple[Observation, bool]:
        values = {
            "id": uuid.uuid4(),
            "child_id": _uid(child_id),
            "conversation_id": candidate.conversation_id or None,
            "setting": candidate.setting or None,
            "antecedent": candidate.antecedent,
            "observable_behavior": candidate.observable_behavior or candidate.content,
            "adult_response": candidate.adult_response,
            "child_response": candidate.child_response,
            "outcome": candidate.outcome,
            "source_kind": "parent_report",
            "confidence": candidate.confidence,
            "content_hash": candidate.content_hash,
            "status": status,
            "is_attempt": candidate.is_attempt,
            "source_interaction_id": _opt_uuid(candidate.source_interaction_id),
            "source_feedback_id": _opt_uuid(candidate.source_feedback_id),
            "source_turn_id": candidate.source_turn_id or None,
            "source_quote": candidate.source_quote,
        }
        stmt = (
            pg_insert(Observation)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(Observation.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = (
                await self.session.execute(
                    select(Observation).where(
                        Observation.child_id == _uid(child_id),
                        Observation.content_hash == candidate.content_hash,
                        Observation.deleted_at.is_(None),
                        Observation.status.in_(["active", "pending_confirmation"]),
                    )
                )
            ).scalars().first()
            return existing, False  # type: ignore[return-value]
        return await self.session.get(Observation, new_id), True  # type: ignore[return-value]

    async def insert_hypothesis(
        self, *, child_id: str, candidate: MemoryCandidate, status: str
    ) -> tuple[Hypothesis, bool]:
        values = {
            "id": uuid.uuid4(),
            "child_id": _uid(child_id),
            "hypothesis_text": candidate.content,
            "confidence_level": confidence_level(candidate.confidence),
            "scope_conditions_json": list(candidate.scope_conditions),
            "status": status,
            "created_by": "llm" if candidate.inference else "parent",
            "content_hash": candidate.content_hash,
            "source_interaction_id": _opt_uuid(candidate.source_interaction_id),
            "source_feedback_id": _opt_uuid(candidate.source_feedback_id),
            "source_turn_id": candidate.source_turn_id or None,
            "source_quote": candidate.source_quote,
            "supersedes_memory_id": candidate.replaces_memory_id or None,
        }
        stmt = (
            pg_insert(Hypothesis)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(Hypothesis.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = (
                await self.session.execute(
                    select(Hypothesis).where(
                        Hypothesis.child_id == _uid(child_id),
                        Hypothesis.content_hash == candidate.content_hash,
                        Hypothesis.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            return existing, False  # type: ignore[return-value]
        return await self.session.get(Hypothesis, new_id), True  # type: ignore[return-value]

    async def weaken_hypothesis(
        self, *, child_id: str, memory_id: str, observation_memory_id_: str
    ) -> Hypothesis | None:
        """给假设挂一条反例，并降一档置信度。

        **反例要真的改变结论**，不能只是记一笔了事 ——
        计划 §10.5 的原话是"降低置信度或标记 weakened"。
        所以这里同时做三件事：状态、置信度、反例列表。
        """
        row = (
            await self.session.execute(
                select(Hypothesis).where(
                    Hypothesis.child_id == _uid(child_id),
                    Hypothesis.id == _uid(memory_id),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        contradicting = list(row.contradicting_observation_ids_json or [])
        if observation_memory_id_ not in contradicting:
            contradicting.append(observation_memory_id_)
        row.contradicting_observation_ids_json = contradicting
        row.status = "weakened"
        order = ["low", "medium", "high"]
        idx = order.index(row.confidence_level) if row.confidence_level in order else 0
        row.confidence_level = order[max(0, idx - 1)]
        row.last_reviewed_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        return row

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
def fact_item_view(item: FactItem) -> dict[str, Any]:
    return {
        "memory_id": item.id,
        "kind": item.memory_type,
        "memory_type": item.memory_type,
        "content": item.content,
        "status": item.status,
        "confidence": item.confidence,
        "scope_conditions": list(item.scope_conditions),
        "source": {
            "kind": item.source_kind,
            "interaction_id": item.source_interaction_id or None,
            "feedback_id": item.source_feedback_id or None,
            "turn_id": item.source_turn_id or None,
            "quote": item.source_quote,
        },
        "supersedes_memory_id": item.supersedes_memory_id,
        "superseded_by_memory_id": item.superseded_by_memory_id,
        "contradicting_observation_ids": [],
        "is_attempt": False,
        "created_at": item.created_at or None,
        "updated_at": item.updated_at or None,
    }


def observation_view(row: Observation) -> dict[str, Any]:
    return {
        "memory_id": observation_memory_id(row.id),
        "kind": "observation",
        "memory_type": "episode",
        "content": observation_content(row),
        "status": row.status,
        "confidence": row.confidence,
        "setting": row.setting,
        "antecedent": row.antecedent,
        "observable_behavior": row.observable_behavior,
        "adult_response": row.adult_response,
        "child_response": row.child_response,
        "outcome": row.outcome,
        "scope_conditions": [],
        "source": {
            "kind": "feedback" if row.source_feedback_id else "interaction",
            "interaction_id": str(row.source_interaction_id) if row.source_interaction_id else None,
            "feedback_id": str(row.source_feedback_id) if row.source_feedback_id else None,
            "turn_id": row.source_turn_id,
            "quote": row.source_quote,
        },
        "supersedes_memory_id": row.supersedes_memory_id,
        "superseded_by_memory_id": row.superseded_by_memory_id,
        "contradicting_observation_ids": [],
        "is_attempt": bool(row.is_attempt),
        "observed_at": _iso(row.observed_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def hypothesis_view(row: Hypothesis) -> dict[str, Any]:
    return {
        "memory_id": hypothesis_memory_id(row.id),
        "kind": "hypothesis",
        "memory_type": "hypothesis",
        "content": row.hypothesis_text,
        "status": row.status,
        "confidence": _LEVEL_TO_CONFIDENCE.get(row.confidence_level, 0.3),
        "confidence_level": row.confidence_level,
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
        "contradicting_observation_ids": list(
            row.contradicting_observation_ids_json or []
        ),
        "supporting_observation_ids": list(row.supporting_observation_ids_json or []),
        "is_attempt": False,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
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

    wanted = types or {"fact", "preference", "observation", "hypothesis", "attempt"}
    items: list[dict[str, Any]] = []

    if {"fact", "preference"} & wanted:
        for item in normalize_fact_items(child.confirmed_facts_json):
            if item.memory_type not in wanted:
                continue
            # **待确认的要显示** —— 它正是"等家长点确认"的那些条目。
            # 默认列表只藏起作废和删除的。
            if not include_history and (
                item.deleted_at
                or item.status not in ("confirmed", "pending_confirmation")
            ):
                continue
            items.append(fact_item_view(item))

    if "observation" in wanted:
        statuses = ["active", "pending_confirmation"]
        if include_history:
            statuses += ["superseded", "deleted"]
        conds = [
            Observation.child_id == _uid(child_id),
            Observation.status.in_(statuses),
        ]
        if not include_history:
            conds.append(Observation.deleted_at.is_(None))
        rows = (
            await session.execute(
                select(Observation)
                .where(*conds)
                .order_by(Observation.observed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        items.extend(observation_view(r) for r in rows)

    if "hypothesis" in wanted:
        statuses = ["candidate", "active", "weakened"]
        if include_history:
            statuses += ["superseded", "deleted"]
        conds = [
            Hypothesis.child_id == _uid(child_id),
            Hypothesis.status.in_(statuses),
        ]
        if not include_history:
            conds.append(Hypothesis.deleted_at.is_(None))
        rows = (
            await session.execute(
                select(Hypothesis)
                .where(*conds)
                .order_by(Hypothesis.updated_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        items.extend(hypothesis_view(r) for r in rows)

    if "attempt" in wanted:
        items.extend(await repo.load_attempts(child_id, limit=5))

    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    return {
        "child_id": child_id,
        "family_id": str(child.family_id),
        "display_label": child.display_label,
        "age_band": child.age_band,
        "memories": items[:limit],
        "counts": counts,
        "pending_confirmation": sum(
            1 for x in items if x["status"] == "pending_confirmation"
        ),
        "truncated": len(items) > limit,
    }


def build_context_patch(child: Child) -> dict[str, Any]:
    """写完之后重新算一遍给 prompt 的上下文片段。"""
    items = normalize_fact_items(child.confirmed_facts_json)
    live = active_facts(items)
    pend = pending_facts(items)
    return {
        "confirmed_facts": [x.content for x in live if x.memory_type == "fact"],
        "preferences": [x.content for x in live if x.memory_type == "preference"],
        "forbidden_fact_texts": forbidden_fact_texts(items),
        "pending_fact_texts": [x.content for x in pend],
    }


def new_fact_from_candidate(
    candidate: MemoryCandidate,
    *,
    existing: list[FactItem],
    status: str,
    supersedes_memory_id: str | None = None,
) -> FactItem:
    return new_fact_item(
        memory_type=str(candidate.memory_type),
        content=candidate.content,
        digest=candidate.content_hash
        or content_hash(str(candidate.memory_type), candidate.content),
        existing=existing,
        status=status,
        source_interaction_id=candidate.source_interaction_id,
        source_feedback_id=candidate.source_feedback_id,
        source_turn_id=candidate.source_turn_id,
        source_kind=candidate.source_kind,
        source_quote=candidate.source_quote,
        confidence=candidate.confidence,
        scope_conditions=candidate.scope_conditions,
        supersedes_memory_id=supersedes_memory_id,
    )


def mark_fact_superseded(
    items: list[FactItem], target_id: str, new_id: str
) -> FactItem | None:
    for item in items:
        if item.id == target_id:
            item.status = "superseded"
            item.superseded_by_memory_id = new_id
            item.updated_at = datetime.now(timezone.utc).isoformat()
            return item
    return None


def mark_fact_deleted(items: list[FactItem], target_id: str) -> FactItem | None:
    for item in items:
        if item.id == target_id:
            item.status = "deleted"
            item.deleted_at = datetime.now(timezone.utc).isoformat()
            item.updated_at = item.deleted_at
            return item
    return None


def find_fact(items: list[FactItem], memory_id: str) -> FactItem | None:
    return next((x for x in items if x.id == memory_id), None)


__all__ = [
    "MemoryRepository",
    "build_context_patch",
    "fact_memory_id",
    "fact_to_candidate",
    "find_fact",
    "list_memories",
    "mark_fact_deleted",
    "mark_fact_superseded",
    "new_fact_from_candidate",
    "observation_content",
    "observation_view",
    "hypothesis_view",
]
