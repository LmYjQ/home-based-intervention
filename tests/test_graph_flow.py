"""流程测试（计划 §8.4 最后一条：3 轮内走完 追问 → 方案 → 选择）。

用假 LLM 打桩：``chat_json`` 按 model_cls 返回预置答案，
这样测的是**图的走向和中断/恢复语义**，不是模型质量。
真模型好不好是阶段 2B 检索评测和阶段 4 的事。

这里不连数据库：两个写库节点被替换掉了，DB 写入本身由
``test_db_integration.py`` 和 ``test_repository.py`` 覆盖。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from rdi_agent import llm
from rdi_agent.graph.builder import build_graph
from rdi_agent.graph.nodes import chat_nodes, write_nodes
from rdi_agent.graph.state import CoachState
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.feedback import FeedbackAnalysis
from rdi_agent.schemas.recommendation import ParentAction, RecommendationCard

CID = "00000000-0000-0000-0000-00000000000a"


def _card(plan_id: str, recommended: bool) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["早上出门会哭"],
        explanation="先缩短难受的时长。",
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=recommended,
    )


class FakeLLM:
    """按 model_cls 返回预置结果，并记录每个 stage 被调用了几次。

    同一契约可以给一个列表，表示"第 1 次返回这个、第 2 次返回那个"——
    用来模拟状态变化（比如第一轮说信息不足、第二轮说够了）。
    给单个值则每次都返回它。
    """

    def __init__(self, script: dict[type, object]) -> None:
        self.script = script
        self.calls: list[str] = []
        self._cursor: dict[type, int] = {}

    async def __call__(self, **kwargs):
        # 必须是 async：节点写的是 `await llm.chat_json(...)`。
        # 用同步替身的话，await 一个 tuple 直接 TypeError。
        model_cls, stage = kwargs["model_cls"], kwargs["stage"]
        self.calls.append(stage)
        payload = self.script.get(model_cls)
        if payload is None:
            raise AssertionError(f"{stage} 没有预置答案")
        if isinstance(payload, list):
            i = self._cursor.get(model_cls, 0)
            # 到末尾就停在最后一个，避免"预置不够"直接 IndexError
            payload = payload[min(i, len(payload) - 1)]
            self._cursor[model_cls] = i + 1
        return payload, llm.LLMTrace(stage=stage, model="fake", source="fake")


@pytest.fixture
def patched_llm(monkeypatch):
    def _install(script: dict[type, object]) -> FakeLLM:
        fake = FakeLLM(script)
        monkeypatch.setattr(llm, "chat_json", fake)
        return fake

    return _install


@pytest.fixture
def no_writes(monkeypatch):
    """把两个写库节点换成空实现，让流程测试不碰数据库。"""
    seen: list[dict] = []

    async def fake_persist(state: CoachState) -> dict:
        seen.append(
            {
                "turn_id": state.get("turn_id"),
                "n_plans": len(state.get("candidate_plans") or []),
            }
        )
        return {"plan_db_ids": {}}

    async def fake_commit(state: CoachState) -> dict:
        seen.append({"committed": state.get("selected_plan_id")})
        return {
            "trial_id": "trial_fake",
            "selected_plan_db_id": "db_fake",
            "final_answer": "已经选定。",
        }

    monkeypatch.setattr(write_nodes, "persist_candidate_plans", fake_persist)
    monkeypatch.setattr(write_nodes, "commit_plan_selection", fake_commit)
    return seen


def _state(message: str, turn_id: str = "t1") -> CoachState:
    return {
        "entrypoint": "chat",
        "conversation_id": CID,
        "thread_id": CID,
        "family_id": "fam-1",
        "child_id": "00000000-0000-0000-0000-000000000001",
        "turn_id": turn_id,
        "user_message": message,
    }


def _cfg() -> dict:
    return {"configurable": {"thread_id": CID}}


def _interrupt(result: dict) -> dict | None:
    ints = result.get("__interrupt__")
    if not ints:
        return None
    value = ints[0].value
    return value if isinstance(value, dict) else None


_SUFFICIENT = SufficiencyDecision(requires_followup=False)


# ------------------------------------------------------------------ 追问
@pytest.mark.asyncio
async def test_insufficient_info_triggers_followup(patched_llm, no_writes):
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁，上幼儿园前会哭"]),
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["不知道哭持续多久"]
            ),
            FollowupQuestions: FollowupQuestions(questions=["哭大概持续多久？", "以前好转过吗？"]),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(_state("孩子 4 岁，上幼儿园前会哭"), config=_cfg())

    payload = _interrupt(result)
    assert payload is not None, "应该在追问处停下"
    assert payload["kind"] == "followup"
    assert 1 <= len(payload["questions"]) <= 3


@pytest.mark.asyncio
async def test_enough_info_skips_followup(patched_llm, no_writes):
    """信息够就直奔方案，中间不追问。"""
    fake = patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁", "早上出门哭五分钟"]),
            SufficiencyDecision: _SUFFICIENT,
            RecommendationSet: RecommendationSet(
                plans=[_card("plan_1", True), _card("plan_2", False)]
            ),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(_state("孩子 4 岁，早上出门哭五分钟"), config=_cfg())

    assert "followup" not in fake.calls
    assert _interrupt(result)["kind"] == "plan_selection"


@pytest.mark.asyncio
async def test_answer_resumes_from_the_same_interrupt(patched_llm, no_writes):
    """补充信息后从原中断点恢复：追问轮数 +1，然后出方案。

    SufficiencyDecision 给的是列表 —— 第一轮"不够"所以会追问，
    第二轮"够了"才出方案。如果两轮都返回"够"，这条用例就根本走不到
    追问中断，测的就不是恢复路径了。
    """
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁，上幼儿园前会哭"]),
            SufficiencyDecision: [
                SufficiencyDecision(requires_followup=True, reasons=["缺细节"]),
                _SUFFICIENT,
            ],
            FollowupQuestions: FollowupQuestions(questions=["哭多久？"]),
            RecommendationSet: RecommendationSet(
                plans=[_card("plan_1", True), _card("plan_2", False)]
            ),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = _cfg()

    first = await graph.ainvoke(_state("上幼儿园前会哭"), config=cfg)
    assert _interrupt(first)["kind"] == "followup", "第一轮应该停在追问"

    result = await graph.ainvoke(Command(resume="哭大概五分钟"), config=cfg)
    assert _interrupt(result)["kind"] == "plan_selection"
    # 追问轮数确实 +1 了
    assert result["followup_rounds"] == 1


@pytest.mark.asyncio
async def test_followup_stops_after_three_rounds(patched_llm, no_writes):
    """追问 3 轮还不齐就必须出方案，不能无限问下去。"""
    fake = patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["还是不清楚"]),
            # 模型永远说"不够" —— 靠 followup_rounds 兜底
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["还缺"]
            ),
            FollowupQuestions: FollowupQuestions(questions=["还有呢？"]),
            RecommendationSet: RecommendationSet(plans=[_card("plan_1", True)]),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = _cfg()

    # 第 1 轮：进图即停在追问
    await graph.ainvoke(_state("还是不清楚"), config=cfg)
    assert _count_followups(fake) == 1

    # 第 2、3 轮：各答一次
    for _ in range(2):
        await graph.ainvoke(Command(resume="还是不清楚"), config=cfg)
    assert _count_followups(fake) == 3

    # 第 3 轮答完就该出方案，而不是问第 4 次
    result = await graph.ainvoke(Command(resume="还是不清楚"), config=cfg)
    assert _interrupt(result)["kind"] == "plan_selection"
    assert _count_followups(fake) == 3


def _count_followups(fake: FakeLLM) -> int:
    return fake.calls.count("followup")


# ------------------------------------------------------------------ 选方案
@pytest.mark.asyncio
async def test_select_plan_commits(patched_llm, no_writes):
    """选完方案才写库；写入的正是选中的那张。"""
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁"]),
            SufficiencyDecision: _SUFFICIENT,
            RecommendationSet: RecommendationSet(
                plans=[_card("plan_1", True), _card("plan_2", False)]
            ),
        }
    )
    written = no_writes
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = _cfg()

    await graph.ainvoke(_state("孩子 4 岁"), config=cfg)
    # 候选卡先落库（proposed），这一步在中断之前
    assert written == [{"turn_id": "t1", "n_plans": 2}]

    result = await graph.ainvoke(Command(resume={"plan_id": "plan_2"}), config=cfg)
    assert result["selected_plan_id"] == "plan_2"
    assert result["trial_id"] == "trial_fake"
    assert written[-1] == {"committed": "plan_2"}


# ------------------------------------------------------------------ 纯函数断言
@pytest.mark.asyncio
async def test_present_plans_has_no_side_effects(monkeypatch):
    """恢复时 present_plans 会被重跑，那个窗口里不能有副作用。

    LangGraph 的 `interrupt` 语义：resume 的时候，含 `interrupt()` 的
    整个节点再执行一遍。所以带中断的节点里任何写库/调模型都会翻倍。

    做法：把中断**之前**的节点都换成干净的替身（它们该做的事已经由
    别的用例覆盖了），然后把 `llm.chat_json` 和 `write_nodes.session_scope`
    换成会抛异常的探针。跑到方案选择中断，再 resume 一次 ——
    探针一次都不该被碰，只有 commit 该跑。

    `persist_candidate_plans` 也换掉：它在中断之前，本来就该写库，
    留着会让探针在分界点之前就炸，把正确行为当失败。
    """
    touched: list[str] = []

    async def boom_llm(**kwargs):
        touched.append(f"llm:{kwargs['stage']}")
        raise AssertionError(f"恢复窗口里不该调 LLM：{kwargs['stage']}")

    def boom_scope(*a, **kw):
        touched.append("db")
        raise AssertionError("恢复窗口里不该碰数据库")

    async def fake_commit(state: CoachState) -> dict:
        touched.append("commit")
        return {"trial_id": "t", "selected_plan_db_id": "d", "final_answer": "好"}

    async def fake_persist(state: CoachState) -> dict:
        touched.append("persist")
        return {"plan_db_ids": {}}

    async def fake_parse(state: CoachState) -> dict:
        return {"case_summary": {}}

    async def fake_sufficiency(state: CoachState) -> dict:
        return {"sufficiency_decision": {"requires_followup": False}}

    async def fake_build(state: CoachState) -> dict:
        return {"candidate_plans": [_card("plan_1", True).model_dump(mode="json")]}

    monkeypatch.setattr(llm, "chat_json", boom_llm)
    monkeypatch.setattr(write_nodes, "session_scope", boom_scope)
    monkeypatch.setattr(write_nodes, "commit_plan_selection", fake_commit)
    monkeypatch.setattr(write_nodes, "persist_candidate_plans", fake_persist)
    monkeypatch.setattr(chat_nodes, "parse_parent_input", fake_parse)
    monkeypatch.setattr(chat_nodes, "check_sufficiency", fake_sufficiency)
    monkeypatch.setattr(chat_nodes, "build_recommendation", fake_build)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "pure"}}

    # 第一次：跑到方案选择中断
    first = await graph.ainvoke(_state("x"), config=cfg)
    assert _interrupt(first) is not None, "没到中断点，探针断言就没意义了"
    assert _interrupt(first)["kind"] == "plan_selection"
    assert touched == ["persist"], f"中断前只该落候选卡，实际：{touched}"

    # 第二次：resume。present_plans 重跑，只有 commit 该有副作用。
    touched.clear()
    await graph.ainvoke(Command(resume={"plan_id": "plan_1"}), config=cfg)

    assert touched == ["commit"], f"恢复期间除了 commit 不该有别的副作用：{touched}"


# ------------------------------------------------------------------ 反馈
@pytest.mark.asyncio
async def test_feedback_entry_runs_without_interrupt(patched_llm, monkeypatch):
    """反馈入口不中断，跑完就结束。"""
    from rdi_agent.graph.nodes import feedback_nodes

    seen: list[dict] = []

    async def fake_capture(state: CoachState) -> dict:
        seen.append(state["feedback"])
        return {
            "feedback": state["feedback"],
            "plan_title": "方案 plan_1",
            "feedback_created": True,
        }

    monkeypatch.setattr(feedback_nodes, "capture_feedback", fake_capture)
    fake = patched_llm(
        {
            FeedbackAnalysis: FeedbackAnalysis(
                actual_execution="提前十分钟出门，不催他",
                observed_changes=["哭的时间变短"],
                relation_to_plan="这一次哭的时间变短，和上次出发前十分钟的场景放在一起看……",
            )
        }
    )
    # 注意是 _async_value({...}) 不是 lambda: _async_value(...)。
    # 写成 lambda 会把"返回 dict 的函数"当节点传给 StateGraph，
    # 图就会把函数的返回值当 state 更新 —— 报 "Expected dict, got function"。
    monkeypatch.setattr(
        feedback_nodes,
        "next_step",
        _async_value(
            {
                "next_step": {"acknowledgment": "辛苦了"},
                "final_answer": "辛苦了\n这周继续观察",
            }
        ),
    )

    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {
            "entrypoint": "feedback",
            "conversation_id": CID,
            "thread_id": "fb-1",
            "family_id": "fam-1",
            "child_id": "00000000-0000-0000-0000-000000000001",
            "turn_id": "fb_t1",
            "feedback": {"trial_id": "trial-1", "turn_id": "fb_t1", "attempted": True},
        },
        config={"configurable": {"thread_id": "fb-1"}},
    )

    assert _interrupt(result) is None
    assert fake.calls == ["feedback_analyze"]
    assert result["final_answer"].startswith("辛苦了")
    assert len(seen) == 1


def _async_value(value: dict):
    async def _f(_state):
        return value

    return _f