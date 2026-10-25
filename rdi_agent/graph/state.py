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
    # 一轮追问。每一项是 {"question": str, "options": [str], "why_asking": str}
    followup_questions: list[dict[str, Any]]
    # 家长对这一轮追问的回答，**结构化**存着：
    # [{"question": str, "answer": str}, ...]
    #
    # **为什么不让模型自己抽**：实测模型会把整段问答丢掉 ——
    # 家长明确答了"没有不愉快的经历"，抽出来的 observed_facts 里还是没有。
    # 答案一丢，模型就以为这件事没问过，换个说法再问一遍。
    # 事实的完整性由代码兜底，模型抽取只当补充来源。
    followup_answers: list[dict[str, Any]]
    # 已经追问过哪些问题（原文）。
    #
    # **为什么单独存**：模型只看得到 case_summary 和 priority_questions，
    # 看不到"上一轮问过什么"。它不知道某件事已经问过，就会换个说法再问一遍
    # （实测"在园是否有不愉快的经历"连问两次，就是这么来的）。
    #
    # 把问过的问题原文交给 prompt，模型才能避开它们。
    asked_questions: list[str]
    # 已经追问几轮。到 MAX_FOLLOWUP_ROUNDS 就强制收手，否则会无限追问下去
    followup_rounds: int
    # generate_followup 发现没东西可问时，直接跳去出方案，省掉一次空中断
    route_override: str

    # 检索与证据
    retrieval_queries: dict[str, Any]
    retrieval_candidates: list[dict[str, Any]]  # 只放 chunk_id + score
    evidence_bundle: dict[str, Any]
    # 证据包落库后的 id（建议卡靠它追溯依据）
    evidence_bundle_db_id: str | None
    evidence_decision: dict[str, Any]
    # 证据门控额外追问了几轮。用来防死循环：阶段 1 的追问环回到
    # parse_parent_input，门控的追问也回那里，两个环会互相打转。
    evidence_followup_rounds: int

    # 生成与审核
    candidate_plans: list[dict[str, Any]]
    selected_plan_id: str | None
    safety_review: dict[str, Any]
    final_answer: str
    # 有限回答的分段内容（answer / low_risk_actions / observe / ...）
    answer_parts: dict[str, Any]
    # 这一路是"证据不够"（limited）还是"家长报告了风险信号"（risk）
    answer_mode: str
    # 多张卡之间的比较说明（模型写的），render 时用
    comparison_note: str
    # 安全审查已经降级过一次没有（防 safety_review 的死循环）
    safety_fallback_used: bool
    # 节点之间的路由提示
    route_after_recommendation: str | None
    route_after_render: str | None

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
