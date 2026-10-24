"""写库节点。

放在独立文件是因为它们和 :mod:`chat_nodes` 的规则相反：
中断之后的节点**才**允许有副作用。LangGraph 恢复时只会重跑包含
``interrupt()`` 的那个节点，这里在中断之后，所以重跑不会翻倍写入；
真正的保险还是 ``turn_id`` 唯一约束。
"""

from __future__ import annotations

import uuid
from typing import Any

from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.state import CoachState
from rdi_agent.schemas.recommendation import RecommendationCard


def _as_cards(state: CoachState) -> list[RecommendationCard]:
    return [
        RecommendationCard.model_validate(p) for p in state.get("candidate_plans") or []
    ]


async def persist_evidence_bundle(state: CoachState) -> dict[str, Any]:
    """把证据包落库。

    **为什么要单独一个节点**：证据包是"回答的审计轨迹" ——
    之后要能回答"当时依据的是哪几段指南里的内容"。
    有限回答那一路没有建议卡，但同样检索过、同样有依据，
    所以证据包的落库不能挂在写卡的那个节点上。

    幂等：靠 UNIQUE(conversation_id, turn_id)。追问循环会让这个节点
    跑不止一次，覆盖成最后一次 —— 那份才是最终回答的依据。
    """
    bundle = state.get("evidence_bundle")
    if not bundle:
        return {}
    async with session_scope() as session:
        bundle_id = await Repository(session).save_evidence_bundle(
            conversation_id=state["conversation_id"],
            turn_id=state.get("turn_id", ""),
            bundle=bundle,
            retrieval_version=str(bundle.get("retrieval_version") or "v1"),
        )
    return {"evidence_bundle_db_id": str(bundle_id)}


async def persist_candidate_plans(state: CoachState) -> dict[str, Any]:
    """中断之前先落候选卡。

    为什么在中断前写：家长可能看到卡之后直接关掉浏览器再也不回来，
    这一轮的分析不该丢掉。写的是 ``status='proposed'``，
    真正的 ``selected`` 由 commit_plan_selection 改。

    **建议卡要能追溯到"当时依据的是哪几段指南里的内容"**（阶段 2C），
    靠 ``plans.evidence_bundle_id`` 指向证据包。证据包由
    ``persist_evidence_bundle`` 节点落库，这里只把它的 id 挂到卡上。
    """
    cards = _as_cards(state)
    if not cards:
        return {"plan_db_ids": {}}
    raw_bundle_id = state.get("evidence_bundle_db_id")
    evidence_bundle_id = uuid.UUID(str(raw_bundle_id)) if raw_bundle_id else None
    async with session_scope() as session:
        repo = Repository(session)
        mapping = await repo.save_candidate_plans(
            conversation_id=state["conversation_id"],
            family_id=state["family_id"],
            child_id=state.get("child_id"),
            cards=cards,
            evidence_bundle_id=evidence_bundle_id,
        )
    return {"plan_db_ids": {k: str(v) for k, v in mapping.items()}}


def _with_selection_note(previous: str, target: str) -> str:
    """在已渲染的回答后面接一句"选好了"。"""
    note = (
        f"已经选定「{target}」。先做一周，别同时改别的。"
        "一周后回来告诉我们实际发生了什么，我们再调。"
    )
    return f"{previous}\n\n{note}" if previous else note


async def commit_plan_selection(state: CoachState) -> dict[str, Any]:
    """中断之后：把选中的卡标 selected 并建 trial。

    幂等：同一个 (conversation_id, plan_id) 已经有 accepted 的 trial 就直接返回，
    重发 select-plan 请求不会产生第二条尝试。
    """
    plan_id = state.get("selected_plan_id")
    if not plan_id:
        return {"error_code": "no_plan_selected"}

    cards = _as_cards(state)
    card = next((c for c in cards if c.plan_id == plan_id), None)
    target = card.title if card else plan_id

    async with session_scope() as session:
        repo = Repository(session)
        plan, trial = await repo.select_plan(
            conversation_id=state["conversation_id"],
            plan_id=plan_id,
            turn_id=state.get("turn_id", ""),
            child_id=state["child_id"],
            target=target,
        )
        if plan is None or trial is None:
            return {"error_code": "plan_not_found"}
        db_plan_id, trial_id = str(plan.id), str(trial.id)

    return {
        "selected_plan_id": plan_id,
        "selected_plan_db_id": db_plan_id,
        "trial_id": trial_id,
        # 前面渲染好的回答要留着 —— 家长刚看到建议就被换成一句话，
        # 等于把内容从对话里抹掉了。
        "final_answer": _with_selection_note(state.get("final_answer") or "", target),
    }