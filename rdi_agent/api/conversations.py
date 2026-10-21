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


def _status_of(interrupt_payload: dict[str, Any] | None) -> str:
    if interrupt_payload is None:
        return "completed"
    kind = interrupt_payload.get("kind")
    return {"followup": "awaiting_followup", "plan_selection": "awaiting_plan_selection"}.get(
        kind, "interrupted"
    )


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
        config=config_for(conversation_id),
    )

    interrupt_payload = _interrupt_payload(result)
    status = _status_of(interrupt_payload)

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
    }


@router.post("/conversations/{conversation_id}/select-plan")
async def select_plan(conversation_id: str, req: SelectPlanRequest) -> dict[str, Any]:
    """恢复方案选择中断。"""
    family_id, child_id = await _ensure_identity(conversation_id)
    turn_id = req.turn_id or _new_id("turn")

    graph = get_graph()
    result = await graph.ainvoke(
        {"selected_plan_id": req.plan_id, "turn_id": turn_id},
        config=config_for(conversation_id),
    )
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

    return {
        "conversation_id": conversation_id,
        "family_id": family_id,
        "child_id": child_id,
        "child_context": context,
        "status": _status_of(pending),
        "pending_interrupt": pending,
        "interactions": interactions,
        "plans": plans,
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