"""聊天入口的节点（计划 §8.2 的上分支）。

一条硬规则贯穿本文件：**带 ``interrupt()`` 的节点必须是纯函数**。
LangGraph 恢复时整个节点会重跑，里面任何数据库写入都会翻倍。
所以本文件里没有 session，只有 prompt → LLM → state 字典；
写库全在 :mod:`rdi_agent.graph.nodes.write_nodes`。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from rdi_agent import llm
from rdi_agent import progress
from rdi_agent.config import get_settings
from rdi_agent.llm import LLMError
from rdi_agent.memory import service as memory_service
from rdi_agent.memory.ids import normalize_content
from rdi_agent.graph.citations import (
    build_evidence_catalogue,
    translate_short_ids,
    validate_cards,
)
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
# **只有一轮**（2026-10-25 改，原值 3）。
#
# 一轮 = 最多 3 个问题，家长**一起答完**，然后就强制进下一步（出方案）。
# 原来允许 3 轮，实践下来两个后果：
#   1. 家长答完第一轮，模型很可能把"没答上来"当成"还没问过"，
#      于是把同一个问题换个说法再问一遍（实测就是"在幼儿园是否有不愉快的经历"
#      连问两次）；
#   2. 家长体验上是"填表"而不是"咨询"—— 计划 §"过度追问"那条
#      明确说"家长感觉在填量表"是要防的。
#
# 信息不够也要出方案 —— 建议卡里有 uncertainty_notes，方案本身就会标出
# 哪里不确定，这比多问一轮有用。
MAX_FOLLOWUP_ROUNDS = 2


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
    """本轮已作废/已删除的事实，归一化后用于过滤。

    归一化的原因：上一轮存进 ``case_summary`` 的是模型转述，
    这一轮的家常话里可能是另一种空白/全半角写法。
    """
    return [
        normalized
        for normalized in (
            normalize_content(x) for x in (state.get("memory_forbidden_facts") or [])
        )
        if normalized
    ]


def _is_forbidden(fact: str, forbidden: list[str]) -> bool:
    """这条事实是不是已经被作废了。

    只看"旧内容是这条事实的全部或一部分" —— 反过来（新事实是旧事实的
    子串）会误杀，比如删掉"孩子 4 岁，喜欢恐龙"之后，
    "喜欢恐龙"这条仍然有效。
    """
    normalized = normalize_content(fact)
    if not normalized:
        return False
    return any(normalized == old or old in normalized for old in forbidden)


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
    dropped = 0

    facts: list[str] = []
    for source in (
        previous.get("observed_facts") or [],
        data.get("observed_facts") or [],
    ):
        for value in source:
            text = str(value).strip()
            if _is_forbidden(text, forbidden):
                # 家长刚纠正过 / 刚删掉的事实，不能再并进本轮的事实清单。
                dropped += 1
                continue
            if text and text not in facts:
                facts.append(text)

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

    # 风险信号**绝不能丢** —— 丢一次就可能漏掉"该找专业人士"的信号。
    signals = list(data.get("risk_signals") or [])
    for value in previous.get("risk_signals") or []:
        text = str(value).strip()
        if text and text not in signals:
            signals.append(text)
    data["risk_signals"] = signals

    note = trace.brief()
    if dropped:
        note += f"；过滤掉 {dropped} 条已作废事实"
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
        asked_questions=list(state.get("asked_questions") or []),
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


def _unpack_resume(value: Any) -> tuple[str, list[dict[str, str]]]:
    """把恢复值拆成 (给模型看的文本, 结构化的问答对)。

    **为什么要结构化**：恢复值光是一段文本的话，"哪句回答的是哪道题"
    就只能靠模型猜。实测模型会把整段问答丢掉 ——
    家长明确答了"没有不愉快的经历"，抽出来的 ``observed_facts``
    里还是只有开场白那句话，于是下一轮它以为这件事没问过，又问了一遍。

    问答对走结构化通道（``followup_answers``），由代码确定性地并进
    ``case_summary``，不指望模型抽。
    """
    if isinstance(value, dict):
        text = str(value.get("text") or "")
        raw_answers = value.get("answers") or []
    elif isinstance(value, list):  # 老形状：家长直接发数组回来
        return "\n".join(str(x) for x in value), []
    else:
        return str(value), []

    answers: list[dict[str, str]] = []
    for item in raw_answers:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not answer:
            continue
        answers.append({"question": question, "answer": answer})
    return text, answers


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
    text, answers = _unpack_resume(answer)
    # 记下这一轮问过的问题 —— 下一轮生成问题时要把它们排掉。
    asked = list(state.get("asked_questions") or [])
    for item in state.get("followup_questions") or []:
        question = str(item.get("question") or "").strip()
        if question and question not in asked:
            asked.append(question)
    return {
        "user_message": text,
        "followup_answers": answers,
        "followup_questions": [],
        "asked_questions": asked,
        # route_override 是 generate_followup 留下的路由提示。
        # 家长已经答完了，这一轮不追问 —— 清掉它，
        # 免得后面某个节点读到过期的"跳过追问"标记。
        "route_override": "",
        "followup_rounds": int(state.get("followup_rounds") or 0) + 1,
    }


# --------------------------------------------------------------- 生成
async def build_recommendation(state: CoachState) -> dict[str, Any]:
    """生成 2–3 张建议卡，并用证据包校验引用。

    **引用校验是硬闸**（计划 §9.3）：生成的每一条 ``source_citations``
    都必须在当前证据包里能找到对应的 ``evidence_id``，页码也要对得上。
    找不到就是编造引用 —— 这种卡给家长看等于骗人。

    失败处理（计划 §9.3 原文）：带错误信息**重试一次** → 仍失败则
    **丢弃该卡**、降级为 ``limited_answer``，并在 trace 里记
    ``citation_validation_failed``。
    """
    bundle = _bundle_of(state)
    evidence = [e.model_dump(mode="json") for e in (bundle.evidence if bundle else [])]

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
        if retry_cards:
            cards = retry_cards
            problems = retry_problems
        else:
            # 第二次全军覆没 —— 退回第一轮里通过校验的那些
            cards = [c for c in cards if c.plan_id not in problems]

    if not cards:
        # 一张都留不下 —— 降级为有限回答
        return {
            "candidate_plans": [],
            "final_answer": "",
            "route_after_recommendation": "limited_answer",
            "trace_metadata": {
                "build_recommendation": "；".join(trace_notes)
                + "；citation_validation_failed：全部建议卡的引用都没通过校验"
            },
        }

    # 丢掉的卡要在 trace 里留痕
    if problems:
        trace_notes.append(
            "citation_validation_failed："
            + "；".join(
                f"{plan_id} -> {errors[0]}" for plan_id, errors in problems.items()
            )
        )

    return {
        "candidate_plans": [c.model_dump(mode="json") for c in cards],
        "comparison_note": note,
        "final_answer": "",
        "route_after_recommendation": "plans",
        "trace_metadata": {"build_recommendation": "；".join(trace_notes)},
    }


async def _generate_cards(
    state: CoachState,
    *,
    evidence: list[dict[str, Any]],
    validation_errors: dict[str, list[str]] | None,
) -> tuple[list[RecommendationCard], str, dict[str, list[str]], str]:
    """生成一轮建议卡并做引用校验。返回 (通过校验的卡, 过程说明, 有问题的卡)。"""
    # 清单和编号映射必须出自**同一次**调用：编号指错了段落，
    # 后面每一处引用都会指向错误的地方。
    catalogue, id_map = build_evidence_catalogue(evidence)
    system, user = P.build_recommendation(
        case_summary=state.get("case_summary") or {},
        child_context=state.get("child_context"),
        n_plans=3,
        evidence=evidence,
        evidence_catalogue=catalogue,
        validation_errors=validation_errors,
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=RecommendationSet,
        name="recommendation_set",
        stage="build_recommendation",
        temperature=0.7,
        # 3 张卡 + 中文说明，一次 2000 token 不够。截断会被 llm.py 检出并重试，
        # 但这里直接给够，省一次往返。
        max_tokens=6000,
        # 这一步的 prompt 里塞了整包证据，输出也要写满 3 张卡，
        # 是全流程里最慢的一次调用。默认的 20 秒超时（settings.llm_timeout_s）
        # 实测会连续三次超时 —— 宁可多等，不要把家长的问题判成"模型连不上"。
        timeout_s=120.0,
    )

    # 模型看到的是 E1/E2 这种短编号，这里换回真实的 evidence_id。
    # 换完之后，卡上的 id 和证据包里的 id 是同一套，校验器只认真实 id。
    translate_short_ids(result.plans, id_map)

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
    good = [c for c in result.plans if c.plan_id not in problems]

    # 恰好一张 recommended 被丢掉了的话，补一张上去 ——
    # 少了这一条，渲染出来的回答就不知道推荐哪张。
    if good and not any(c.recommended for c in good):
        good[0] = good[0].model_copy(update={"recommended": True})

    return good, trace.brief(), problems, result.comparison_note


async def present_plans(state: CoachState) -> dict[str, Any]:
    """中断点二：把候选卡交给家长选。纯函数，同上。"""
    # 打点：这一步之前家长等的是"安全检查"，之后就该看到卡了。
    # 少了这一条，进度条会停在"正在做安全检查"然后突然蹦出三张卡。
    progress.emit("present_plans")
    selection = interrupt({"kind": "plan_selection", "plans": state.get("candidate_plans", [])})
    plan_id = selection.get("plan_id") if isinstance(selection, dict) else str(selection)
    return {"selected_plan_id": plan_id}