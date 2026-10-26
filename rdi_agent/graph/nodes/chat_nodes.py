"""聊天入口的节点（计划 §8.2 的上分支）。

一条硬规则贯穿本文件：**带 ``interrupt()`` 的节点必须是纯函数**。
LangGraph 恢复时整个节点会重跑，里面任何数据库写入都会翻倍。
所以本文件里没有 session，只有 prompt → LLM → state 字典；
写库全在 :mod:`rdi_agent.graph.nodes.write_nodes`。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from rdi_agent import llm
from rdi_agent import progress
from rdi_agent.config import get_settings
from rdi_agent.llm import LLMError
from rdi_agent.memory import service as memory_service
from rdi_agent.graph.citations import (
    build_evidence_catalogue,
    sanitize_card_citations,
    translate_short_ids,
    validate_cards,
)
from rdi_agent.graph.facts_merge import (
    filter_forbidden_texts as _filter_forbidden_texts,
    forbidden_hit as _forbidden_hit,
    forbidden_terms,
    restated_terms as _restated_terms,
)
from rdi_agent.graph.nodes.interrupt_utils import build_asked_qa, unpack_resume
from rdi_agent.graph.state import CoachState
from rdi_agent.prompts import chat as P
from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.evidence import EvidenceBundle
from rdi_agent.schemas.conversation import (
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.recommendation import RecommendationCard

# 追问到第几轮就强制收手。
#
# **两轮**（2026-10-26 改，原值 1）。
#
# 一轮 = 最多 3 个问题，家长**一起答完**，然后进下一轮判断。
#
# **为什么从 1 调回 2**：两轮够把场景补全（第一轮问"发生了什么"，
# 第二轮问"具体什么样"），又不至于变成填表。
#
# **重复提问不是靠轮数上限解决的**（2026-10-26 修）——
# 之前把轮数压到 1 只是把症状盖住了。真正的根因是**模型看不到
# "问过的问题和家长的答案"**：
#
#   - 只有问题列表时，模型分不清"这件事问过了"和"问过了但没答上来"，
#     两种情况它都会再问一遍（实测"孩子在幼儿园的表现和在家相比
#     有没有什么不同"被问了两遍，第二次换个说法）。
#   - 家长答了"换过老师"，模型不知道这个答案对不对题，
#     就继续追着原问题问。
#
# 修法是把**问答对**（``asked_qa_pairs``）透传给 prompt，
# 让模型基于**更新过的信息**往下走：
# 答了就当问完了（要补就另起新角度），没答上来才换角度重问。
#
# 信息不够也要出方案 —— 建议卡里有 uncertainty_notes，方案本身就会标出
# 哪里不确定，这比多问一轮有用。
MAX_FOLLOWUP_ROUNDS = 2


def plan_prefix(version: int) -> str:
    """卡号前缀。第 1 版保持原样，第 2 版起加 ``r2_``。

    **不加前缀会撞车**：模型出的卡号写死是 ``plan_1/plan_2/plan_3``，
    重新生成时和旧卡同号，``save_candidate_plans`` 会当成"已经存过"
    直接跳过 —— 新卡根本没落库，家长"选"到的是旧卡。
    """
    return "" if version <= 1 else f"r{version}_"


_ANY_PLAN_PREFIX = re.compile(r"^r\d+_")


def apply_plan_id_prefix(cards: list[RecommendationCard], version: int) -> None:
    """给模型生成的卡号统一加上版本前缀。

    模型可能照做、可能忽略前缀、也可能自己写了个号。这里不信任模型：
    先去掉已有的 ``rN_``，保证是 ``plan_x``，再挂上当前版本的前缀。
    """
    prefix = plan_prefix(version)
    if not prefix:
        return
    used: set[str] = set()
    for index, card in enumerate(cards, start=1):
        base = _ANY_PLAN_PREFIX.sub("", str(card.plan_id or "").strip())
        if not base.startswith("plan_"):
            base = f"plan_{index}"
        candidate = f"{prefix}{base}"
        suffix = 2
        while candidate in used:
            candidate = f"{prefix}{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        card.plan_id = candidate


def _current_checkpoint_id(config: RunnableConfig | None) -> str:
    """当前节点所在的那个 checkpoint id。

    **不能直接读 ``configurable.checkpoint_id``**：LangGraph 1.2 在节点
    执行时把它设成 None，真实 id 在 runtime 的 ``execution_info``
    或 ``checkpoint_map[""]`` 里。这两处都是"进入本节点时所在"的
    checkpoint —— 也就是生成建议卡之前的回溯点。
    """
    configurable = ((config or {}).get("configurable") or {})
    runtime = configurable.get("__pregel_runtime")
    info = getattr(runtime, "execution_info", None)
    checkpoint_id = getattr(info, "checkpoint_id", None)
    if checkpoint_id:
        return str(checkpoint_id)
    checkpoint_map = configurable.get("checkpoint_map") or {}
    if checkpoint_map.get(""):
        return str(checkpoint_map[""])
    return str(configurable.get("checkpoint_id") or "")


def _bundle_of(state: CoachState) -> EvidenceBundle | None:
    """从 state 里还原证据包。"""
    raw = state.get("evidence_bundle")
    if not raw:
        return None
    try:
        return EvidenceBundle.model_validate(raw)
    except Exception:  # noqa: BLE001 - state 里的东西坏了不该让整条链崩
        return None


# --------------------------------------------------------------- 理解
async def load_context(state: CoachState) -> dict[str, Any]:
    """装上这个孩子已有的记忆。

    冷启动会话本来就没有历史档案，返回空不是错误。
    任何一步失败都退回薄上下文 —— **记忆读不出来，家长的问题还得回答**。
    """
    base = dict(state.get("child_context") or {})
    child_id = state.get("child_id") or ""
    if not child_id or not get_settings().memory_enabled:
        return {"child_context": base, "memory_context": {"disabled": True}}

    try:
        ctx = await memory_service.load_memory_context(
            child_id=child_id,
            query=state.get("user_message", ""),
        )
    except Exception as e:  # noqa: BLE001 - 记忆是增强项，不能拖垮整轮
        return {
            "child_context": base,
            "memory_context": {
                "degraded": True,
                "error": f"{type(e).__name__}: {e}",
            },
        }

    base.update(ctx.get("child_context_patch") or {})
    return {
        "child_context": base,
        "memory_context": ctx.get("memory_context") or {},
        "memory_forbidden_facts": ctx.get("forbidden_facts") or [],
    }


def _forbidden_terms(state: CoachState) -> list[str]:
    """本轮已作废/已删除的事实，归一化后用于过滤。"""
    return forbidden_terms(state.get("memory_forbidden_facts") or [])


async def parse_parent_input(state: CoachState) -> dict[str, Any]:
    """把这一轮家长的话抽成 CaseSummary，和之前累积的事实合并。"""
    system, user = P.parse_parent_input(
        user_message=state.get("user_message", ""),
        previous_summary=state.get("case_summary"),
        child_context=state.get("child_context"),
    )
    summary, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=CaseSummary,
        name="case_summary",
        stage="intake_parse",
        temperature=0.0,
    )
    data = summary.model_dump(mode="json")

    # 事实的完整性**由代码兜底**，模型抽取只当补充来源。
    #
    # **为什么不能只靠模型抽**：实测模型会把整段问答丢掉 ——
    # 家长答了"没有不愉快的经历"，抽出来的 observed_facts 里还是没有。
    # 答案一丢，下一轮模型就以为这件事没问过，于是换个说法再问一遍
    # （实测连着问了两遍"在幼儿园是否有不愉快的经历"）。
    #
    # 三份来源，**加**在一起（不是覆盖）：
    #   1. 上一轮累积的事实 —— 事实是累积的，问一轮丢一轮就退化成单轮问答；
    #      模型偶尔也会漏掉 previous_summary 里的旧事实。
    #   2. 这一轮模型新抽出来的事实。
    #   3. 家长明确答了的问答对 —— 结构化传进来的，**一定**要算进事实。
    previous = state.get("case_summary") or {}
    forbidden = _forbidden_terms(state)
    # 家长这一轮**原话**里又提到的作废内容 —— 这些不作废。
    #
    # 见 :func:`_restated_terms`：家长刚说的话不可能是"过期信息"。
    restated = _restated_terms(state.get("user_message") or "", forbidden)
    dropped = 0

    # 累积的旧事实 + 本轮新抽的事实，一起过滤、一起去重。
    #
    # **为什么本轮新抽的也要过滤**：模型会复述家长早就纠正过的内容，
    # 那还是不能要。
    # **为什么本轮新抽的可以留下**：家长这一轮**亲口又说了**同一句，
    # 那是当前事实（见上面的 restated）。
    facts, dropped_facts = _filter_forbidden_texts(
        [
            *(previous.get("observed_facts") or []),
            *(data.get("observed_facts") or []),
        ],
        forbidden,
        restated,
    )
    dropped += dropped_facts

    for item in state.get("followup_answers") or []:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not answer:
            continue
        if question:
            line = f"家长对「{question}」的回答：{answer}"
        else:
            line = f"家长补充：{answer}"
        if line not in facts:
            facts.append(line)
    data["observed_facts"] = facts

    # **作废内容不能从别的字段漏出去**（2026-10-26 修）。
    #
    # 实测漏过一条：``retrieval_concepts`` 没过滤，
    # 一条被 superseded 的"孩子最近换过老师"直接进了检索 query ——
    # 家长已经不要这条了，它还在左右检索结果。
    for field in ("already_tried", "missing_information", "retrieval_concepts"):
        values, dropped_n = _filter_forbidden_texts(
            data.get(field) or [], forbidden, restated
        )
        data[field] = values
        dropped += dropped_n

    # context 是 dict，值也要过一遍。
    filtered_context: dict[str, str] = {}
    for key, value in (data.get("context") or {}).items():
        hit = _forbidden_hit(str(value), forbidden)
        if hit and hit not in restated:
            dropped += 1
            continue
        filtered_context[str(key)] = value
    data["context"] = filtered_context

    # **risk_signals 不过滤** —— 风险信号**绝不能丢**，
    # 丢一次就可能漏掉"该找专业人士"的信号。
    # 家长作废的是一条事实，不是这条事实里可能藏着的风险。
    signals = list(data.get("risk_signals") or [])
    for value in previous.get("risk_signals") or []:
        text = str(value).strip()
        if text and text not in signals:
            signals.append(text)
    data["risk_signals"] = signals

    note = trace.brief()
    if dropped:
        note += f"；过滤掉 {dropped} 条已作废内容"
    if restated:
        note += f"；家长本轮重述了 {len(restated)} 条已作废内容，按最新说法保留"
    return {
        "case_summary": data,
        # 答案已经并进事实了，清掉 —— 免得下一轮又并一遍。
        "followup_answers": [],
        "trace_metadata": {"intake_parse": note},
    }


async def check_sufficiency(state: CoachState) -> dict[str, Any]:
    """信息够不够。追问轮数到上限就强制不再问。"""
    rounds = int(state.get("followup_rounds") or 0)
    system, user = P.check_sufficiency(
        case_summary=state.get("case_summary") or {},
        followup_rounds=rounds,
        max_followup_rounds=MAX_FOLLOWUP_ROUNDS,
        asked_questions=list(state.get("asked_questions") or []),
        asked_qa=list(state.get("asked_qa_pairs") or []),
        child_context=state.get("child_context"),
    )
    decision, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=SufficiencyDecision,
        name="sufficiency",
        stage="sufficiency",
        temperature=0.0,
    )
    # 模型说够，但已经追问 3 轮还拿不准 —— 尊重模型的判断，只是不让它无休止地问下去。
    if rounds >= MAX_FOLLOWUP_ROUNDS and decision.requires_followup:
        decision = SufficiencyDecision(
            requires_followup=False,
            reasons=[
                f"已经追问 {rounds} 轮，信息仍不完整；"
                "继续追问的收益低于给一份标注了不确定性的建议。"
            ],
            missing_information=decision.missing_information,
        )
    return {
        "sufficiency_decision": decision.model_dump(mode="json"),
        "trace_metadata": {"sufficiency": trace.brief()},
    }


def route_after_sufficiency(state: CoachState) -> str:
    decision = state.get("sufficiency_decision") or {}
    if decision.get("requires_followup"):
        return "generate_followup"
    # 信息够了就进检索链：rewrite_query → ... → evidence_gate
    return "rewrite_query"


async def generate_followup(state: CoachState) -> dict[str, Any]:
    """把候选问题收敛成最多 3 个给家长看的问法。"""
    decision = state.get("sufficiency_decision") or {}
    rounds = int(state.get("followup_rounds") or 0)
    system, user = P.generate_followup(
        case_summary=state.get("case_summary") or {},
        priority_questions=decision.get("priority_questions") or [],
        followup_rounds=rounds,
        asked_questions=list(state.get("asked_questions") or []),
        asked_qa=list(state.get("asked_qa_pairs") or []),
        child_context=state.get("child_context"),
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FollowupQuestions,
        name="followup_questions",
        stage="followup",
        temperature=0.3,
    )
    if not result.questions:
        # 候选问题被全部丢掉了，说明已经没什么可问的，直接进方案。
        return {"followup_questions": [], "route_override": "rewrite_query"}
    return {
        # 转成 dict 再存：state 要能序列化（checkpointer 存的是 JSON），
        # 前端拿到的也是 dict。
        "followup_questions": [q.model_dump(mode="json") for q in result.questions],
        "trace_metadata": {"followup": trace.brief()},
    }


def route_after_recommendation(state: CoachState) -> str:
    """建议卡一张都没留下（引用校验全挂）就退回有限回答。"""
    if state.get("candidate_plans"):
        return "plans"
    return "limited_answer"


def route_after_followup(state: CoachState) -> str:
    if state.get("route_override") == "rewrite_query":
        return "rewrite_query"
    return "await_user_answer"


async def await_user_answer(state: CoachState) -> dict[str, Any]:
    """中断点一：等家长回答追问。

    纯函数：只接 resume 值、只回 state。不碰数据库、不调 LLM ——
    这个节点在恢复时会重跑，任何副作用都会重复发生。
    """
    # 打点：问题已经生成完了，正在等家长回答。
    # 不打的话家长看到"正在准备要问你的问题"之后进度条就不动了 ——
    # 实际上问题早就好了，只是在等**人**回答。
    progress.emit("await_answer")
    answer = interrupt({"kind": "followup", "questions": state.get("followup_questions", [])})
    text, answers = unpack_resume(answer)
    # 记下这一轮问过的问题 —— 下一轮生成问题时要把它们排掉。
    asked = list(state.get("asked_questions") or [])
    for item in state.get("followup_questions") or []:
        question = str(item.get("question") or "").strip()
        if question and question not in asked:
            asked.append(question)
    # 问过什么 + 家长答了什么，成对累积。
    #
    # **为什么要成对**：只给问题列表，模型分不清"问过了"和"问过了没答上来"，
    # 两种都会再问一遍（实测"在园和在家表现有没有不同"被问了两遍）。
    # 给了答案，模型才知道该基于新信息往下走，而不是把原问题换个说法重问。
    asked_qa = list(state.get("asked_qa_pairs") or [])
    asked_qa.extend(build_asked_qa(state.get("followup_questions") or [], answers))
    return {
        "user_message": text,
        "followup_answers": answers,
        "followup_questions": [],
        "asked_questions": asked,
        "asked_qa_pairs": asked_qa,
        # route_override 是 generate_followup 留下的路由提示。
        # 家长已经答完了，这一轮不追问 —— 清掉它，
        # 免得后面某个节点读到过期的"跳过追问"标记。
        "route_override": "",
        "followup_rounds": int(state.get("followup_rounds") or 0) + 1,
    }


# --------------------------------------------------------------- 生成
async def build_recommendation(
    # 注解写 ``RunnableConfig`` 而不是 ``RunnableConfig | None``：
    # 本文件开了 ``from __future__ import annotations``，注解会变成字符串，
    # LangGraph 只认几个固定字符串，带 ``| None`` 时它不会注入 config。
    state: CoachState,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """生成 2–3 张建议卡，并用证据包校验引用。

    **引用校验是硬闸**（计划 §9.3）：生成的每一条 ``source_citations``
    都必须在当前证据包里能找到对应的 ``evidence_id``，页码也要对得上。
    找不到就是编造引用 —— 这种卡给家长看等于骗人。

    失败处理：带错误信息**重试一次** → 仍有问题的卡**摘掉对不上的引用**
    后保留，并把 ``application_label`` 降为 ``ai_adaptation``、在
    ``uncertainty_notes`` 里说明。只有一张卡都生成不出来时才降级为
    ``limited_answer``。原因：家长要的是能执行的动作，一条出处对不上
    不该让整张卡消失。
    """
    bundle = _bundle_of(state)
    evidence = [e.model_dump(mode="json") for e in (bundle.evidence if bundle else [])]
    # 进入本节点时所在的那个 checkpoint = 生成建议卡之前的回溯点。
    # 修订时从这里分叉，事实/证据/门控结论原样沿用。
    fork_checkpoint_id = _current_checkpoint_id(config)

    try:
        cards, first_trace, problems, note = await _generate_cards(
            state,
            evidence=evidence,
            validation_errors=None,
        )
    except LLMError as e:
        # 生成本身炸了（schema 连着几次不合规、模型连不上）——
        # 计划 §9.3 的意思是"生成不出来就给有限回答"，不是"整个请求 500"。
        # 家长拿到一句有限回答，比拿到一个 500 强。
        return {
            "candidate_plans": [],
            "final_answer": "",
            "route_after_recommendation": "limited_answer",
            "trace_metadata": {"build_recommendation": f"生成失败，降级为有限回答：{e}"},
        }
    trace_notes = [first_trace]
    if problems:
        # 带错误信息重试一次
        try:
            retry_cards, retry_notes, retry_problems, retry_note = await _generate_cards(
                state,
                evidence=evidence,
                validation_errors=problems,
            )
        except LLMError as e:
            retry_cards, retry_notes, retry_problems, retry_note = [], str(e), {}, ""
        note = retry_note or note
        trace_notes.append(retry_notes)
        # 第二轮只要有一张通过校验，就用第二轮；否则保留第一轮。
        # 被摘掉引用的卡不再整张丢，后面统一清洗。
        retry_good = [c for c in retry_cards if c.plan_id not in retry_problems]
        if retry_good:
            cards = retry_cards
            problems = retry_problems

    if problems:
        validation_bundle = bundle
        if validation_bundle is None:
            validation_bundle = EvidenceBundle(
                original_question="",
                case_summary=state.get("case_summary") or {},
                evidence=[],
                retrieval_version="none",
            )
        sanitized: list[RecommendationCard] = []
        stripped: list[str] = []
        for card in cards:
            if card.plan_id in problems:
                card, removed = sanitize_card_citations(card, validation_bundle)
                if removed:
                    stripped.append(f"{card.plan_id} -> {removed[0]}")
            sanitized.append(card)
        cards = sanitized
        if stripped:
            trace_notes.append("citation_stripped：" + "；".join(stripped))

    if not cards:
        # 模型一张卡都没生成出来 —— 降级为有限回答
        return {
            "candidate_plans": [],
            "final_answer": "",
            "route_after_recommendation": "limited_answer",
            "trace_metadata": {
                "build_recommendation": "；".join(trace_notes)
                + "；没有生成可用的建议卡"
            },
        }

    # 推荐位兜底：清洗后如果恰好没有 recommended，补第一张。
    if not any(c.recommended for c in cards):
        cards[0] = cards[0].model_copy(update={"recommended": True})

    return {
        "candidate_plans": [c.model_dump(mode="json") for c in cards],
        "comparison_note": note,
        "final_answer": "",
        "route_after_recommendation": "plans",
        # 落进 plans.fork_checkpoint_id，下次修订从这里分叉。
        "fork_checkpoint_id": fork_checkpoint_id,
        "trace_metadata": {"build_recommendation": "；".join(trace_notes)},
    }


async def _generate_cards(
    state: CoachState,
    *,
    evidence: list[dict[str, Any]],
    validation_errors: dict[str, list[str]] | None,
) -> tuple[list[RecommendationCard], str, dict[str, list[str]], str]:
    """生成一轮建议卡并做引用校验。

    返回 ``(全部卡, 过程说明, {plan_id: 错误}, 比较说明)``。
    这里不筛卡 —— 有问题的卡由调用方摘引用后保留。
    """
    # 清单和编号映射必须出自**同一次**调用：编号指错了段落，
    # 后面每一处引用都会指向错误的地方。
    version = int(state.get("plan_version") or 1)
    catalogue, id_map = build_evidence_catalogue(evidence)
    system, user = P.build_recommendation(
        case_summary=state.get("case_summary") or {},
        child_context=state.get("child_context"),
        n_plans=3,
        evidence=evidence,
        evidence_catalogue=catalogue,
        validation_errors=validation_errors,
        asked_qa=list(state.get("asked_qa_pairs") or []),
        plan_version=version,
        plan_id_prefix=plan_prefix(version),
        revision_note=state.get("revision_note") or "",
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=RecommendationSet,
        name="recommendation_set",
        stage="build_recommendation",
        temperature=0.7,
        # 3 张卡 + 中文说明，输出量大。截断会被 llm.py 检出并重试，
        # 但这里直接给够，省一次往返。
        max_tokens=12000,
        # 这一步的 prompt 里塞了整包证据，输出也要写满 3 张卡，
        # 是全流程里最慢的一次调用。默认的 20 秒超时（settings.llm_timeout_s）
        # 实测会连续三次超时 —— 宁可多等，不要把家长的问题判成"模型连不上"。
        timeout_s=120.0,
    )

    # 模型看到的是 E1/E2 这种短编号，这里换回真实的 evidence_id。
    # 换完之后，卡上的 id 和证据包里的 id 是同一套，校验器只认真实 id。
    translate_short_ids(result.plans, id_map)
    # 版本前缀由代码兜底：模型可能忽略 prompt 里的前缀要求。
    # 必须在引用校验之前改，否则 problems 的 key 和最终落库的卡号对不上。
    apply_plan_id_prefix(result.plans, version)

    bundle = _bundle_of(state)
    if bundle is None:
        # 没有证据包也要校验：模型凭空引一个 id 出来，同样得拦住
        bundle = EvidenceBundle(
            original_question="",
            case_summary=state.get("case_summary") or {},
            evidence=[],
            retrieval_version="none",
        )
    problems = validate_cards(result.plans, bundle)

    # 返回**全部**卡，不只返回通过校验的：
    # 调用方会把有问题的卡摘掉引用后保留，而不是整张丢掉。
    return list(result.plans), trace.brief(), problems, result.comparison_note


async def present_plans(state: CoachState) -> dict[str, Any]:
    """中断点二：把候选卡交给家长选。纯函数，同上。"""
    # 打点：这一步之前家长等的是"安全检查"，之后就该看到卡了。
    # 少了这一条，进度条会停在"正在做安全检查"然后突然蹦出三张卡。
    progress.emit("present_plans")
    version = int(state.get("plan_version") or 1)
    # 卡上带版本，刷新页面/修订后前端才能按版本分组。
    plans = [
        {**p, "plan_version": version, "is_current_version": True}
        for p in (state.get("candidate_plans") or [])
    ]
    selection = interrupt(
        {"kind": "plan_selection", "plans": plans, "plan_version": version}
    )
    plan_id = selection.get("plan_id") if isinstance(selection, dict) else str(selection)
    return {"selected_plan_id": plan_id}