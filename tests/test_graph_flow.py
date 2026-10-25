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
from rdi_agent.graph.nodes import (
    chat_nodes,
    memory_nodes,
    retrieval_nodes,
    write_nodes,
)
from rdi_agent.graph.state import CoachState
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    FollowupQuestion,
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.conversation import LimitedAnswer, SafetyReviewCheck
from rdi_agent.schemas.evidence import EvidenceDecision
from rdi_agent.schemas.feedback import FeedbackAnalysis
from rdi_agent.schemas.memory import MemoryExtractionResult
from rdi_agent.schemas.recommendation import (
    ParentAction,
    RecommendationCard,
    SourceCitation,
)

CID = "00000000-0000-0000-0000-00000000000a"

# 检索节点要连库、要调 embedding。流程测试不碰这两样，
# 所以整段检索被替身顶掉（见 no_retrieval fixture）。
EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"
EVIDENCE_SUMMARY = "the guided participation is a way of interacting with the child"


def _card(plan_id: str, recommended: bool) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["早上出门会哭"],
        explanation="先缩短难受的时长。",
        parent_actions=[ParentAction(action="提前十分钟出门", context="早上")],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没变化就停下重新评估"],
        application_label="source_summary",
        source_citations=[
            SourceCitation(
                source_id=EVIDENCE_ID,
                source_type="guideline",
                title="Guided Participation",
                chapter="Guided Participation",
                pdf_page_start=154,
                pdf_page_end=155,
                claim_supported="书里讲的是 guided participation",
            )
        ],
        uncertainty_notes=["还不确定原因"],
        recommended=recommended,
    )


def _bundle() -> dict:
    """一份"有一条可用证据"的证据包。"""
    return {
        "original_question": "孩子 4 岁",
        "case_summary": {},
        "evidence": [
            {
                "evidence_id": EVIDENCE_ID,
                "source_type": "guideline",
                "title": "Guided Participation",
                "section_title": "Guided Participation",
                "pdf_page_start": 154,
                "pdf_page_end": 155,
                "print_page_start": None,
                "print_page_end": None,
                "summary": EVIDENCE_SUMMARY,
                "source_excerpt": EVIDENCE_SUMMARY,
                "relevance_reason": "向量召回第 1 名；关键词召回第 1 名",
                "limitations": [],
                "review_status": "unreviewed",
                "may_quote_to_user": False,
                "similarity": 0.61,
                "fts_matched": True,
            }
        ],
        "evidence_gaps": [],
        "retrieval_version": "2c-v1",
    }


_DIRECT = EvidenceDecision(
    evidence_status="direct",
    supported_claims=["书里讲的是引导式参与"],
    unsupported_claims=[],
    missing_information=[],
    can_support_personalized_advice=True,
    high_risk=False,
    needs_followup=False,
    decision="recommend",
)

_NO_EVIDENCE = EvidenceDecision(
    evidence_status="none",
    supported_claims=[],
    unsupported_claims=[],
    missing_information=["知识库里没有对应内容"],
    can_support_personalized_advice=False,
    high_risk=False,
    needs_followup=False,
    decision="limited_answer",
)

_SAFETY_OK = SafetyReviewCheck(passed=True, severity="none", concerns=[])

_LIMITED = LimitedAnswer(
    answer="手上没有书里对应的内容，先记一记情况。",
    low_risk_actions=["把发生的时间和当时的情形记下来"],
    observe=["他有没有主动靠近你"],
    when_to_seek_professional_help=["情绪持续剧烈、明显抗拒时"],
    knowledge_gap_note="这次知识库里没有对应的内容，所以没有给出书里的结论。",
)


class FakeLLM:
    """按 model_cls 返回预置结果，并记录每个 stage 被调用了几次。

    同一契约可以给一个列表，表示"第 1 次返回这个、第 2 次返回那个"——
    用来模拟状态变化（比如第一轮说信息不足、第二轮说够了）。
    给单个值则每次都返回它。
    """

    # 阶段 2C 之后，图上多了证据门控和安全复核两个要调模型的节点。
    # 这两个的"正常答案"是固定的，脚本里没写就用默认 ——
    # 免得每加一个用例都要记得补两行。
    DEFAULTS: dict[type, object] = {}

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
            payload = self.DEFAULTS.get(model_cls)
        if payload is None:
            raise AssertionError(f"{stage} 没有预置答案")
        if isinstance(payload, list):
            i = self._cursor.get(model_cls, 0)
            # 到末尾就停在最后一个，避免"预置不够"直接 IndexError
            payload = payload[min(i, len(payload) - 1)]
            self._cursor[model_cls] = i + 1
        return payload, llm.LLMTrace(stage=stage, model="fake", source="fake")


FakeLLM.DEFAULTS = {
    EvidenceDecision: _DIRECT,
    SafetyReviewCheck: _SAFETY_OK,
    LimitedAnswer: _LIMITED,
    # 阶段 3：默认"这一轮什么也没记"。个别用例要测记忆时自己覆盖。
    MemoryExtractionResult: MemoryExtractionResult(candidates=[], notes=[]),
}


@pytest.fixture(autouse=True)
def no_memory(monkeypatch):
    """流程测试不碰长期记忆。

    记忆节点要连 Postgres、要调模型，而这个文件测的是**图的走向和
    中断/恢复语义**。记忆本身的行为由 ``tests/test_memory_*.py`` 覆盖。

    **autouse 是有意的**：三个记忆节点现在夹在主链路上，
    每加一个新流程用例都要记得关掉它 —— 迟早会漏。
    """

    async def fake_extract(state: CoachState) -> dict:
        return {"memory_candidates": [], "memory_notes": []}

    async def fake_merge(state: CoachState) -> dict:
        return {"memory_update_result": {}}

    async def fake_commit(state: CoachState) -> dict:
        return {}

    async def fake_load(**kwargs):
        return {
            "child_context_patch": {},
            "memory_context": {},
            "forbidden_facts": [],
        }

    monkeypatch.setattr(memory_nodes, "extract_memories", fake_extract)
    monkeypatch.setattr(memory_nodes, "merge_memories", fake_merge)
    monkeypatch.setattr(memory_nodes, "commit_memory", fake_commit)
    monkeypatch.setattr(
        chat_nodes.memory_service, "load_memory_context", fake_load
    )


@pytest.fixture
def patched_llm(monkeypatch):
    def _install(script: dict[type, object]) -> FakeLLM:
        fake = FakeLLM(script)
        monkeypatch.setattr(llm, "chat_json", fake)
        return fake

    return _install


@pytest.fixture
def no_retrieval(monkeypatch):
    """把整段检索换成替身。

    检索要连 Postgres、要调 embedding API —— 流程测试不碰这两样。
    检索本身的行为由 ``tests/test_retrieval_2c.py`` 覆盖。
    """

    async def fake_rewrite(state):
        return {
            "retrieval_queries": {
                "original_query": state.get("user_message", ""),
                "semantic_query": state.get("user_message", ""),
                "keyword_query": state.get("user_message", ""),
                "concept_ids": [],
                "filters": {},
                "bridged_terms": [],
            }
        }

    async def fake_retrieve(state):
        return {"retrieval_candidates": []}

    async def fake_rerank(state):
        return {"evidence_bundle": _bundle()}

    monkeypatch.setattr(retrieval_nodes, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(retrieval_nodes, "retrieve_knowledge", fake_retrieve)
    monkeypatch.setattr(retrieval_nodes, "rerank_and_expand", fake_rerank)


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

    async def fake_persist_bundle(state: CoachState) -> dict:
        return {"evidence_bundle_db_id": "bundle_fake"}

    monkeypatch.setattr(
        write_nodes, "persist_evidence_bundle", fake_persist_bundle
    )
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
async def test_insufficient_info_triggers_followup(patched_llm, no_writes, no_retrieval):
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁，上幼儿园前会哭"]),
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["不知道哭持续多久"]
            ),
            FollowupQuestions: FollowupQuestions(
                questions=[
                    FollowupQuestion(question="哭大概持续多久？", options=["几分钟", "半小时以上"]),
                    FollowupQuestion(question="以前好转过吗？"),
                ]
            ),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(_state("孩子 4 岁，上幼儿园前会哭"), config=_cfg())

    payload = _interrupt(result)
    assert payload is not None, "应该在追问处停下"
    assert payload["kind"] == "followup"
    assert 1 <= len(payload["questions"]) <= 3


@pytest.mark.asyncio
async def test_enough_info_skips_followup(patched_llm, no_writes, no_retrieval):
    """信息够就直奔方案，中间不追问。"""
    fake = patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁", "早上出门哭五分钟"]),
            SufficiencyDecision: _SUFFICIENT,
            EvidenceDecision: _DIRECT,
            SafetyReviewCheck: _SAFETY_OK,
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
async def test_answer_resumes_from_the_same_interrupt(
    patched_llm, no_writes, no_retrieval
):
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
            EvidenceDecision: _DIRECT,
            SafetyReviewCheck: _SAFETY_OK,
            FollowupQuestions: FollowupQuestions(
                questions=[FollowupQuestion(question="哭多久？", options=["几分钟"])]
            ),
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
async def test_followup_stops_after_one_round(patched_llm, no_writes, no_retrieval):
    """只允许追问**一轮**。家长答完就必须出方案，不能再问第二遍。

    模型永远说"不够" —— 靠 followup_rounds 兜底。
    """
    fake = patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["还是不清楚"]),
            # 模型永远说"不够" —— 靠 followup_rounds 兜底
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["还缺"]
            ),
            FollowupQuestions: FollowupQuestions(
                questions=[FollowupQuestion(question="还有呢？")]
            ),
            RecommendationSet: RecommendationSet(plans=[_card("plan_1", True)]),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = _cfg()

    # 第 1 轮：进图即停在追问
    await graph.ainvoke(_state("还是不清楚"), config=cfg)
    assert _count_followups(fake) == 1

    # 答完这一轮就该出方案，而不是再问一轮
    result = await graph.ainvoke(Command(resume="还是不清楚"), config=cfg)
    assert _interrupt(result)["kind"] == "plan_selection"
    assert _count_followups(fake) == 1, "答完一轮还在追问"


@pytest.mark.asyncio
async def test_asked_questions_are_passed_to_prompt(patched_llm, no_writes, no_retrieval):
    """问过的问题要出现在 prompt 里，模型才知道该避开哪些。

    **为什么**：模型只看得到 case_summary 和 priority_questions，
    看不到"上一轮问过什么"。它不知道某件事已经问过，就会换个说法再问一遍
    （实测"在园是否有不愉快的经历"连问两次就是这么来的）。
    """
    seen: list[dict] = []

    async def fake_chat_json(**kwargs):
        if kwargs["model_cls"] is SufficiencyDecision:
            seen.append(kwargs["user"])
        payload = FakeLLM.DEFAULTS.get(kwargs["model_cls"])
        if payload is None:
            payload = fake.script.get(kwargs["model_cls"])
        if payload is None:
            raise AssertionError(f"{kwargs['stage']} 没有预置答案")
        if isinstance(payload, list):
            i = getattr(fake_chat_json, "_i", 0)
            fake_chat_json._i = i + 1
            payload = payload[min(i, len(payload) - 1)]
        return payload, llm.LLMTrace(stage=kwargs["stage"], model="fake", source="fake")

    fake = patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁，上幼儿园前会哭"]),
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["缺细节"]
            ),
            FollowupQuestions: FollowupQuestions(
                questions=[FollowupQuestion(question="在园有不开心的经历吗？")]
            ),
            EvidenceDecision: _DIRECT,
            SafetyReviewCheck: _SAFETY_OK,
            RecommendationSet: RecommendationSet(plans=[_card("plan_1", True)]),
        }
    )
    import rdi_agent.graph.nodes.chat_nodes as chat_nodes

    real = chat_nodes.llm.chat_json
    chat_nodes.llm.chat_json = fake_chat_json
    try:
        graph = build_graph(checkpointer=InMemorySaver())
        cfg = _cfg()
        await graph.ainvoke(_state("上幼儿园前会哭"), config=cfg)
        await graph.ainvoke(
            Command(resume={"text": "没有", "answers": [
                {"question": "在园有不开心的经历吗？", "answer": "没有"},
            ]}),
            config=cfg,
        )
    finally:
        chat_nodes.llm.chat_json = real

    assert seen, "check_sufficiency 没被调到"
    assert "在园有不开心的经历吗？" in seen[-1], (
        f"问过的问题没进 prompt，模型会再问一遍：{seen[-1][:400]}"
    )


@pytest.mark.asyncio
async def test_answers_are_merged_into_case_summary(patched_llm, no_writes, no_retrieval):
    """家长明确答了的东西，必须**进** case_summary。

    **为什么**：实测模型会把整段问答丢掉 —— 家长答了"没有不愉快的经历"，
    抽出来的 observed_facts 里还是没有。答案一丢，模型就以为这件事
    没问过，换个说法再问一遍。所以答案由代码确定性并进去。
    """
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁，上幼儿园前会哭"]),
            SufficiencyDecision: SufficiencyDecision(
                requires_followup=True, reasons=["缺细节"]
            ),
            FollowupQuestions: FollowupQuestions(
                questions=[FollowupQuestion(question="在园有不开心的经历吗？")]
            ),
            EvidenceDecision: _DIRECT,
            SafetyReviewCheck: _SAFETY_OK,
            RecommendationSet: RecommendationSet(plans=[_card("plan_1", True)]),
        }
    )
    graph = build_graph(checkpointer=InMemorySaver())
    cfg = _cfg()

    await graph.ainvoke(_state("上幼儿园前会哭"), config=cfg)
    result = await graph.ainvoke(
        Command(resume={"text": "在园有不开心的经历吗？\n没有", "answers": [
            {"question": "在园有不开心的经历吗？", "answer": "没有"},
        ]}),
        config=cfg,
    )

    facts = result["case_summary"]["observed_facts"]
    assert any("没有" in f for f in facts), (
        f"家长的回答没进 observed_facts：{facts} —— "
        "答案一丢，模型就会换个说法再问一遍"
    )


def _count_followups(fake: FakeLLM) -> int:
    return fake.calls.count("followup")


# ------------------------------------------------------------------ 选方案
@pytest.mark.asyncio
async def test_select_plan_commits(patched_llm, no_writes, no_retrieval):
    """选完方案才写库；写入的正是选中的那张。"""
    patched_llm(
        {
            CaseSummary: CaseSummary(observed_facts=["4 岁"]),
            SufficiencyDecision: _SUFFICIENT,
            EvidenceDecision: _DIRECT,
            SafetyReviewCheck: _SAFETY_OK,
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

    做法：跑到方案选择中断，再 resume 一次 —— 恢复窗口里
    探针一次都不该被碰，只有 commit 该跑。

    **阶段 2C 之后中断前面多了几个调模型的节点**（证据门控、安全复核），
    所以探针是"上膛"式的：第一遍正常跑，第二遍（resume）才装探针。
    否则测的就不是恢复路径，而是"前半段流程有没有调模型"。

    `persist_candidate_plans` 也换掉：它在中断之前，本来就该写库，
    留着会让探针在分界点之前就炸，把正确行为当失败。
    """
    touched: list[str] = []
    armed = {"on": False}

    async def fake_llm(**kwargs):
        if armed["on"]:
            touched.append(f"llm:{kwargs['stage']}")
            raise AssertionError(f"恢复窗口里不该调 LLM：{kwargs['stage']}")
        model_cls = kwargs["model_cls"]
        payload = FakeLLM.DEFAULTS.get(model_cls)
        if payload is None:
            raise AssertionError(f"{kwargs['stage']} 没有预置答案")
        return payload, llm.LLMTrace(stage=kwargs["stage"], model="fake", source="fake")

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

    async def fake_rewrite(state):
        return {"retrieval_queries": {}}

    async def fake_retrieve(state):
        return {"retrieval_candidates": []}

    async def fake_rerank(state):
        return {"evidence_bundle": _bundle()}

    monkeypatch.setattr(llm, "chat_json", fake_llm)
    async def fake_persist_bundle(state: CoachState) -> dict:
        touched.append("persist_bundle")
        return {"evidence_bundle_db_id": "bundle_fake"}

    monkeypatch.setattr(write_nodes, "session_scope", boom_scope)
    monkeypatch.setattr(write_nodes, "commit_plan_selection", fake_commit)
    monkeypatch.setattr(
        write_nodes, "persist_evidence_bundle", fake_persist_bundle
    )
    monkeypatch.setattr(write_nodes, "persist_candidate_plans", fake_persist)
    monkeypatch.setattr(chat_nodes, "parse_parent_input", fake_parse)
    monkeypatch.setattr(chat_nodes, "check_sufficiency", fake_sufficiency)
    monkeypatch.setattr(chat_nodes, "build_recommendation", fake_build)
    monkeypatch.setattr(retrieval_nodes, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(retrieval_nodes, "retrieve_knowledge", fake_retrieve)
    monkeypatch.setattr(retrieval_nodes, "rerank_and_expand", fake_rerank)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "pure"}}

    # 第一次：跑到方案选择中断
    first = await graph.ainvoke(_state("x"), config=cfg)
    assert _interrupt(first) is not None, "没到中断点，探针断言就没意义了"
    assert _interrupt(first)["kind"] == "plan_selection"
    assert "persist" in touched, "中断前该落候选卡"
    assert "persist_bundle" in touched, "证据包也该在中断前落库"

    # 第二次：resume。present_plans 重跑，只有 commit 该有副作用。
    touched.clear()
    armed["on"] = True
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