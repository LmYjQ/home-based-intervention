# -*- coding: utf-8 -*-
"""
编排层（LangGraph）
===================

`asd_agent/` 是平铺的、模块之间用 `import store` 这种顶层导入互认，
所以这个子包必须把**父目录**塞进 sys.path，否则 `import store` 会找不到。
放在 `__init__.py` 里做一次，`nodes.py` / `builder.py` 就都能直接顶层导入。

为什么要有这个包
----------------
它**不装业务逻辑**。它只做一件事：把已经存在的纯函数排成一条有 checkpoint
的流程，让"流程里等人"这件事第一次成为可能。

三条不可回退的边界（LANGGRAPH.md §四 / 计划文件「核心原则」）
------------------------------------------------------------
1. **Checkpoint 与业务数据库是两回事。** `sessions` / `skill_state` /
   `observations` / `daily_summary` 全部保留 append-only。checkpoint 是**追加的**
   resume + provenance 层，不是替代品。别因为"反正有 checkpoint 了"去删业务表。
2. **模型给证据，Python 做判定。** 节点体默认是纯函数；
   引入 LLM 节点必须写清楚它为什么不能是判定函数。
3. **`interrupt()` 恢复时会重跑整个节点。** 因此节点在 `interrupt()` 之前
   不得有任何不可重复的副作用（写库、发消息、通知）。
   → 所以所有落库都集中在 `publish` 节点，它排在所有 interrupt 之后。
"""

from __future__ import annotations

import os
import sys

# 父目录（asd_agent/）必须在 sys.path 上，`import store` 才成立
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from .state import (  # noqa: E402
    InterventionState, ClarifyAnswer, EscalationVerdict,
    thread_config, fork_config, thread_id_for,
)
from .checkpointer import saver_for  # noqa: E402
from .builder import build_graph, GRAPH_NODES  # noqa: E402
from . import draft  # noqa: E402
from .runtime import get_graph, get_draft_graph  # noqa: E402

__all__ = [
    "InterventionState", "ClarifyAnswer", "EscalationVerdict",
    "thread_config", "fork_config", "thread_id_for",
    "saver_for", "build_graph", "GRAPH_NODES",
    "draft", "get_graph", "get_draft_graph",
]
