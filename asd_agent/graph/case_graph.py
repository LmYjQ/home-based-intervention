# -*- coding: utf-8 -*-
"""
第四张图：案例图
================

    START → load_case_context → extract_new_evidence(LLM) → update_assessment
          → safety_check → urgent? ──是──→ escalate ──┐
                            │                        │
                            ↓                        │
                        detect_unknowns               │
                            │ 需要追问? ──是──→ ⚡clarify_with_parent
                            │                        │  （回抽取）
                            ↓                        ↓
                       publish_case_events ←─────────┘（唯一写库节点）

★ thread 是**长期**的
--------------------
观察链的 thread 是 `{child}:{date}`（一天一条），本图是 `case:{case_id}`
（一个案例一条，一直用下去）。这正是"案例"与"某一天"的区别 ——
一个案例要看好几轮，而每一轮之间必须接着上一轮。

★ 为什么 `clarify_with_parent` 回到**抽取**而不是判定
-----------------------------------------------------
回答已经拼进语料了，不重抽一遍，补的信息就只是躺在 state 里没人用。
（与观察链同款，理由见 `graph/nodes.py` 的 `clarify`。）

★ 条件边是"跳过"不是"进出"
---------------------------
`needs_clarification` 为假时 `clarify_with_parent` **根本不执行**。
interrupt 节点每进出一次就是一个 checkpoint，而澄清最多三轮。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from langgraph.graph import START, END, StateGraph      # noqa: E402

from . import case_nodes                               # noqa: E402
from .state import CaseState                           # noqa: E402

CASE_GRAPH_NODES = [
    "load_case_context", "extract_new_evidence", "update_assessment",
    "safety_check", "detect_unknowns", "clarify_with_parent",
    "escalate", "publish_case_events",
]


# ================================================================== 条件路由
def _route_safety(state) -> str:
    """命中 urgent → 先给转诊指引，但**仍然 publish**。

    ★ 与观察链同款的理由：`escalate → END` 会让"孩子今天咬自己"那一天
      最该被记下来的东西反倒没留下（`graph/builder.py` 里记着那个改边）。
      这里是新表新图，但同一条理由照样成立。
    """
    v = state.get("verdict") or {}
    return "escalate" if v.get("escalate") else "detect_unknowns"


def _route_clarify(state) -> str:
    """不追问时**跳过** clarify 节点（不是进去再返回）。"""
    return "clarify" if state.get("needs_clarification") else "publish"


# ================================================================== 组装
def build_case_graph(saver=None, *, name: str = "ganyu_case"):
    """编一张带 checkpointer 的案例图。

    ★ 与另外三张图**分开编译、分开缓存**（`runtime.get_case_graph`）——
      共用一个 dict 的话，第二张图会把第一张覆盖掉，
      而 `/api/cases/{id}/messages` 拿到的是观察链的图 ——
      症状是"建案例的接口返回了一段观察"，**没有任何报错**。
      这是 `runtime.get_plan_graph` 注释里记着的那个坑。
    """
    if saver is None:
        from .checkpointer import saver_for
        saver = saver_for()

    g = StateGraph(CaseState)

    g.add_node("load_case_context", case_nodes.load_case_context)
    g.add_node("extract_new_evidence", case_nodes.extract_new_evidence)
    g.add_node("update_assessment", case_nodes.update_assessment)
    g.add_node("safety_check", case_nodes.safety_check)
    g.add_node("detect_unknowns", case_nodes.detect_unknowns)
    g.add_node("clarify_with_parent", case_nodes.clarify_with_parent)
    g.add_node("escalate", case_nodes.escalate)
    g.add_node("publish_case_events", case_nodes.publish_case_events)

    g.add_edge(START, "load_case_context")
    g.add_edge("load_case_context", "extract_new_evidence")
    g.add_edge("extract_new_evidence", "update_assessment")
    g.add_edge("update_assessment", "safety_check")

    g.add_conditional_edges(
        "safety_check", _route_safety,
        {"escalate": "escalate", "detect_unknowns": "detect_unknowns"})
    # urgent 也要落库 —— 见 _route_safety 的注释。
    g.add_edge("escalate", "publish_case_events")

    g.add_conditional_edges(
        "detect_unknowns", _route_clarify,
        {"clarify": "clarify_with_parent", "publish": "publish_case_events"})
    # 答完之后回到**抽取**（理由见模块 docstring）。
    g.add_edge("clarify_with_parent", "extract_new_evidence")

    g.add_edge("publish_case_events", END)

    return g.compile(checkpointer=saver, name=name)
