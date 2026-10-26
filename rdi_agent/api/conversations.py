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

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel

from rdi_agent.age import parse_birth_date
from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.db.repositories.memory import list_memories as list_memories_repo
from rdi_agent.graph.runtime import config_for, feedback_thread_id, get_graph
from rdi_agent.schemas.feedback import FeedbackSubmission

router = APIRouter(prefix="/api", tags=["conversations"])


# ------------------------------------------------------------------ 请求体
class CreateConversationRequest(BaseModel):
    family_id: str | None = None
    # **复用已有孩子**：传了就用这个孩子开新会话。
    #
    # 没有它，验收第 1 条（"第二轮咨询会主动使用第一轮的事实"）
    # 根本演示不出来 —— 每次建会话都新建一个孩子，
    # 上一轮的长期记忆就永远挂在另一个 child_id 上。
    child_id: str | None = None
    display_label: str = "孩子"
    age_band: str | None = None
    # 出生年月（YYYY-MM 或 YYYY-MM-DD）。年龄不存，读的时候现算。
    birth_date: str | None = None


class FollowupAnswer(BaseModel):
    """家长对**某一个**追问的回答。

    ``answer`` 是家长点选的选项原文，或者自己写的字 ——
    两者在前端是互斥的（写了字，选项就作废），
    所以这里不需要再区分"点的"还是"写的"。
    """

    # 原问题文本。用来把答案和问题对上，也方便留痕。
    question: str
    answer: str


class SendMessageRequest(BaseModel):
    message: str
    # 幂等键：同一个 turn_id 重发不会重复写业务记录
    turn_id: str | None = None
    debug: bool = False
    # 一轮追问里每个问题的回答。
    #
    # **为什么要有它**：一轮追问有多个问题，家长是**逐题作答、最后一起提交**的。
    # 只传一个扁平的 message，就分不清哪段话是回答哪个问题的 ——
    # 下游 parse_parent_input 只能靠猜，多题回答会串。
    #
    # 有 answers 时，``message`` 可以是空的；接口会用 answers 拼出给
    # 图看的文本。两者都传则以 answers 为准。
    answers: list[FollowupAnswer] | None = None


class SelectPlanRequest(BaseModel):
    plan_id: str
    turn_id: str | None = None


class FeedbackFollowupRequest(BaseModel):
    """反馈追问的恢复请求。

    ``turn_id`` 必须和最初提交反馈时的一致 —— 它同时是幂等键和
    checkpoint 线程的一部分（追问中断就停在那条线程上）。
    """

    turn_id: str
    message: str = ""
    answers: list[FollowupAnswer] = []


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


def _question_texts(questions: list[Any]) -> list[str]:
    """把一轮追问统一成"纯问题文本"列表。

    ``followup_questions`` 里存的是 dict（带选项），
    但有两处只想要问题本身：
      - 存助手消息（interactions.content 是纯文本列）
      - 兼容老数据：``questions`` 里也可能是裸字符串
    """
    texts: list[str] = []
    for q in questions or []:
        if isinstance(q, dict):
            texts.append(str(q.get("question") or "").strip())
        else:
            texts.append(str(q).strip())
    return [t for t in texts if t]


def _compose_answer_text(
    answers: list[FollowupAnswer] | None,
    message: str,
) -> str:
    """把"每题一个回答"拼成给图看的一段文本。

    图里 ``parse_parent_input`` 只吃一段文本，所以这里要拼。
    **带上原问题** —— 不带的话，多题回答会互相串：
    下游看到"在家更自在、换过老师"，分不清哪个说的是哪个。
    """
    if not answers:
        return message
    lines = []
    for a in answers:
        ans = (a.answer or "").strip()
        if not ans:
            continue
        q = (a.question or "").strip()
        lines.append(f"{q}\n{ans}" if q else ans)
    if not lines:
        return message
    joined = "\n\n".join(lines)
    # message 里家长自己额外说的话（如果有）也带上
    extra = (message or "").strip()
    return f"{joined}\n\n{extra}" if extra else joined



def _structured_answers(
    answers: list[FollowupAnswer] | None,
) -> list[dict[str, str]]:
    """把每题一个回答变成结构化的问答对。

    **为什么除了拼文本还要单独传一份结构化的**：
    光有文本的话，"哪句回答的是哪道题"只能靠模型猜 ——
    实测模型会把整段问答丢掉，家长答了"没有不愉快的经历"，
    抽出来的事实里还是没有，于是换个说法又问一遍。

    结构化的这份由代码直接并进 ``case_summary``，不指望模型抽。
    """
    result: list[dict[str, str]] = []
    for a in answers or []:
        ans = (a.answer or "").strip()
        if not ans:
            continue
        result.append(
            {"question": (a.question or "").strip(), "answer": ans}
        )
    return result


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

    **判断依据要看 ``answer_mode``，不是只看 ``evidence_decision``**：
    证据门控判的是 "recommend"（证据够），但建议卡生成可能整体失败
    （schema 连着几次不合规、引用校验全挂）—— 这时图走的是
    ``build_limited_answer``，家长拿到的是一段文字而不是方案卡。
    只看 evidence_decision 会把这种情况报成 "completed"，
    前端就以为流程正常跑完了，家长一脸莫名其妙。
    """
    if interrupt_payload is not None:
        kind = interrupt_payload.get("kind")
        return {
            "followup": "awaiting_followup",
            "plan_selection": "awaiting_plan_selection",
            "feedback_followup": "awaiting_feedback_followup",
        }.get(kind, "interrupted")
    values = result or {}
    decision = values.get("evidence_decision") or {}
    if decision.get("high_risk"):
        return "needs_professional_support"
    # 反馈迭代的"证据支撑的调整建议"：不是方案卡，也不是拒答，单独一种状态。
    if values.get("answer_mode") == "adjustment":
        return "adjustment"
    # 图走到了有限回答这一路 —— 不管是因为证据不够，还是因为生成建议卡失败了。
    if values.get("answer_mode") in ("limited", "risk"):
        return "needs_professional_support" if values.get("answer_mode") == "risk" else "limited_answer"
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
    """建会话。conversation_id 里带上 family/child，省一张会话表。

    传了 ``child_id`` 就复用那个孩子（长期记忆挂在孩子上，
    换会话不该换孩子）；没传才新建。
    """
    async with session_scope() as session:
        repo = Repository(session)
        if req.child_id:
            try:
                uuid.UUID(req.child_id)
            except ValueError:
                raise HTTPException(400, "child_id 不是合法 UUID") from None
            child = await repo.get_child(req.child_id)
            if child is None:
                raise HTTPException(404, "孩子档案不存在")
            if req.family_id and str(child.family_id) != req.family_id:
                raise HTTPException(403, "这个孩子不属于该家庭")
            child_id, family_uuid = str(child.id), str(child.family_id)
        else:
            # family_id 必须是 UUID —— children.family_id 是 UUID 列，
            # 而且 conversation_id 靠 "." 分段解析出它，前缀 id 解析不出来。
            family_id = req.family_id or str(uuid.uuid4())
            try:
                birth = parse_birth_date(req.birth_date)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            child = await repo.ensure_child(
                family_id=family_id,
                display_label=req.display_label,
                age_band=req.age_band,
                birth_date=birth,
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
    """发一条消息。可能在追问或方案选择处停下。

    **回答追问**时，请求体里可以带 ``answers``（每题一个回答）。
    带了就用 answers 拼出给图看的文本；不带就退回 ``message``。

    **这是非流式版本**。想看进度用 ``/messages/stream``（SSE）——
    两边跑的是同一套逻辑（``_run_turn``），只是后者边跑边推事件。
    """
    result = await _run_turn(conversation_id, req)
    return _turn_payload(conversation_id, result)


async def _run_turn(conversation_id: str, req: SendMessageRequest) -> dict[str, Any]:
    """跑一轮对话，返回图的原始返回值。

    **流式和非流式共用这一份逻辑** —— 两边必须产出完全一样的结果，
    否则手测时看到的和线上跑的就不是一回事了。
    """
    family_id, child_id = await _ensure_identity(conversation_id)
    turn_id = req.turn_id or _new_id("turn")

    # 图看到的文本。有 answers 就用 answers 拼的（带上原问题，避免多题串味）。
    user_text = _compose_answer_text(req.answers, req.message)
    # 结构化的问答对 —— 走图里的 followup_answers 通道。
    # 文本那条路是给模型看的，这条路是给代码用的（保证答案不丢）。
    structured_answers = _structured_answers(req.answers)

    async with session_scope() as session:
        repo = Repository(session)
        # 调试分支不写业务表（计划 §12.4）：调试跑出来的对话不能混进
        # 家长真实看到的历史里。
        if not req.debug:
            await repo.record_interaction(
                conversation_id=conversation_id,
                thread_id=conversation_id,
                turn_id=turn_id,
                role="user",
                content=user_text,
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
        # 恢复值带上结构化的问答对。
        #
        # **为什么不用裸字符串**：只有文本的话，答案要靠模型抽，
        # 而实测模型会把它整段丢掉 —— 家长答过的事下一轮又被问一遍。
        # 传 dict 的话，``await_user_answer`` 能把问答对原样放进 state，
        # 由代码并进 case_summary。
        resume_value: Any = {"text": user_text, "answers": structured_answers}
        result = await graph.ainvoke(
            Command(resume=resume_value, update={"turn_id": turn_id}),
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
                "user_message": user_text,
                "child_context": context,
                "debug": req.debug,
            },
            config=config,
        )

    # 助手侧也记一条，方便 GET 查回完整对话。
    assistant_text = _assistant_text(result)
    if assistant_text and not req.debug:
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

    result["_assistant_text"] = assistant_text
    result["_turn_id"] = turn_id
    return result


def _assistant_text(result: dict[str, Any]) -> str:
    """这一轮存进对话记录的助手文本。

    追问那一轮存的是**问题文本**（纯文本列存不了 dict），
    具体题目由前端用 ``questions`` 里的结构重新渲染。
    """
    interrupt_payload = _interrupt_payload(result)
    status = _status_of(interrupt_payload, result)
    if status == "awaiting_followup":
        return "\n".join(_question_texts((interrupt_payload or {}).get("questions", [])))
    return result.get("final_answer") or ""


def _turn_payload(conversation_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """把图的返回值翻译成接口响应。"""
    interrupt_payload = _interrupt_payload(result)
    status = _status_of(interrupt_payload, result)
    evidence = _evidence_panel(result.get("evidence_bundle"))
    decision = result.get("evidence_decision") or {}
    assistant_text = result.get("_assistant_text") or result.get("final_answer") or ""

    return {
        "conversation_id": conversation_id,
        "turn_id": result.get("_turn_id"),
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
        # 这一轮之后这个孩子记住了什么（前端"记忆区"直接用）
        "memory": result.get("memory_context") or {},
        "memory_notes": result.get("memory_notes") or [],
    }


@router.post("/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: str, req: SendMessageRequest
) -> StreamingResponse:
    """发一条消息，**边跑边推进度**（SSE）。

    **推的是什么**：进度事件（"正在检索指南…"），不是答案正文。
    答案要等图跑完才有一个完整形态 —— 抽取类调用走的是
    ``json_schema`` 严格约束解码，模型必须保证 JSON 语法合法才能吐
    token，中途吐出来的是半截 JSON，家长也看不懂。

    所以这条流解决的是"它卡在哪儿"，不是"字一个字出来"。

    **和 ``/messages`` 的关系**：跑的是同一套逻辑（``_run_turn``），
    最后推的 ``result`` 事件和非流式接口的 body **字段完全一样**。
    前端拿它替换掉普通请求即可。

    事件格式（``text/event-stream``）：

    - ``event: progress`` —— 进度，data 是 ``{stage, label, kind, attempt}``
    - ``event: result``   —— 最终结果，data 和 ``/messages`` 的 body 一样
    - ``event: error``    —— 出错，data 是 ``{message}``
    """
    # 先把身份校验跑一遍 —— 错了要立刻返回 4xx，
    # 不能已经开流了再报错（SSE 里发错误，前端只能看到流里的一条消息）。
    await _ensure_identity(conversation_id)

    async def event_source():
        from rdi_agent.progress import spawn_with_progress

        # 进度事件和最终结果走同一个出口队列 ——
        # 分两个队列的话要处理"结果先于进度到达"的乱序问题。
        outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def put(event: str, data: dict[str, Any]) -> None:
            outbox.put_nowait({"event": event, "data": data})

        async def run_graph() -> None:
            """跑图，把进度和结果都塞进出站队列。"""
            try:
                task, progress_queue = spawn_with_progress(
                    _run_turn(conversation_id, req)
                )
                # 一个 task 跑图，另一个把进度转成 SSE 事件。
                pump_task = asyncio.create_task(pump(progress_queue))
                try:
                    result = await task
                finally:
                    pump_task.cancel()
                put("result", _turn_payload(conversation_id, result))
            except Exception as exc:  # noqa: BLE001 - 错误要发给前端，不是吞掉
                put("error", {"message": f"{type(exc).__name__}: {exc}"})
            finally:
                outbox.put_nowait(None)  # 哨兵：告诉消费方"完了"

        async def pump(progress_queue) -> None:
            """把进度事件转成 SSE。"""
            while True:
                event = await progress_queue.get()
                put(
                    "progress",
                    {
                        "stage": event.stage,
                        "label": event.label,
                        "kind": event.kind,
                        "detail": event.detail,
                        "attempt": event.attempt,
                    },
                )

        runner = asyncio.create_task(run_graph())
        try:
            while True:
                item = await outbox.get()
                if item is None:
                    break
                yield _sse(item["event"], item["data"])
        finally:
            # 客户端断线时取消图 —— 不然后台白跑一遍 LLM。
            if not runner.done():
                runner.cancel()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 的缓冲，否则 SSE 会被攒着一起发。
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    """一条 SSE 事件。

    ``data`` 必须是单行 JSON —— SSE 的 ``data:`` 每行一个字段，
    多行的话要按行拆开写。这里 json.dumps 不换行，所以是单行。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


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
        # 孩子档案没了（清过库/换过库），这条会话也就不存在了。
        # 不报 404 的话前端会拿着一个空会话继续用，发消息才炸。
        if not context:
            raise HTTPException(404, "孩子档案不存在")
        # 直接复用当前 session —— 另开一个 ``session_scope`` 会多占一条连接，
        # 而且和这里的外层事务没有任何协同的必要。
        memories = await list_memories_repo(
            session, child_id=child_id, family_id=family_id
        )

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

    # 停在选方案中断上时，**用中断里那份候选卡**。
    #
    # **为什么**：数据库里的 plans 是落库后的行（status/recommended 这些
    # 字段是后来改的），而中断里的是"当时给家长看的那三张卡"——
    # 引用、家长可以做的事、观察指标都在里面。刷新页面后前端要
    # 把卡原样接回来，用落库那份会丢掉引用。
    if pending and pending.get("kind") == "plan_selection" and pending.get("plans"):
        plans = list(pending["plans"])

    return {
        "conversation_id": conversation_id,
        "family_id": family_id,
        "child_id": child_id,
        "child_context": context,
        "status": _status_of(pending, values),
        "pending_interrupt": pending,
        "interactions": interactions,
        "plans": plans,
        # 选了方案之后 trial 才落库。刷新页面后前端要靠它把反馈表单接回来 ——
        # 少了这个字段，刷新一次反馈表单就没了，家长以为没提交成功。
        "trial_id": values.get("trial_id"),
        "selected_plan_id": values.get("selected_plan_id"),
        "evidence": _evidence_panel(values.get("evidence_bundle")),
        "evidence_decision": values.get("evidence_decision") or {},
        # 记忆区：刷新页面后仍然能看到这个孩子记住了什么
        "memories": (memories or {}).get("memories") or [],
        "memory_counts": (memories or {}).get("counts") or {},
        "memory_pending": (memories or {}).get("pending_confirmation", 0),
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


def _feedback_payload(
    *,
    trial: Any,
    turn_id: str,
    row: Any = None,
    bundle: dict[str, Any] | None = None,
    pending: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """反馈入口的统一响应。

    图刚跑完（``result``）、命中断言（``pending``）、命中缓存（``row``）
    三种来源走同一个形状，前端不用分支。
    """
    interrupt_payload = _interrupt_payload(result) if result is not None else None
    effective_pending = pending or interrupt_payload
    result_json = dict(getattr(row, "result_json", None) or {})

    if result is not None:
        status = _status_of(interrupt_payload, result)
    elif effective_pending is not None:
        status = _status_of(effective_pending)
    elif getattr(row, "completed_at", None) is not None:
        status = result_json.get("status") or "completed"
    elif row is not None:
        # 有反馈行但没跑完（上次请求中途挂了）—— 前端可以提示重试。
        status = "processing"
    else:
        status = "completed"

    analysis = (
        (result or {}).get("feedback_analysis")
        or dict(getattr(row, "analysis_json", None) or {})
        or {}
    )
    next_step = (
        (result or {}).get("next_step")
        or dict(getattr(row, "next_step_json", None) or {})
        or {}
    )
    answer_bundle = (result or {}).get("evidence_bundle") or bundle or {}
    decision = (
        (result or {}).get("evidence_decision")
        or result_json.get("evidence_decision")
        or {}
    )

    return {
        "trial_id": str(getattr(trial, "id", "")),
        "turn_id": turn_id,
        "status": status,
        "feedback_created": bool((result or {}).get("feedback_created", False)),
        "trial_status": (result or {}).get("trial_status")
        or getattr(trial, "status", ""),
        "plan_version": (result or {}).get("plan_version")
        or getattr(trial, "plan_version", 1),
        "analysis": analysis,
        "next_step": next_step,
        "questions": list((effective_pending or {}).get("questions") or []),
        "message": (result or {}).get("final_answer")
        or result_json.get("message")
        or "",
        "feedback_adjustment": (result or {}).get("feedback_adjustment") or None,
        "evidence": _evidence_panel(answer_bundle),
        "evidence_decision": decision,
        "evidence_gaps": answer_bundle.get("evidence_gaps") or [],
        "writes_skipped": (result or {}).get("writes_skipped") or [],
    }


async def _load_feedback_row(trial_id: str, turn_id: str) -> Any:
    async with session_scope() as session:
        return await Repository(session).get_feedback(
            trial_id=trial_id, turn_id=turn_id
        )


async def _load_feedback_bundle(trial: Any, turn_id: str) -> dict[str, Any] | None:
    async with session_scope() as session:
        row = await Repository(session).get_evidence_bundle(
            str(trial.conversation_id), turn_id
        )
    return dict(row.bundle_json) if row is not None else None


@router.post("/trials/{trial_id}/feedback")
async def submit_feedback(trial_id: str, req: FeedbackSubmission) -> dict[str, Any]:
    """反馈入口。

    四件事按顺序判断：
    1. 已经停在反馈追问上 → 直接把问题还给前端，不再跑图；
    2. 这次反馈已经处理完（``completed_at``）→ 返回缓存，不再跑 LLM；
    3. 否则跑图；
    4. 同一个 ``(trial_id, turn_id)`` 重试由 UNIQUE 约束和缓存共同兜住。
    """
    trial, family_id, child_id = await _ensure_identity_for_trial(trial_id)
    if req.trial_id != trial_id:
        raise HTTPException(400, "请求体里的 trial_id 和路径不一致")

    turn_id = req.turn_id
    config = config_for(feedback_thread_id(trial_id, turn_id))
    row = await _load_feedback_row(trial_id, turn_id)

    pending = await _pending_interrupt(config)
    if pending is not None:
        if pending.get("kind") != "feedback_followup":
            raise HTTPException(409, "这条反馈没有待回答的追问")
        return _feedback_payload(
            trial=trial, turn_id=turn_id, row=row, pending=pending
        )

    if row is not None and getattr(row, "completed_at", None) is not None:
        bundle = await _load_feedback_bundle(trial, turn_id)
        return _feedback_payload(
            trial=trial, turn_id=turn_id, row=row, bundle=bundle
        )

    graph = get_graph()
    result = await graph.ainvoke(
        {
            "entrypoint": "feedback",
            "conversation_id": str(trial.conversation_id),
            "thread_id": feedback_thread_id(trial_id, turn_id),
            "family_id": family_id,
            "child_id": child_id,
            "turn_id": turn_id,
            "feedback": req.model_dump(mode="json"),
        },
        config=config,
    )
    row = await _load_feedback_row(trial_id, turn_id)
    return _feedback_payload(trial=trial, turn_id=turn_id, row=row, result=result)


@router.post("/trials/{trial_id}/feedback/resume")
async def resume_feedback(
    trial_id: str, req: FeedbackFollowupRequest
) -> dict[str, Any]:
    """回答反馈追问，从 interrupt 处恢复。"""
    trial, _, _ = await _ensure_identity_for_trial(trial_id)
    config = config_for(feedback_thread_id(trial_id, req.turn_id))
    pending = await _pending_interrupt(config)
    if pending is None or pending.get("kind") != "feedback_followup":
        raise HTTPException(409, "当前没有待回答的反馈追问")

    user_text = _compose_answer_text(req.answers, req.message)
    structured_answers = _structured_answers(req.answers)
    graph = get_graph()
    result = await graph.ainvoke(
        Command(
            resume={"text": user_text, "answers": structured_answers},
            update={"turn_id": req.turn_id},
        ),
        config=config,
    )
    row = await _load_feedback_row(trial_id, req.turn_id)
    return _feedback_payload(
        trial=trial, turn_id=req.turn_id, row=row, result=result
    )


@router.get("/trials/{trial_id}/feedback/{turn_id}")
async def get_feedback_state(trial_id: str, turn_id: str) -> dict[str, Any]:
    """刷新页面时恢复反馈状态：追问问题，或已落库的结果。"""
    trial, _, _ = await _ensure_identity_for_trial(trial_id)
    config = config_for(feedback_thread_id(trial_id, turn_id))
    pending = await _pending_interrupt(config)
    row = await _load_feedback_row(trial_id, turn_id)
    if pending is not None:
        return _feedback_payload(
            trial=trial, turn_id=turn_id, row=row, pending=pending
        )
    if row is None:
        raise HTTPException(404, "还没有这条反馈")
    bundle = await _load_feedback_bundle(trial, turn_id)
    return _feedback_payload(trial=trial, turn_id=turn_id, row=row, bundle=bundle)


async def _ensure_identity_for_trial(trial_id: str) -> tuple[Any, str, str]:
    """取 trial + 所属 family / child。反馈入口的身份都从 trial 推出来。"""
    from rdi_agent.db.models import Child

    async with session_scope() as session:
        trial = await Repository(session).get_trial(trial_id)
        if trial is None:
            raise HTTPException(404, "尝试不存在")
        child = await session.get(Child, trial.child_id)
    if child is None:
        raise HTTPException(404, "孩子档案不存在")
    return trial, str(child.family_id), str(child.id)