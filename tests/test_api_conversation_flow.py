"""API 层的会话流程集成测试。

**为什么单独一个文件**：`tests/test_graph_flow.py` 是直接调图、用
`Command(resume=...)` 恢复的 —— 它测的是图，不是 API。而 API 层一度用
普通 dict 调 `ainvoke`，那**不会恢复中断**，只会让图从 START 重跑：
`await_user_answer` 永远跑不完，`followup_rounds` 永远是 0，
"追问 3 轮就收手"的兜底形同虚设，`commit_plan_selection` 也永远不会执行
（trial 建不出来）。

图是对的，API 是错的 —— 所以这个 bug 能一路躲过全部单测。
这个文件从 API 入口走一遍，把那条路径钉住。

要跑它：先 ``docker compose -f deploy/docker-compose.yml up -d db``，
再 ``pytest -q tests/test_api_conversation_flow.py``。
数据库不可用时**跳过**而不是失败。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from rdi_agent import llm
from rdi_agent.api import conversations as api
from rdi_agent.db.engine import dispose_engine, get_engine
from rdi_agent.db.models import Trial
from rdi_agent.graph.nodes import retrieval_nodes
from rdi_agent.graph.nodes.chat_nodes import MAX_FOLLOWUP_ROUNDS
from rdi_agent.graph.runtime import setup_graph, shutdown_graph
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    FollowupQuestion,
    FollowupQuestions,
    LimitedAnswer,
    RecommendationSet,
    SafetyReviewCheck,
    SufficiencyDecision,
)
from rdi_agent.schemas.evidence import EvidenceDecision
from rdi_agent.schemas.memory import MemoryExtractionResult
from rdi_agent.schemas.recommendation import (
    ParentAction,
    RecommendationCard,
    SourceCitation,
)


EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"
EVIDENCE_SUMMARY = "the guided participation is a way of interacting with the child"


def _bundle() -> dict:
    """一份"有一条可用证据"的证据包（检索节点被替身顶掉了）。"""
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
                "relevance_reason": "向量召回第 1 名",
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


@pytest.fixture
def no_retrieval(monkeypatch):
    """把检索链换成替身。

    检索要连库调 embedding —— 这个文件测的是**中断恢复**，
    检索的行为由 ``tests/test_retrieval_2c.py`` 覆盖。

    **这个 fixture 必须排在 ``graph`` 前面**：
    ``build_graph()`` 会把节点函数捕获进图里，之后再改模块属性对
    已经编译好的图不起作用 —— 检索替身就装不上，图会真的去连库。
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
async def db():
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        await dispose_engine()
        pytest.skip(f"Postgres 不可用，跳过集成测试：{type(e).__name__}: {e}")
    yield
    await dispose_engine()


@pytest.fixture
async def graph(db):
    """真图 + 真 Checkpointer（Postgres）。

    用 InMemorySaver 测不出这个 bug：进程内的 saver 行为跟 Postgres 一致，
    但这里要的就是"同一 thread 反复 invoke"这条真实路径。
    """
    await setup_graph()
    try:
        yield
    finally:
        await shutdown_graph()


def _card(plan_id: str, recommended: bool) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["4 岁，早上出门哭"],
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


@pytest.fixture
def always_wants_more(monkeypatch):
    """模型永远说"信息还不够"。

    这样只有图自己的 `MAX_FOLLOWUP_ROUNDS` 兜底能让它停下来 ——
    兜底要是没生效，这个用例就永远走不到方案选择，会直接超时失败。
    """

    async def fake_chat_json(**kwargs):
        model_cls = kwargs["model_cls"]
        if model_cls is CaseSummary:
            payload = CaseSummary(observed_facts=["4 岁，早上出门哭"])
        elif model_cls is SufficiencyDecision:
            payload = SufficiencyDecision(
                requires_followup=True, reasons=["还缺信息"]
            )
        elif model_cls is FollowupQuestions:
            payload = FollowupQuestions(
                questions=[FollowupQuestion(question="还缺什么？", options=["不清楚"])]
            )
        elif model_cls is RecommendationSet:
            payload = RecommendationSet(
                plans=[_card("plan_1", True), _card("plan_2", False)]
            )
        elif model_cls is EvidenceDecision:
            payload = EvidenceDecision(
                evidence_status="direct",
                supported_claims=["书里讲的是引导式参与"],
                unsupported_claims=[],
                missing_information=[],
                high_risk=False,
                needs_followup=False,
            )
        elif model_cls is SafetyReviewCheck:
            payload = SafetyReviewCheck(passed=True, severity="none", concerns=[])
        elif model_cls is MemoryExtractionResult:
            # 这个用例测的是追问恢复语义，不测记忆写了什么。
            payload = MemoryExtractionResult(candidates=[], notes=[])
        elif model_cls is LimitedAnswer:
            payload = LimitedAnswer(
                answer="手上没有书里对应的内容。",
                low_risk_actions=["把情况记下来"],
                observe=["他有没有主动靠近你"],
                when_to_seek_professional_help=["情绪持续剧烈时"],
                knowledge_gap_note="这次没有书里的依据。",
            )
        else:
            raise AssertionError(f"没预置答案：{kwargs.get('stage')}")
        return payload, llm.LLMTrace(stage=kwargs.get("stage", ""), source="fake")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)


async def _new_conversation() -> str:
    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label="4 岁男孩", age_band="3-4")
    )
    return conv["conversation_id"]


async def _send_until_plan_selection(cid: str) -> dict:
    """发第一条消息，然后按配置的追问上限把问答完，直到出方案。

    轮数跟着 ``MAX_FOLLOWUP_ROUNDS`` 走，不写死 —— 调那个常量
    不该让这个文件一起变红。
    """
    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    turn = 1
    while res["status"] == "awaiting_followup":
        turn += 1
        assert turn <= MAX_FOLLOWUP_ROUNDS + 1, (
            f"追问兜底没生效：已经问了 {turn} 轮还没出方案"
        )
        res = await api.send_message(
            cid,
            api.SendMessageRequest(message=f"补充信息 {turn}", turn_id=f"t{turn}"),
        )
    return res


@pytest.mark.asyncio
async def test_followup_resume_increments_rounds(no_retrieval, graph, always_wants_more):
    """回答追问必须**恢复**中断，不能让图从 START 重跑。

    断言两件事：
    1. 回答之后 `followup_rounds` 真的 +1（重跑的话它永远是 0）；
    2. 兜底生效：答满 `MAX_FOLLOWUP_ROUNDS` 轮就强制出方案，不再继续问。
    """
    from rdi_agent.graph.runtime import config_for, get_graph

    cid = await _new_conversation()

    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    assert res["status"] == "awaiting_followup"

    # 每答一轮，followup_rounds 都要 +1；到上限就强制出方案。
    # 如果图是从 START 重跑而不是 resumes 中断，这个计数会一直是 0。
    for round_no in range(1, MAX_FOLLOWUP_ROUNDS + 1):
        res = await api.send_message(
            cid,
            api.SendMessageRequest(
                message=f"第 {round_no + 1} 轮补充信息",
                turn_id=f"t{round_no + 1}",
            ),
        )
        state = await get_graph().aget_state(config_for(cid))
        assert state.values.get("followup_rounds") == round_no, (
            f"答完第 {round_no} 轮追问后 followup_rounds 应该是 {round_no}，"
            f"实际 {state.values.get('followup_rounds')} —— "
            "多半是图被重跑了而不是从中断恢复"
        )

    assert res["status"] == "awaiting_plan_selection", (
        "到追问上限后应该强制出方案，实际状态："
        f"{res['status']} —— MAX_FOLLOWUP_ROUNDS 兜底没生效"
    )
    assert len(res["plans"]) == 2


@pytest.mark.asyncio
async def test_followup_answers_carry_question_text(no_retrieval, graph, always_wants_more):
    """一轮追问可以带多个问题的回答，一起提交。

    **为什么要带原问题**：图里 `parse_parent_input` 只吃一段文本。
    不带问题的话，"在园更自在"和"换过老师"两段回答会互相串，
    下游抽出来的事实就分不清哪句对应哪个问题了。
    """
    from rdi_agent.graph.runtime import config_for, get_graph

    cid = await _new_conversation()
    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    assert res["status"] == "awaiting_followup"

    # 家长逐题作答，最后一起提交
    res = await api.send_message(
        cid,
        api.SendMessageRequest(
            message="",
            turn_id="t2",
            answers=[
                api.FollowupAnswer(question="在园更自在吗？", answer="在家更自在"),
                api.FollowupAnswer(question="换过老师吗？", answer="换过老师"),
            ],
        ),
    )
    state = await get_graph().aget_state(config_for(cid))
    user_message = state.values.get("user_message") or ""
    assert "在园更自在吗？" in user_message, (
        f"回答里要带上原问题，实际 user_message={user_message!r}"
    )
    assert "在家更自在" in user_message
    assert "换过老师吗？" in user_message
    assert "换过老师" in user_message


@pytest.mark.asyncio
async def test_answers_reach_case_summary(no_retrieval, graph, always_wants_more):
    """家长答的内容必须进 case_summary（走 API 入口）。

    **为什么这条在 API 层测**：答案的传递要经过
    ``_compose_answer_text``（拼文本）和 ``_structured_answers``（结构化）
    两条路，图层单测测不到中间这一段。

    实测模型会把整段问答丢掉 —— 家长答了"没有不愉快的经历"，
    抽出来的 observed_facts 里还是没有。答案一丢，模型就以为这件事
    没问过，换个说法再问一遍。结构化那条路就是为此存在的。
    """
    from rdi_agent.graph.runtime import config_for, get_graph

    cid = await _new_conversation()
    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    assert res["status"] == "awaiting_followup"

    res = await api.send_message(
        cid,
        api.SendMessageRequest(
            message="",
            turn_id="t2",
            answers=[
                api.FollowupAnswer(
                    question="在园有不开心的经历吗？", answer="没有这种情况"
                ),
            ],
        ),
    )
    state = await get_graph().aget_state(config_for(cid))
    facts = (state.values.get("case_summary") or {}).get("observed_facts") or []
    assert any("没有这种情况" in f for f in facts), (
        f"家长的回答没进 case_summary：{facts}"
    )


@pytest.mark.asyncio
async def test_followup_questions_carry_options(no_retrieval, graph, always_wants_more):
    """接口要把每题的选项一起返回给前端。

    前端要靠这个渲染点选按钮 —— 少了它就只剩自由输入了。
    """
    cid = await _new_conversation()
    res = await api.send_message(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    assert res["status"] == "awaiting_followup"
    assert res["questions"], "追问时应该返回问题列表"
    first = res["questions"][0]
    assert "question" in first
    assert "options" in first
    assert first["options"], "测试替身配了选项，接口要原样带出来"


@pytest.mark.asyncio
async def test_select_plan_creates_trial(no_retrieval, graph, always_wants_more):
    """选方案必须真的建出 trial。

    普通 dict 调 `ainvoke` 的话，`present_plans` 的中断不会被恢复，
    `commit_plan_selection` 就不执行 —— trial 建不出来，`trial_id` 是 null。
    """
    cid = await _new_conversation()

    res = await _send_until_plan_selection(cid)
    assert res["status"] == "awaiting_plan_selection"

    plan_id = next(p["plan_id"] for p in res["plans"] if p.get("recommended"))
    sel = await api.select_plan(
        cid, api.SelectPlanRequest(plan_id=plan_id, turn_id="t-select")
    )

    assert sel["status"] == "completed"
    assert sel["selected_plan_id"] == plan_id
    assert sel["trial_id"], "trial_id 是空的 —— commit_plan_selection 没跑"
    assert sel["selected_plan_db_id"]

    # trial 真的落库了
    from rdi_agent.db.repositories.case import Repository
    from rdi_agent.db.engine import session_scope

    async with session_scope() as session:
        trial = await Repository(session).get_trial(sel["trial_id"])
    assert trial is not None
    assert trial.status == "accepted"
    assert isinstance(trial, Trial)


@pytest.mark.asyncio
async def test_select_plan_is_idempotent(no_retrieval, graph, always_wants_more):
    """重发同一次选择，不该建出第二条 trial。"""
    cid = await _new_conversation()

    res = await _send_until_plan_selection(cid)
    plan_id = next(p["plan_id"] for p in res["plans"] if p.get("recommended"))

    first = await api.select_plan(
        cid, api.SelectPlanRequest(plan_id=plan_id, turn_id="t-select")
    )
    second = await api.select_plan(
        cid, api.SelectPlanRequest(plan_id=plan_id, turn_id="t-select")
    )
    assert first["trial_id"] == second["trial_id"], "重发选了两次，建了两条 trial"

    from rdi_agent.db.engine import session_scope
    from rdi_agent.db.repositories.case import Repository

    async with session_scope() as session:
        trials = await Repository(session).list_plans(cid)
    assert trials is not None
