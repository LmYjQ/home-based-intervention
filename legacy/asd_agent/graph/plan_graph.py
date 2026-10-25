# -*- coding: utf-8 -*-
"""
计划链图
========

    START → select_goal → retrieve_activities → generate_plan → validate_plan
                                                                  │
                              needs_review? ──是──→ ⚡ approve_plan ─┤
                                                    approve ────────┤
                                                    reject ─────────┤→ freeze_plan
                                                                  │
                                              否 ──────────────────┘
                                                                  ↓
                                                          publish_plan → END

★ 为什么它**不是**观察图后面接一段
------------------------------------
技术方案 §5.1 把它画在同一张图里（安全检查之后接着排计划）。
Phase 3 落地时拆成独立一张，三个具体理由写在 `plan_nodes.py` 的模块
docstring 里（触发时机不同 / thread_id 会打架 / 写库节点会变成两个）。

★ 两条支路最后都汇到 `publish_plan`
------------------------------------
红旗落库在 Phase 2 已经归观察图的 `publish` 了（第四节之三），
这里**不再造第二个写业务表的节点**去写红旗 ——
计划链只写 `plans` 一张表。

★ `edit` 的处理：再生成一次，不弹第二次审批
------------------------------------------
`edit` = 家长说"改一下"。这一版的需求是**改**，不是**批**，
所以 `edit` 回到 `retrieve_activities` 再走一遍，然后**跳过审批**，
直接 `freeze_plan`（理由：家长刚刚已经表达过不满了，再问一次
"这次可以吗"是在追问而不是在尊重）。

★ 循环出口守卫
--------------
`select_goal → retrieve → generate → validate → approve(⚡) →(edit)→ retrieve …`
是一个环。守卫有两个，缺一不可：

1. **`already_reviewed`** —— `edit` 之后重新生成的那一版**不再弹审批**。
   家长已经表达过不满了，再问一次"这次可以吗"是在追问而不是在尊重。
2. **`edit_loop` + `MAX_EDIT_LOOPS`** —— 兜底。
   有了 (1) 之后这条守卫在正常路径上够不着，
   但它守的是另一件事：**将来有人把 (1) 去掉**（比如想让家长逐版确认），
   那时 edit↔retrieve 就是一个没有出口的环。
   而那个环在界面上看不出来（图每轮都"成功"了），
   唯一的表现是 CPU 一直在转、plans 表一直不写。

★ 为什么两条守卫都留在代码里，而不是留一条
------------------------------------------
删掉 (2) 的人会说"(1) 已经挡住了"——而这正是删它的理由：
(2) 守的正是 (1) 被删掉之后的世界。
一个只防"当前会发生什么"的守卫，在有人改了当前行为的那一刻就失效了。
和观察链的 `clarify_asked` 是同一个守卫的同一个理由。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from langgraph.graph import START, END, StateGraph      # noqa: E402

from . import plan_nodes                                 # noqa: E402
from .state import PlanState                             # noqa: E402

PLAN_NODES = [
    "select_goal", "retrieve_activities", "generate_plan",
    "validate_plan", "approve_plan", "freeze_plan", "publish_plan",
]

#: 允许「改一下」几轮。2 轮够家长说清诉求，也够系统照做；
#: 再多就不是澄清而是原地打转。
MAX_EDIT_LOOPS = 2


def _route_after_review(state) -> str:
    """`approve_plan` 之后往哪走。

    `edit` → 回 `retrieve_activities` 再生成一次（见模块 docstring）；
    `approve` / `reject` → 冻结。

    ★ 计数由 `approve_plan` **节点**写进 state，路由只读它。
      刻意不在这里做 `loops = state.get(...) + 1`：
      条件边拿到的是 state 的**只读视图**，本地算出来的新值不会写回，
      于是下一轮读到的还是旧计数 —— 守卫看起来存在，实际一次都不生效。
      （这正是 selfcheck 第 [16] 组断言要抓的东西。）
    """
    if (state.get("review") or {}).get("action") == "edit" \
            and int(state.get("edit_loop", 0)) < MAX_EDIT_LOOPS:
        return "retrieve_activities"
    return "freeze_plan"


def _route_needs_review(state) -> str:
    """要不要打断家长。

    ★ `already_reviewed` 是 `edit` 那条路的**唯一出口**：
      家长已经在这个 thread 上批过一次（哪怕那次批的是"改一下"），
      再问一次"这次可以吗"是在追问而不是在尊重 ——
      而每次多问一次，家长就少信任这个功能一分。
      所以 `edit` 重新生成之后**直接冻结**，不再弹第二次审批。
    """
    if state.get("already_reviewed"):
        return "freeze_plan"
    return "approve_plan" if state.get("needs_review") else "freeze_plan"


def _route_paused(state) -> str:
    """暂停时不生成计划，也不审批 —— 直接落库一行 `paused` 的记录。

    ★ 为什么要落这一行而不是什么都不写：
      「今天没有排计划」必须有一条**可查的记录**，
      否则家长过了三天回来看，界面上是"什么都没发生"，
      而真相是"因为 10-20 那条自伤红旗还没关闭，系统停着没敢排"。
      不写这一行 = 把安全机制变成静默故障。
    """
    return "freeze_plan"


def build_plan_graph(saver=None, *, name: str = "ganyu_plan"):
    """编计划链图。`saver=None` 时按 `ASD_DB_PATH` 自己开一个。"""
    if saver is None:
        from .checkpointer import saver_for
        saver = saver_for()

    g = StateGraph(PlanState)
    g.add_node("select_goal", plan_nodes.select_goal)
    g.add_node("retrieve_activities", plan_nodes.retrieve_activities)
    g.add_node("generate_plan", plan_nodes.generate_plan)
    g.add_node("validate_plan", plan_nodes.validate_plan)
    g.add_node("approve_plan", plan_nodes.approve_plan)
    g.add_node("freeze_plan", plan_nodes.freeze_plan)
    g.add_node("publish_plan", plan_nodes.publish_plan)

    g.add_edge(START, "select_goal")
    g.add_edge("select_goal", "retrieve_activities")
    g.add_edge("retrieve_activities", "generate_plan")
    g.add_conditional_edges(
        "generate_plan",
        lambda s: _route_paused(s) if s.get("plan_paused") else "continue",
        {"freeze_plan": "freeze_plan", "continue": "validate_plan"})

    g.add_conditional_edges(
        "validate_plan", _route_needs_review,
        {"approve_plan": "approve_plan", "freeze_plan": "freeze_plan"})

    g.add_conditional_edges(
        "approve_plan", _route_after_review,
        {"retrieve_activities": "retrieve_activities", "freeze_plan": "freeze_plan"})

    g.add_edge("freeze_plan", "publish_plan")
    g.add_edge("publish_plan", END)

    return g.compile(checkpointer=saver, name=name)


# ================================================================== 图入口
def plan_inputs(conn, child_id: str, day: str, *,
                extra_items: list | None = None) -> dict:
    """★ 计划链的入参装配。**图入口，节点体之外**。

    三件事，顺序不能换：

    1. **`store.plan_paused(conn, child_id)`** —— 全项目「暂停计划生成」的
       **唯一口径**（LANGGRAPH.md 第四节之三定的那条 SQL）。
       在这里算一次、注入 `state["paused"]`，节点体只读它。
       放这里而不是节点里，是因为节点体是纯计算层 ——
       一旦 `generate_plan` 自己连库判暂停，那条 SQL 就有了第二份拷贝。

    2. **`loop.build_request(conn, child_id, prof, before=day)`** ——
       画像 + 状态机 + 记忆库 → 检索请求。全项目只有这一处装配
       （`server.py:_build_request` 是它的薄封装）。
       `before=day` 让教练/日结读的是**这一天之前**的记忆：
       今天刚写下的反馈不该回过头来解释今天做什么。

    3. **`extra_items`** —— 装配进来的生成项（不在活动库里的那些方案项）。
       **唯一口径在入口**：节点体不查库、也不自己再拼一次池
       （REFACTOR.md §1.1 / §1.3.4）。
       它们跟着 `plan_request` 一起序列化进 checkpoint，
       节点还原 request 时一并拿到 —— 于是"生成项在不在池里"
       这件事在整条链上只有一处说了算。

    没有画像时抛异常而不是造一份降级画像：
    降级画像 `overall_confidence=0.1`，会让状态机的升级闸门全部关死
    （PROGRESS.md 阶段 4 坑 6 的同款后果），
    于是一份降级画像排出来的计划是"永远只推最简单的"。
    对"没有评估报告"来说那是**诚实的**，所以让 HTTP 层返回 400。
    """
    import loop as loop_mod
    import store as store_mod
    from activities import request_to_dict

    prof = store_mod.latest_profile(conn, child_id)
    if prof is None:
        raise LookupError(f"{child_id} 还没有画像，先走 POST /api/profile 建档")
    # ★ 当前生效的那一版的**指纹**，从 plans 表里读出来带进去。
    #   「今天要不要打断家长」比的就是它。
    #   取的是**存进去时算好的那份**，不是现在从活动库重算：
    #   活动库改了一条活动的难度，会让一份半年前的计划凭空变成
    #   "有变更"，而那次变更根本没人提过 ——
    #   家长会看到一个自己从没被问过的"改了什么"。
    #   （实测：漏了这一步时，第二天输入完全相同也会弹审批，
    #     理由还写着"第一次生成计划"。）
    cur = store_mod.active_plan(conn, child_id)
    req = loop_mod.build_request(conn, child_id, prof, before=day)
    # ★ 生成项进 request。**唯一口径在入口** ——
    #   节点体是纯计算层，一旦两个节点各加一次，池就会不一致
    #   （`excluded` 里说明了、装箱时却不见了）。
    #   `build_request` 已经装配过一次（候选表/卡片/计划链共用它），
    #   这里只有调用方**显式**给了生成项时才覆盖 ——
    #   否则会把它刚装好的那批冲掉（自检里就是这么注入 fixture 的）。
    if extra_items is not None:
        req.extra_items = list(extra_items)
    return {
        "child_id": child_id,
        "date": day,
        "profile_id": store_mod.latest_profile_id(conn, child_id),
        "current_fingerprint": ((cur or {}).get("payload") or {}).get("fingerprint"),
        # ★ 暂停口径：只此一处
        "paused": store_mod.plan_paused(conn, child_id),
        # ★ 检索请求口径：只此一处。存的是**纯 dict**
        #   （request_to_dict），理由见 plan_nodes._req 的注释。
        "plan_request": request_to_dict(req),
    }
