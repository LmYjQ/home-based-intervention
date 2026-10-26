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
    # 已经问过的问题**和家长的回答**，成对存着。
    # [{"question": "...", "answer": "..."}, ...]
    #
    # **为什么光有 asked_questions 不够**（2026-10-26 补）：
    # 只有问题没有答案，模型不知道"这件事已经问过了"还是"问过了但没答上来"，
    # 两种情况它都会再问一遍 —— 实测"孩子在幼儿园的表现和在家相比有没有不同"
    # 被问了两遍，第二次换个说法。
    #
    # 有了答案，模型才能做两件不同的事：
    #   1. 答案有内容 → 这件事**已经问完了**，基于新信息往下走，
    #      不再问同一件事（家长说"换过老师"，该问的是
    #      "换老师之后表现有没有变化"，不是把原问题重问一遍）。
    #   2. 答案是空的/答不上来 → 换个**角度**问，问得更具体，
    #      但不要换个说法重问原问题。
    asked_qa_pairs: list[dict[str, Any]]
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
    # 被安全审查逐卡丢弃的 plan_id。
    #
    # **为什么记**：家长看到"少了一张卡"要知道少在哪、为什么少 ——
    # 之前整组卡被连坐丢掉，前端一律显示"证据不够"，
    # 真实原因（某张卡命中了"忽视明显不适"）谁也看不到。
    safety_dropped_plans: list[str]
    # 节点之间的路由提示
    route_after_recommendation: str | None
    route_after_render: str | None

    # 反馈与记忆
    feedback: dict[str, Any]
    feedback_analysis: dict[str, Any]
    next_step: dict[str, Any]
    trial_id: str
    plan_title: str
    # 这次尝试执行的方案正文和版本 —— analyze/next_step/adjust 都要看
    plan_context: dict[str, Any]
    # 方案版本：1 = 首版，修订一次 +1。卡号前缀也按它生成。
    plan_version: int
    # 家长在反馈里补充的新情况。首版为空；修订版会透给生成 prompt。
    revision_note: str
    # 修订来源：{"trial_id", "turn_id", "base_plan_version", "fork_checkpoint_id"}
    revision_source: dict[str, Any]
    # 生成这一版之前的 checkpoint id，落进 plans.fork_checkpoint_id。
    # 下次修订从它分叉，事实/证据/门控结论都沿用。
    fork_checkpoint_id: str
    # 修订时复用/重跑了哪些环节（给接口响应和调试看）
    revision_reused: list[str]
    # 反馈提交后 trial 的执行状态（attempted / not_attempted / stopped）
    trial_status: str
    selected_plan_db_id: str
    plan_db_ids: dict[str, str]
    feedback_created: bool
    # 同一 child 过去的尝试 + 对应反馈，给历史对比用
    feedback_history: list[dict[str, Any]]
    # 重试时命中库里缓存的分析/下一步，避免再跑 LLM
    feedback_cached_analysis: dict[str, Any]
    feedback_cached_next_step: dict[str, Any]
    feedback_analysis_from_cache: bool
    # 反馈不足时的追问中断
    feedback_followup_questions: list[dict[str, Any]]
    feedback_followup_answers: list[dict[str, Any]]
    feedback_followup_rounds: int
    # 反馈追问的"问过什么 + 家长答了什么"，成对累积。
    # 理由同 asked_qa_pairs：模型只有看到答案，才分得清
    # "问过了"和"问过了没答上来"。
    feedback_asked_qa: list[dict[str, Any]]
    # 证据支撑的调整建议（不出卡）
    feedback_adjustment: dict[str, Any]
    memory_candidates: list[dict[str, Any]]
    memory_update_result: dict[str, Any]
    # 这一轮给前端"记忆区"看的内容（事实/观察/假设/尝试）
    memory_context: dict[str, Any]
    # 本轮被纠正/删除掉的旧事实原文。
    #
    # **只给代码用**：``parse_parent_input`` 拿它过滤掉累积事实里的旧值。
    # 永远不进 prompt —— 那等于把家长删掉的隐私又喂回模型。
    memory_forbidden_facts: list[str]
    # 记忆节点的说明（降级原因等），给 trace 和排查用
    memory_notes: list[str]
    # 反馈记录落库后的 id，记忆抽取要拿它溯源
    feedback_db_id: str

    # 运维
    error_code: str | None
    trace_metadata: dict[str, Any]
    # 调试分支跳过了哪些业务写入（计划 §12.4）
    writes_skipped: list[str]
