"""SSE 流式端点的测试（方案 A：流进度，不流半截答案）。

**这个文件测什么**：

1. ``/messages/stream`` 推的事件**顺序和内容**对不对
2. 流式和非流式**产出一样的结果** —— 这是最要紧的一条：
   手测时看到的东西必须和线上跑的是同一套逻辑
3. 进度事件**覆盖全部阶段** —— 少了哪一步，家长就有一段时间
   只能对着一个不动的转圈

**要跑它**：先 ``docker compose -f deploy/docker-compose.yml up -d db``，
再 ``pytest -q tests/test_api_stream_flow.py``。
数据库不可用时**跳过**而不是失败。

**为什么不用 mock 替身测进度**：进度事件是 ``llm.chat_json`` 里
打的点，而测试里 ``chat_json`` 被替身顶掉了 —— 替身不打点。
所以进度这条线单独测（直接调真实 LLM 出口的替身，看事件流），
流程那条线用替身测（只看阶段顺序对不对）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from rdi_agent import llm
from rdi_agent.api import conversations as api
from rdi_agent.config import reset_settings
from rdi_agent.db.engine import dispose_engine, get_engine
from rdi_agent.graph.nodes import retrieval_nodes
from rdi_agent.graph.runtime import setup_graph, shutdown_graph
from rdi_agent.progress import (
    STAGE_LABELS,
    ProgressEvent,
    emit as progress_emit,
    label_for,
    progress_channel,
)
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
from rdi_agent.schemas.recommendation import (
    ParentAction,
    RecommendationCard,
    SourceCitation,
)


EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"
EVIDENCE_SUMMARY = "the guided participation is a way of interacting with the child"


def _bundle() -> dict:
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
    """把检索链换成替身（理由同 test_api_conversation_flow.py）。"""

    async def fake_rewrite(state):
        # 替身也要打点：真的这三个节点会 emit（rewrite_query/retrieve/rerank）。
        # 不打的话，测"每个阶段都有进度"就会误报缺这三个。
        progress_emit("rewrite_query")
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
        progress_emit("retrieve")
        return {"retrieval_candidates": []}

    async def fake_rerank(state):
        progress_emit("rerank")
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
def scripted_llm(monkeypatch):
    """按 stage 返回预置答案。

    **和 ``always_wants_more`` 的区别**：那个永远说"信息不够"，
    只能测追问环；这个让图按剧本走完 —— 追问一轮就出方案，
    这样能测到**整条链上的所有阶段**。
    """
    state = {"sufficiency_calls": 0}

    async def fake_chat_json(**kwargs):
        payload = _scripted_payload(
            kwargs["model_cls"],
            kwargs.get("stage", ""),
            state,
        )
        return payload, llm.LLMTrace(stage=kwargs.get("stage", ""), source="fake")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return state


@pytest.fixture
def stateless_llm(monkeypatch):
    """永远说"信息够了"，直接出方案。

    **和 ``scripted_llm`` 的区别**：那个带计数器（第一次说"要追问"），
    计数器是**跨调用共享**的 —— 拿它跑两个会话，第二个会话会走到
    另一条分支上，两边就没法比了。

    这个没有状态：同一个输入永远同一个输出，
    拿它跑两遍才能验证"流式和非流式结果一样"。
    """
    async def fake_chat_json(**kwargs):
        payload = _scripted_payload(
            kwargs["model_cls"], kwargs.get("stage", ""), None
        )
        return payload, llm.LLMTrace(stage=kwargs.get("stage", ""), source="fake")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)


def _scripted_payload(model_cls, stage: str, state: dict | None):
    """按契约类型返回预置答案。``state`` 为 None 表示无状态模式。"""
    # 替身也要打进度点 —— 真的 ``chat_json`` 在每次调用开头
    # 会 emit 一次（llm.py 里那行）。替身不打的话，
    # 流式测试就测不到"阶段顺序对不对"，只能测到
    # 那些手动打点的阶段（rewrite_query/retrieve/...）。
    progress_emit(stage)
    if model_cls is CaseSummary:
        return CaseSummary(observed_facts=["4 岁，早上出门哭"])
    if model_cls is SufficiencyDecision:
        if state is None:
            # 无状态模式：一律说"够了"
            return SufficiencyDecision(
                requires_followup=False, reasons=["信息够了"]
            )
        # 有状态模式：第一次说"要追问"，之后说"够了" —— 让图进检索链。
        state["sufficiency_calls"] += 1
        if state["sufficiency_calls"] == 1:
            return SufficiencyDecision(
                requires_followup=True, reasons=["还缺信息"]
            )
        return SufficiencyDecision(requires_followup=False, reasons=["信息够了"])
    if model_cls is FollowupQuestions:
        return FollowupQuestions(
            questions=[FollowupQuestion(question="还缺什么？", options=["不清楚"])]
        )
    if model_cls is RecommendationSet:
        return RecommendationSet(
            plans=[_card("plan_1", True), _card("plan_2", False)]
        )
    if model_cls is EvidenceDecision:
        return EvidenceDecision(
            evidence_status="direct",
            supported_claims=["书里讲的是引导式参与"],
            unsupported_claims=[],
            missing_information=[],
            high_risk=False,
            needs_followup=False,
        )
    if model_cls is SafetyReviewCheck:
        return SafetyReviewCheck(passed=True, severity="none", concerns=[])
    if model_cls is LimitedAnswer:
        return LimitedAnswer(
            answer="手上没有书里对应的内容。",
            low_risk_actions=["把情况记下来"],
            observe=["他有没有主动靠近你"],
            when_to_seek_professional_help=["情绪持续剧烈时"],
            knowledge_gap_note="这次没有书里的依据。",
        )
    raise AssertionError(f"没预置答案：{stage}")


async def _new_conversation() -> str:
    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label="4 岁男孩", age_band="3-4")
    )
    return conv["conversation_id"]


async def _collect(resp) -> list[dict]:
    """把 StreamingResponse 的内容读成事件列表。"""
    events: list[dict] = []
    async for chunk in resp.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for frame in text.split("\n\n"):
            if not frame.strip():
                continue
            name = None
            data_lines: list[str] = []
            for line in frame.split("\n"):
                if line.startswith("event:"):
                    name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
            if name is None or not data_lines:
                continue
            events.append({"event": name, "data": json.loads("\n".join(data_lines))})
    return events


# ------------------------------------------------------------------ 进度机制
def test_progress_event_has_chinese_label():
    """每个阶段都得有给人看的中文说法。

    **为什么**：家长看到的是 ``label``，不是 ``stage``。
    "intake_parse" 对家长毫无意义。
    """
    for stage in (
        "intake_parse",
        "sufficiency",
        "followup",
        "rewrite_query",
        "retrieve",
        "rerank",
        "evidence_gate",
        "build_recommendation",
        "safety_review",
        "render",
    ):
        assert stage in STAGE_LABELS, f"{stage} 没有中文说法"
        assert label_for(stage) != stage, f"{stage} 的中文说法没翻译"


def test_progress_emit_without_subscriber_is_silent():
    """没有订阅者时 emit 不能报错。

    普通（非流式）请求里没人听进度 ——
    那里报错的话，整条链路就废了。
    """
    from rdi_agent.progress import emit

    emit("intake_parse")  # 不能抛


@pytest.mark.asyncio
async def test_progress_channel_collects_events():
    """开了通道就能收到事件，出了通道就收不到了。"""
    from rdi_agent.progress import emit

    with progress_channel() as queue:
        emit("intake_parse", attempt=2)
        event: ProgressEvent = queue.get_nowait()
    assert event.stage == "intake_parse"
    assert event.attempt == 2
    assert event.label == label_for("intake_parse")

    # 通道关了之后不该再收到
    emit("sufficiency")
    assert queue.empty()


@pytest.mark.asyncio
async def test_llm_call_emits_progress(monkeypatch):
    """真实 LLM 出口每调一次就打一个进度点。

    **为什么这条最要紧**：进度不是靠每个节点手动打点实现的，
    而是接在 ``llm.chat_json`` 这个唯一的出口上 ——
    所以新增一个抽取节点，**不用改代码就自动有进度**。

    打点断了的话，症状是"新加的那一步家长看不到提示"，
    而且很难查（因为其他步骤都正常）。
    """
    events: list[ProgressEvent] = []

    async def fake_create(**kwargs):
        class _Msg:
            content = '{"observed_facts": []}'

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()

    class _FakeCompletions:
        create = staticmethod(fake_create)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "client", lambda timeout_s=None: _FakeClient())
    # conftest 默认把 DEMO_MODE 设成 replay —— 那样会在调 client **之前**
    # 就短路返回（去读回放文件），走不到打点那行。要走 live。
    monkeypatch.setenv("DEMO_MODE", "live")
    reset_settings()

    with progress_channel() as queue:
        await llm.chat_json(
            system="s",
            user="u",
            model_cls=CaseSummary,
            name="case_summary",
            stage="intake_parse",
        )
        while not queue.empty():
            events.append(queue.get_nowait())

    assert [e.stage for e in events] == ["intake_parse"]
    assert events[0].label == label_for("intake_parse")


# ------------------------------------------------------------------ SSE 端点
@pytest.mark.asyncio
async def test_stream_emits_progress_then_result(
    no_retrieval, graph, scripted_llm
):
    """流式端点要先推进度，最后推结果。"""
    cid = await _new_conversation()
    resp = await api.send_message_stream(
        cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )
    events = await _collect(resp)

    names = [e["event"] for e in events]
    assert names[-1] == "result", f"最后一个事件应该是 result，实际 {names}"
    assert "error" not in names, f"不该有 error 事件：{events}"

    result = events[-1]["data"]
    assert result["status"] == "awaiting_followup"
    assert len(result["questions"]) == 1


@pytest.mark.asyncio
async def test_stream_result_matches_non_streaming(
    no_retrieval, graph, stateless_llm
):
    """流式和非流式必须产出**一样的结果**。

    **为什么这是最要紧的一条**：手测台的价值就在于"看到的就是真的"。
    如果两条路跑的是不同逻辑，手测看到的东西就不能代表线上 ——
    之前就出过一次这种问题（API 层用普通 dict 调图，中断不恢复，
    图从 START 重跑，单测全绿但线上是错的）。
    """
    cid_a = await _new_conversation()
    cid_b = await _new_conversation()

    streamed = await _collect(
        await api.send_message_stream(
            cid_a, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
        )
    )
    plain = await api.send_message(
        cid_b, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
    )

    stream_result = streamed[-1]["data"]
    # turn_id 是随机生成的，比对时排除掉。
    for payload in (stream_result, plain):
        payload.pop("turn_id", None)
        payload.pop("conversation_id", None)

    assert stream_result == plain, (
        "流式和非流式的结果不一致 —— 两条路跑的不是同一套逻辑"
    )


@pytest.mark.asyncio
async def test_stream_covers_all_stages(no_retrieval, graph, scripted_llm):
    """走完整条链时，每一个阶段都要有进度点。

    **为什么要钉死**：少了哪一步，家长就有一段时间只能对着
    一个不动的转圈。而这种缺失是**静默**的 —— 流照样通，
    只是少了一条提示，很容易漏过去。
    """
    cid = await _new_conversation()

    # 第一轮：停在追问中断
    first = await _collect(
        await api.send_message_stream(
            cid, api.SendMessageRequest(message="孩子 4 岁，早上出门哭", turn_id="t1")
        )
    )
    stages = [e["data"]["stage"] for e in first if e["event"] == "progress"]
    assert "intake_parse" in stages
    assert "sufficiency" in stages
    assert "followup" in stages

    # 第二轮：回答追问 → 走检索链 → 出方案
    second = await _collect(
        await api.send_message_stream(
            cid,
            api.SendMessageRequest(
                message="", turn_id="t2", answers=[{"question": "还缺什么？", "answer": "不清楚"}]
            ),
        )
    )
    stages = [e["data"]["stage"] for e in second if e["event"] == "progress"]
    for expected in (
        "intake_parse",
        "sufficiency",
        "rewrite_query",
        "retrieve",
        "rerank",
        "evidence_gate",
        "build_recommendation",
        "safety_review",
        "render",
    ):
        assert expected in stages, f"缺 {expected} 的进度点，实际走了：{stages}"


@pytest.mark.asyncio
async def test_stream_reports_error_as_event(no_retrieval, graph, monkeypatch):
    """出错时推 error 事件，不是把连接掐了。

    **为什么**：SSE 里没法改 HTTP 状态码了（头都发出去了）。
    前端只能从流里读到错误 —— 所以错误必须**在流里说清楚**。
    """
    from rdi_agent.api import conversations as conv_mod

    async def boom(*args, **kwargs):
        raise RuntimeError("图炸了")

    monkeypatch.setattr(conv_mod, "_run_turn", boom)

    cid = await _new_conversation()
    events = await _collect(
        await api.send_message_stream(
            cid, api.SendMessageRequest(message="孩子 4 岁", turn_id="t1")
        )
    )
    names = [e["event"] for e in events]
    assert "error" in names, f"出错时应该推 error 事件，实际 {names}"
    err = [e for e in events if e["event"] == "error"][0]
    assert "图炸了" in err["data"]["message"]
