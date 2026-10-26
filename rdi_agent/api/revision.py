"""方案修订接口：从"生成建议卡"之前的历史 checkpoint 派生新版本。

家长执行一周后回填反馈，发现情况变了（原来早上送，现在只有晚上有时间；
原来以为怕分离，后来发现怕声音）。这时不该重新开一段对话：
已经问清的事实、已经找到的指南依据都还在。

**回溯点固定在一个位置** —— 生成建议卡之前，也就是 ``evidence_gate``
之后。分界线左边（理解/追问/检索/门控）全部沿用；右边（生成卡 /
安全审查 / 渲染 / 落库 / 等家长选）全部重跑。

**同一条会话线程**：分叉落在原来的 thread 上，LangGraph 会在 checkpoint
历史里自然出现分支；选卡仍然复用 ``/select-plan``，不需要新中断类型。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from rdi_agent.api.conversations import (
    _ensure_identity_for_trial,
    _evidence_panel,
    _status_of,
)
from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph import evidence_gate as gate
from rdi_agent.graph.facts_merge import merge_parent_supplement
from rdi_agent.graph.runtime import config_for, get_graph
from rdi_agent.memory import service as memory_service
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceDecision

router = APIRouter(prefix="/api", tags=["revision"])

# 复用了哪些环节 / 重跑了哪些环节。接口直接回给前端和调试面板，
# 比只返回一版新卡更能说明"这次没有重新问、没有重新查"。
REUSED_STAGES = [
    "memory_context",
    "sufficiency",
    "followup_qa",
    "retrieval",
    "evidence_bundle",
    "evidence_gate",
]
RERUN_STAGES = [
    "build_recommendation",
    "safety_review",
    "render_answer",
    "persist_candidate_plans",
]


class ReviseRequest(BaseModel):
    """家长补充的新情况。

    ``turn_id`` 是幂等键：同一个 turn_id 重复提交不会产生第二条分支。
    """

    revision_note: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)


def _checkpoint_config(thread_id: str, checkpoint_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
        }
    }


def _pending_of(snapshot: Any) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", None) or ():
        for intr in getattr(task, "interrupts", None) or ():
            value = getattr(intr, "value", None)
            return value if isinstance(value, dict) else {"raw": str(value)}
    return None


async def _find_fork_point(
    graph: Any, conversation_id: str, base_plan: Any
) -> tuple[Any, str]:
    """找到"生成建议卡之前"的 checkpoint，以及它的生产节点。"""
    thread_id = conversation_id
    recorded = getattr(base_plan, "fork_checkpoint_id", None)
    if recorded:
        snapshot = await graph.aget_state(_checkpoint_config(thread_id, recorded))
        if (
            snapshot is not None
            and "build_recommendation" in (snapshot.next or ())
            and (snapshot.values or {}).get("evidence_bundle")
        ):
            producer = await _producer_for(graph, snapshot)
            if producer:
                return snapshot, producer

    # 兜底：扫线程历史，找最近一个"下一步是生成建议卡"的 checkpoint。
    # 多轮会话下优先按方案版本和来源轮次匹配，避免拿到别的版本的。
    base_version = int(getattr(base_plan, "plan_version", 1) or 1)
    base_turn = getattr(base_plan, "source_turn_id", None)
    async for snapshot in graph.aget_state_history(config_for(thread_id)):
        if "build_recommendation" not in (snapshot.next or ()):
            continue
        values = snapshot.values or {}
        if not values.get("evidence_bundle"):
            continue
        if int(values.get("plan_version") or 1) != base_version:
            continue
        if base_turn and values.get("turn_id") != base_turn:
            continue
        producer = await _producer_for(graph, snapshot)
        if producer:
            return snapshot, producer

    raise HTTPException(
        409,
        "找不到生成这一版方案之前的检查点，无法修订（可能是历史已被清理）",
    )


async def _producer_for(graph: Any, snapshot: Any) -> str | None:
    """找"生产这个 checkpoint"的节点。

    **不能只看一层 parent**：修订本身是用 ``aupdate_state(as_node=...)``
    合成的 checkpoint，它的 parent 也是"下一步生成建议卡"的 checkpoint
    （只是还没有被跑）。第二次修订如果只看一层，会把
    ``build_recommendation`` 当成生产节点 —— 一跳过它就直接出旧卡了。
    所以沿 parent 往上走，直到找到一个"下一步不是 build_recommendation"
    的节点，那才是真正的分叉前驱（本图里是 ``evidence_gate``）。
    """
    current = snapshot
    for _ in range(6):
        parent = (
            await graph.aget_state(current.parent_config)
            if current.parent_config
            else None
        )
        if parent is None:
            return None
        nexts = tuple(parent.next or ())
        if len(nexts) == 1:
            producer = nexts[0]
            if producer not in ("__start__", "", "build_recommendation"):
                return producer
        current = parent
    return None


def _risk_decision(
    revision_note: str, old_decision: dict[str, Any]
) -> dict[str, Any] | None:
    """新信息里的风险信号复查（纯规则，不调模型）。

    证据门控不重跑，家长补充"孩子最近开始打人"这类新风险不会被发现。
    这里按门控同一套严重风险词表再判一次，命中就把结论改写成
    ``professional_support`` —— 路由函数读的是这个字段，会自动改道。
    """
    signals = gate.serious_signals({"risk_signals": [revision_note]})
    if not signals:
        return None
    decision = dict(old_decision or {})
    missing = list(decision.get("missing_information") or [])
    for signal in signals:
        if signal not in missing:
            missing.append(signal)
    decision.update(
        evidence_status="risk",
        high_risk=True,
        needs_followup=False,
        decision="professional_support",
        missing_information=missing,
    )
    return decision


def _safe_decision(
    old_decision: dict[str, Any],
    bundle: EvidenceBundle,
    case_summary: dict[str, Any],
) -> dict[str, Any]:
    """按当前门控规则重算流向。

    **不沿用旧 ``decision``**：旧检查点里可能是简化前的
    ``limited_answer``。修订的意义是重新出卡，不能被旧状态拖回有限回答；
    高风险仍然会由 ``decide_flow`` 改写成 ``professional_support``。
    """
    decision = dict(old_decision or {})
    try:
        parsed = EvidenceDecision.model_validate(
            {k: v for k, v in decision.items() if k != "decision"}
        )
    except Exception:  # noqa: BLE001 - 老状态缺字段时按证据本身算
        parsed = EvidenceDecision(
            evidence_status=str(decision.get("evidence_status") or "none"),
            high_risk=bool(decision.get("high_risk")),
        )
    decision["decision"] = gate.decide_flow(parsed, bundle, case_summary)
    return decision


def _with_version(cards: list[dict[str, Any]], version: int) -> list[dict[str, Any]]:
    """给候选卡补上版本信息，前端据此分组。"""
    result: list[dict[str, Any]] = []
    for card in cards:
        card_version = int(card.get("plan_version") or version)
        result.append(
            {
                **card,
                "plan_version": card_version,
                "is_current_version": card_version == version,
            }
        )
    return result


async def _archived_plans(
    conversation_id: str, current_version: int
) -> list[dict[str, Any]]:
    """更早版本的卡，前端灰显保留。"""
    async with session_scope() as session:
        rows = await Repository(session).list_plans(conversation_id)
    return [
        row
        for row in rows
        if int(row.get("plan_version") or 1) < current_version
    ]


async def _payload(
    *,
    conversation_id: str,
    turn_id: str,
    revision_note: str,
    snapshot: Any,
) -> dict[str, Any]:
    """修订接口的统一响应（新跑完 / 幂等回读两种来源共用）。"""
    values = dict(getattr(snapshot, "values", None) or {})
    pending = _pending_of(snapshot)
    version = int(values.get("plan_version") or 1)

    if pending and pending.get("kind") == "plan_selection":
        version = int(pending.get("plan_version") or version)
        current = _with_version(list(pending.get("plans") or []), version)
    else:
        current = _with_version(list(values.get("candidate_plans") or []), version)

    # 风险分支/生成失败不出当前卡时，不把旧卡也塞回来 ——
    # "这次不出卡"要能一眼看出来。旧卡仍可从 GET 会话查。
    archived = await _archived_plans(conversation_id, version) if current else []
    decision = values.get("evidence_decision") or {}

    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "status": _status_of(pending, values),
        "plan_version": version,
        # 当前版本在前，旧版本随后；前端按 plan_version 分组展示。
        "plans": current + archived,
        # 选新卡之前仍是触发修订的那次尝试；选完变成新 trial。
        "trial_id": values.get("trial_id") or "",
        "message": values.get("final_answer") or "",
        "evidence": _evidence_panel(values.get("evidence_bundle")),
        "evidence_decision": {
            "evidence_status": decision.get("evidence_status"),
            "high_risk": decision.get("high_risk"),
            "missing_information": decision.get("missing_information") or [],
        },
        "evidence_gaps": (values.get("evidence_bundle") or {}).get(
            "evidence_gaps"
        ) or [],
        "revision_note": revision_note,
        "fork_checkpoint_id": values.get("fork_checkpoint_id") or "",
        "reused": REUSED_STAGES,
        "rerun": RERUN_STAGES,
    }


async def _record_revision_interactions(
    *,
    conversation_id: str,
    family_id: str,
    child_id: str,
    turn_id: str,
    revision_note: str,
    assistant_text: str,
) -> None:
    """把这次修订记进对话历史。

    它同时是幂等标记：风险分支不出卡，光靠 plans 的 source_turn_id
    判重覆盖不到，所以成功的修订一定会写一条 user 交互。
    """
    async with session_scope() as session:
        repo = Repository(session)
        await repo.record_interaction(
            conversation_id=conversation_id,
            thread_id=conversation_id,
            turn_id=turn_id,
            role="user",
            content=f"补充的新情况：{revision_note}",
            family_id=family_id,
            child_id=child_id,
        )
        if assistant_text:
            await repo.record_interaction(
                conversation_id=conversation_id,
                thread_id=conversation_id,
                turn_id=turn_id,
                role="assistant",
                content=assistant_text,
                family_id=family_id,
                child_id=child_id,
            )


async def _already_revised(
    *, conversation_id: str, turn_id: str
) -> bool:
    """同一个修订 turn_id 是不是已经跑过。"""
    async with session_scope() as session:
        repo = Repository(session)
        row = await repo.get_interaction(
            conversation_id=conversation_id, turn_id=turn_id, role="user"
        )
        # 普通的聊天 turn 也会写 user 交互，靠内容前缀区分，
        # 免得别处复用了同一个 turn_id 就把修订误判成"已经跑过"。
        if row is not None and str(row.content or "").startswith("补充的新情况："):
            return True
        rows = await repo.get_plans_by_source_turn(
            conversation_id=conversation_id, source_turn_id=turn_id
        )
        # 首版方案的 source_turn_id 就是普通聊天 turn。如果客户端把
        # 那个 turn_id 拿来当修订键，不能误判成"已经修订过"。
        return any(int(plan.plan_version or 1) > 1 for plan in rows)


@router.post("/trials/{trial_id}/revise")
async def revise_plan(trial_id: str, req: ReviseRequest) -> dict[str, Any]:
    """反馈后补充新情况，只重做"生成建议卡"及之后，出一版新卡。"""
    note = req.revision_note.strip()
    if not note:
        raise HTTPException(400, "补充的新情况不能为空")

    trial, family_id, child_id = await _ensure_identity_for_trial(trial_id)
    conversation_id = str(trial.conversation_id)
    graph = get_graph()

    # 幂等：同一个 turn_id 已经跑过 → 直接回读当前状态，不再分叉。
    if await _already_revised(conversation_id=conversation_id, turn_id=req.turn_id):
        snapshot = await graph.aget_state(config_for(conversation_id))
        return await _payload(
            conversation_id=conversation_id,
            turn_id=req.turn_id,
            revision_note=note,
            snapshot=snapshot,
        )

    async with session_scope() as session:
        repo = Repository(session)
        base_plan = await repo.get_latest_plan(conversation_id)
    if base_plan is None:
        raise HTTPException(409, "这个会话还没有可修订的方案")

    version = int(base_plan.plan_version or 1) + 1
    source, producer = await _find_fork_point(graph, conversation_id, base_plan)
    source_values = dict(source.values or {})

    # ---- 1. 新信息并进事实（纯函数，不依赖模型抽取） ----
    forbidden: list[str] = []
    if child_id:
        try:
            ctx = await memory_service.load_memory_context(
                child_id=child_id, query=note
            )
            forbidden = ctx.get("forbidden_facts") or []
        except Exception:  # noqa: BLE001 - 记忆读不出来不阻断修订
            forbidden = []
    case_summary, _merge_meta = merge_parent_supplement(
        source_values.get("case_summary") or {}, note, forbidden
    )

    # ---- 2. 新信息里的风险信号复查（纯规则） ----
    old_decision = dict(source_values.get("evidence_decision") or {})
    risk_decision = _risk_decision(note, old_decision)
    if risk_decision is not None:
        decision = risk_decision
        # 风险信号也写进事实，有限回答和后续轮次都能看到。
        signals = list(case_summary.get("risk_signals") or [])
        if note not in signals:
            signals.append(note)
        case_summary["risk_signals"] = signals
    else:
        try:
            bundle = EvidenceBundle.model_validate(
                source_values.get("evidence_bundle") or {}
            )
        except Exception:  # noqa: BLE001 - 老状态缺证据包时按"没有证据"处理
            bundle = EvidenceBundle(
                original_question="",
                case_summary=case_summary,
                evidence=[],
                retrieval_version="none",
            )
        decision = _safe_decision(old_decision, bundle, case_summary)

    reused = ["case_summary", *REUSED_STAGES]
    if not source_values.get("sufficiency_decision"):
        # 首版没跑完充分性判断的话，别在响应里虚报复用了它。
        reused.remove("sufficiency")
    if not source_values.get("asked_qa_pairs"):
        reused.remove("followup_qa")
    if forbidden:
        reused.append("memory_forbidden_facts")

    # ---- 3. 在"生成建议卡之前"的 checkpoint 上派生同线程分支 ----
    fork_values = {
        **source_values,
        "case_summary": case_summary,
        "evidence_decision": decision,
        "plan_version": version,
        "revision_note": note,
        "revision_source": {
            "trial_id": trial_id,
            "turn_id": req.turn_id,
            "base_plan_id": (base_plan.plan_json or {}).get("plan_id"),
            "base_plan_version": int(base_plan.plan_version or 1),
            "fork_checkpoint_id": (source.config or {})
            .get("configurable", {})
            .get("checkpoint_id"),
        },
        "revision_reused": reused,
        "entrypoint": "chat",
        "turn_id": req.turn_id,
        "user_message": note,
        "debug": False,
        # 右边这一侧全部重做 —— 旧卡、旧审查结论、旧选择都不能带过来。
        "candidate_plans": [],
        "selected_plan_id": None,
        "selected_plan_db_id": "",
        "plan_db_ids": {},
        "safety_review": {},
        "safety_dropped_plans": [],
        "safety_fallback_used": False,
        "final_answer": "",
        "answer_parts": {},
        "answer_mode": "",
        "comparison_note": "",
        "route_after_recommendation": None,
        "route_after_render": None,
        "route_override": "",
        # 保留触发这次修订的那次尝试：家长还没选新卡之前，
        # 反馈结果和"记录反馈"入口仍要能挂在这条历史上。
        # 选定新卡后 commit_plan_selection 会把它换成新 trial。
        "trial_id": trial_id,
        "fork_checkpoint_id": "",
    }

    fork_config = _checkpoint_config(
        conversation_id,
        (source.config or {}).get("configurable", {}).get("checkpoint_id"),
    )
    await graph.aupdate_state(fork_config, fork_values, as_node=producer)
    await graph.ainvoke(None, config_for(conversation_id))

    snapshot = await graph.aget_state(config_for(conversation_id))
    values = snapshot.values or {}
    await _record_revision_interactions(
        conversation_id=conversation_id,
        family_id=family_id,
        child_id=child_id,
        turn_id=req.turn_id,
        revision_note=note,
        assistant_text=str(values.get("final_answer") or ""),
    )
    return await _payload(
        conversation_id=conversation_id,
        turn_id=req.turn_id,
        revision_note=note,
        snapshot=snapshot,
    )
