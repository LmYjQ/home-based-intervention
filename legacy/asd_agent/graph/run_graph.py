# -*- coding: utf-8 -*-
"""
反馈链图
========

    START → record_execution → analyze_outcome → next_action → apply_transitions → END

★ 为什么它是**第三张独立的图**
------------------------------
Phase 3 把计划链从观察图里拆出来，理由是三个具体的
（触发时机不同 / thread_id 会打架 / 写库节点会变成两个）。
反馈链的理由**完全同款**，而且更硬：反馈链的 run **要落库**，
而观察链与计划链的 run 各自只写自己那一张表。

★ 它与另外两张链的分工
----------------------
    观察链（`{child}:{date}`）  家长写的一段话 → observations + red_flags
    计划链（`plan:{child}:{date}`）今天做哪几条 → plans（要家长点头）
    反馈链（`run:{child}:{date}`）  家长填完的反馈 → sessions + skill_state
                                  + daily_summary（并给出"接下来怎么办"）

★ ★ 这条链的价值不在 HITL，在**幂等**
------------------------------------
家长会重复提交（手快点两下、网络重试、前端重渲染）。
以前 `/api/feedbacks` 没有幂等保护，于是 sessions 里多出一行 refused，
`refusal_counts` 翻倍到 2，**当天误触冷却阈值** ——
那条活动被踢出明天的候选，而界面上看不出为什么。

现在这件事由**两层**保证，缺一不可：
  1. **落库那半幂等**（唯一键 + upsert）—— 数据的性质。
     同一次执行重跑一次不多写一行；家长更正了会覆盖当天那一条。
  2. **checkpoint 那半幂等**（input_digest）—— 不白跑。
     同一天提交**一模一样**的内容 → 指纹相同 → 直接返回上次的结果，
     连那一次 LLM（日结）都不付。
     只做第 1 层的话，数据是对的，但家长要为一个重复提交再等 20-60 秒。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from langgraph.graph import START, END, StateGraph      # noqa: E402

from . import run_nodes                                # noqa: E402
from .state import RunState                            # noqa: E402

RUN_NODES = [
    "record_execution", "analyze_outcome", "next_action", "apply_transitions",
]


def build_run_graph(saver=None, *, name: str = "ganyu_run"):
    """编反馈链图。`saver=None` 时按 `ASD_DB_PATH` 自己开一个。

    ★ 这张图**没有分支也没有环** —— 是一条直线。
      看着不像 LangGraph，但它的价值在别处：
        · 四个节点各自可单测（纯函数 + 一次 LLM）
        · checkpoint 落在「一次执行」这个粒度上（run_id）
        · 落库集中在最后一个节点，幂等键于是只有一处
      以前这些都在一个 HTTP 函数里，于是幂等只能靠"调用方的自觉"。
    """
    if saver is None:
        from .checkpointer import saver_for
        saver = saver_for()

    g = StateGraph(RunState)
    g.add_node("record_execution", run_nodes.record_execution)
    g.add_node("analyze_outcome", run_nodes.analyze_outcome)
    g.add_node("next_action", run_nodes.next_action)
    g.add_node("apply_transitions", run_nodes.apply_transitions)

    g.add_edge(START, "record_execution")
    g.add_edge("record_execution", "analyze_outcome")
    g.add_edge("analyze_outcome", "next_action")
    g.add_edge("next_action", "apply_transitions")
    g.add_edge("apply_transitions", END)

    return g.compile(checkpointer=saver, name=name)


# ================================================================== 图入口
def run_inputs(conn, child_id: str, day: str, feedbacks=None, *,
               no_llm: bool = False) -> dict:
    """★ 反馈链的入参装配。**图入口，节点体之外。**

    两件事：

    1. **`store.plan_paused(conn, child_id)`** —— 「暂停计划生成」的
       **唯一口径**（LANGGRAPH.md 第四节之三定的那条 SQL）。
       在这里算一次、注入 `state["paused"]`，节点体只读它。
       放这里而不是节点里，是因为节点体是纯计算层 ——
       一旦 `next_action` 自己连库判暂停，那条 SQL 就有了第二份拷贝，
       而两处迟早漂移；漂移的后果是"暂停了但界面说没暂停"。

    2. **`feedback.feedback_digest(...)`** —— 这份输入的指纹。
       HTTP 层拿它做幂等守卫：指纹相同就不重跑这张图。

    没有画像时**不**抛异常：反馈链不排计划，它只是把今天记下来。
    没有画像 → 状态机拿不到可信度 → 不会发生任何转移 ——
    那正是闸门该关的时候（PROGRESS.md 阶段 4 坑 6 的同款后果）。
    """
    from feedback import feedback_digest
    import store

    fbs = list(feedbacks or [])
    return {
        "child_id": child_id,
        "date": day,
        "no_llm": bool(no_llm),
        # ★ run_id = thread_id = 「一天的执行」的身份
        "run_id": store.day_run_id(child_id, day),
        "feedbacks": [{
            "activity_id": f.activity_id,
            "outcome": f.outcome,
            "prompt_level": f.prompt_level,
            "steps_done": f.steps_done,
            "note": f.note,
        } for f in fbs],
        "input_digest": feedback_digest(fbs, day),
        # ★ 暂停口径：只此一处
        "paused": store.plan_paused(conn, child_id),
    }
