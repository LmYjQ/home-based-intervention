"""阶段 1 的数据访问层。

只做三件事：建最小档案、记交互、提交方案选择。阶段 3 才加记忆读写。

**所有写操作都必须幂等**：HTTP 请求会重试，重试不能多建一条方案或多建一次尝试。
做法是用唯一约束（``interactions`` 的 UNIQUE(conversation_id, turn_id, role)、
``feedbacks`` 的 UNIQUE(trial_id, turn_id)）在数据库层挡住，而不是靠先查后写 ——
先查后写在并发下不成立。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rdi_agent.db.models import (
    Child,
    EvidenceBundleRecord,
    Feedback,
    Interaction,
    Plan,
    Trial,
)
from rdi_agent.schemas.feedback import FeedbackSubmission
from rdi_agent.schemas.recommendation import RecommendationCard


class Repository:
    """把 Stage 数据访问收在一个对象里，节点只跟它打交道。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ 最小档案
    async def ensure_child(
        self,
        *,
        family_id: str,
        display_label: str,
        age_band: str | None = None,
        child_id: str | None = None,
    ) -> Child:
        """按 child_id 或 (family_id, label) 拿档案，没有就建。

        阶段 1 只需要最小档案：显示名 + 年龄段。完整的沟通档案、兴趣强项
        留给阶段 3 的记忆写入。
        """
        if child_id:
            found = await self.session.get(Child, uuid.UUID(str(child_id)))
            if found is not None:
                return found

        fam = uuid.UUID(str(family_id))
        existing = (
            await self.session.execute(
                select(Child).where(
                    Child.family_id == fam, Child.display_label == display_label
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        child = Child(
            family_id=fam,
            display_label=display_label,
            age_band=age_band,
        )
        self.session.add(child)
        await self.session.flush()
        return child

    async def child_context(self, child_id: str) -> dict[str, Any]:
        """给 prompt 用的最小上下文。刻意很薄，别把整张表塞进去。"""
        child = await self.session.get(Child, uuid.UUID(str(child_id)))
        if child is None:
            return {}
        return {
            "display_label": child.display_label,
            "age_band": child.age_band,
            "family_goals": child.family_goals_json or [],
            "known_contexts": child.known_contexts_json or [],
            "confirmed_facts": child.confirmed_facts_json or [],
        }

    # ------------------------------------------------------------ 交互记录
    async def record_interaction(
        self,
        *,
        conversation_id: str,
        thread_id: str,
        turn_id: str,
        role: str,
        content: str,
        family_id: str,
        child_id: str | None = None,
        trace: dict[str, Any] | None = None,
    ) -> bool:
        """写一条交互。重复的 (conversation_id, turn_id, role) 静默跳过。

        返回 True 表示这次真的写进去了。ON CONFLICT DO NOTHING 靠数据库唯一索引
        挡重放，不靠应用层判断。
        """
        stmt = (
            pg_insert(Interaction)
            .values(
                family_id=uuid.UUID(str(family_id)),
                child_id=uuid.UUID(str(child_id)) if child_id else None,
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                role=role,
                content=content,
                trace_json=trace or {},
            )
            # 二选一，不能同时给。给 constraint 名更稳：万一将来唯一索引
            # 的列变了，这里不用跟着改。
            .on_conflict_do_nothing(constraint="uq_interactions_turn_role")
            .returning(Interaction.id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_interactions(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Interaction)
                .where(Interaction.conversation_id == conversation_id)
                .order_by(Interaction.created_at, Interaction.role)
            )
        ).scalars().all()
        return [
            {
                "turn_id": r.turn_id,
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 证据包
    async def save_evidence_bundle(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        bundle: dict[str, Any],
        retrieval_version: str = "v1",
    ) -> uuid.UUID:
        """把证据包落库，返回它的 id。

        幂等：靠 ``evidence_bundles`` 上的 UNIQUE(conversation_id, turn_id)
        挡重放（迁移 003 加的）。

        **重复写会覆盖**：同一轮里如果发生过追问循环，检索会跑不止一次，
        最后一次才是"最终回答所依据的那份"。所以这里用 DO UPDATE 而不是
        DO NOTHING —— 追溯时要看到的是给出回答时依据的那份。
        """
        stmt = (
            pg_insert(EvidenceBundleRecord)
            .values(
                conversation_id=conversation_id,
                turn_id=turn_id,
                bundle_json=bundle,
                retrieval_version=retrieval_version,
            )
            .on_conflict_do_update(
                constraint="uq_evidence_bundles_conversation_turn",
                set_={
                    "bundle_json": bundle,
                    "retrieval_version": retrieval_version,
                },
            )
            .returning(EvidenceBundleRecord.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is not None:
            return new_id
        existing = (
            await self.session.execute(
                select(EvidenceBundleRecord).where(
                    EvidenceBundleRecord.conversation_id == conversation_id,
                    EvidenceBundleRecord.turn_id == turn_id,
                )
            )
        ).scalar_one()
        return existing.id

    async def get_evidence_bundle(
        self, conversation_id: str, turn_id: str
    ) -> EvidenceBundleRecord | None:
        return (
            await self.session.execute(
                select(EvidenceBundleRecord).where(
                    EvidenceBundleRecord.conversation_id == conversation_id,
                    EvidenceBundleRecord.turn_id == turn_id,
                )
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------ 方案
    async def save_candidate_plans(
        self,
        *,
        conversation_id: str,
        family_id: str,
        child_id: str | None,
        cards: list[RecommendationCard],
        evidence_bundle_id: uuid.UUID | None = None,
    ) -> dict[str, uuid.UUID]:
        """把候选建议卡落库，返回 {plan_id: db_uuid}。

        同一 conversation 已经存过的卡不再重复写 —— 一轮只应该出一组候选，
        重试同一轮请求时保住原来的 UUID，不然 select-plan 会指向一条新记录。
        """
        existing = (
            await self.session.execute(
                select(Plan).where(Plan.conversation_id == conversation_id)
            )
        ).scalars().all()
        by_business_id = {
            p.plan_json.get("plan_id"): p.id for p in existing if p.plan_json
        }
        mapping = {bid: pid for bid, pid in by_business_id.items() if bid}

        for card in cards:
            if card.plan_id in mapping:
                continue
            row = Plan(
                family_id=uuid.UUID(str(family_id)),
                child_id=uuid.UUID(str(child_id)) if child_id else None,
                conversation_id=conversation_id,
                plan_json=card.model_dump(mode="json"),
                evidence_bundle_id=evidence_bundle_id,
                application_label=card.application_label,
                recommended=card.recommended,
                status="proposed",
            )
            self.session.add(row)
            await self.session.flush()
            mapping[card.plan_id] = row.id
        return mapping

    async def select_plan(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        turn_id: str,
        child_id: str,
        target: str,
    ) -> tuple[Plan | None, Trial | None]:
        """把一张卡标成 selected 并建 trial。用 (plan_id) 判重。

        幂等靠"这个 plan_id 上已经有 active 的 trial 就直接返回"，
        所以同一个 select-plan 请求重发不会产生第二条尝试。
        """
        plans = (
            await self.session.execute(
                select(Plan).where(Plan.conversation_id == conversation_id)
            )
        ).scalars().all()
        target_plan = next(
            (p for p in plans if p.plan_json.get("plan_id") == plan_id), None
        )
        if target_plan is None:
            return None, None

        for p in plans:
            if p.id != target_plan.id and p.status == "selected":
                p.status = "archived"

        prior = (
            await self.session.execute(
                select(Trial).where(
                    Trial.plan_id == target_plan.id, Trial.status == "accepted"
                )
            )
        ).scalars().first()
        if prior is not None:
            target_plan.status = "selected"
            return target_plan, prior

        target_plan.status = "selected"
        trial = Trial(
            plan_id=target_plan.id,
            child_id=uuid.UUID(str(child_id)),
            conversation_id=conversation_id,
            target=target,
            status="accepted",
            planned_context_json={"turn_id": turn_id},
        )
        self.session.add(trial)
        await self.session.flush()
        return target_plan, trial

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self.session.get(Plan, uuid.UUID(str(plan_id)))

    async def list_plans(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Plan)
                .where(Plan.conversation_id == conversation_id)
                .order_by(Plan.created_at)
            )
        ).scalars().all()
        return [
            {
                "db_plan_id": str(r.id),
                "plan_id": (r.plan_json or {}).get("plan_id"),
                "title": (r.plan_json or {}).get("title"),
                "status": r.status,
                "recommended": r.recommended,
                "application_label": r.application_label,
            }
            for r in rows
        ]

    async def get_trial(self, trial_id: str) -> Trial | None:
        return await self.session.get(Trial, uuid.UUID(str(trial_id)))

    # ------------------------------------------------------------ 反馈
    async def save_feedback(
        self, *, trial_id: str, submission: FeedbackSubmission
    ) -> tuple[Feedback | None, bool]:
        """写反馈。UNIQUE(trial_id, turn_id) 保证重试不重复写。

        返回 (记录, 是否新建)。
        """
        stmt = (
            pg_insert(Feedback)
            .values(
                trial_id=uuid.UUID(str(trial_id)),
                turn_id=submission.turn_id,
                attempted=submission.attempted,
                implementation_notes=submission.implementation_notes,
                child_response_json=submission.child_response.model_dump(),
                parent_observations_json=submission.parent_observations,
                difficulties_json=submission.difficulties,
            )
            .on_conflict_do_nothing(constraint="uq_feedbacks_trial_turn")
            .returning(Feedback.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = (
                await self.session.execute(
                    select(Feedback).where(
                        Feedback.trial_id == uuid.UUID(str(trial_id)),
                        Feedback.turn_id == submission.turn_id,
                    )
                )
            ).scalars().first()
            return existing, False
        return await self.session.get(Feedback, new_id), True