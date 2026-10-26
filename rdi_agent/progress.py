"""进度事件总线（方案 A：只流"进度"，不流"半截字"）。

**为什么不做流式输出**：抽取类调用一律走 ``json_schema`` 严格约束解码
（仓库铁律，见 ``rdi_agent/llm.py``）。结构化输出下模型必须保证 JSON
语法合法才能吐 token，于是吐出来的是 ``{"plan_id": "pl`` 这种半截 ——
浏览器端没法用，家长也看不懂。

**但家长仍然需要知道"它卡在哪儿"**。实测同一条 prompt 的模型侧延迟
在 2.8s–55s 之间波动（provider 排队），家长面对一个 40 秒的转圈
会以为系统坏了。所以这里流的是**步骤进度**，不是答案本身：

    正在理解你说的情况… → 正在判断信息够不够… → 正在检索指南… → …

**实现在哪**：``llm.chat_json`` 有个 ``on_step`` 钩子，每步开始时调用。
它以前是**空钩子** —— 声明了但没有任何地方传。本模块把它接上。

**为什么用 ContextVar 而不是全局变量**：一次请求可能同时有多个会话在跑，
全局队列会把 A 会话的事件串到 B 会话去。ContextVar 跟着 task 走，
每个请求一份，互不干扰。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ProgressEvent:
    """一个进度事件。

    ``stage`` 是机器名（``intake_parse``），``label`` 是给人看的说法。
    两个都要有：前端拿 ``label`` 渲染，调试时靠 ``stage`` 对后端日志。
    """

    stage: str
    label: str
    kind: str = "step"  # step / done / error
    detail: str = ""
    attempt: int = 1
    data: dict[str, Any] = field(default_factory=dict)


# 阶段名 -> 给家长看的中文说法。
#
# **这些字是给家长看的**，不是给工程师看的。
# "intake_parse" 对家长毫无意义，"正在理解你说的情况"才有。
#
# 措辞原则：不承诺结果、只描述当前在做什么。
# 不写"正在生成建议"（可能最后生成失败），写"正在整理依据"。
STAGE_LABELS: dict[str, str] = {
    "memory_extract": "正在整理这次说的情况",
    "intake_parse": "正在理解你说的情况",
    "sufficiency": "正在判断信息够不够",
    "followup": "正在准备要问你的问题",
    "await_answer": "问题已经好了，等你回答",
    "rewrite_query": "正在整理检索词",
    "retrieve": "正在检索指南",
    "rerank": "正在筛选最相关的段落",
    "evidence_gate": "正在检查依据够不够",
    "build_recommendation": "正在整理建议",
    "build_limited_answer": "正在整理回复",
    "safety_review": "正在做安全检查",
    "present_plans": "正在把方案交给你看",
    "render": "正在整理最终回复",
    "summary_zh": "正在准备段落摘要",
    "feedback_analyze": "正在分析你的反馈",
    "next_step": "正在想下一步",
}


def label_for(stage: str) -> str:
    """阶段名 -> 中文说法。没登记过的阶段退回原名（宁可显示英文也不假装懂）。"""
    return STAGE_LABELS.get(stage, stage)


# 每请求一份的队列。ContextVar 跟着 task 走，所以并发请求互不串。
_CURRENT: ContextVar[asyncio.Queue[ProgressEvent] | None] = ContextVar(
    "rdi_progress_queue", default=None
)


def current_queue() -> asyncio.Queue[ProgressEvent] | None:
    """当前请求的进度队列。没有订阅者时返回 None。"""
    return _CURRENT.get()


def emit(
    stage: str,
    *,
    kind: str = "step",
    detail: str = "",
    attempt: int = 1,
    **data: Any,
) -> None:
    """发一个进度事件。

    **没有订阅者时静默返回** —— 普通的（非流式）请求不该因为没人
    听进度而报错。
    """
    queue = _CURRENT.get()
    if queue is None:
        return
    event = ProgressEvent(
        stage=stage,
        label=label_for(stage),
        kind=kind,
        detail=detail,
        attempt=attempt,
        data=data,
    )
    # put_nowait：队列满了说明没人消费（客户端断开了），
    # 这时不能阻塞 LLM 调用 —— 宁可丢一个进度事件。
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


@contextmanager
def progress_channel() -> Iterator[asyncio.Queue[ProgressEvent]]:
    """在当前 task 里开一个进度通道。

    用法：

        with progress_channel() as q:
            ...  # 里面的代码调 llm.chat_json，事件自动进 q
    """
    queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    token = _CURRENT.set(queue)
    try:
        yield queue
    finally:
        _CURRENT.reset(token)


def spawn_with_progress(coro: Any) -> tuple[asyncio.Task, asyncio.Queue[ProgressEvent]]:
    """把一个协程放到新 task 里跑，并给它开一个进度通道。

    **为什么要开新 task**：ContextVar 是**按 task 复制的** ——
    ``asyncio.create_task`` 会把当前 context 拷一份过去。所以在调用方
    这里 ``set()`` 是不管用的，必须在 task **内部** set。

    返回 (task, queue)。调用方消费 queue 里的事件。

    用法：

        task, queue = spawn_with_progress(some_coro())
        while (ev := await queue.get()):
            ...
        result = await task
    """
    queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()

    async def runner() -> Any:
        token = _CURRENT.set(queue)
        try:
            return await coro
        finally:
            _CURRENT.reset(token)

    return asyncio.create_task(runner()), queue
