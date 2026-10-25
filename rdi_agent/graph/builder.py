"""图装配（计划 §8.2 + §9.3）。

图有两条入口，用 ``route_entry`` 分流：

    START → chat:    load_context → extract_memories → merge_memories
                                  → commit_memory → parse_parent_input
                                  → check_sufficiency
                                  ├─ 需要补充 → generate_followup
                                  │             → await_user_answer (interrupt)
                                  │             → parse_parent_input  （回到判断）
                                  └─ 信息足够 → rewrite_query
                                                → retrieve_knowledge
                                                → rerank_and_expand
                                                → evidence_gate
                                                   ├─ recommend  → build_recommendation
                                                   ├─ limited    → build_limited_answer
                                                   ├─ risk       → build_limited_answer
                                                   └─ followup   → generate_evidence_followup
                                                → safety_review
                                                   ├─ pass    → render_answer
                                                   └─ 不通过  → build_limited_answer
                                                → render_answer
                                                   ├─ 有卡 → persist_candidate_plans
                                                   │        → present_plans (interrupt)
                                                   │        → commit_plan_selection → END
                                                   └─ 无卡 → END

    START → feedback: capture_feedback → analyze_feedback → next_step → END

追问那圈是**故意**回到 ``parse_parent_input`` 而不是接一个新节点：
家长的回答要重新并进 CaseSummary，事实是累积的，不能问一轮丢一轮。
收敛靠 ``followup_rounds``（阶段 1，3 轮）和 ``evidence_followup_rounds``
（证据门控，1 轮）两个上限 —— 两个环都回 ``parse_parent_input``，
没有上限就会互相打转。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from rdi_agent.graph.nodes import chat_nodes as chat
from rdi_agent.graph.nodes import feedback_nodes as fb
from rdi_agent.graph.nodes import memory_nodes as mem
from rdi_agent.graph.nodes import retrieval_nodes as ret
from rdi_agent.graph.nodes import write_nodes as write
from rdi_agent.graph.state import CoachState


def route_entry(state: CoachState) -> str:
    return "feedback" if state.get("entrypoint") == "feedback" else "chat"


def build_graph(checkpointer: Any | None = None) -> Any:
    """返回编译好的 StateGraph。"""
    g = StateGraph(CoachState)

    # ---- 长期记忆（阶段 3）----
    #
    # 顺序是**记忆在前、解析在后**：解析会把上一轮累积的事实并进
    # case_summary，先记忆才能让本轮知道"哪些旧事实刚被作废"。
    # 详见 memory_nodes.py 的模块说明。
    g.add_node("extract_memories", mem.extract_memories)
    g.add_node("merge_memories", mem.merge_memories)
    g.add_node("commit_memory", mem.commit_memory)

    # ---- 聊天入口 ----
    g.add_node("load_context", chat.load_context)
    g.add_node("parse_parent_input", chat.parse_parent_input)
    g.add_node("check_sufficiency", chat.check_sufficiency)
    g.add_node("generate_followup", chat.generate_followup)
    g.add_node("await_user_answer", chat.await_user_answer)
    g.add_node("build_recommendation", chat.build_recommendation)
    g.add_node("persist_evidence_bundle", write.persist_evidence_bundle)
    g.add_node("persist_candidate_plans", write.persist_candidate_plans)
    g.add_node("present_plans", chat.present_plans)
    g.add_node("commit_plan_selection", write.commit_plan_selection)

    # ---- 检索与证据（阶段 2C）----
    g.add_node("rewrite_query", ret.rewrite_query)
    g.add_node("retrieve_knowledge", ret.retrieve_knowledge)
    g.add_node("rerank_and_expand", ret.rerank_and_expand)
    g.add_node("evidence_gate", ret.evidence_gate)
    g.add_node("generate_evidence_followup", ret.generate_evidence_followup)
    g.add_node("build_limited_answer", ret.build_limited_answer)
    g.add_node("safety_review", ret.safety_review)
    g.add_node("render_answer", ret.render_answer)

    # ---- 反馈入口 ----
    g.add_node("capture_feedback", fb.capture_feedback)
    g.add_node("analyze_feedback", fb.analyze_feedback)
    g.add_node("next_step", fb.next_step)

    # ---- 分流 ----
    g.add_conditional_edges(
        START,
        route_entry,
        {"chat": "load_context", "feedback": "capture_feedback"},
    )

    g.add_edge("load_context", "extract_memories")
    g.add_edge("extract_memories", "merge_memories")
    g.add_edge("merge_memories", "commit_memory")
    g.add_conditional_edges(
        "commit_memory",
        mem.route_after_memory,
        {"chat": "parse_parent_input", "feedback": "next_step"},
    )
    g.add_edge("parse_parent_input", "check_sufficiency")
    g.add_conditional_edges(
        "check_sufficiency",
        chat.route_after_sufficiency,
        {
            "generate_followup": "generate_followup",
            "rewrite_query": "rewrite_query",
        },
    )
    g.add_conditional_edges(
        "generate_followup",
        chat.route_after_followup,
        {
            "await_user_answer": "await_user_answer",
            "rewrite_query": "rewrite_query",
        },
    )
    # 家长答完 → **先过记忆**，再并进 CaseSummary
    #
    # 顺序不能反：家长的回答里可能就有一句纠正（"不对，他是五岁"），
    # 先并进 case_summary 就再也拿不出来了。
    g.add_edge("await_user_answer", "extract_memories")

    # ---- 检索链 ----
    g.add_edge("rewrite_query", "retrieve_knowledge")
    g.add_edge("retrieve_knowledge", "rerank_and_expand")
    g.add_edge("rerank_and_expand", "persist_evidence_bundle")
    g.add_edge("persist_evidence_bundle", "evidence_gate")
    g.add_conditional_edges(
        "evidence_gate",
        ret.route_after_evidence_gate,
        {
            "recommend": "build_recommendation",
            "limited_answer": "build_limited_answer",
            "professional_support": "build_limited_answer",
            "followup": "generate_evidence_followup",
        },
    )
    # 证据不足导致的追问：答完回到 parse_parent_input，事实重新并一遍
    g.add_edge("generate_evidence_followup", "await_user_answer")

    # ---- 生成 → 审查 → 渲染 ----
    g.add_conditional_edges(
        "build_recommendation",
        chat.route_after_recommendation,
        {"plans": "safety_review", "limited_answer": "build_limited_answer"},
    )
    g.add_edge("build_limited_answer", "safety_review")
    g.add_conditional_edges(
        "safety_review",
        ret.route_after_safety_review,
        {"present": "render_answer", "fallback": "build_limited_answer"},
    )
    g.add_conditional_edges(
        "render_answer",
        ret.route_after_render,
        {"plans": "persist_candidate_plans", "end": END},
    )

    g.add_edge("persist_candidate_plans", "present_plans")
    g.add_edge("present_plans", "commit_plan_selection")
    g.add_edge("commit_plan_selection", END)

    g.add_edge("capture_feedback", "analyze_feedback")
    # 反馈里"孩子反应 / 执行困难"正是观察的来源（计划 §10.3）。
    # 放在分析之后、给下一步之前：分析结果一并喂给抽取做背景。
    g.add_edge("analyze_feedback", "extract_memories")
    g.add_edge("next_step", END)

    return g.compile(checkpointer=checkpointer)


def get_graph(checkpointer: Any | None = None) -> Any:
    """别名，让 API 层读起来和业务名词一致。"""
    return build_graph(checkpointer)
