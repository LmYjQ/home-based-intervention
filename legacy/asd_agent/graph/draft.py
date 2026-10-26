# -*- coding: utf-8 -*-
"""
草稿
====
家长填一半的反馈（逐条活动的行 + 「其他观察」那段话），**存 checkpoint**。

★ 为什么不用 localStorage
------------------------
项目约定草稿存服务端（技术方案 §5.5 的 provenance 那一层），好处是多设备、
刷新、换浏览器都在，而且草稿与「这一天」的 run 是同一个 thread 家族，
出问题能顺着 thread_id 查回去。

★ 为什么单独一张图，而不是复用干预主图
--------------------------------------
1. **字段污染**：草稿的行是半填的（outcome 可能还是空串），放主图的 state 里
   会让 `publish` 看到一堆不属于 observation 的字段。
2. **thread 隔离**：草稿走 `draft:{child}:{date}` 前缀，一次 PUT 打不到
   真实 run 的 thread 上 —— 这是靠命名空间保证的，不是在代码里靠自觉 if。
3. **★ 没有节点执行，所以「恢复重跑节点」这条硬约束在这里结构上不可能被违反**。
   主图那条约束是「节点在 interrupt 之前不得有不可重复的副作用」，
   而草稿图**从不 invoke**（只用 `update_state` 写、`get_state` 读），
   没有任何节点会被执行，自然也就没有「重跑」这回事。
   这比在主图里加一个「记得判重」的 if 可靠得多。

★ `update_state` 在从未 invoke 过的 thread 上可用
-------------------------------------------------
已实测（2026-10-20）：`graph.update_state(cfg, values, as_node="save")`
在一个没有任何 checkpoint 的 thread 上成功，`get_state` 随即能读出来。
本项目的其余部分都是先 invoke 再 resume，这里是例外，注释留着以免
下一个人「顺手」改成 invoke。
"""

from __future__ import annotations

import os
import sys
from typing import Any, TypedDict

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from langgraph.graph import START, END, StateGraph      # noqa: E402

DRAFT_PREFIX = "draft:"
_NODE = "save"


class DraftState(TypedDict, total=False):
    """一行都不强制。草稿的本质就是「填了一半」。"""

    child_id: str
    date: str
    rows: list[dict[str, Any]]
    other_notes: str
    saved_at: str


def draft_thread_id(child_id: str, date: str) -> str:
    """`draft:{child_id}:{date}`。

    ★ 与 run 的 thread_id（`{child_id}:{date}`，state.thread_id_for）**不同命名空间**。
      断言在 selfcheck 里：草稿写不得到真实 run 的 thread 上，反之亦然。
    """
    return f"{DRAFT_PREFIX}{child_id}:{date}"


def draft_config(child_id: str, date: str) -> dict:
    return {"configurable": {"thread_id": draft_thread_id(child_id, date)}}


def build_draft_graph(saver):
    """单节点图。**永远不会被 invoke**（见模块 docstring）。"""
    g = StateGraph(DraftState)
    # 节点体是空转：update_state 只写 checkpoint，不跑节点。
    # 真跑起来也不会有任何副作用（纯 dict 返回），但设计上不依赖它。
    g.add_node(_NODE, lambda s: dict(s))
    g.add_edge(START, _NODE)
    g.add_edge(_NODE, END)
    return g.compile(checkpointer=saver, name="ganyu_draft")


def save_draft(graph, *, child_id: str, date: str,
               rows: list[dict[str, Any]] | None = None,
               other_notes: str = "", saved_at: str = "") -> dict[str, Any]:
    """覆盖式保存（不是追加）—— 家长改的是同一份草稿，不是记历史。

    刻意**不做**版本/时间线：草稿的价值是「刷新后还在」，不是「能翻到
    三分钟前那一版」。要历史的话那是 checkpoint 的事，不该在这里再实现一遍。
    """
    graph.update_state(
        draft_config(child_id, date),
        {"child_id": child_id, "date": date,
         "rows": list(rows or []), "other_notes": other_notes,
         "saved_at": saved_at},
        as_node=_NODE,
    )
    return {"thread_id": draft_thread_id(child_id, date), "saved_at": saved_at}


def load_draft(graph, *, child_id: str, date: str) -> dict[str, Any] | None:
    """读草稿。**没有就 None** —— 调用方据此决定要不要渲染已填的内容。"""
    snap = graph.get_state(draft_config(child_id, date))
    vals = snap.values or {}
    if not vals.get("rows") and not vals.get("other_notes"):
        return None
    return {
        "thread_id": draft_thread_id(child_id, date),
        "rows": vals.get("rows") or [],
        "other_notes": vals.get("other_notes") or "",
        "saved_at": vals.get("saved_at") or "",
    }


def clear_draft(saver, *, child_id: str, date: str) -> None:
    """删掉整个草稿 thread。

    ★ 为什么用 `delete_thread` 而不是「写一份空的」：
      写空的那份会在 checkpoint 表里留一行永远为空的 state，
      而 `load_draft` 分不清「存过但清空了」和「从没存过」。
      提交成功后草稿就不该存在，这是语义上的删除。
    """
    saver.delete_thread(draft_thread_id(child_id, date))
