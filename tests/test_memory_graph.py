"""记忆节点接进图之后的行为（不连库）。

这里测的是**降级和接线**，不是记忆规则本身：

- ``load_context`` 把记忆装进 ``child_context``，读不出来就退回薄上下文
- ``extract_memories`` 失败只留一条 note，不抛异常
- ``parse_parent_input`` 会滤掉本轮已作废的旧事实（验收第 2 条）
- ``check_sufficiency`` 的 prompt 里真的有已知事实（验收第 1 条）
"""

from __future__ import annotations

import json

import pytest

from rdi_agent import llm
from rdi_agent.graph.nodes import chat_nodes, memory_nodes
from rdi_agent.memory import service as memory_service
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import SufficiencyDecision


def _state(**extra):
    base = {
        "entrypoint": "chat",
        "child_id": "00000000-0000-0000-0000-000000000001",
        "conversation_id": "conv_x",
        "turn_id": "t1",
        "user_message": "孩子今年五岁了",
    }
    base.update(extra)
    return base


class TestLoadContext:
    async def test_memory_is_injected(self, monkeypatch):
        async def fake_load(**_kwargs):
            return {
                "child_context_patch": {
                    "confirmed_facts": ["孩子 5 岁"],
                    "preferences": ["不希望用药"],
                    "memories": {"observations": [], "hypotheses": []},
                },
                "memory_context": {"confirmed_facts": ["孩子 5 岁"]},
                "forbidden_facts": ["孩子 4 岁"],
            }

        monkeypatch.setattr(memory_service, "load_memory_context", fake_load)
        result = await chat_nodes.load_context(_state())
        assert result["child_context"]["confirmed_facts"] == ["孩子 5 岁"]
        assert result["memory_forbidden_facts"] == ["孩子 4 岁"]

    async def test_db_failure_degrades_instead_of_raising(self, monkeypatch):
        async def boom(**_kwargs):
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr(memory_service, "load_memory_context", boom)
        result = await chat_nodes.load_context(_state())
        assert result["memory_context"]["degraded"] is True
        assert "数据库挂了" in result["memory_context"]["error"]
        # 薄上下文还在 —— 家长的回合不能因为记忆读不出来就中断
        assert "child_context" in result


class TestExtractNode:
    async def test_llm_failure_does_not_break_the_turn(self, monkeypatch):
        async def boom(**_kwargs):
            raise TimeoutError("模型超时")

        monkeypatch.setattr(memory_service, "extract_candidates", boom)
        result = await memory_nodes.extract_memories(_state())
        assert result["memory_candidates"] == []
        assert any("抽取失败" in note for note in result["memory_notes"])

    async def test_debug_branch_skips_memory(self):
        result = await memory_nodes.extract_memories(_state(debug=True))
        assert result["memory_candidates"] == []


class TestForbiddenFacts:
    async def test_superseded_fact_is_filtered_from_case_summary(self, monkeypatch):
        async def fake_chat_json(**kwargs):
            return (
                CaseSummary(observed_facts=["孩子 4 岁"]),
                llm.LLMTrace(stage="intake_parse", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        state = _state(
            case_summary={"observed_facts": ["孩子 4 岁", "早上出门会哭"]},
            followup_answers=[],
            memory_forbidden_facts=["孩子 4 岁"],
        )
        result = await chat_nodes.parse_parent_input(state)
        facts = result["case_summary"]["observed_facts"]
        assert "孩子 4 岁" not in facts
        assert "早上出门会哭" in facts
        assert "过滤掉" in result["trace_metadata"]["intake_parse"]

    async def test_followup_answer_lines_are_not_filtered(self, monkeypatch):
        """追问的回答是这一轮新说的，不该被"作废清单"误伤。"""

        async def fake_chat_json(**kwargs):
            return (
                CaseSummary(observed_facts=[]),
                llm.LLMTrace(stage="intake_parse", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        state = _state(
            case_summary={"observed_facts": []},
            followup_answers=[
                {"question": "孩子几岁？", "answer": "孩子 4 岁"}
            ],
            memory_forbidden_facts=["孩子 4 岁"],
        )
        facts = (await chat_nodes.parse_parent_input(state))["case_summary"][
            "observed_facts"
        ]
        assert any("孩子 4 岁" in fact for fact in facts)


class TestSufficiencyPrompt:
    async def test_known_facts_reach_the_prompt(self, monkeypatch):
        captured: list[str] = []

        async def fake_chat_json(**kwargs):
            captured.append(kwargs["user"])
            return (
                SufficiencyDecision(requires_followup=False, reasons=["够了"]),
                llm.LLMTrace(stage="sufficiency", model="fake", source="fake"),
            )

        monkeypatch.setattr(llm, "chat_json", fake_chat_json)
        state = _state(
            case_summary={"observed_facts": ["早上出门会哭"]},
            child_context={
                "confirmed_facts": ["孩子 5 岁"],
                "preferences": ["不希望用药"],
                "known_contexts": ["在读幼儿园"],
                "memories": {"hypotheses": [{"content": "可能在分离时紧张"}]},
            },
        )
        await chat_nodes.check_sufficiency(state)

        payload = json.loads(captured[0])
        assert payload["known_facts"] == ["孩子 5 岁"]
        assert payload["known_preferences"] == ["不希望用药"]
        assert payload["known_contexts"] == ["在读幼儿园"]
        assert payload["active_hypotheses"][0]["content"] == "可能在分离时紧张"
