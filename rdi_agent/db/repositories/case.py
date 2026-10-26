"""阶段 1 的数据访问层。

只做三件事：建最小档案、记交互、提交方案选择。阶段 3 才加记忆读写。

**所有写操作都必须幂等**：HTTP 请求会重试，重试不能多建一条方案或多建一次尝试。
做法是用唯一约束（``interactions`` 的 UNIQUE(conversation_id, turn_id, role)、
``feedbacks`` 的 UNIQUE(trial_id, turn_id)）在数据库层挡住，而不是靠先查后写 ——
先查后写在并发下不成立。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
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
        birth_date: date | None = None,
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
            birth_date=birth_date,
        )
        self.session.add(child)
        await self.session.flush()
        return child

    async def get_child(self, child_id: str) -> Child | None:
        """按 id 取档案。给"复用已有孩子开新会话"用。"""
        return await self.session.get(Child, uuid.UUID(str(child_id)))

    async def list_children(
        self, *, limit: int = 100, label_prefix: str | None = None
    ) -> list[Child]:
        """列出孩子，新建的排在前面。

        只给手动测试台的账号下拉用 —— **不按 family 过滤**，
        因为测试台本来就是一个人在自己电脑上对比几套数据。

        ``label_prefix`` 用来只看测试账号：老的演示孩子全叫"孩子"，
        混进下拉里会出现几十个同名选项，没法选。
        """
        stmt = select(Child)
        if label_prefix:
            stmt = stmt.where(Child.display_label.like(f"{label_prefix}%"))
        rows = await self.session.execute(
            stmt.order_by(Child.created_at.desc()).limit(limit)
        )
        return list(rows.scalars().all())

    async def child_context(self, child_id: str) -> dict[str, Any]:
        """给 prompt 用的最小上下文。刻意很薄，别把整张表塞进去。

        **事实只给"活着的"**：阶段 3 之后 ``confirmed_facts_json`` 里
        可能躺着已作废、已删除的条目（审计要留）。把它们喂给模型，
        家长纠正过的旧信息就又回来了 —— 这正是阶段 3 要防的事。

        旧数据是裸字符串数组，:func:`normalize_fact_items` 兼容。
        """
        from rdi_agent.age import describe_age
        from rdi_agent.memory.facts import active_facts, normalize_fact_items

        child = await self.session.get(Child, uuid.UUID(str(child_id)))
        if child is None:
            return {}
        live = active_facts(normalize_fact_items(child.confirmed_facts_json))
        # **年龄每次现算**：家长可能用这个系统好几年，
        # 存下来的年龄过一年就是错的。
        age = describe_age(child.birth_date)
        return {
            "display_label": child.display_label,
            "birth_date": age.get("birth_date"),
            "age_display": age.get("age_display"),
            "age_years": age.get("age_years"),
            "age_months": age.get("age_months"),
            # 有出生日期就以现算的段位为准；老档案退回存量的 age_band
            "age_band": age.get("age_band") or child.age_band,
            "family_goals": child.family_goals_json or [],
            "known_contexts": child.known_contexts_json or [],
            "confirmed_facts": [
                x.content for x in live if x.memory_type == "fact"
            ],
            "preferences": [
                x.content for x in live if x.memory_type == "preference"
            ],
            "communication_profile": child.communication_profile_json or {},
            "strengths_interests": child.strengths_interests_json or [],
            "support_preferences": child.support_preferences_json or {},
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
            # 把版本钉在这次尝试上：方案以后被调整，历史反馈仍对应当时的版本。
            plan_version=target_plan.plan_version,
            planned_context_json={"turn_id": turn_id},
        )
        self.session.add(trial)
        await self.session.flush()
        return target_plan, trial

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self.session.get(Plan, uuid.UUID(str(plan_id)))

    async def list_plans(self, conversation_id: str) -> list[dict[str, Any]]:
        """这个会话的方案卡。

        **返回整张卡**（``plan_json`` 展开在顶层），不只是摘要字段。

        **为什么**：前端刷新页面后靠这个接口把方案卡接回来。
        之前只返回 plan_id / title / status / recommended / application_label，
        前端把这份数据直接喂给 ``PlanCard`` 组件渲染 ——
        于是卡上只剩标题，**引用、家长可以做的事、观察指标全没了**，
        看起来像"引用被清空了"。

        顶层同时保留那几个摘要字段（历史上就是这么用的，
        ``test_api_conversation_flow`` 也这么断言），
        整张卡的内容放在 ``card`` 下面。
        """
        rows = (
            await self.session.execute(
                select(Plan)
                .where(Plan.conversation_id == conversation_id)
                .order_by(Plan.created_at)
            )
        ).scalars().all()
        result: list[dict[str, Any]] = []
        for r in rows:
            card = dict(r.plan_json or {})
            item = {
                "db_plan_id": str(r.id),
                "plan_id": card.get("plan_id"),
                "title": card.get("title"),
                "status": r.status,
                "recommended": r.recommended,
                "application_label": r.application_label,
                # 整张卡。前面那几个字段是为了兼容，card 才是完整的。
                **card,
                "card": card,
            }
            result.append(item)
        return result

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
                stopped=submission.stopped,
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

    async def get_feedback(self, *, trial_id: str, turn_id: str) -> Feedback | None:
        """按幂等键取反馈。给"重试直接返回缓存"和刷新页面用。"""
        row = (
            await self.session.execute(
                select(Feedback).where(
                    Feedback.trial_id == uuid.UUID(str(trial_id)),
                    Feedback.turn_id == turn_id,
                )
            )
        ).scalars().first()
        return row

    async def update_feedback_result(
        self,
        *,
        feedback_id: str,
        analysis_json: dict[str, Any] | None = None,
        next_step_json: dict[str, Any] | None = None,
        followup_answers_json: list[dict[str, Any]] | None = None,
        followup_rounds: int | None = None,
        result_json: dict[str, Any] | None = None,
        analyzed: bool = False,
        completed: bool = False,
    ) -> Feedback | None:
        """把分析/下一步/追问答案写回反馈行。

        **为什么落库而不是只放 state**：反馈入口没有选方案中断，
        重试同一个 ``turn_id`` 时 Checkpointer 里可能已经是终态；
        从库里读缓存比再跑一次 LLM 便宜，也保证结果稳定。
        """
        row = await self.session.get(Feedback, uuid.UUID(str(feedback_id)))
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        if analysis_json is not None:
            row.analysis_json = analysis_json
        if next_step_json is not None:
            row.next_step_json = next_step_json
        if followup_answers_json is not None:
            row.followup_answers_json = followup_answers_json
        if followup_rounds is not None:
            row.followup_rounds = followup_rounds
        if result_json is not None:
            row.result_json = result_json
        if analyzed and row.analyzed_at is None:
            row.analyzed_at = now
        if completed:
            row.completed_at = now
        await self.session.flush()
        return row

    async def update_trial_execution(
        self,
        *,
        trial_id: str,
        status: str,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> Trial | None:
        """记录这次尝试的实际执行状态。

        **只在反馈首次写入时调用**：旧反馈重试不能把已经更新的状态改回去。
        """
        trial = await self.session.get(Trial, uuid.UUID(str(trial_id)))
        if trial is None:
            return None
        trial.status = status
        if started_at is not None and trial.started_at is None:
            trial.started_at = started_at
        if ended_at is not None:
            trial.ended_at = ended_at
        await self.session.flush()
        return trial

    async def load_attempt_history(
        self,
        *,
        child_id: str,
        exclude_trial_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """这个孩子过去的尝试 + 对应方案 + 最近一条反馈。

        **给"历史对比"用**：``analyze_feedback`` 只拿到当前反馈时，
        说得出"这次做了什么"，说不出"和上次比有什么不同"。
        条数刻意很少（默认 5），prompt 不会因为历史膨胀。
        """
        stmt = select(Trial).where(Trial.child_id == uuid.UUID(str(child_id)))
        if exclude_trial_id:
            stmt = stmt.where(Trial.id != uuid.UUID(str(exclude_trial_id)))
        trials = (
            await self.session.execute(
                stmt.order_by(Trial.created_at.desc()).limit(limit)
            )
        ).scalars().all()

        history: list[dict[str, Any]] = []
        for trial in trials:
            plan = await self.session.get(Plan, trial.plan_id)
            feedback = (
                await self.session.execute(
                    select(Feedback)
                    .where(Feedback.trial_id == trial.id)
                    .order_by(Feedback.submitted_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            card = dict(plan.plan_json or {}) if plan is not None else {}
            history.append(
                {
                    "trial_id": str(trial.id),
                    "plan_title": card.get("title") or trial.target,
                    "plan_version": trial.plan_version,
                    "trial_status": trial.status,
                    "attempted": feedback.attempted if feedback else None,
                    "stopped": feedback.stopped if feedback else False,
                    "implementation_notes": (
                        feedback.implementation_notes if feedback else ""
                    ),
                    "parent_observations": (
                        feedback.parent_observations_json if feedback else []
                    ),
                    "difficulties": feedback.difficulties_json if feedback else [],
                    "analysis": feedback.analysis_json if feedback else {},
                    "submitted_at": (
                        feedback.submitted_at.isoformat()
                        if feedback and feedback.submitted_at
                        else None
                    ),
                }
            )
        return history