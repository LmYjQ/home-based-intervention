# -*- coding: utf-8 -*-
"""
图的形状
========

Phase 0 只画**底座已经存在且是纯函数**的那一段：

    START → record_intake → extract_observation → validate_observation
                                                      │
                                    needs_clarification? ├─ 是 → ⚡ clarify → 回抽取
                                                      └─ 否 ↓
                                              safety_gate → highest=="urgent"?
                                                                ├─ 是 → escalate ↓
                                                                └─ 否 ──────┴─→ publish → END

**为什么计划链（select_goal / retrieve / generate / validate / approve / freeze）
和反馈链（record_execution / analyze_outcome / next_action）现在不画：**

不是没想清楚，是它们各自缺一块 Phase 0 明确不许动的东西 ——
`plans` 表没有 `status` / `version`，`sessions` 没有 `plan_id` / `run_id`，
红旗没有落库位置。现在画就只能画一版**注定要改的**图，
而改图比改表贵得多（表的迁移是一次性的，图的形状会渗进 checkpoint 的历史里）。

节点顺序本身有一条约束，是上图的形状由不得的：
**所有落库都在所有 interrupt 之后**（`publish` 是唯一写业务表的节点）。
理由是实测出来的 —— `interrupt()` 恢复时会重跑整个节点，
所以凡是在 interrupt 之前写过库的东西，resume 一次就写两遍。
这条不能靠"我记得判重"，只能靠节点的位置。

★ `escalate → publish`（不是 `escalate → END`）
-------------------------------------------------
Phase 0/1 是 escalate → END，于是"孩子今天咬自己"那天，家长的原话从不落库、
红旗也无处可落（见 `nodes.py` 里那段改边说明）。判定没变，改的只是
"判完之后这一天还写不写"。而且它让红旗落库对将来 Phase 3 的
`⚡ approve_plan` interrupt **结构上免疫** —— 那是这条边真正的长期理由。

`PlanReview` / `PlanRequest` 的形状已经在 `state.py` 里定好了，
Phase 3 接的时候不需要再改 state 的定义 —— 那是为了让分阶段不互相欠债。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from langgraph.graph import START, END, StateGraph      # noqa: E402

from . import nodes                                      # noqa: E402
from .state import InterventionState                    # noqa: E402

GRAPH_NODES = [
    "record_intake", "extract_observation", "validate_observation",
    "clarify", "safety_gate", "escalate", "publish",
]


# ================================================================== 条件路由
def _route_clarify(state) -> str:
    """needs_clarification 为假时**跳过** clarify 节点。

    ★ 注意是"跳过"而不是"进去再返回"：`add_conditional_edges` 的两端
      都画在图上，而 skip 是真的不执行它。interrupt 节点如果在图里被
      反复进出，每进出一次都是一个 checkpoint —— 而澄清一天最多一次。
    """
    return "clarify" if state.get("needs_clarification") else "after_validate"


def _route_safety(state) -> str:
    v = state.get("verdict") or {}
    return "escalate" if v.get("escalate") else "publish"


# ================================================================== 组装
def build_graph(saver=None, *, name: str = "ganyu_intervention"):
    """编一张带 checkpointer 的图。

    `saver=None` 时按 `ASD_DB_PATH` 自己开一个 —— CLI / 测试的常见情形。
    生产上传进来的 saver 要与业务连接**不同的连接**（见 checkpointer.py 的 ②）。
    """
    if saver is None:
        from .checkpointer import saver_for
        saver = saver_for()

    g = StateGraph(InterventionState)

    g.add_node("record_intake", nodes.record_intake)
    g.add_node("extract_observation", nodes.extract_observation)
    g.add_node("validate_observation", nodes.validate_observation)
    g.add_node("clarify", nodes.clarify)
    g.add_node("safety_gate", nodes.safety_gate)
    g.add_node("escalate", nodes.escalate)
    g.add_node("publish", nodes.publish)

    g.add_edge(START, "record_intake")
    g.add_edge("record_intake", "extract_observation")
    g.add_edge("extract_observation", "validate_observation")

    g.add_conditional_edges(
        "validate_observation", _route_clarify,
        {"clarify": "clarify", "after_validate": "safety_gate"})
    # 答完之后回到**抽取**，不是回到判定 ——
    # 回答已经拼进语料了，不重抽一遍的话补的信息就只是躺在 state 里没人用。
    g.add_edge("clarify", "extract_observation")

    g.add_conditional_edges(
        "safety_gate", _route_safety,
        {"escalate": "escalate", "publish": "publish"})
    # ★ escalate 之后仍然走 publish —— 见模块 docstring。
    #   escalate 自己一个字节都不写（纯计算），落库全在 publish。
    g.add_edge("escalate", "publish")
    g.add_edge("publish", END)

    return g.compile(checkpointer=saver, name=name)


# ================================================================== 自检
def _cli() -> int:
    from .selfcheck import main as selfcheck_main
    return selfcheck_main()


if __name__ == "__main__":
    sys.exit(_cli())
