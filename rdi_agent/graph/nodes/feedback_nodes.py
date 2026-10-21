"""反馈入口的节点（计划 §8.2 的下分支）。

反馈入口没有中断：家长点提交就是一个完整请求，跑完就结束。
刻意把"做了什么"和"看到了什么"分开记 —— ``attempts="今天没哭"``
是一条观察，不等于上一轮方案有效。一次观察说明不了因果。
"""

from __future__ import annotations

from typing import Any

from rdi_agent import llm
from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.state import CoachState
from rdi_agent.prompts import chat as P
from rdi_agent.schemas.conversation import NextStep
from rdi_agent.schemas.feedback import FeedbackAnalysis, FeedbackSubmission


async def capture_feedback(state: CoachState) -> dict[str, Any]:
    """落库 + 取出上一轮方案的标题。

    幂等：UNIQUE(trial_id, turn_id) 挡住重复提交。
    """
    submission = FeedbackSubmission.model_validate(state["feedback"])
    async with session_scope() as session:
        repo = Repository(session)
        _, created = await repo.save_feedback(
            trial_id=submission.trial_id, submission=submission
        )
        trial = await repo.get_trial(submission.trial_id)
        title = ""
        if trial is not None:
            plan = await repo.get_plan(str(trial.plan_id))
            title = (plan.plan_json or {}).get("title", "") if plan else ""
    return {
        "feedback": state["feedback"],
        "plan_title": title,
        "feedback_created": created,
    }


async def analyze_feedback(state: CoachState) -> dict[str, Any]:
    """分析执行情况。只描述观察到的关系，不下因果结论。"""
    system, user = P.analyze_feedback(
        plan_title=state.get("plan_title") or "",
        feedback=state["feedback"],
    )
    analysis, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FeedbackAnalysis,
        name="feedback_analysis",
        stage="feedback_analyze",
        temperature=0.3,
    )
    return {
        "feedback_analysis": analysis.model_dump(mode="json"),
        "trace_metadata": {"feedback_analyze": trace.brief()},
    }


async def next_step(state: CoachState) -> dict[str, Any]:
    """给下一步。信息不足时明说缺什么，不急着换方案。"""
    system, user = P.next_step(analysis=state.get("feedback_analysis") or {})
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=NextStep,
        name="next_step",
        stage="next_step",
        temperature=0.5,
    )
    lines = [result.acknowledgment, *result.next_steps]
    return {
        "next_step": result.model_dump(mode="json"),
        "final_answer": "\n".join(x for x in lines if x),
        "trace_metadata": {"next_step": trace.brief()},
    }