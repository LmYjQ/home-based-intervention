"""反馈图的路由与中断（阶段 4），不连数据库、不调真模型。

这个文件钉的是**流程**：
- 四选一路由（continue / adjust / followup / stop）；
- 追问 interrupt 的恢复会重新分析，且轮数有上限；
- adjust 走检索，但只出文字调整建议、不出方案卡。

节点内部实现由 ``test_feedback_api.py`` 和仓储测试覆盖。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from rdi_agent.graph import builder as builder_mod
from rdi_agent.graph.evidence_gate import route_after_evidence_gate
from rdi_agent.graph.nodes import (
    chat_nodes,
    feedback_nodes,
    memory_nodes,
    retrieval_nodes,
    write_nodes,
)
from rdi_agent.graph.nodes.feedback_nodes import (
    MAX_FEEDBACK_FOLLOWUP_ROUNDS,
    route_after_next_step,
)
from rdi_agent.schemas.feedback import FeedbackSubmission


# --------------------------------------------------------------- 纯规则
def test_execution_status_precedence():
    base = {"trial_id": "t", "turn_id": "x", "attempted": False}
    assert FeedbackSubmission(**base).execution_status() == "not_attempted"
    assert (
        FeedbackSubmission(**{**base, "attempted": True}).execution_status()
        == "attempted"
    )
    # 停了 > 做了 > 没做：勾了"没做"又勾"中途停"，按更具体的算
    assert (
        FeedbackSubmission(**{**base, "stopped": True}).execution_status()
        == "stopped"
    )


@pytest.mark.parametrize(
    ("decision", "rounds", "expected"),
    [
        ("continue", 0, "finish"),
        ("stop", 0, "finish"),
        ("adjust", 0, "retrieve"),
        ("followup", 0, "followup"),
        # 追问额度用完后不许再问
        ("followup", MAX_FEEDBACK_FOLLOWUP_ROUNDS, "finish"),
    ],
)
def test_route_after_next_step(decision, rounds, expected):
    state = {
        "next_step": {"decision": decision},
        "feedback_followup_rounds": rounds,
    }
    assert route_after_next_step(state) == expected


def test_feedback_evidence_gate_skips_recommendation():
    state = {
        "entrypoint": "feedback",
        "evidence_decision": {"decision": "recommend", "high_risk": False},
    }
    assert route_after_evidence_gate(state) == "feedback_adjustment"


def test_chat_evidence_gate_unchanged():
    state = {
        "entrypoint": "chat",
        "evidence_decision": {"decision": "recommend", "high_risk": False},
    }
    assert route_after_evidence_gate(state) == "recommend"


# --------------------------------------------------------------- 图
def _feedback_state(**extra):
    return {
        "entrypoint": "feedback",
        "conversation_id": "c1",
        "thread_id": "t1",
        "turn_id": "turn_1",
        "family_id": "f1",
        "child_id": "child_1",
        "feedback": {
            "trial_id": "trial_1",
            "turn_id": "turn_1",
            "attempted": True,
            "implementation_notes": "做了，但中途停了",
        },
        **extra,
    }


def _patch_feedback_nodes(monkeypatch, decisions, calls):
    """把反馈节点换成可控替身。``decisions`` 按 next_step 调用次数取值。"""
    queue = list(decisions)

    async def capture(state):
        calls.append("capture")
        return {
            "feedback_created": True,
            "trial_status": "attempted",
            "conversation_id": "c1",
        }

    async def context(state):
        calls.append("context")
        return {"plan_context": {"title": "原方案"}}

    async def analyze(state):
        calls.append("analyze")
        return {"feedback_analysis": {"actual_execution": "做了"}}

    async def next_step(state):
        calls.append("next_step")
        decision = queue.pop(0) if queue else "continue"
        return {
            "next_step": {"decision": decision},
            "final_answer": f"decision={decision}",
        }

    async def followup(state):
        calls.append("followup")
        return {"feedback_followup_questions": [{"question": "多久？", "options": []}]}

    async def finalize(state):
        calls.append("finalize")
        return {}

    monkeypatch.setattr(feedback_nodes, "capture_feedback", capture)
    monkeypatch.setattr(feedback_nodes, "load_feedback_context", context)
    monkeypatch.setattr(feedback_nodes, "analyze_feedback", analyze)
    monkeypatch.setattr(feedback_nodes, "next_step", next_step)
    monkeypatch.setattr(feedback_nodes, "generate_feedback_followup", followup)
    monkeypatch.setattr(feedback_nodes, "finalize_feedback", finalize)

    # 记忆节点：这一步测路由，不测记忆
    async def no_memory(state):
        calls.append("memory")
        return {}

    monkeypatch.setattr(memory_nodes, "extract_memories", no_memory)
    monkeypatch.setattr(memory_nodes, "merge_memories", no_memory)
    monkeypatch.setattr(memory_nodes, "commit_memory", no_memory)


def _build():
    return builder_mod.build_graph(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_continue_goes_through_memory_and_finalize(monkeypatch):
    calls: list[str] = []
    _patch_feedback_nodes(monkeypatch, ["continue"], calls)
    graph = _build()

    result = await graph.ainvoke(
        _feedback_state(), config={"configurable": {"thread_id": "f-continue"}}
    )
    assert "__interrupt__" not in result
    assert calls[0] == "capture"
    assert "analyze" in calls and "next_step" in calls
    assert "memory" in calls
    assert calls[-1] == "finalize"


@pytest.mark.asyncio
async def test_followup_interrupt_then_resume_reanalyzes(monkeypatch):
    calls: list[str] = []
    _patch_feedback_nodes(monkeypatch, ["followup", "continue"], calls)
    graph = _build()
    config = {"configurable": {"thread_id": "f-followup"}}

    first = await graph.ainvoke(_feedback_state(), config=config)
    interrupts = first.get("__interrupt__") or []
    assert interrupts, "反馈不足应该停在 interrupt"
    assert interrupts[0].value["kind"] == "feedback_followup"
    assert interrupts[0].value["questions"]

    analyze_before = calls.count("analyze")
    final = await graph.ainvoke(
        Command(
            resume={
                "text": "不到五分钟",
                "answers": [{"question": "多久？", "answer": "不到五分钟"}],
            }
        ),
        config=config,
    )
    assert "__interrupt__" not in final
    assert calls.count("analyze") == analyze_before + 1
    assert calls[-1] == "finalize"
    assert final["feedback_followup_rounds"] == 1


@pytest.mark.asyncio
async def test_adjust_uses_retrieval_but_never_creates_cards(monkeypatch):
    calls: list[str] = []
    _patch_feedback_nodes(monkeypatch, ["adjust"], calls)

    async def prepare(state):
        calls.append("prepare")
        return {"case_summary": {"observed_facts": ["做了"]}, "user_message": "做了"}

    async def rewrite(state):
        calls.append("rewrite")
        return {}

    async def retrieve(state):
        calls.append("retrieve")
        return {}

    async def rerank(state):
        calls.append("rerank")
        return {"evidence_bundle": {"evidence": [], "evidence_gaps": []}}

    async def persist(state):
        calls.append("persist")
        return {}

    async def gate(state):
        calls.append("gate")
        return {
            "evidence_decision": {
                "evidence_status": "none",
                "decision": "limited_answer",
                "high_risk": False,
            }
        }

    async def adjustment(state):
        calls.append("adjustment")
        return {
            "feedback_adjustment": {"headline": "调整一下"},
            "candidate_plans": [],
            "answer_mode": "adjustment",
            "final_answer": "调整一下",
        }

    async def safety(state):
        calls.append("safety")
        return {"safety_review": {"result": "pass"}}

    def render(state):
        calls.append("render")
        return {"final_answer": "调整一下", "route_after_render": "end"}

    monkeypatch.setattr(feedback_nodes, "prepare_feedback_retrieval", prepare)
    monkeypatch.setattr(feedback_nodes, "build_feedback_adjustment", adjustment)
    monkeypatch.setattr(retrieval_nodes, "rewrite_query", rewrite)
    monkeypatch.setattr(retrieval_nodes, "retrieve_knowledge", retrieve)
    monkeypatch.setattr(retrieval_nodes, "rerank_and_expand", rerank)
    monkeypatch.setattr(write_nodes, "persist_evidence_bundle", persist)
    monkeypatch.setattr(retrieval_nodes, "evidence_gate", gate)
    monkeypatch.setattr(retrieval_nodes, "safety_review", safety)
    monkeypatch.setattr(retrieval_nodes, "render_answer", render)

    graph = _build()
    result = await graph.ainvoke(
        _feedback_state(), config={"configurable": {"thread_id": "f-adjust"}}
    )

    assert "__interrupt__" not in result
    assert calls[:3] == ["capture", "context", "analyze"]
    # 检索链真的跑了
    for name in ("prepare", "rewrite", "retrieve", "rerank", "gate", "adjustment"):
        assert name in calls, f"{name} 没跑"
    assert result["candidate_plans"] == []
    assert calls[-1] == "finalize"
