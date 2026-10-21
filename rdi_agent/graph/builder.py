"""Graph 装配（计划 §8.2）。

图有两条入口，用 ``route_entry`` 分流：

    START → chat:    load_context → parse_parent_input → check_sufficiency
                                  ├─ 需要补充 → generate_followup
                                  │             → await_user_answer (interrupt)
                                  │             → parse_parent_input  （回到判断）
                                  └─ 信息足够 → build_recommendation
                                                → persist_candidate_plans
                                                → present_plans (interrupt)
                                                → commit_plan_selection → END

    START → feedback: capture_feedback → analyze_feedback → next_step → END

追问那圈是**故意**回到 ``parse_parent_input`` 而不是接一个新节点：
家长的回答要重新并进 CaseSummary，事实是累积的，不能问一轮丢一轮。
收敛靠 ``followup_rounds``，到 3 轮强制出方案。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from rdi_agent.graph.nodes import chat_nodes as chat
from rdi_agent.graph.nodes import feedback_nodes as fb
from rdi_agent.graph.nodes import write_nodes as write
from rdi_agent.graph.state import CoachState


def route_entry(state: CoachState) -> str:
    return "feedback" if state.get("entrypoint") == "feedback" else "chat"


def build_graph(checkpointer: Any | None = None) -> Any:
    """返回编译好的 StateGraph。"""
    g = StateGraph(CoachState)

    # ---- 聊天入口 ----
    g.add_node("load_context", chat.load_context)
    g.add_node("parse_parent_input", chat.parse_parent_input)
    g.add_node("check_sufficiency", chat.check_sufficiency)
    g.add_node("generate_followup", chat.generate_followup)
    g.add_node("await_user_answer", chat.await_user_answer)
    g.add_node("build_recommendation", chat.build_recommendation)
    g.add_node("persist_candidate_plans", write.persist_candidate_plans)
    g.add_node("present_plans", chat.present_plans)
    g.add_node("commit_plan_selection", write.commit_plan_selection)

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

    g.add_edge("load_context", "parse_parent_input")
    g.add_edge("parse_parent_input", "check_sufficiency")
    g.add_conditional_edges(
        "check_sufficiency",
        chat.route_after_sufficiency,
        {
            "generate_followup": "generate_followup",
            "build_recommendation": "build_recommendation",
        },
    )
    g.add_conditional_edges(
        "generate_followup",
        chat.route_after_followup,
        {
            "await_user_answer": "await_user_answer",
            "build_recommendation": "build_recommendation",
        },
    )
    # 家长答完 → 重新并进 CaseSummary 再判断一次
    g.add_edge("await_user_answer", "parse_parent_input")

    g.add_edge("build_recommendation", "persist_candidate_plans")
    g.add_edge("persist_candidate_plans", "present_plans")
    g.add_edge("present_plans", "commit_plan_selection")
    g.add_edge("commit_plan_selection", END)

    g.add_edge("capture_feedback", "analyze_feedback")
    g.add_edge("analyze_feedback", "next_step")
    g.add_edge("next_step", END)

    return g.compile(checkpointer=checkpointer)


def get_graph(checkpointer: Any | None = None) -> Any:
    """别名，让 API 层读起来和业务名词一致。"""
    return build_graph(checkpointer)