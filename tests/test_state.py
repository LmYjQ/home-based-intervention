"""State 与图装配的冒烟测试。"""

from __future__ import annotations

from rdi_agent.graph.builder import build_graph
from rdi_agent.graph.state import CoachState


def test_state_fields():
    for key in (
        "family_id",
        "conversation_id",
        "thread_id",
        "turn_id",
        "case_summary",
        "evidence_bundle",
        "evidence_decision",
        "candidate_plans",
        "selected_plan_id",
        "memory_candidates",
        "memory_update_result",
        "memory_context",
        "memory_forbidden_facts",
        "memory_notes",
        "feedback_db_id",
        "error_code",
    ):
        assert key in CoachState.__annotations__


def test_state_is_total_false():
    """所有字段都是可选的，节点可以只返回增量。"""
    assert CoachState.__total__ is False


def test_build_graph_compiles():
    graph = build_graph()
    assert hasattr(graph, "ainvoke")
