"""作废事实（forbidden facts）的过滤行为。

**这个文件测的是一条真实的线上事故**（2026-10-26）：

一条事实被错判成 ``superseded``（被一条完全无关的"孩子最近换过老师"
顶掉了），于是它进了 ``forbidden_facts``。
家长下一轮**一字不差地又说了同一句**，
那句话被当成"过期信息"丢掉 —— ``observed_facts`` 直接变成空数组，
整轮一条事实都没有。

同一批事实还从 ``retrieval_concepts`` 漏了出去，
被 superseded 的内容直接进了检索 query。

所以这里盯两件事：
1. 家长这轮**亲口说了**的作废内容，要留下；
2. 家长**没提**的作废内容，所有字段都不许漏。
"""

from __future__ import annotations

import pytest

from rdi_agent import llm
from rdi_agent.graph.nodes import chat_nodes
from rdi_agent.graph.state import CoachState
from rdi_agent.schemas.case import CaseSummary

# 家长当初说过、后来被顶掉/删掉的那句
DELETED = "孩子4岁，最近不愿意去幼儿园，早上出门就哭。"


@pytest.fixture
def fake_llm(monkeypatch):
    """把模型换成预置答案。

    只测过滤逻辑，模型抽出来什么由测试自己指定。
    """

    def _install(summary: CaseSummary):
        async def fake(**kwargs):
            return summary, llm.LLMTrace(stage="intake_parse", model="fake", source="fake")

        monkeypatch.setattr(llm, "chat_json", fake)

    return _install


def _summary(**kwargs) -> CaseSummary:
    base = {
        "observed_facts": [],
        "parent_goals": [],
        "context": {},
        "already_tried": [],
        "missing_information": [],
        "retrieval_concepts": [],
        "risk_signals": [],
    }
    base.update(kwargs)
    return CaseSummary(**base)


async def _run(state: CoachState) -> dict:
    return await chat_nodes.parse_parent_input(state)


# ------------------------------------------------------------------ 保留
@pytest.mark.asyncio
async def test_restated_fact_is_kept(fake_llm):
    """家长这轮又亲口说了同一句 —— 那是当前事实，不能丢。"""
    fake_llm(_summary(observed_facts=[DELETED]))
    state: CoachState = {
        "user_message": DELETED,
        "memory_forbidden_facts": [DELETED],
    }
    out = await _run(state)
    assert DELETED in out["case_summary"]["observed_facts"]
    assert "按最新说法保留" in out["trace_metadata"]["intake_parse"]


@pytest.mark.asyncio
async def test_restated_fact_survives_even_with_old_copy(fake_llm):
    """累积的旧事实和本轮新抽的**都**是那句 —— 也要留下，且不重复。"""
    fake_llm(_summary(observed_facts=[DELETED]))
    state: CoachState = {
        "user_message": DELETED,
        "case_summary": {"observed_facts": [DELETED]},
        "memory_forbidden_facts": [DELETED],
    }
    out = await _run(state)
    facts = out["case_summary"]["observed_facts"]
    assert facts.count(DELETED) == 1


# ------------------------------------------------------------------ 丢掉
@pytest.mark.asyncio
async def test_unmentioned_forbidden_fact_is_dropped(fake_llm):
    """家长没提，模型却抽出来了 —— 那是要作废的，丢掉。"""
    fake_llm(_summary(observed_facts=[DELETED]))
    state: CoachState = {
        "user_message": "孩子今天不太想去幼儿园。",
        "memory_forbidden_facts": [DELETED],
    }
    out = await _run(state)
    assert DELETED not in out["case_summary"]["observed_facts"]
    assert "过滤掉 1 条已作废内容" in out["trace_metadata"]["intake_parse"]


@pytest.mark.asyncio
async def test_stale_accumulated_fact_is_dropped(fake_llm):
    """上一轮累积下来的作废事实，也不能带进这一轮。"""
    fake_llm(_summary())
    state: CoachState = {
        "user_message": "孩子今天不太想去幼儿园。",
        "case_summary": {"observed_facts": [DELETED]},
        "memory_forbidden_facts": [DELETED],
    }
    out = await _run(state)
    assert DELETED not in out["case_summary"]["observed_facts"]


# ------------------------------------------------------------------ 不许漏
@pytest.mark.asyncio
async def test_forbidden_fact_does_not_leak_into_other_fields(fake_llm):
    """作废内容不能从别的字段漏出去。

    实测漏过 ``retrieval_concepts`` —— 被 superseded 的"换过老师"
    直接进了检索 query。
    """
    fake_llm(
        _summary(
            retrieval_concepts=["孩子最近换过老师，可能影响其情绪。"],
            already_tried=[DELETED],
            missing_information=[DELETED],
            context={"上学": DELETED},
        )
    )
    state: CoachState = {
        "user_message": "孩子今天不太想去幼儿园。",
        "memory_forbidden_facts": [DELETED, "孩子最近换过老师，可能影响其情绪。"],
    }
    out = await _run(state)
    summary = out["case_summary"]
    assert summary["retrieval_concepts"] == []
    assert summary["already_tried"] == []
    assert summary["missing_information"] == []
    assert summary["context"] == {}


@pytest.mark.asyncio
async def test_risk_signals_are_never_filtered(fake_llm):
    """风险信号绝不能因为"事实被作废"被一起丢掉。

    家长作废的是一条事实，不是这条事实里可能藏着的风险 ——
    风险信号漏一次就可能漏掉"该找专业人士"。
    """
    fake_llm(_summary(risk_signals=[DELETED]))
    state: CoachState = {
        "user_message": "孩子今天不太想去幼儿园。",
        "memory_forbidden_facts": [DELETED],
    }
    out = await _run(state)
    assert out["case_summary"]["risk_signals"] == [DELETED]
