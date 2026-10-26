"""调试接口（阶段 5）：checkpoint 摘要、分叉隔离、脱敏。

不连数据库：用 ``InMemorySaver`` 编译一张小图，替换 ``debug.get_graph``。
分叉配方本身是和 LangGraph 版本相关的，所以这里钉的是**行为**：

- 分叉落在独立 thread；
- 源 thread 的 head、历史和业务 state 一行不动；
- 摘要里不出现儿童原文 / 完整回答。
"""

from __future__ import annotations

import json
import uuid
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from rdi_agent.api import debug
from rdi_agent.graph.nodes import write_nodes
from rdi_agent.graph.write_guard import writes_allowed


class S(TypedDict, total=False):
    x: int
    hit: list[str]
    debug: bool
    thread_id: str
    conversation_id: str
    final_answer: str
    candidate_plans: list[dict]
    evidence_bundle: dict
    child_context: dict
    case_summary: dict


def _build_graph():
    def a(state):
        return {"hit": [*state.get("hit", []), "a"], "x": state.get("x", 0) + 1}

    def b(state):
        return {"hit": [*state.get("hit", []), "b"], "x": state.get("x", 0) + 10}

    def c(state):
        return {"hit": [*state.get("hit", []), "c"], "x": state.get("x", 0) + 100}

    g = StateGraph(S)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_node("c", c)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile(checkpointer=InMemorySaver())


def _conversation_id() -> str:
    return f"{uuid.uuid4()}.{uuid.uuid4()}.debugtest"


@pytest.fixture
def debug_graph(monkeypatch):
    graph = _build_graph()
    monkeypatch.setattr(debug, "get_graph", lambda: graph)
    cid = _conversation_id()
    graph.invoke(
        {
            "x": 0,
            "hit": [],
            "child_context": {"display_label": "不许出现"},
            "case_summary": {"observed_facts": ["不许出现"]},
            "final_answer": "这是一段完整回答" * 50,
        },
        config={"configurable": {"thread_id": cid}},
    )
    return graph, cid


@pytest.mark.asyncio
async def test_state_summary_is_sanitized(debug_graph):
    _, cid = debug_graph
    summary = await debug.get_state(cid)
    body = json.dumps(summary, ensure_ascii=False)
    assert "不许出现" not in body
    assert summary["answer_preview"].endswith("…") or len(summary["answer_preview"]) <= 200
    assert "child_context" not in summary
    assert "case_summary" not in summary


@pytest.mark.asyncio
async def test_checkpoint_history(debug_graph):
    _, cid = debug_graph
    result = await debug.list_checkpoints(cid)
    assert result["thread_id"] == cid
    assert len(result["checkpoints"]) >= 3
    # 最新一条已经跑完
    assert result["checkpoints"][0]["next"] == []
    assert any(cp["resumable"] for cp in result["checkpoints"])


@pytest.mark.asyncio
async def test_fork_is_isolated_and_uses_patch(debug_graph):
    graph, cid = debug_graph
    snapshots = list(graph.get_state_history(config={"configurable": {"thread_id": cid}}))
    mid = next(s for s in snapshots if s.next == ("b",))
    source_before = graph.get_state({"configurable": {"thread_id": cid}})
    history_before = len(snapshots)

    result = await debug.fork(
        cid,
        debug.ForkRequest(
            checkpoint_id=mid.config["configurable"]["checkpoint_id"],
            # 只改一个变量，其它 state 继承
            values_patch={"x": 999},
            run=True,
        ),
    )

    assert result["thread_id"].startswith("debug-fork-")
    assert result["thread_id"] != cid
    assert result["as_node"] == "a"
    assert result["ran"] is True
    assert result["state"]["debug"] is True

    fork_state = graph.get_state({"configurable": {"thread_id": result["thread_id"]}})
    # a 已经跑过，从 b 继续；x 从 patch 的 999 往上加
    assert fork_state.values["hit"][-2:] == ["b", "c"]
    assert fork_state.values["x"] == 999 + 10 + 100

    source_after = graph.get_state({"configurable": {"thread_id": cid}})
    assert source_after.values == source_before.values
    assert (
        len(list(graph.get_state_history({"configurable": {"thread_id": cid}})))
        == history_before
    )


@pytest.mark.asyncio
async def test_debug_branch_skips_business_writes():
    """写节点看到 debug=True 必须直接跳过，不碰数据库。"""
    state = {
        "debug": True,
        "conversation_id": "c1",
        "turn_id": "t1",
        "evidence_bundle": {"evidence": []},
        "candidate_plans": [],
        "selected_plan_id": "plan_1",
    }
    assert writes_allowed(state) is False
    for fn, name in (
        (write_nodes.persist_evidence_bundle, "persist_evidence_bundle"),
        (write_nodes.persist_candidate_plans, "persist_candidate_plans"),
        (write_nodes.commit_plan_selection, "commit_plan_selection"),
    ):
        result = await fn(state)
        assert name in result["writes_skipped"]


@pytest.mark.asyncio
async def test_fork_rejects_finished_checkpoint(debug_graph):
    graph, cid = debug_graph
    head = graph.get_state({"configurable": {"thread_id": cid}})
    with pytest.raises(Exception) as exc:
        await debug.fork(
            cid,
            debug.ForkRequest(
                checkpoint_id=head.config["configurable"]["checkpoint_id"],
                run=False,
            ),
        )
    assert "结束" in str(exc.value)
