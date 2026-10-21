"""Graph 装配。

阶段 0 只有状态定义和一个 ``echo`` 占位节点。
为什么要有占位节点：LangGraph 拒绝编译没有入口的图（"Graph must have an
entrypoint"），所以空图连 ``compile()`` 都过不去。阶段 1 起把真正的节点
（parse_parent_input / check_sufficiency / ...）挂上来，``echo`` 删掉。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from rdi_agent.graph.state import CoachState


async def echo(state: CoachState) -> dict[str, Any]:
    """占位节点：原样回传，不做任何事。阶段 1 用它验证图能跑通。"""
    return {"trace_metadata": {"phase": "scaffold"}}


def build_graph(checkpointer: Any | None = None) -> Any:
    """返回编译好的 StateGraph。"""
    g = StateGraph(CoachState)
    g.add_node("echo", echo)
    g.add_edge(START, "echo")
    g.add_edge("echo", END)
    return g.compile(checkpointer=checkpointer)
