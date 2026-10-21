# -*- coding: utf-8 -*-
"""
案例的生命周期
==============

**纯函数**。判据全在这里，图与 HTTP 层都只调它。

★ 为什么生命周期要单独收一份表
------------------------------
`cases.status` 是**唯一一个可以改**的案例字段（其余 append-only），
所以"哪些改法是合法的"必须是**唯一**的一份清单 ——
散在调用点各写一遍 if，将来必然有一处落后，而落后那一处不报错，
只是让案例卡在一个说不清的状态里。

★ 八个状态对应家长真实看到的阶段
--------------------------------
`assessing` / `awaiting_parent_input` / `ready_to_execute` / `awaiting_feedback` /
`reviewing_outcome` / `revising_plan` / `paused` / `closed`

阶段 2 只用得到前三个 + `paused`（其余留给阶段 3/4），
但**一次就把八个定齐** —— 生命周期是案例的骨架，
等做第 4 个状态时再回头改表，改的是**已经落库的历史数据**。
"""

from __future__ import annotations

#: 全部合法状态。**顺序即家长看到的推进顺序**（除了 paused / closed）。
CASE_STATUSES: tuple[str, ...] = (
    "assessing",
    "awaiting_parent_input",
    "ready_to_execute",
    "awaiting_feedback",
    "reviewing_outcome",
    "revising_plan",
    "paused",
    "closed",
)

#: 允许的迁移。**不在表里的一律拒绝**（默认拒绝，不是默认允许）。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "assessing": {"awaiting_parent_input", "ready_to_execute", "paused", "closed"},
    "awaiting_parent_input": {"assessing", "ready_to_execute", "paused", "closed"},
    "ready_to_execute": {"awaiting_feedback", "revising_plan", "assessing",
                         "paused", "closed"},
    "awaiting_feedback": {"reviewing_outcome", "ready_to_execute", "paused", "closed"},
    "reviewing_outcome": {"revising_plan", "ready_to_execute", "assessing",
                          "paused", "closed"},
    "revising_plan": {"ready_to_execute", "assessing", "paused", "closed"},
    "paused": {"assessing", "ready_to_execute", "closed"},
    "closed": set(),
}


def can_transition(frm: str, to: str) -> bool:
    """这次迁移合法吗。**默认拒绝** —— 表里没有就是不行。"""
    return to in ALLOWED_TRANSITIONS.get(frm, set())


def next_status(*, has_unknowns: bool, awaiting_clarify: bool,
                escalated: bool) -> str:
    """一次评估跑完之后，案例该在哪个状态。

    判据三条，按优先级：

    1. `escalated`（命中 urgent 红旗）→ `paused`
       —— 有需要先找专业人士的情况，就不该继续往下走。
       这与 `store.plan_paused` 是同一个安全立场（fail-closed）。
    2. `awaiting_clarify`（图停在 ⚡ 追问上）→ `awaiting_parent_input`
    3. 其余 → `ready_to_execute`（信息够了，可以出方案 —— 阶段 3 的事）

    ★ `has_unknowns` 单独留着是有意的：**有未知项不等于要追问**。
      追问与否由 `awaiting_clarify` 决定（那一层还管着"最多三轮"
      和"这轮没有新证据就不问"两条规则），这里不再重复判断 ——
      同一个判据出现在两处，就会有两处口径打架。
    """
    if escalated:
        return "paused"
    if awaiting_clarify:
        return "awaiting_parent_input"
    return "ready_to_execute"
