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
from rdi_agent.llm import LLMError
from rdi_agent.graph.citations import validate_set_citations
from rdi_agent.graph.state import CoachState
from rdi_agent.prompts import chat as P
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.evidence import EvidenceBundle
from rdi_agent.schemas.conversation import (
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.recommendation import RecommendationCard

# 追问到第几轮就强制收手。3 是计划 §8.4 定的上限。
MAX_FOLLOWUP_ROUNDS = 3


def _bundle_of(state: CoachState) -> EvidenceBundle | None:
    """从 state 里还原证据包。"""
    raw = state.get("evidence_bundle")
    if not raw:
        return None
    try:
        return EvidenceBundle.model_validate(raw)
    except Exception:  # noqa: BLE001 - state 里的东西坏了不该让整条链崩
        return None


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
    # 信息够了就进检索链：rewrite_query → ... → evidence_gate
    return "rewrite_query"


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
        return {"followup_questions": [], "route_override": "rewrite_query"}
    return {
        "followup_questions": result.questions,
        "trace_metadata": {"followup": trace.brief()},
    }


def route_after_recommendation(state: CoachState) -> str:
    """建议卡一张都没留下（引用校验全挂）就退回有限回答。"""
    if state.get("candidate_plans"):
        return "plans"
    return "limited_answer"


def route_after_followup(state: CoachState) -> str:
    if state.get("route_override") == "rewrite_query":
        return "rewrite_query"
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
    """生成 2–3 张建议卡，并用证据包校验引用。

    **引用校验是硬闸**（计划 §9.3）：生成的每一条 ``source_citations``
    都必须在当前证据包里能找到对应的 ``evidence_id``，页码也要对得上。
    找不到就是编造引用 —— 这种卡给家长看等于骗人。

    失败处理（计划 §9.3 原文）：带错误信息**重试一次** → 仍失败则
    **丢弃该卡**、降级为 ``limited_answer``，并在 trace 里记
    ``citation_validation_failed``。
    """
    bundle = _bundle_of(state)
    evidence = [e.model_dump(mode="json") for e in (bundle.evidence if bundle else [])]

    try:
        cards, first_trace, problems, note = await _generate_cards(
            state,
            evidence=evidence,
            validation_errors=None,
        )
    except LLMError as e:
        # 生成本身炸了（schema 连着几次不合规、模型连不上）——
        # 计划 §9.3 的意思是"生成不出来就给有限回答"，不是"整个请求 500"。
        # 家长拿到一句有限回答，比拿到一个 500 强。
        return {
            "candidate_plans": [],
            "final_answer": "",
            "route_after_recommendation": "limited_answer",
            "trace_metadata": {"build_recommendation": f"生成失败，降级为有限回答：{e}"},
        }
    trace_notes = [first_trace]
    if problems:
        # 带错误信息重试一次
        try:
            retry_cards, retry_notes, retry_problems, retry_note = await _generate_cards(
                state,
                evidence=evidence,
                validation_errors=problems,
            )
        except LLMError as e:
            retry_cards, retry_notes, retry_problems, retry_note = [], str(e), {}, ""
        note = retry_note or note
        trace_notes.append(retry_notes)
        if retry_cards:
            cards = retry_cards
            problems = retry_problems
        else:
            # 第二次全军覆没 —— 退回第一轮里通过校验的那些
            cards = [c for c in cards if c.plan_id not in problems]

    if not cards:
        # 一张都留不下 —— 降级为有限回答
        return {
            "candidate_plans": [],
            "final_answer": "",
            "route_after_recommendation": "limited_answer",
            "trace_metadata": {
                "build_recommendation": "；".join(trace_notes)
                + "；citation_validation_failed：全部建议卡的引用都没通过校验"
            },
        }

    # 丢掉的卡要在 trace 里留痕
    if problems:
        trace_notes.append(
            "citation_validation_failed："
            + "；".join(
                f"{plan_id} -> {errors[0]}" for plan_id, errors in problems.items()
            )
        )

    return {
        "candidate_plans": [c.model_dump(mode="json") for c in cards],
        "comparison_note": note,
        "final_answer": "",
        "route_after_recommendation": "plans",
        "trace_metadata": {"build_recommendation": "；".join(trace_notes)},
    }


async def _generate_cards(
    state: CoachState,
    *,
    evidence: list[dict[str, Any]],
    validation_errors: dict[str, list[str]] | None,
) -> tuple[list[RecommendationCard], str, dict[str, list[str]], str]:
    """生成一轮建议卡并做引用校验。返回 (通过校验的卡, 过程说明, 有问题的卡)。"""
    system, user = P.build_recommendation(
        case_summary=state.get("case_summary") or {},
        child_context=state.get("child_context"),
        n_plans=3,
        evidence=evidence,
        validation_errors=validation_errors,
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
        # 这一步的 prompt 里塞了整包证据，输出也要写满 3 张卡，
        # 是全流程里最慢的一次调用。默认的 20 秒超时（settings.llm_timeout_s）
        # 实测会连续三次超时 —— 宁可多等，不要把家长的问题判成"模型连不上"。
        timeout_s=120.0,
    )

    bundle = _bundle_of(state)
    problems = validate_set_citations(result.plans, bundle) if bundle else {}
    good = [c for c in result.plans if c.plan_id not in problems]

    # 恰好一张 recommended 被丢掉了的话，补一张上去 ——
    # 少了这一条，渲染出来的回答就不知道推荐哪张。
    if good and not any(c.recommended for c in good):
        good[0] = good[0].model_copy(update={"recommended": True})

    return good, trace.brief(), problems, result.comparison_note


async def present_plans(state: CoachState) -> dict[str, Any]:
    """中断点二：把候选卡交给家长选。纯函数，同上。"""
    selection = interrupt({"kind": "plan_selection", "plans": state.get("candidate_plans", [])})
    plan_id = selection.get("plan_id") if isinstance(selection, dict) else str(selection)
    return {"selected_plan_id": plan_id}