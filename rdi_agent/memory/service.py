"""记忆的编排层：加载上下文 / 抽取 / 合并 / 提交。

**职责边界**：

    这一层                协调"读快照 → 调模型 → 调纯规则 → 写库"
    memory.merge          只做判定（纯函数）
    db.repositories.memory 只做存取
    节点（memory_nodes）   只接异常、只改 state

**记忆只有两层**：

    拆句  —— 家长的一段话拆成几个短句，每句只讲一件事
    分类  —— 每个短句归一个类目

    同类合并 —— 每个类目一条小结，**只有小结进 prompt**。

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
from rdi_agent.db.models import MemoryItem
from rdi_agent.db.repositories.memory import (
    MemoryRepository,
    item_content,
    item_view,
    list_memories,
    summary_view,
)
from rdi_agent.memory.categories import (
    CATEGORY_LABEL,
    bucket_by_category,
    categories_needing_update,
    normalize_category,
)
from rdi_agent.memory.ids import (
    content_hash,
    note_content_hash,
    note_memory_id,
    parse_memory_id,
)
from rdi_agent.memory.merge import merge_candidates
from rdi_agent.memory.relevance import rank_by_relevance, take_by_budget
from rdi_agent.memory.summaries import (
    active_summaries,
    live_by_category,
    mark_deleted as mark_summary_deleted,
    mark_superseded as mark_summary_superseded,
    new_summary_item,
    normalize_summary_items,
)
from rdi_agent.prompts import memory as MP
from rdi_agent.schemas.memory import (
    ConsolidationResult,
    MemoryCandidate,
    MemoryExtractionResult,
    MemoryUpdateResult,
)


# ------------------------------------------------------------------ 读取上下文
def _summary_view_items(items: list[Any]) -> list[dict[str, Any]]:
    """类目小结，给 prompt 看。"""
    return [
        {
            "memory_id": item.id,
            "content": item.content,
            "status": item.status,
            "category": item.category,
            "scope_conditions": list(item.scope_conditions),
            "derived_from": list(item.derived_from),
            "source_kind": item.source_kind,
        }
        for item in items
    ]


async def load_memory_context(
    *, child_id: str, query: str
) -> dict[str, Any]:
    """装配这一轮给 prompt 用的记忆 + 给前端看的记忆。

    返回：

        {"child_context_patch": {...}, "memory_context": {...},
         "forbidden_facts": [...]}

    **进 prompt 的只有三样**：类目小结、家长偏好、系统推断。
    短句本身只留在库里 —— 原始短句只在前端面板里给家长看。
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
        summaries = normalize_summary_items(child.memory_summaries_json)
        preferences = await repo.load_preferences(child_id)
        inferences = await repo.load_inferences(child_id)
        attempts = await repo.load_attempts(child_id, limit=3)
        forbidden = await repo.load_forbidden_texts(child_id)

    summary_items = _summary_view_items(active_summaries(summaries))
    selected_summaries = rank_by_relevance(
        query, summary_items, limit=settings.memory_context_max_summaries
    )

    # 偏好不参与裁剪：条数少，而且是"不希望怎样"这种硬约束，
    # 漏一条的代价比漏一条小结还大。
    preference_texts = [item_content(x) for x in preferences]

    inference_items = [
        {
            "memory_id": note_memory_id(x.id),
            "content": item_content(x),
            "category": x.category or "other",
            "scope_conditions": [str(s) for s in (x.scope_conditions_json or [])],
        }
        for x in inferences
    ]
    selected_inferences = rank_by_relevance(
        query, inference_items, limit=settings.memory_context_max_inferences
    )

    # 小结 + 推断 + 尝试共用一份字符预算 —— 它们一起进 prompt 的同一段。
    # 偏好不进预算：它必须完整送到。
    tail, truncated = take_by_budget(
        [*selected_summaries, *selected_inferences, *attempts],
        text_of=lambda item: str(item.get("content") or ""),
        max_chars=settings.memory_context_max_chars,
    )
    selected_summaries = [x for x in tail if x["memory_id"].startswith("summ_")]
    selected_inferences = [x for x in tail if x["memory_id"].startswith("mi_")]
    selected_attempts = [x for x in tail if x["memory_id"].startswith("attempt_")]

    memories = {
        "summaries": selected_summaries,
        "preferences": preference_texts,
        "inferences": selected_inferences,
        "attempts": selected_attempts,
        "truncated": truncated,
    }

    return {
        "child_context_patch": {
            "summaries": selected_summaries,
            "preferences": preference_texts,
            "inferences": selected_inferences,
            "memories": memories,
        },
        "memory_context": {
            **memories,
            "degraded": False,
        },
        "forbidden_facts": forbidden,
    }


# ------------------------------------------------------------------ 合并（归纳）
async def consolidate(
    *,
    child_id: str,
    conversation_id: str = "",
    turn_id: str = "",
) -> list[str]:
    """把同类短句归纳成一条小结。返回说明（给 trace 看）。

    **这是"图里跑的 memory 全是合并后的"这条要求的落地点**：
    短句只留在库里，不进 prompt；进 prompt 的是每个类目一条小结。

    **两类小结**：
    - ``consolidated`` —— 3 条以上、来自 2 轮以上，LLM 归纳出来的。
    - ``direct``       —— 其余情况，直接把短句当小结（不调 LLM，
      不引入任何新断言，纯粹换个存放位置）。

    为什么要 direct 这一档：家长第一轮就说了"在幼儿园门口哭"，
    这时候还没到 3 条，没有 direct 的话这句话就永远进不了 prompt ——
    而它恰恰是分离场景里最该被用上的信息。

    **每个类目的小结必须覆盖该类目当前的全部短句**。
    差一条就算没覆盖 —— 新短句落空、或家长删了短句，
    两种情况都要重算。判据见 ``categories_needing_update``。

    **失败不丢东西**：LLM 挂了 → 退回 direct（短句直述）。
    任何一个类目都不会因为一次合并失败就变成空白 ——
    家长说过的话不能因为模型挂了就白说。
    """
    settings = get_settings()
    if not settings.memory_enabled:
        return ["记忆：已关闭"]

    notes: list[str] = []
    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.get_child(child_id)
        if child is None:
            return ["孩子档案不存在，跳过合并"]

        items = await repo.load_merging_items(child_id)
        summaries = normalize_summary_items(child.memory_summaries_json)
        live = live_by_category(summaries)

        if not items:
            return ["没有可合并的短句，无需合并"]

        # 现有小结覆盖了哪些短句 —— 判"要不要更新"用。
        #
        # **只算活着的**：作废的小结不算数，它不再进 prompt。
        # **direct 的也算**：direct 小结就是"这个类目现在的全部短句"，
        # 它覆盖的那些短句已经被它带进 prompt 了。
        covered_by_category: dict[str, set[str]] = {}
        for item in summaries:
            if item.status != "active":
                continue
            covered_by_category.setdefault(item.category, set()).update(
                item.derived_from
            )

        rows = [
            {
                "memory_id": note_memory_id(row.id),
                "category": row.category or "other",
                "text": item_content(row),
                "turn_id": row.source_turn_id or "",
            }
            for row in items
        ]

        # **要更新的类目**：小结没覆盖住这个类目现在的全部短句。
        #
        # 判据是"覆盖完整性"，不是"新增几条" ——
        # 按新增数判断的话，一条新短句会一直落空
        # （既没被旧小结覆盖，又不够触发重算），永远进不了 prompt。
        updates = categories_needing_update(
            rows, covered_ids_by_category=covered_by_category
        )

        buckets = bucket_by_category(rows)
        changed = False

        async def _write_summary(
            *,
            category: str,
            content: str,
            derived_from: list[str],
            scope_conditions: list[str] | None = None,
            confidence_level: str = "medium",
            source_kind: str = "direct",
        ) -> None:
            """写一条小结，并把该类目旧的小结标成 superseded。

            **每个类目同时只有一条活的小结** —— 新写的这条顶掉旧的。
            旧的保留在库里（审计要能查），只是不再进 prompt。
            """
            nonlocal changed
            old = live.get(category)
            item = new_summary_item(
                category=category,
                content=content,
                digest=content_hash("summary", f"{category}\x00{content}"),
                existing=summaries,
                derived_from=derived_from,
                scope_conditions=scope_conditions or [],
                confidence_level=confidence_level,
                source_turn_id=turn_id,
                supersedes_memory_id=old.id if old else None,
            )
            item.source_kind = source_kind
            if old is not None:
                mark_summary_superseded(summaries, old.id, item.id)
            summaries.append(item)
            live[category] = item
            changed = True
            if old is not None:
                await repo.record_event(
                    child_id=child_id,
                    memory_table="children",
                    memory_id=old.id,
                    event_type="superseded",
                    before=old.to_dict(),
                    after={"superseded_by_memory_id": item.id},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=False,
                )
            await repo.record_event(
                child_id=child_id,
                memory_table="children",
                memory_id=item.id,
                event_type="created",
                after=item.to_dict(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=False,
            )

        # ---- 1. 不够格让模型归纳的类目：直接复述短句 ----
        #
        # 不调模型，内容就是短句的直接拼接 ——
        # 不引入任何新断言，纯粹换个存放位置。
        #
        # **为什么不能跳过**：家长第一轮就说了"在幼儿园门口哭"，
        # 这时候还没到 3 条。没有这一档，这句话就永远进不了 prompt。
        direct_targets = {
            category: (bucket, use_llm, reason)
            for category, (bucket, use_llm, reason) in updates.items()
            if not use_llm
        }
        for category, (bucket, _use_llm, _reason) in direct_targets.items():
            await _write_summary(
                category=category,
                content="；".join(x for x in bucket.texts if x),
                derived_from=list(bucket.observation_ids),
                source_kind="direct",
            )

        # ---- 2. 够格的类目：一次 LLM 批量归纳 ----
        model_notes: list[str] = []
        llm_targets = {
            category: (bucket, reason)
            for category, (bucket, use_llm, reason) in updates.items()
            if use_llm
        }
        if llm_targets:
            payload_buckets = [
                {
                    "category": category,
                    "label": CATEGORY_LABEL.get(category, category),
                    "items": [
                        {"memory_id": mid, "text": text}
                        for mid, text in zip(
                            bucket.observation_ids, bucket.texts
                        )
                    ],
                }
                for category, (bucket, _reason) in llm_targets.items()
            ]

            try:
                system, user = MP.consolidate_summaries(
                    buckets=payload_buckets, today=date.today().isoformat()
                )
                result, _trace = await llm.chat_json(
                    system=system,
                    user=user,
                    model_cls=ConsolidationResult,
                    name="memory_consolidation",
                    stage="memory_consolidate",
                    temperature=0.0,
                    max_tokens=3000,
                    timeout_s=settings.memory_extract_timeout_s,
                    max_attempts=settings.memory_extract_max_attempts,
                )
            except Exception as e:  # noqa: BLE001 - 合并失败不该拖垮整轮
                # **退回直接复述**，而不是让这个类目变成空白 ——
                # 家长说过的话不能因为模型挂了就白说。
                notes.append(f"合并失败（改用短句直述）：{type(e).__name__}: {e}")
                for category, (bucket, _reason) in llm_targets.items():
                    await _write_summary(
                        category=category,
                        content="；".join(x for x in bucket.texts if x),
                        derived_from=list(bucket.observation_ids),
                        source_kind="direct",
                    )
                if changed:
                    await repo.update_summary_items(child, summaries)
                return notes

            handled: set[str] = set()
            for draft in result.summaries:  # type: ignore[union-attr]
                model_notes.extend(str(x) for x in (draft.notes or []) if str(x))
                category = normalize_category(draft.category)
                bucket = buckets.get(category)
                if bucket is None:
                    notes.append(f"模型给了不存在的类目 {draft.category}，已忽略")
                    continue
                # **只接受确实参与归纳的短句 id** —— 模型编的 id 直接丢。
                valid = [
                    x for x in (draft.derived_from or []) if x in bucket.observation_ids
                ]
                if not valid:
                    notes.append(f"{category} 的小结没有可用的溯源 id，已跳过")
                    continue
                content = (draft.content or "").strip()
                if not content:
                    continue

                await _write_summary(
                    category=category,
                    content=content,
                    derived_from=valid,
                    scope_conditions=[
                        str(x) for x in (draft.scope_conditions or []) if str(x)
                    ],
                    confidence_level=str(draft.confidence_level or "medium"),
                    source_kind="consolidated",
                )
                handled.add(category)

            # **模型没提到的类目**：它可能判定那些记录互相矛盾，
            # 或者干脆漏写了。这时候退回直接复述 ——
            # 矛盾的内容原样摆着，好过凭空给一个自洽的说法；
            # 而且这样小结仍然覆盖全部短句，不会有内容落空。
            for category, (bucket, _reason) in llm_targets.items():
                if category in handled:
                    continue
                await _write_summary(
                    category=category,
                    content="；".join(x for x in bucket.texts if x),
                    derived_from=list(bucket.observation_ids),
                    source_kind="direct",
                )

        if changed:
            await repo.update_summary_items(child, summaries)

    notes.extend(model_notes)
    return notes


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
        existing = await repo.load_items(child_id, limit=20)

    system, user = MP.extract_memories(
        source_kind=source_kind,
        parent_text=parent_text,
        followup_answers=followup_answers,
        child_profile=child_context or {},
        existing_items=[
            {
                "id": note_memory_id(x.id),
                "content": item_content(x),
                "category": x.category or "other",
                "is_preference": bool(x.is_preference),
                "inference": bool(x.inference),
            }
            for x in existing
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
        max_tokens=4000,
        timeout_s=settings.memory_extract_timeout_s,
        max_attempts=settings.memory_extract_max_attempts,
    )

    candidates: list[MemoryCandidate] = []
    for draft in result.candidates:  # type: ignore[union-attr]
        candidate = MemoryCandidate(
            content=(draft.content or "").strip(),
            category=normalize_category(draft.category or ""),
            is_preference=bool(draft.is_preference),
            inference=bool(draft.inference),
            source_quote=(draft.source_quote or "").strip(),
            confidence=float(draft.confidence or 0.5),
            proposed_decision=draft.proposed_decision,
            reason=(draft.reason or "").strip(),
            scope_conditions=[str(x) for x in (draft.scope_conditions or [])],
            replaces_memory_id=(draft.replaces_memory_id or "").strip(),
            conversation_id=conversation_id,
            source_turn_id=turn_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            source_interaction_id=source_interaction_id,
            source_feedback_id=source_feedback_id,
        )
        candidate.content_hash = note_content_hash(
            candidate.content,
            is_preference=candidate.is_preference,
            inference=candidate.inference,
        )
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
    )
    result.merge_notes(list(state.get("memory_notes") or []))
    return result


# ------------------------------------------------------------------ 提交
async def commit_for_state(state: dict[str, Any]) -> MemoryUpdateResult:
    """把合并结果写进库，并重新算一份当前的上下文。"""
    result = MemoryUpdateResult.model_validate(
        state.get("memory_update_result") or {}
    )
    if not any(d.decision == "auto_commit" for d in result.decisions):
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

        for decision in result.decisions:
            if decision.decision != "auto_commit":
                continue
            candidate = decision.candidate

            row, created = await repo.insert_item(
                child_id=child_id, candidate=candidate
            )
            if row is None:
                continue
            candidate.memory_id = note_memory_id(row.id)
            if not created:
                # 同内容已经有一条了 —— 重放保护，别写第二条。
                continue
            if decision.action == "supersede" and decision.target_memory_id:
                row.supersedes_memory_id = decision.target_memory_id

            # ---- 明确纠正：旧的那条标作废 ----
            if decision.action == "supersede":
                target = await _get_note(session, decision.target_memory_id)
                if target is not None and str(target.child_id) == str(child.id):
                    result.superseded_facts.append(target.content)
                    target.status = "superseded"
                    target.superseded_by_memory_id = candidate.memory_id
                    await repo.record_event(
                        child_id=child_id,
                        memory_table="memory_items",
                        memory_id=decision.target_memory_id,
                        event_type="superseded",
                        before={"content": target.content},
                        after={"superseded_by_memory_id": candidate.memory_id},
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        confirmed_by_parent=True,
                    )
            await repo.record_event(
                child_id=child_id,
                memory_table="memory_items",
                memory_id=candidate.memory_id,
                event_type="created",
                after={
                    "content": row.content,
                    "category": row.category,
                    "is_preference": row.is_preference,
                    "inference": row.inference,
                },
                conversation_id=conversation_id,
                turn_id=turn_id,
                confirmed_by_parent=False,
            )

        result.superseded_facts = list(dict.fromkeys(result.superseded_facts))
        result.context_patch = await _build_context_patch(
            repo,
            child,
            state.get("user_message") or "",
            attempts=list(
                ((state.get("child_context") or {}).get("memories") or {}).get(
                    "attempts"
                )
                or []
            ),
        )
    return result


async def _build_context_patch(
    repo: MemoryRepository,
    child: Any,
    query: str,
    *,
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """写完之后重新算一遍给 prompt 的上下文片段。"""
    settings = get_settings()
    summaries = normalize_summary_items(child.memory_summaries_json)
    summary_items = _summary_view_items(active_summaries(summaries))
    selected_summaries = rank_by_relevance(
        query, summary_items, limit=settings.memory_context_max_summaries
    )
    preferences = await repo.load_preferences(str(child.id))
    inferences = await repo.load_inferences(str(child.id))
    preference_texts = [item_content(x) for x in preferences]
    inference_items = [
        {
            "memory_id": note_memory_id(x.id),
            "content": item_content(x),
            "category": x.category or "other",
            "scope_conditions": [str(s) for s in (x.scope_conditions_json or [])],
        }
        for x in inferences
    ]
    selected_inferences = rank_by_relevance(
        query, inference_items, limit=settings.memory_context_max_inferences
    )
    memories = {
        "summaries": selected_summaries,
        "preferences": preference_texts,
        "inferences": selected_inferences,
        "attempts": list(attempts or []),
    }
    return {
        "summaries": selected_summaries,
        "preferences": preference_texts,
        "inferences": selected_inferences,
        "memories": memories,
    }


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
    """确认一条记忆。

    **记忆现在是自动维护的，没有"待家长点头"这一档** ——
    所以这个接口现在只是原样返回那条记忆。
    留着是为了兼容前端和历史数据。
    """
    kind, raw = parse_memory_id(memory_id)
    async with session_scope() as session:
        repo = MemoryRepository(session)
        child = await repo.lock_child(child_id)
        if child is None or str(child.family_id) != str(family_id):
            return None
        if kind == "note":
            row = await _get_note(session, memory_id)
            if row is None or str(row.child_id) != str(child.id):
                return None
            return item_view(row)
        if kind == "summary":
            item = next(
                (
                    x
                    for x in normalize_summary_items(child.memory_summaries_json)
                    if x.id == memory_id
                ),
                None,
            )
            return summary_view(item) if item is not None else None
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

        # **小结不支持纠正**。
        #
        # 小结是**派生产物** —— 由该类目下的短句归纳而来，
        # 下一轮合并会按类目全量重算。手改的内容留不住，
        # 留住了也是假的一致性（短句和改过的对不上）。
        #
        # 真正要改的是它归纳自的那几条短句 ——
        # 家长在对话里说"不是这样"就会走那条链路。
        if kind == "summary":
            return None

        if kind != "note":
            return None

        old = await _get_note(session, memory_id)
        if old is None or str(old.child_id) != str(child.id):
            return None

        digest = note_content_hash(
            text, is_preference=old.is_preference, inference=old.inference
        )
        existing = (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.child_id == old.child_id,
                    MemoryItem.content_hash == digest,
                    MemoryItem.deleted_at.is_(None),
                    MemoryItem.status == "active",
                )
            )
        ).scalars().first()
        if existing is not None:
            return item_view(existing)

        new = MemoryItem(
            id=uuid.uuid4(),
            child_id=old.child_id,
            conversation_id=old.conversation_id,
            content=text,
            category=old.category,
            is_preference=old.is_preference,
            inference=old.inference,
            scope_conditions_json=list(old.scope_conditions_json or []),
            source_kind="parent_correction",
            confidence=1.0,
            content_hash=digest,
            status="active",
            source_turn_id=turn_id,
            # 家长直接改的内容是**确定的话** —— 引用的就是他自己写的字。
            source_quote=text,
            supersedes_memory_id=memory_id,
        )
        session.add(new)
        old.status = "superseded"
        old.superseded_by_memory_id = note_memory_id(new.id)
        await session.flush()
        # server default 的列（created_at / updated_at）刷新后才拿得到值 ——
        # 不刷新的话，转视图时会在同步代码里触发 IO，直接炸。
        await session.refresh(new)
        await repo.record_event(
            child_id=old.child_id,
            memory_table="memory_items",
            memory_id=memory_id,
            event_type="superseded",
            after={"superseded_by_memory_id": note_memory_id(new.id)},
            conversation_id=conversation_id,
            turn_id=turn_id,
            confirmed_by_parent=True,
        )
        await repo.record_event(
            child_id=old.child_id,
            memory_table="memory_items",
            memory_id=note_memory_id(new.id),
            event_type="created",
            after={"content": text},
            conversation_id=conversation_id,
            turn_id=turn_id,
            confirmed_by_parent=True,
        )
        return item_view(new)


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

        if kind == "summary":
            items = normalize_summary_items(child.memory_summaries_json)
            item = next((x for x in items if x.id == memory_id), None)
            if item is None:
                return None
            if item.status != "deleted":
                mark_summary_deleted(items, item.id)
                await repo.update_summary_items(child, items)
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
                # **连它归纳自的短句一起删**。
                #
                # 不删的话，下一轮合并会拿这些短句重新归纳出一条小结 ——
                # 家长点了删除，结果东西又回来了。
                # 删掉它们，这一类就彻底从记忆里消失了。
                for note_id in item.derived_from:
                    row = await _get_note(session, note_id)
                    if row is None or str(row.child_id) != str(child.id):
                        continue
                    if row.deleted_at is None:
                        row.status = "deleted"
                        row.deleted_at = _now()
                        await repo.record_event(
                            child_id=child_id,
                            memory_table="memory_items",
                            memory_id=note_id,
                            event_type="deleted",
                            after={"status": "deleted"},
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            confirmed_by_parent=True,
                        )
            return summary_view(item)

        if kind == "note":
            row = await _get_note(session, memory_id)
            if row is None or str(row.child_id) != str(child.id):
                return None
            if row.deleted_at is None:
                row.status = "deleted"
                row.deleted_at = _now()
                await repo.record_event(
                    child_id=child_id,
                    memory_table="memory_items",
                    memory_id=memory_id,
                    event_type="deleted",
                    after={"status": "deleted"},
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    confirmed_by_parent=True,
                )
                # 同上：updated_at 是 server default，改过之后要刷新才拿得到。
                await session.refresh(row)
            return item_view(row)

    return None


# ------------------------------------------------------------------ 小工具
async def _get_note(session: Any, memory_id: str) -> MemoryItem | None:
    """按 ``mi_xxx`` 的 id 取一条短句。"""
    kind, raw = parse_memory_id(memory_id)
    if kind != "note":
        return None
    try:
        row_id = uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
    return await session.get(MemoryItem, row_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
