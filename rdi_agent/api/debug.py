"""调试接口（计划 §12.5）。

三个能力：

- 看当前 State（只给摘要，不给儿童原文）；
- 看 checkpoint 历史（每个节点走了哪一步）；
- 从历史 checkpoint 派生一条**独立**调试分支。

**分叉配方**（本机 langgraph 1.2.16 实测）::

    source = aget_state({thread_id: 源, checkpoint_id: 选中})
    parent = aget_state(source.parent_config)
    producer = parent.next[0]          # 产生这个 checkpoint 的节点

    aupdate_state({thread_id: 新线程}, source.values + patch, as_node=producer)
    ainvoke(None, {thread_id: 新线程})

- 目标线程独立，源线程的 head 和历史一行都不动；
- 不要用 ``saver.acopy_thread()``：当前 ``AsyncPostgresSaver`` 没实现，
  会抛 ``NotImplementedError``；
- 这是"用源 state 做种子 + as_node 定位下一节点"的**合成** checkpoint，
  不是逐字节复制整条历史链。对"改一个变量再看输出"够用。

**调试分支不写业务表**：分叉时强制 ``debug=True``，
写节点看到它就会跳过并记 ``writes_skipped``。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from rdi_agent.api.conversations import _ensure_identity, _status_of
from rdi_agent.graph.runtime import config_for, get_graph

router = APIRouter(prefix="/api/debug", tags=["debug"])


class ForkRequest(BaseModel):
    checkpoint_id: str
    # 不传就用 conversation_id 对应的主线程
    thread_id: str | None = None
    # 递归合并字典；列表/标量直接替换
    values_patch: dict[str, Any] = Field(default_factory=dict)
    # false = 只种下分支，不执行
    run: bool = True


def _pending_of(snapshot: Any) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", None) or ():
        for intr in getattr(task, "interrupts", None) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict):
                return value
            return {"raw": str(value)}
    return None


def _pending_summary(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    if not pending:
        return None
    plans = pending.get("plans") or []
    questions = pending.get("questions") or []
    return {
        "kind": pending.get("kind"),
        "question_count": len(questions),
        "plan_ids": [
            p.get("plan_id") for p in plans if isinstance(p, dict) and p.get("plan_id")
        ],
    }


def _plan_ids(values: dict[str, Any]) -> list[str]:
    return [
        str(p.get("plan_id"))
        for p in (values.get("candidate_plans") or [])
        if isinstance(p, dict) and p.get("plan_id")
    ]


def _summary(snapshot: Any, *, thread_id: str) -> dict[str, Any]:
    """把 StateSnapshot 压成一个不含儿童原文的摘要。"""
    values = dict(getattr(snapshot, "values", None) or {})
    metadata = dict(getattr(snapshot, "metadata", None) or {})
    pending = _pending_of(snapshot)
    bundle = values.get("evidence_bundle") or {}
    answer = str(values.get("final_answer") or "")

    return {
        "thread_id": thread_id,
        "checkpoint_id": (getattr(snapshot, "config", None) or {})
        .get("configurable", {})
        .get("checkpoint_id"),
        "status": _status_of(pending, values),
        "entrypoint": values.get("entrypoint"),
        "turn_id": values.get("turn_id"),
        # 调试分支要能一眼看出"这条不是真实对话"
        "debug": values.get("debug") is True,
        "next": list(getattr(snapshot, "next", ()) or ()),
        "step": metadata.get("step"),
        "source": metadata.get("source"),
        "created_at": getattr(snapshot, "created_at", None),
        "counts": {
            "questions": len(values.get("followup_questions") or []),
            "feedback_followup_questions": len(
                values.get("feedback_followup_questions") or []
            ),
            "plans": len(values.get("candidate_plans") or []),
            "evidence_items": len(bundle.get("evidence") or []),
        },
        "trial_id": values.get("trial_id"),
        "feedback_db_id": values.get("feedback_db_id"),
        "plan_ids": _plan_ids(values),
        "has_final_answer": bool(answer),
        # 只给预览：调试面板需要知道"这一步有没有产出"，不需要读全文
        "answer_preview": answer[:200],
        "has_pending_interrupt": pending is not None,
        "pending_interrupt": _pending_summary(pending),
        "error_code": values.get("error_code"),
        "writes_skipped": values.get("writes_skipped") or [],
    }


def _checkpoint_summary(snapshot: Any, *, thread_id: str) -> dict[str, Any]:
    item = _summary(snapshot, thread_id=thread_id)
    parent = getattr(snapshot, "parent_config", None) or {}
    return {
        "checkpoint_id": item["checkpoint_id"],
        "parent_checkpoint_id": parent.get("configurable", {}).get("checkpoint_id"),
        "step": item["step"],
        "source": item["source"],
        "created_at": item["created_at"],
        "next": item["next"],
        "entrypoint": item["entrypoint"],
        "turn_id": item["turn_id"],
        "plan_ids": item["plan_ids"],
        "has_pending_interrupt": item["has_pending_interrupt"],
        "resumable": bool(item["next"]),
        "answer_preview": item["answer_preview"],
        "writes_skipped": item["writes_skipped"],
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """字典递归合并，列表/标量直接替换。

    **为什么不是全部覆盖**：调试时通常只想改 `case_summary.observed_facts`
    这一项，把整份 state 都替掉会把证据包、记忆上下文一起清空，
    分叉出来的结果就没有对照意义了。
    """
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _config(conversation_id: str, thread_id: str | None) -> dict[str, Any]:
    return config_for(thread_id or conversation_id)


def _checkpoint_config(thread_id: str, checkpoint_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
        }
    }


@router.get("/conversations/{conversation_id}/state")
async def get_state(
    conversation_id: str,
    # Annotated 而不是默认值 Query(...)：仓库里的接口测试是直接调函数，
    # 后者会让直接调用时拿到 Query 对象而不是 None。
    thread_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    await _ensure_identity(conversation_id)
    graph = get_graph()
    snapshot = await graph.aget_state(_config(conversation_id, thread_id))
    if snapshot is None or not snapshot.values:
        raise HTTPException(404, "这条线程还没有 checkpoint")
    return _summary(snapshot, thread_id=thread_id or conversation_id)


@router.get("/conversations/{conversation_id}/checkpoints")
async def list_checkpoints(
    conversation_id: str,
    thread_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    await _ensure_identity(conversation_id)
    graph = get_graph()
    tid = thread_id or conversation_id
    checkpoints: list[dict[str, Any]] = []
    async for snapshot in graph.aget_state_history(
        config_for(tid), limit=limit
    ):
        checkpoints.append(_checkpoint_summary(snapshot, thread_id=tid))
    return {"thread_id": tid, "checkpoints": checkpoints}


@router.post("/conversations/{conversation_id}/fork")
async def fork(
    conversation_id: str, req: ForkRequest
) -> dict[str, Any]:
    await _ensure_identity(conversation_id)
    graph = get_graph()
    source_thread = req.thread_id or conversation_id

    source = await graph.aget_state(
        _checkpoint_config(source_thread, req.checkpoint_id)
    )
    if source is None or not source.values:
        raise HTTPException(404, "checkpoint 不存在")
    if not source.next:
        raise HTTPException(409, "这个 checkpoint 已经结束，没有可继续的节点")
    if source.parent_config is None:
        raise HTTPException(409, "不能从根 checkpoint 分叉")

    parent = await graph.aget_state(source.parent_config)
    producer = parent.next[0] if parent is not None and len(parent.next) == 1 else None
    if not producer or producer == "__start__":
        raise HTTPException(409, "判断不出产生这个 checkpoint 的节点")

    fork_thread = f"debug-fork-{uuid.uuid4().hex[:12]}"
    values = _deep_merge(dict(source.values), req.values_patch)
    values.update(
        {
            "debug": True,
            "thread_id": fork_thread,
            "conversation_id": conversation_id,
        }
    )
    fork_cfg = config_for(fork_thread)
    await graph.aupdate_state(fork_cfg, values, as_node=producer)

    result: Any = None
    if req.run:
        result = await graph.ainvoke(None, fork_cfg)

    snapshot = await graph.aget_state(fork_cfg)
    return {
        "source_thread_id": source_thread,
        "source_checkpoint_id": req.checkpoint_id,
        "thread_id": fork_thread,
        "as_node": producer,
        "ran": req.run,
        "status": _status_of(None, snapshot.values or {}),
        "interrupt": _pending_summary(_pending_of(snapshot)),
        "message": (result or {}).get("final_answer", "") if req.run else "",
        "state": _summary(snapshot, thread_id=fork_thread),
    }
