"""记忆的编排层：加载上下文 / 抽取 / 合并 / 提交。

**职责边界**：

    这一层                协调"读快照 → 调模型 → 调纯规则 → 写库"
    memory.merge          只做判定（纯函数）
    db.repositories.memory 只做存取
    节点（memory_nodes）   只接异常、只改 state

**失败策略**：这一层**不吞异常**。数据库挂了、模型超时了，
都要让节点看见 —— 节点负责降级并写 trace。
如果这里悄悄吞掉，线上表现就是"记忆莫名不生效"，极难查。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select

from rdi_agent import llm
from rdi_agent.config import get_settings
from rdi_agent.db.engine import session_scope
from rdi_agent.db.models import Hypothesis, Observation
from rdi_agent.db.repositories.memory import (
    MemoryRepository,
    build_context_patch,
    fact_item_view,
    find_fact,
    list_memories,
    mark_fact_deleted,
    mark_fact_superseded,
    new_fact_from_candidate,
    observation_content,
    observation_memory_id,
    observation_view,
    hypothesis_memory_id,
    hypothesis_view,
)
from rdi_agent.memory.facts import (
    active_facts,
    forbidden_fact_texts,
    normalize_fact_items,
    pending_facts,
)
from rdi_agent.memory.ids import content_hash, parse_memory_id
from rdi_agent.memory.merge import ExistingSnapshot, merge_candidates
from rdi_agent.memory.relevance import (
    rank_by_relevance,
    take_by_budget,
)
from rdi_agent.prompts import memory as MP
from rdi_agent.schemas.memory import (
    MemoryCandidate,
    MemoryExtractionResult,
    MemoryUpdateResult,
)


# ------------------------------------------------------------------ 读取上下文
def _fact_prompt_items(items: list[Any]) -> list[dict[str, Any]]:
    return [
        {"id": x.id, "memory_id": x.id, "content": x.content, "memory_type": x.memory_type}
        for x in items
    ]


async def load_memory_context(
    *, child_id: str, query: str
) -> dict[str, Any]:
    """装配这一轮给 prompt 用的记忆 + 给前端看的记忆。

    返回：

        {"child_context_patch": {...}, "memory_context": {...},
         "forbidden_facts": [...]}

    ``pending_confirmation`` 的内容**不进 prompt** —— 家长还没说要记，
    拿它当背景等于系统替家长做了决定。
    """
    settings = get_settings()
    if not settings.memory_enabled:
        return {
            "child_context_patch": {},
            "memory_context": {"disabled": True},
            "forbidden_facts": [],
        }

    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.get_child(child_id)
        if child is None:
            return {
                "child_context_patch": {},
                "memory_context": {"missing_child": True},
                "forbidden_facts": [],
            }
        facts = normalize_fact_items(child.confirmed_facts_json)
        observations = await repo.load_observations(
            child_id, limit=30, include_pending=False
        )
        hypotheses = await repo.load_hypotheses(child_id, limit=20)
        attempts = await repo.load_attempts(child_id, limit=3)

    live = active_facts(facts)
    fact_items = [x for x in live if x.memory_type == "fact"]
    pref_items = [x for x in live if x.memory_type == "preference"]

    observation_items = [
        {
            "memory_id": observation_memory_id(row.id),
            "content": observation_content(row),
            "status": row.status,
            "is_attempt": bool(row.is_attempt),
        }
        for row in observations
    ]
    selected_obs = rank_by_relevance(
        query,
        observation_items,
        limit=settings.memory_context_max_observations,
    )

    hypothesis_items = [
        {
            "memory_id": hypothesis_memory_id(row.id),
            "content": row.hypothesis_text,
            "status": row.status,
            "confidence_level": row.confidence_level,
            "scope_conditions": [str(x) for x in (row.scope_conditions_json or [])],
        }
        for row in hypotheses
    ]
    selected_hyp = rank_by_relevance(
        query,
        hypothesis_items,
        limit=settings.memory_context_max_hypotheses,
    )

    # 观察 + 假设 + 尝试共用一份字符预算 —— 它们一起进 prompt 的同一段。
    tail, truncated = take_by_budget(
        [*selected_obs, *selected_hyp, *attempts],
        text_of=lambda item: str(item.get("content") or ""),
        max_chars=settings.memory_context_max_chars,
    )
    selected_obs = [x for x in tail if x["memory_id"].startswith("obs_")]
    selected_hyp = [x for x in tail if x["memory_id"].startswith("hyp_")]
    selected_attempts = [x for x in tail if x["memory_id"].startswith("attempt_")]

    memories = {
        "observations": selected_obs,
        "hypotheses": selected_hyp,
        "attempts": selected_attempts,
        "truncated": truncated,
    }

    return {
        "child_context_patch": {
            "confirmed_facts": [x.content for x in fact_items],
            "preferences": [x.content for x in pref_items],
            "memories": memories,
        },
        "memory_context": {
            "confirmed_facts": [x.content for x in fact_items],
            "preferences": [x.content for x in pref_items],
            **memories,
            "pending_confirmation": len(pending_facts(facts)),
            "degraded": False,
        },
        "forbidden_facts": forbidden_fact_texts(facts),
    }


# ------------------------------------------------------------------ 抽取
def _source_text_for_state(
    *,
    parent_text: str,
    followup_answers: list[dict[str, str]] | None,
    feedback: dict[str, Any] | None,
) -> str:
    """校验原话用的"这一轮家长说的全部内容"。

    追问的回答也要算进去 —— 模型可能从某一道题的回答里摘原话，
    只把主消息当原文会把它们全判成"引不出来"。
    """
    parts = [parent_text or ""]
    for item in followup_answers or []:
        answer = str(item.get("answer") or "").strip()
        if answer:
            parts.append(answer)
    if feedback:
        parts.append(str(feedback.get("implementation_notes") or ""))
        parts.extend(str(x) for x in (feedback.get("parent_observations") or []))
        parts.extend(str(x) for x in (feedback.get("difficulties") or []))
        child_response = feedback.get("child_response") or {}
        if isinstance(child_response, dict):
            parts.extend(
                str(value)
                for value in child_response.values()
                if isinstance(value, str)
            )
    return "\n".join(part for part in parts if part)


async def extract_candidates(
    *,
    child_id: str,
    conversation_id: str,
    turn_id: str,
    source_kind: str,
    parent_text: str,
    followup_answers: list[dict[str, str]] | None = None,
    child_context: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
    feedback_analysis: dict[str, Any] | None = None,
    source_interaction_id: str = "",
    source_feedback_id: str = "",
) -> tuple[list[MemoryCandidate], list[str]]:
    """调一次模型，把草稿补成带溯源的候选。"""
    settings = get_settings()
    if not settings.memory_enabled:
        return [], ["记忆功能已关闭"]

    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.get_child(child_id)
        if child is None:
            return [], ["孩子档案不存在，本轮不抽取记忆"]
        if not source_interaction_id and source_kind == "interaction":
            # 溯源用：这条记忆是从哪一句家长原话来的。
            # 找不到不算错 —— 比如接口在记录交互之前就调了图。
            source_interaction_id = await repo.get_interaction_id(
                conversation_id, turn_id
            )
        facts = normalize_fact_items(child.confirmed_facts_json)
        hypotheses = await repo.load_hypotheses(child_id, limit=10)
        observations = await repo.load_observations(child_id, limit=10)
    live = active_facts(facts)

    system, user = MP.extract_memories(
        source_kind=source_kind,
        parent_text=parent_text,
        followup_answers=followup_answers,
        child_profile=child_context or {},
        existing_facts=_fact_prompt_items(live[:20]),
        existing_hypotheses=[
            {
                "id": hypothesis_memory_id(row.id),
                "content": row.hypothesis_text,
                "confidence_level": row.confidence_level,
                "scope_conditions": list(row.scope_conditions_json or []),
            }
            for row in hypotheses
        ],
        recent_observations=[
            {"id": observation_memory_id(row.id), "summary": observation_content(row)}
            for row in observations
        ],
        today=date.today().isoformat(),
        feedback=feedback,
        feedback_analysis=feedback_analysis,
    )

    result, _trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=MemoryExtractionResult,
        name="memory_extraction",
        stage="memory_extract",
        temperature=0.0,
        max_tokens=6000,
        timeout_s=settings.memory_extract_timeout_s,
        max_attempts=settings.memory_extract_max_attempts,
    )

    candidates: list[MemoryCandidate] = []
    for draft in result.candidates:  # type: ignore[union-attr]
        memory_type = draft.memory_type
        candidate = MemoryCandidate(
            memory_type=memory_type,
            content=(draft.content or "").strip(),
            source_quote=(draft.source_quote or "").strip(),
            confidence=float(draft.confidence or 0.5),
            inference=bool(draft.inference),
            proposed_decision=draft.proposed_decision,
            reason=(draft.reason or "").strip(),
            scope_conditions=[str(x) for x in (draft.scope_conditions or [])],
            replaces_memory_id=(draft.replaces_memory_id or "").strip(),
            contradicts_memory_ids=[
                str(x) for x in (draft.contradicts_memory_ids or [])
            ],
            setting=(draft.setting or "").strip(),
            antecedent=(draft.antecedent or "").strip(),
            observable_behavior=(draft.observable_behavior or "").strip(),
            adult_response=(draft.adult_response or "").strip(),
            child_response=(draft.child_response or "").strip(),
            outcome=(draft.outcome or "").strip(),
            is_attempt=bool(draft.is_attempt),
            conversation_id=conversation_id,
            source_turn_id=turn_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            source_interaction_id=source_interaction_id,
            source_feedback_id=source_feedback_id,
        )
        candidate.content_hash = content_hash(memory_type, candidate.content)
        candidates.append(candidate)

    return candidates, list(result.notes or [])  # type: ignore[union-attr]


# ------------------------------------------------------------------ 合并
async def merge_for_state(state: dict[str, Any]) -> MemoryUpdateResult:
    """读快照 + 跑纯规则。**不写库。**"""
    settings = get_settings()
    candidates = [
        MemoryCandidate.model_validate(x)
        for x in (state.get("memory_candidates") or [])
    ]
    if not candidates:
        return MemoryUpdateResult(notes=list(state.get("memory_notes") or []))

    child_id = state.get("child_id") or ""
    source_text = _source_text_for_state(
        parent_text=state.get("user_message") or "",
        followup_answers=state.get("followup_answers") or [],
        feedback=state.get("feedback") or None,
    )
    async with session_scope() as session:
        snapshot = await MemoryRepository(session).load_snapshot(
            child_id, source_text
        )
    result = merge_candidates(
        candidates,
        snapshot,
        max_candidates=settings.memory_max_candidates_per_turn,
        max_active_facts=settings.memory_max_active_facts,
    )
    result.merge_notes(list(state.get("memory_notes") or []))
    return result


# ------------------------------------------------------------------ 提交
async def commit_for_state(state: dict[str, Any]) -> MemoryUpdateResult:
    """把合并结果写进库，并重新算一份当前的上下文。"""
    result = MemoryUpdateResult.model_validate(
        state.get("memory_update_result") or {}
    )
    if not any(
        d.decision in ("auto_commit", "needs_confirmation")
        for d in result.decisions
    ):
        return result

    child_id = state.get("child_id") or ""
    conversation_id = state.get("conversation_id") or ""
    turn_id = state.get("turn_id") or ""

    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.lock_child(child_id)
        if child is None:
            result.notes.append("孩子档案不存在，记忆未写入")
            return result

        items = normalize_fact_items(child.confirmed_facts_json)
        live_hashes = {
            x.content_hash for x in items if x.status in ("confirmed", "pending_confirmation")
        }
        facts_changed = False

        for decision in result.decisions:
            if decision.decision not in ("auto_commit", "needs_confirmation"):
                continue
            candidate = decision.candidate
            status = (
                "confirmed"
                if decision.decision == "auto_commit"
                else "pending_confirmation"
            )

            # ---- 重放保护：同内容已经在库里，直接跳过 ----
            # （追问恢复会重跑这一轮；不挡的话每答一题就多一条）
            if candidate.content_hash and candidate.content_hash in live_hashes:
                continue
            live_hashes.add(candidate.content_hash)

            if candidate.memory_type in ("fact", "preference"):
                if decision.action == "supersede":
                    target = find_fact(items, decision.target_memory_id)
                    if target is None or target.status != "confirmed":
                        # 目标没了（被删了/被并发了）—— 降级成待确认，不动旧值
                        decision.decision = "needs_confirmation"
                        decision.action = "create"
                        status = "pending_confirmation"
                    else:
                        item = new_fact_from_candidate(
                            candidate, existing=items, status="confirmed"
                        )
                        item.supersedes_memory_id = target.id
                        mark_fact_superseded(items, target.id, item.id)
                        result.superseded_facts.append(target.content)
                        items.append(item)
                        facts_changed = True
                        await repo.record_event(
                            child_id=child_id,
                            memory_table="children",
                            memory_id=target.id,
                            event_type="superseded",
                            before=target.to_dict(),
                            after={"superseded_by_memory_id": item.id},
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            confirmed_by_parent=True,
                        )
                        await repo.record_event(
                            child_id=child_id,
                            memory_table="children",
                            memory_id=item.id,
                            event_type="created",
                            after=item.to_dict(),
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            confirmed_by_parent=True,
                        )
                        continue

                item = new_fact_from_candidate(
                    candidate, existing=items, status=status
                )
                items.append(item)
                facts_changed = True
                await repo.record_event(
                    child_id=child_id,
                    memory_table="children",
                    memory_id=item.id,
                    event_type="created",
                    after=item.to_dict(),
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=status == "confirmed",
                )
                continue

            if candidate.memory_type == "episode":
                if decision.action == "link_contradiction":
                    for hid in decision.contradiction_ids:
                        await repo.weaken_hypothesis(
                            child_id=child_id,
                            memory_id=hid[len("hyp_") :]
                            if hid.startswith("hyp_")
                            else hid,
                            observation_memory_id_=decision.target_memory_id,
                        )
                    continue
                # 观察的状态词和事实不一样：事实是 confirmed，
                # 观察是 active（表里有 CHECK 约束，写错会直接被库拒掉）。
                observation_status = (
                    "active"
                    if decision.decision == "auto_commit"
                    else "pending_confirmation"
                )
                row, _created = await repo.insert_observation(
                    child_id=child_id,
                    candidate=candidate,
                    status=observation_status,
                )
                if row is not None:
                    await repo.record_event(
                        child_id=child_id,
                        memory_table="observations",
                        memory_id=observation_memory_id(row.id),
                        event_type="created",
                        after={"content": observation_content(row)},
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        confirmed_by_parent=status == "active",
                    )
                    for hid in decision.contradiction_ids:
                        await repo.weaken_hypothesis(
                            child_id=child_id,
                            memory_id=hid[len("hyp_") :]
                            if hid.startswith("hyp_")
                            else hid,
                            observation_memory_id_=observation_memory_id(row.id),
                        )
                continue

            if candidate.memory_type == "hypothesis":
                row, created = await repo.insert_hypothesis(
                    child_id=child_id, candidate=candidate, status="candidate"
                )
                if created and row is not None:
                    await repo.record_event(
                        child_id=child_id,
                        memory_table="hypotheses",
                        memory_id=hypothesis_memory_id(row.id),
                        event_type="created",
                        after={"content": row.hypothesis_text},
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        confirmed_by_parent=False,
                    )

        if facts_changed:
            await repo.update_fact_items(child, items)

        patch = build_context_patch(child)
        result.context_patch = {
            "confirmed_facts": patch["confirmed_facts"],
            "preferences": patch["preferences"],
        }
        # 只保留**本轮**被作废的旧事实。历次作废的全量清单由
        # ``load_context`` 在下一轮开始时给 —— 混进这里会让
        # "本轮纠正了什么"这个信息失真。
        result.superseded_facts = list(dict.fromkeys(result.superseded_facts))
    return result


# ------------------------------------------------------------------ 家长操作
async def list_for_child(
    *, child_id: str, family_id: str, **kwargs: Any
) -> dict[str, Any]:
    async with session_scope() as session:
        return await list_memories(
            session, child_id=child_id, family_id=family_id, **kwargs
        )


async def confirm_memory(
    *,
    child_id: str,
    family_id: str,
    memory_id: str,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """家长点"确认"。待确认 → 正式记录。

    幂等：已经是目标状态就原样返回，不重复写事件。
    """
    kind, raw = parse_memory_id(memory_id)
    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.lock_child(child_id)
        if child is None or str(child.family_id) != str(family_id):
            return None

        if kind in ("fact", "preference"):
            items = normalize_fact_items(child.confirmed_facts_json)
            item = find_fact(items, memory_id)
            if item is None:
                return None
            if item.status != "confirmed":
                item.status = "confirmed"
                await repo.update_fact_items(child, items)
                await repo.record_event(
                    child_id=child_id,
                    memory_table="children",
                    memory_id=item.id,
                    event_type="confirmed",
                    after=item.to_dict(),
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return _fact_view(item)

        if kind == "observation":
            row = await session.get(Observation, _uid_or_none(raw))
            if row is None or str(row.child_id) != str(_uid_or_none(child_id)):
                return None
            if row.status == "pending_confirmation":
                row.status = "active"
                await repo.record_event(
                    child_id=child_id,
                    memory_table="observations",
                    memory_id=memory_id,
                    event_type="confirmed",
                    after={"content": observation_content(row)},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return observation_view(row)

        if kind == "hypothesis":
            row = await session.get(Hypothesis, _uid_or_none(raw))
            if row is None or str(row.child_id) != str(_uid_or_none(child_id)):
                return None
            if row.status == "candidate":
                row.status = "active"
                await repo.record_event(
                    child_id=child_id,
                    memory_table="hypotheses",
                    memory_id=memory_id,
                    event_type="confirmed",
                    after={"content": row.hypothesis_text},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return hypothesis_view(row)

    return None


async def correct_memory(
    *,
    child_id: str,
    family_id: str,
    memory_id: str,
    content: str,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """家长改一条记忆：新建 + 旧记录标 superseded，**不覆盖历史**。"""
    text = (content or "").strip()
    if not text:
        return None
    kind, raw = parse_memory_id(memory_id)

    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.lock_child(child_id)
        if child is None or str(child.family_id) != str(family_id):
            return None

        if kind in ("fact", "preference"):
            items = normalize_fact_items(child.confirmed_facts_json)
            old = find_fact(items, memory_id)
            if old is None:
                return None
            digest = content_hash(old.memory_type, text)
            # 幂等：同样的纠正重发一次，不再新建
            already = next(
                (
                    x
                    for x in items
                    if x.content_hash == digest
                    and x.status in ("confirmed", "pending_confirmation")
                ),
                None,
            )
            if already is not None:
                return _fact_view(already)
            new = new_fact_from_candidate(
                MemoryCandidate(
                    memory_type=old.memory_type,  # type: ignore[arg-type]
                    content=text,
                    content_hash=digest,
                    confidence=1.0,
                    source_kind="parent_correction",
                ),
                existing=items,
                status="confirmed",
                supersedes_memory_id=old.id,
            )
            # 家长直接改的内容是**确定的事实**，不再挂"家长原话" ——
            # 引用的就是他自己写的字。
            new.source_quote = text
            mark_fact_superseded(items, old.id, new.id)
            items.append(new)
            await repo.update_fact_items(child, items)
            await repo.record_event(
                child_id=child_id,
                memory_table="children",
                memory_id=old.id,
                event_type="superseded",
                before=old.to_dict(),
                after={"superseded_by_memory_id": new.id},
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            await repo.record_event(
                child_id=child_id,
                memory_table="children",
                memory_id=new.id,
                event_type="created",
                after=new.to_dict(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            return _fact_view(new)

        if kind == "observation":
            old = await session.get(Observation, _uid_or_none(raw))
            if old is None or str(old.child_id) != str(_uid_or_none(child_id)):
                return None
            digest = content_hash("episode", text)
            existing = (
                await session.execute(
                    select(Observation).where(
                        Observation.child_id == old.child_id,
                        Observation.content_hash == digest,
                        Observation.deleted_at.is_(None),
                        Observation.status.in_(["active", "pending_confirmation"]),
                    )
                )
            ).scalars().first()
            if existing is not None:
                return observation_view(existing)
            new = Observation(
                id=uuid.uuid4(),
                child_id=old.child_id,
                conversation_id=old.conversation_id,
                setting=old.setting,
                participants_json=old.participants_json,
                antecedent=old.antecedent,
                observable_behavior=text,
                adult_response=old.adult_response,
                child_response=old.child_response,
                outcome=old.outcome,
                source_kind="parent_correction",
                confidence=1.0,
                content_hash=digest,
                status="active",
                source_turn_id=turn_id,
                source_quote=text,
                supersedes_memory_id=memory_id,
            )
            session.add(new)
            old.status = "superseded"
            old.superseded_by_memory_id = observation_memory_id(new.id)
            await session.flush()
            await repo.record_event(
                child_id=child_id,
                memory_table="observations",
                memory_id=memory_id,
                event_type="superseded",
                after={"superseded_by_memory_id": observation_memory_id(new.id)},
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            await repo.record_event(
                child_id=child_id,
                memory_table="observations",
                memory_id=observation_memory_id(new.id),
                event_type="created",
                after={"content": observation_content(new)},
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            return observation_view(new)

        if kind == "hypothesis":
            old = await session.get(Hypothesis, _uid_or_none(raw))
            if old is None or str(old.child_id) != str(_uid_or_none(child_id)):
                return None
            digest = content_hash("hypothesis", text)
            existing = (
                await session.execute(
                    select(Hypothesis).where(
                        Hypothesis.child_id == old.child_id,
                        Hypothesis.content_hash == digest,
                        Hypothesis.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            if existing is not None:
                return hypothesis_view(existing)
            new = Hypothesis(
                id=uuid.uuid4(),
                child_id=old.child_id,
                hypothesis_text=text,
                confidence_level=old.confidence_level,
                scope_conditions_json=old.scope_conditions_json,
                status="candidate",
                created_by="parent",
                content_hash=digest,
                source_turn_id=turn_id,
                source_quote=text,
                supersedes_memory_id=memory_id,
            )
            session.add(new)
            old.status = "superseded"
            old.superseded_by_memory_id = hypothesis_memory_id(new.id)
            await session.flush()
            await repo.record_event(
                child_id=child_id,
                memory_table="hypotheses",
                memory_id=memory_id,
                event_type="superseded",
                after={"superseded_by_memory_id": hypothesis_memory_id(new.id)},
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            await repo.record_event(
                child_id=child_id,
                memory_table="hypotheses",
                memory_id=hypothesis_memory_id(new.id),
                event_type="created",
                after={"content": text},
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=True,
            )
            return hypothesis_view(new)

    return None


async def delete_memory(
    *,
    child_id: str,
    family_id: str,
    memory_id: str,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """软删除。审计留痕，内容不再进任何 prompt。"""
    kind, raw = parse_memory_id(memory_id)
    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.lock_child(child_id)
        if child is None or str(child.family_id) != str(family_id):
            return None

        if kind in ("fact", "preference"):
            items = normalize_fact_items(child.confirmed_facts_json)
            item = find_fact(items, memory_id)
            if item is None:
                return None
            if item.status != "deleted":
                mark_fact_deleted(items, item.id)
                await repo.update_fact_items(child, items)
                await repo.record_event(
                    child_id=child_id,
                    memory_table="children",
                    memory_id=item.id,
                    event_type="deleted",
                    before=item.to_dict(),
                    after={"status": "deleted"},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return _fact_view(item)

        if kind == "observation":
            row = await session.get(Observation, _uid_or_none(raw))
            if row is None or str(row.child_id) != str(_uid_or_none(child_id)):
                return None
            if row.deleted_at is None:
                row.status = "deleted"
                row.deleted_at = _now()
                await repo.record_event(
                    child_id=child_id,
                    memory_table="observations",
                    memory_id=memory_id,
                    event_type="deleted",
                    after={"status": "deleted"},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return observation_view(row)

        if kind == "hypothesis":
            row = await session.get(Hypothesis, _uid_or_none(raw))
            if row is None or str(row.child_id) != str(_uid_or_none(child_id)):
                return None
            if row.deleted_at is None:
                row.status = "deleted"
                row.deleted_at = _now()
                await repo.record_event(
                    child_id=child_id,
                    memory_table="hypotheses",
                    memory_id=memory_id,
                    event_type="deleted",
                    after={"status": "deleted"},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
            return hypothesis_view(row)

    return None


# ------------------------------------------------------------------ 小工具
def _uid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fact_view(item: Any) -> dict[str, Any]:
    return fact_item_view(item)


__all__ = [
    "commit_for_state",
    "confirm_memory",
    "correct_memory",
    "delete_memory",
    "extract_candidates",
    "list_for_child",
    "load_memory_context",
    "merge_for_state",
]
