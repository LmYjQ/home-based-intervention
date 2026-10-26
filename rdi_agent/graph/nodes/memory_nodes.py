"""长期记忆的三个节点（计划 §10.3 的写入流程）。

    extract_memories → merge_memories → commit_memory

**放在 ``parse_parent_input`` 之前**，不是之后 —— 这是阶段 3 最关键的一个
顺序决定：

`parse_parent_input` 会把上一轮累积的 ``observed_facts`` 并进
``case_summary``。如果先解析再记忆，"孩子 4 岁"这句旧事实已经进了
本轮的事实清单，之后即使记忆层把它标成作废，本轮回答也已经用上了。
先记忆、后解析，``commit_memory`` 才能把"哪些旧事实已作废"交给
解析节点过滤掉（验收第 2 条：纠正后不再沿用旧值）。

**三个节点都不含 ``interrupt()``**，所以它们在中断恢复时的重跑语义是安全的：
选方案中断恢复只会重跑 ``present_plans`` 之后的节点，不会重跑这里；
追问中断恢复会重跑这里一次 —— 那是**新的一轮回答**，本来就该抽。

**失败一律降级**：模型超时、库连不上，这轮就当没记忆。
记忆是增强项，不能因为它把家长的问题也拖挂。
"""

from __future__ import annotations

from typing import Any

from rdi_agent import progress
from rdi_agent.config import get_settings
from rdi_agent.graph.state import CoachState
from rdi_agent.memory import service as memory_service
from rdi_agent.schemas.memory import MemoryUpdateResult


def _disabled(state: CoachState) -> bool:
    return (
        not get_settings().memory_enabled
        or not state.get("child_id")
        or state.get("debug") is True
    )


def _note(state: CoachState, message: str) -> list[str]:
    return [*(state.get("memory_notes") or []), message]


def _feedback_text(feedback: dict[str, Any]) -> str:
    """把一次反馈里"家长自己写的字"拼成一段，当抽取的原文。

    **只拼家长写的**：``implementation_notes`` / ``parent_observations`` /
    ``difficulties`` / 孩子反应的文字字段。
    不拼 ``feedback_analysis`` —— 那是模型的转述，
    拿它当"家长原话"去生成 ``source_quote``，等于让模型给自己作证。
    """
    parts: list[str] = []
    notes = str(feedback.get("implementation_notes") or "").strip()
    if notes:
        parts.append(notes)
    for key in ("parent_observations", "difficulties"):
        for value in feedback.get(key) or []:
            text = str(value).strip()
            if text:
                parts.append(text)
    child_response = feedback.get("child_response") or {}
    if isinstance(child_response, dict):
        for value in child_response.values():
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts)


async def extract_memories(state: CoachState) -> dict[str, Any]:
    """从这一轮家长的话里抽候选记忆。

    **不是每个候选都会写库** —— 这里只负责"提出候选"，
    能不能记由 ``merge_memories`` 的纯规则决定。
    """
    if _disabled(state):
        return {"memory_candidates": [], "memory_notes": _note(state, "记忆：已跳过")}

    entrypoint = state.get("entrypoint") or "chat"
    source_kind = "feedback" if entrypoint == "feedback" else "interaction"
    # 反馈入口没有 ``user_message``：原文是家长自己填的那几栏。
    parent_text = (
        _feedback_text(state.get("feedback") or {})
        if source_kind == "feedback"
        else (state.get("user_message") or "")
    )
    progress.emit("memory_extract")
    try:
        candidates, notes = await memory_service.extract_candidates(
            child_id=state.get("child_id") or "",
            conversation_id=state.get("conversation_id") or "",
            turn_id=state.get("turn_id") or "",
            source_kind=source_kind,
            parent_text=parent_text,
            followup_answers=state.get("followup_answers") or [],
            child_context=state.get("child_context") or {},
            feedback=state.get("feedback") or None,
            feedback_analysis=state.get("feedback_analysis") or None,
            source_feedback_id=state.get("feedback_db_id") or "",
        )
    except Exception as e:  # noqa: BLE001 - 记忆失败不该拖垮整轮
        return {
            "memory_candidates": [],
            "memory_notes": _note(state, f"记忆抽取失败（本轮回退为不记）：{type(e).__name__}: {e}"),
        }

    return {
        "memory_candidates": [c.model_dump(mode="json") for c in candidates],
        "memory_notes": [*(state.get("memory_notes") or []), *notes],
    }


async def merge_memories(state: CoachState) -> dict[str, Any]:
    """跑纯规则：哪条自动记、哪条等确认、哪条丢掉。"""
    if _disabled(state):
        return {"memory_update_result": MemoryUpdateResult().model_dump(mode="json")}

    if not state.get("memory_candidates"):
        return {
            "memory_update_result": MemoryUpdateResult(
                notes=list(state.get("memory_notes") or [])
            ).model_dump(mode="json")
        }

    try:
        result = await memory_service.merge_for_state(state)
    except Exception as e:  # noqa: BLE001
        result = MemoryUpdateResult(
            notes=_note(state, f"记忆合并失败（本轮回退为不记）：{type(e).__name__}: {e}")
        )
    return {"memory_update_result": result.model_dump(mode="json")}


async def commit_memory(state: CoachState) -> dict[str, Any]:
    """写库，并把刷新后的上下文/作废清单交回 state。"""
    if _disabled(state):
        return {}

    raw = state.get("memory_update_result") or {}
    if not raw.get("decisions") and not raw.get("notes"):
        return {}

    try:
        result = await memory_service.commit_for_state(state)
    except Exception as e:  # noqa: BLE001
        return {
            "memory_update_result": MemoryUpdateResult(
                notes=_note(state, f"记忆写入失败（本轮不记）：{type(e).__name__}: {e}")
            ).model_dump(mode="json")
        }

    patch = dict(result.context_patch or {})
    child_context = dict(state.get("child_context") or {})
    child_context.update(patch)

    forbidden = list(
        dict.fromkeys(
            [
                *(state.get("memory_forbidden_facts") or []),
                *(result.superseded_facts or []),
            ]
        )
    )

    return {
        "memory_update_result": result.model_dump(mode="json"),
        "child_context": child_context,
        "memory_forbidden_facts": forbidden,
        "memory_context": {
            "confirmed_facts": child_context.get("confirmed_facts") or [],
            "preferences": child_context.get("preferences") or [],
            **(child_context.get("memories") or {}),
        },
    }


def route_after_memory(state: CoachState) -> str:
    """两条入口在记忆之后回到各自的下一步。"""
    return "feedback" if state.get("entrypoint") == "feedback" else "chat"
