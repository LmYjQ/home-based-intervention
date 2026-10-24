"""会话相关的 HTTP 接口（计划 附录 A）。

三个入口，同一个 thread：

- ``POST /api/conversations``                  建会话，拿到 conversation_id
- ``POST /api/conversations/{id}/messages``    发消息；可能停在追问中断或方案选择中断
- ``POST /api/conversations/{id}/select-plan`` 恢复方案选择中断
- ``GET  /api/conversations/{id}``             查会话当前状态
- ``POST /api/trials/{trial_id}/feedback``     反馈入口

**中断怎么表达**：``ainvoke`` 会在 ``interrupt()`` 处停下，返回值的
``__interrupt__`` 里有暂停信息，state 里 ``selected_plan_id`` 还没填。
接口把它翻译成一个显式的 ``status`` 字段，让前端不用去猜 LangGraph 的内部形状。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from pydantic import BaseModel

from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.runtime import config_for, get_graph
from rdi_agent.schemas.feedback import FeedbackSubmission

router = APIRouter(prefix="/api", tags=["conversations"])


# ------------------------------------------------------------------ 请求体
class CreateConversationRequest(BaseModel):
    family_id: str | None = None
    display_label: str = "孩子"
    age_band: str | None = None


class SendMessageRequest(BaseModel):
    message: str
    # 幂等键：同一个 turn_id 重发不会重复写业务记录
    turn_id: str | None = None
    debug: bool = False


class SelectPlanRequest(BaseModel):
    plan_id: str
    turn_id: str | None = None


# ------------------------------------------------------------------ 工具
def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """从 ainvoke 的返回值里把中断信息抠出来。

    LangGraph 暂停时把中断放在 ``__interrupt__``（Interrupt 对象的列表）。
    没有这个键就是跑到底了。
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", None)
    return value if isinstance(value, dict) else {"raw": str(value)}


async def _pending_interrupt(config: dict[str, Any]) -> dict[str, Any] | None:
    """当前 thread 停在哪个中断上。没有就返回 None。

    没有它就没法判断"这次调用是新开一轮，还是在回答上一个问题"——
    而这个判断决定了要传普通 dict 还是 Command(resume=...)。
    """
    try:
        snapshot = await get_graph().aget_state(config)
    except RuntimeError:
        return None
    for task in snapshot.tasks or ():
        for intr in getattr(task, "interrupts", None) or ():
            value = getattr(intr, "value", None)
            return value if isinstance(value, dict) else {"raw": str(value)}
    return None


def _status_of(
    interrupt_payload: dict[str, Any] | None,
    result: dict[str, Any] | None = None,
) -> str:
    """这一轮的状态。

    阶段 2C 之后多了一种"跑完了但没给建议卡"的情况 ——
    证据不够（limited_answer）或者家长报告了风险信号
    （needs_professional_support）。这两种都要和"正常完成"区分开，
    否则前端会把"我没给建议"当成"建议生成完了"。
    """
    if interrupt_payload is not None:
        kind = interrupt_payload.get("kind")
        return {
            "followup": "awaiting_followup",
            "plan_selection": "awaiting_plan_selection",
        }.get(kind, "interrupted")
    decision = (result or {}).get("evidence_decision") or {}
    if decision.get("high_risk"):
        return "needs_professional_support"
    if decision.get("decision") == "limited_answer":
        return "limited_answer"
    return "completed"


def _evidence_panel(bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    """证据面板（计划 §9.3：章节、页码、摘要、来源标签、审核状态）。

    **不给原文**：这份指南的授权没核实过，``may_quote_to_user`` 恒为 False，
    所以响应里只有摘要和页码，没有 ``source_excerpt``。
    """
    if not bundle:
        return []
    panel: list[dict[str, Any]] = []
    for item in bundle.get("evidence") or []:
        panel.append(
            {
                "evidence_id": item.get("evidence_id"),
                "source_type": item.get("source_type"),
                "title": item.get("title"),
                "section_title": item.get("section_title"),
                "pdf_page_start": item.get("pdf_page_start"),
                "pdf_page_end": item.get("pdf_page_end"),
                "summary": item.get("summary"),
                "relevance_reason": item.get("relevance_reason"),
                "review_status": item.get("review_status"),
                "similarity": item.get("similarity"),
                "may_quote_to_user": item.get("may_quote_to_user", False),
            }
        )
    return panel


async def _ensure_identity(
    conversation_id: str,
) -> tuple[str, str]:
    """从会话 id 里拿 family_id / child_id。

    阶段 1 的会话 id 编码了三段：``family_id.child_id.随机后缀``。
    这不是加密，只是让 API 层不用额外维护一张会话表就能拿到归属；
    阶段 5 持久化调试上线后会换成真正的 conversation 表。

    三段都要校验成 UUID —— 只数段数的话，"abc.def.ghi" 这种会被
    当成合法 id 传下去，到 SQLAlchemy 才炸，报错很难看懂。
    """
    parts = conversation_id.split(".")
    if len(parts) != 3:
        raise HTTPException(400, "conversation_id 格式不对")
    family_id, child_id = parts[0], parts[1]
    for part in (family_id, child_id):
        try:
            uuid.UUID(part)
        except ValueError:
            raise HTTPException(400, "conversation_id 里的 id 不是合法 UUID") from None
    return family_id, child_id


# ------------------------------------------------------------------ 接口
@router.post("/conversations")
async def create_conversation(req: CreateConversationRequest) -> dict[str, Any]:
    """建会话。conversation_id 里带上 family/child，省一张会话表。"""
    async with session_scope() as session:
        repo = Repository(session)
        # family_id 必须是 UUID —— children.family_id 是 UUID 列，
        # 而且 conversation_id 靠 "." 分段解析出它，前缀 id 解析不出来。
        family_id = req.family_id or str(uuid.uuid4())
        child = await repo.ensure_child(
            family_id=family_id,
            display_label=req.display_label,
            age_band=req.age_band,
        )
        child_id, family_uuid = str(child.id), str(child.family_id)

    conversation_id = f"{family_uuid}.{child_id}.{uuid.uuid4().hex[:12]}"
    return {
        "conversation_id": conversation_id,
        "thread_id": conversation_id,  # 一个会话一条线，见 runtime.config_for
        "child_id": child_id,
        "family_id": family_uuid,
    }


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, req: SendMessageRequest) -> dict[str, Any]:
    """发一条消息。可能在追问或方案选择处停下。"""
    family_id, child_id = await _ensure_identity(conversation_id)
    turn_id = req.turn_id or _new_id("turn")

    async with session_scope() as session:
        repo = Repository(session)
        await repo.record_interaction(
            conversation_id=conversation_id,
            thread_id=conversation_id,
            turn_id=turn_id,
            role="user",
            content=req.message,
            family_id=family_id,
            child_id=child_id,
        )
        context = await repo.child_context(child_id)

    graph = get_graph()
    config = config_for(conversation_id)
    pending = await _pending_interrupt(config)

    if pending and pending.get("kind") == "followup":
        # 这条消息是**回答上一个追问**，不是新开一轮。
        # 必须用 Command(resume=...) 恢复：普通 dict 会让图从 START 重跑，
        # await_user_answer 永远跑不完，followup_rounds 也就永远停在 0，
        # "追问 3 轮就收手"的兜底形同虚设。
        result = await graph.ainvoke(
            Command(resume=req.message, update={"turn_id": turn_id}),
            config=config,
        )
    else:
        result = await graph.ainvoke(
            {
                "entrypoint": "chat",
                "conversation_id": conversation_id,
                "thread_id": conversation_id,
                "family_id": family_id,
                "child_id": child_id,
                "turn_id": turn_id,
                "user_message": req.message,
                "child_context": context,
                "debug": req.debug,
            },
            config=config,
        )

    interrupt_payload = _interrupt_payload(result)
    status = _status_of(interrupt_payload, result)
    evidence = _evidence_panel(result.get("evidence_bundle"))
    decision = result.get("evidence_decision") or {}

    # 助手侧也记一条，方便 GET 查回完整对话。
    assistant_text = result.get("final_answer") or ""
    if status == "awaiting_followup":
        assistant_text = "\n".join(interrupt_payload.get("questions", []))
    elif status == "awaiting_plan_selection":
        assistant_text = result.get("final_answer") or ""
    if assistant_text:
        async with session_scope() as session:
            await Repository(session).record_interaction(
                conversation_id=conversation_id,
                thread_id=conversation_id,
                turn_id=turn_id,
                role="assistant",
                content=assistant_text,
                family_id=family_id,
                child_id=child_id,
            )

    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "status": status,
        "questions": (interrupt_payload or {}).get("questions", []),
        "plans": (interrupt_payload or {}).get("plans", []),
        "message": assistant_text,
        "trial_id": result.get("trial_id"),
        # 证据面板：家长能看见"这次依据的是书里哪几段"
        "evidence": evidence,
        "evidence_decision": {
            "evidence_status": decision.get("evidence_status"),
            "can_support_personalized_advice": decision.get(
                "can_support_personalized_advice"
            ),
            "missing_information": decision.get("missing_information") or [],
        },
        "evidence_gaps": (result.get("evidence_bundle") or {}).get(
            "evidence_gaps"
        ) or [],
    }


@router.post("/conversations/{conversation_id}/select-plan")
async def select_plan(conversation_id: str, req: SelectPlanRequest) -> dict[str, Any]:
    """恢复方案选择中断。"""
    family_id, child_id = await _ensure_identity(conversation_id)
    turn_id = req.turn_id or _new_id("turn")

    graph = get_graph()
    config = config_for(conversation_id)
    pending = await _pending_interrupt(config)

    if pending and pending.get("kind") == "plan_selection":
        # 同样要恢复：present_plans 后面才是 commit_plan_selection，
        # 不恢复的话 trial 根本不会被创建。
        result = await graph.ainvoke(
            Command(resume={"plan_id": req.plan_id}, update={"turn_id": turn_id}),
            config=config,
        )
    else:
        # 没有待选的中断：可能已经选过了（重试），从已落库的结果里回读。
        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        if values.get("selected_plan_id") == req.plan_id and values.get("trial_id"):
            return {
                "conversation_id": conversation_id,
                "status": "completed",
                "selected_plan_id": values.get("selected_plan_id"),
                "selected_plan_db_id": values.get("selected_plan_db_id"),
                "trial_id": values.get("trial_id"),
                "message": values.get("final_answer", ""),
            }
        raise HTTPException(409, "当前会话没有待选的方案")
    if result.get("error_code"):
        raise HTTPException(404, f"选方案失败：{result['error_code']}")

    return {
        "conversation_id": conversation_id,
        "status": result.get("error_code") or "completed",
        "selected_plan_id": result.get("selected_plan_id"),
        "selected_plan_db_id": result.get("selected_plan_db_id"),
        "trial_id": result.get("trial_id"),
        "message": result.get("final_answer", ""),
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    """会话当前状态：消息、候选方案、是否停在中断上。"""
    family_id, child_id = await _ensure_identity(conversation_id)
    async with session_scope() as session:
        repo = Repository(session)
        interactions = await repo.list_interactions(conversation_id)
        plans = await repo.list_plans(conversation_id)
        context = await repo.child_context(child_id)

    # 从 Checkpointer 读当前 pending interrupt
    pending: dict[str, Any] | None = None
    try:
        snapshot = await get_graph().aget_state(config_for(conversation_id))
        for task in snapshot.tasks or ():
            for intr in getattr(task, "interrupts", ()) or ():
                value = getattr(intr, "value", None)
                pending = value if isinstance(value, dict) else {"raw": str(value)}
                break
    except RuntimeError:
        pending = None  # 图没 setup（比如只跑 /health），不算错误

    values: dict[str, Any] = {}
    try:
        snapshot = await get_graph().aget_state(config_for(conversation_id))
        values = snapshot.values or {}
    except RuntimeError:
        values = {}

    return {
        "conversation_id": conversation_id,
        "family_id": family_id,
        "child_id": child_id,
        "child_context": context,
        "status": _status_of(pending, values),
        "pending_interrupt": pending,
        "interactions": interactions,
        "plans": plans,
        "evidence": _evidence_panel(values.get("evidence_bundle")),
        "evidence_decision": values.get("evidence_decision") or {},
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict[str, Any]:
    """方案卡全文。

    版权：阶段 1 的卡 ``application_label`` 是 ``ai_adaptation``、引用为空，
    所以没有原文可给。接了知识库之后，这个接口要按 ``may_quote_to_user``
    决定返回摘要还是原文。
    """
    async with session_scope() as session:
        plan = await Repository(session).get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "recommended": plan.recommended,
        "application_label": plan.application_label,
        "card": plan.plan_json,
    }


@router.post("/trials/{trial_id}/feedback")
async def submit_feedback(trial_id: str, req: FeedbackSubmission) -> dict[str, Any]:
    """反馈入口。重试同一个 turn_id 不会重复写，也不会重复跑 LLM 分析。"""
    family_id, child_id = await _ensure_identity_for_trial(trial_id)

    graph = get_graph()
    result = await graph.ainvoke(
        {
            "entrypoint": "feedback",
            "conversation_id": "",
            "thread_id": _new_id("fb"),
            "family_id": family_id,
            "child_id": child_id,
            "turn_id": req.turn_id,
            "feedback": req.model_dump(mode="json"),
        },
        # 反馈入口不带 Checkpointer 也不会退化：它不中断，跑完就结束。
        # 单独一条 thread 免得和聊天的 thread 状态互相覆盖。
        config=config_for(f"feedback-{trial_id}-{req.turn_id}"),
    )
    return {
        "trial_id": trial_id,
        "status": "completed",
        "feedback_created": result.get("feedback_created"),
        "analysis": result.get("feedback_analysis"),
        "next_step": result.get("next_step"),
        "message": result.get("final_answer", ""),
    }


async def _ensure_identity_for_trial(trial_id: str) -> tuple[str, str]:
    from rdi_agent.db.models import Child

    async with session_scope() as session:
        trial = await Repository(session).get_trial(trial_id)
        if trial is None:
            raise HTTPException(404, "尝试不存在")
        child = await session.get(Child, trial.child_id)
    if child is None:
        raise HTTPException(404, "孩子档案不存在")
    return str(child.family_id), str(child.id)