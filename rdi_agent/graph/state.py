"""Graph State。

State 只放当前工作流需要的最小信息（计划 §4.2）：
完整对话历史在 ``interactions`` 表，原文片段只放 ``evidence_id`` 或摘要，
避免 State 膨胀和日志泄露儿童信息。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class CoachState(TypedDict, total=False):
    # 身份与入口
    family_id: str
    child_id: str
    conversation_id: str
    thread_id: str
    turn_id: str
    entrypoint: Literal["chat", "feedback"]
    debug: bool

    # 当前输入
    user_message: str

    # 上下文与理解
    child_context: dict[str, Any]
    case_summary: dict[str, Any]
    sufficiency_decision: dict[str, Any]
    followup_questions: list[str]
    # 已经追问几轮。到 MAX_FOLLOWUP_ROUNDS 就强制收手，否则会无限追问下去
    followup_rounds: int
    # generate_followup 发现没东西可问时，直接跳去出方案，省掉一次空中断
    route_override: str

    # 检索与证据
    retrieval_queries: dict[str, Any]
    retrieval_candidates: list[dict[str, Any]]  # 只放 chunk_id + score
    evidence_bundle: dict[str, Any]
    evidence_decision: dict[str, Any]

    # 生成与审核
    candidate_plans: list[dict[str, Any]]
    selected_plan_id: str | None
    safety_review: dict[str, Any]
    final_answer: str

    # 反馈与记忆
    feedback: dict[str, Any]
    feedback_analysis: dict[str, Any]
    next_step: dict[str, Any]
    trial_id: str
    plan_title: str
    selected_plan_db_id: str
    plan_db_ids: dict[str, str]
    feedback_created: bool
    memory_candidates: list[dict[str, Any]]
    memory_update_result: dict[str, Any]

    # 运维
    error_code: str | None
    trace_metadata: dict[str, Any]
