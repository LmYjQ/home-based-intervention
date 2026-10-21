"""聊天入口的节点（计划 §8.2 的上分支）。

一条硬规则贯穿本文件：**带 ``interrupt()`` 的节点必须是纯函数**。
LangGraph 恢复时整个节点会重跑，里面任何数据库写入都会翻倍。
所以本文件里没有 session，只有 prompt → LLM → state 字典；
写库全在 :mod:`rdi_agent.graph.nodes.write_nodes`。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from rdi_agent import llm
from rdi_agent.graph.state import CoachState
from rdi_agent.prompts import chat as P
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)

# 追问到第几轮就强制收手。3 是计划 §8.4 定的上限。
MAX_FOLLOWUP_ROUNDS = 3


# --------------------------------------------------------------- 理解
async def load_context(state: CoachState) -> dict[str, Any]:
    """阶段 1 的上下文很薄：只取最小档案。

    阶段 3 之后再接 observations / hypotheses 检索。返回空字典不是错误 ——
    冷启动会话本来就没有历史档案。
    """
    return {"child_context": state.get("child_context") or {}}


async def parse_parent_input(state: CoachState) -> dict[str, Any]:
    """把这一轮家长的话抽成 CaseSummary，和之前累积的事实合并。"""
    system, user = P.parse_parent_input(
        user_message=state.get("user_message", ""),
        previous_summary=state.get("case_summary"),
        child_context=state.get("child_context"),
    )
    summary, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=CaseSummary,
        name="case_summary",
        stage="intake_parse",
        temperature=0.0,
    )
    return {
        "case_summary": summary.model_dump(mode="json"),
        "trace_metadata": {"intake_parse": trace.brief()},
    }


async def check_sufficiency(state: CoachState) -> dict[str, Any]:
    """信息够不够。追问轮数到上限就强制不再问。"""
    rounds = int(state.get("followup_rounds") or 0)
    system, user = P.check_sufficiency(
        case_summary=state.get("case_summary") or {},
        followup_rounds=rounds,
    )
    decision, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=SufficiencyDecision,
        name="sufficiency",
        stage="sufficiency",
        temperature=0.0,
    )
    # 模型说够，但已经追问 3 轮还拿不准 —— 尊重模型的判断，只是不让它无休止地问下去。
    if rounds >= MAX_FOLLOWUP_ROUNDS and decision.requires_followup:
        decision = SufficiencyDecision(
            requires_followup=False,
            reasons=[
                f"已经追问 {rounds} 轮，信息仍不完整；"
                "继续追问的收益低于给一份标注了不确定性的建议。"
            ],
            missing_information=decision.missing_information,
        )
    return {
        "sufficiency_decision": decision.model_dump(mode="json"),
        "trace_metadata": {"sufficiency": trace.brief()},
    }


def route_after_sufficiency(state: CoachState) -> str:
    decision = state.get("sufficiency_decision") or {}
    if decision.get("requires_followup"):
        return "generate_followup"
    return "build_recommendation"


async def generate_followup(state: CoachState) -> dict[str, Any]:
    """把候选问题收敛成最多 3 个给家长看的问法。"""
    decision = state.get("sufficiency_decision") or {}
    rounds = int(state.get("followup_rounds") or 0)
    system, user = P.generate_followup(
        case_summary=state.get("case_summary") or {},
        priority_questions=decision.get("priority_questions") or [],
        followup_rounds=rounds,
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FollowupQuestions,
        name="followup_questions",
        stage="followup",
        temperature=0.3,
    )
    if not result.questions:
        # 候选问题被全部丢掉了，说明已经没什么可问的，直接进方案。
        return {"followup_questions": [], "route_override": "build_recommendation"}
    return {
        "followup_questions": result.questions,
        "trace_metadata": {"followup": trace.brief()},
    }


def route_after_followup(state: CoachState) -> str:
    if state.get("route_override") == "build_recommendation":
        return "build_recommendation"
    return "await_user_answer"


async def await_user_answer(state: CoachState) -> dict[str, Any]:
    """中断点一：等家长回答追问。

    纯函数：只接 resume 值、只回 state。不碰数据库、不调 LLM ——
    这个节点在恢复时会重跑，任何副作用都会重复发生。
    """
    answer = interrupt({"kind": "followup", "questions": state.get("followup_questions", [])})
    if isinstance(answer, list):  # 家长直接发数组回来
        text = "\n".join(str(x) for x in answer)
    else:
        text = str(answer)
    return {
        "user_message": text,
        "followup_questions": [],
        "followup_rounds": int(state.get("followup_rounds") or 0) + 1,
    }


# --------------------------------------------------------------- 生成
async def build_recommendation(state: CoachState) -> dict[str, Any]:
    """生成 2–3 张建议卡。"""
    system, user = P.build_recommendation(
        case_summary=state.get("case_summary") or {},
        child_context=state.get("child_context"),
        n_plans=3,
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=RecommendationSet,
        name="recommendation_set",
        stage="build_recommendation",
        temperature=0.7,
        # 3 张卡 + 中文说明，一次 2000 token 不够。截断会被 llm.py 检出并重试，
        # 但这里直接给够，省一次往返。
        max_tokens=6000,
    )
    return {
        "candidate_plans": [c.model_dump(mode="json") for c in result.plans],
        "final_answer": result.comparison_note,
        "trace_metadata": {"build_recommendation": trace.brief()},
    }


async def present_plans(state: CoachState) -> dict[str, Any]:
    """中断点二：把候选卡交给家长选。纯函数，同上。"""
    selection = interrupt({"kind": "plan_selection", "plans": state.get("candidate_plans", [])})
    plan_id = selection.get("plan_id") if isinstance(selection, dict) else str(selection)
    return {"selected_plan_id": plan_id}