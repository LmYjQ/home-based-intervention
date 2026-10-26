"""反馈入口的节点（计划 §11.2 的反馈流程）。

    capture_feedback → load_feedback_context → analyze_feedback → next_step
        ├─ continue / stop / 追问用尽 → extract_memories → ... → finalize_feedback
        ├─ followup → generate_feedback_followup → await_feedback_answer (interrupt)
        │                                → analyze_feedback（最多 1 轮）
        └─ adjust → prepare_feedback_retrieval → 检索链 → build_feedback_adjustment
                     → safety_review → render_answer → extract_memories

几条硬规则：

1. **反馈入口没有中断**，家长点提交就是一个完整请求 —— 除了反馈不足时的
   追问中断。追问节点是纯函数，恢复时会重跑，所以不能写库。
2. **执行和结果分开记**：``implementation_notes`` 写"做了什么"，
   ``child_response`` 写"观察到什么"。一次观察说明不了因果。
3. **未执行和中途停止同样有价值**：trial 状态要落 ``not_attempted`` /
   ``stopped``，不能当成"方案无效"丢掉。
4. **失败一律降级**：历史读不出来、记忆服务挂了，这一轮照样给下一步；
   记忆是增强项，不能因为它把家长的问题也拖挂。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langgraph.types import interrupt

from rdi_agent import llm
from rdi_agent import progress
from rdi_agent.config import get_settings
from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository
from rdi_agent.graph.citations import (
    build_evidence_catalogue,
    resolve_short_ids,
    validate_evidence_notes,
)
from rdi_agent.graph.nodes.interrupt_utils import build_asked_qa, unpack_resume
from rdi_agent.graph.state import CoachState
from rdi_agent.graph.write_guard import skip_write, writes_allowed
from rdi_agent.memory import service as memory_service
from rdi_agent.prompts import chat as P
from rdi_agent.schemas.conversation import (
    FeedbackAdjustment,
    FollowupQuestions,
    NextStep,
)
from rdi_agent.schemas.evidence import EvidenceBundle
from rdi_agent.schemas.feedback import FeedbackAnalysis, FeedbackSubmission

# 反馈不足时最多追问几轮。
#
# **只追问一轮**：和聊天入口一致（MAX_FOLLOWUP_ROUNDS = 1）。
# 一轮 = 最多 3 个问题，答完就必须给下一步。追问环回到 ``analyze_feedback``，
# 没有上限会一直问下去；有上限也会让家长觉得在填表。
MAX_FEEDBACK_FOLLOWUP_ROUNDS = 1


def _feedback_text(feedback: dict[str, Any]) -> str:
    """把一次反馈里"家长自己写的字"拼成一段，当抽取/检索的原文。

    **只拼家长写的**：``implementation_notes`` / ``parent_observations`` /
    ``difficulties`` / 孩子反应的文字字段。
    不拼 ``feedback_analysis`` —— 那是模型的转述，
    拿它当"家长原话"去生成 ``source_quote``，等于让模型给自己作证。
    """
    parts: list[str] = []
    notes = str(feedback.get("implementation_notes") or "").strip()
    if notes:
        parts.append(notes)
    for key in ("parent_observations", "difficulties"):
        for value in feedback.get(key) or []:
            text = str(value).strip()
            if text:
                parts.append(text)
    child_response = feedback.get("child_response") or {}
    if isinstance(child_response, dict):
        for value in child_response.values():
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts)


def _explanation_text(card: dict[str, Any]) -> str:
    """从建议卡里取说明。兼容老卡的整段 ``explanation``。"""
    parts = card.get("explanation_parts") or []
    text = " ".join(
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict)
    ).strip()
    return text or str(card.get("explanation") or "")


def _plan_context(plan: Any, trial: Any) -> dict[str, Any]:
    """给 analyze / next_step / adjust 看的方案正文。

    **为什么要整张卡而不只是标题**：家长说"老人送的时候做不到"时，
    系统要知道原方案里哪些动作依赖"妈妈送"，否则只能给泛泛的鼓励。
    """
    if plan is None:
        return {}
    card = dict(plan.plan_json or {})
    return {
        "plan_id": card.get("plan_id"),
        "title": card.get("title"),
        "plan_version": getattr(trial, "plan_version", plan.plan_version),
        "target": getattr(trial, "target", ""),
        "application_label": plan.application_label,
        "observed_facts": card.get("observed_facts") or [],
        "explanation_text": _explanation_text(card),
        "parent_actions": card.get("parent_actions") or [],
        "success_indicators": card.get("success_indicators") or [],
        "stop_conditions": card.get("stop_conditions") or [],
        "uncertainty_notes": card.get("uncertainty_notes") or [],
    }


def _note(state: CoachState, message: str) -> list[str]:
    return [*(state.get("memory_notes") or []), message]


async def capture_feedback(state: CoachState) -> dict[str, Any]:
    """落反馈并更新 trial 的执行状态。

    幂等：UNIQUE(trial_id, turn_id) 挡住重复提交。
    **只有新插入的反馈才更新 trial** —— 旧反馈重试不能把后来
    更新的执行状态改回去。
    """
    submission = FeedbackSubmission.model_validate(state["feedback"])

    if not writes_allowed(state):
        return {
            **skip_write(state, "capture_feedback"),
            "feedback": state["feedback"],
            "feedback_created": False,
        }

    async with session_scope() as session:
        repo = Repository(session)
        feedback_row, created = await repo.save_feedback(
            trial_id=submission.trial_id, submission=submission
        )
        trial = await repo.get_trial(submission.trial_id)
        title = ""
        plan_version = 1
        plan = await repo.get_plan(str(trial.plan_id)) if trial is not None else None
        if plan is not None:
            title = (plan.plan_json or {}).get("title", "")
            plan_version = plan.plan_version

        if created and trial is not None:
            now = datetime.now(timezone.utc)
            status = submission.execution_status()
            await repo.update_trial_execution(
                trial_id=submission.trial_id,
                status=status,
                # attempted 才记开始时间；没做/中途停都直接记结束时间。
                started_at=now if status == "attempted" else None,
                ended_at=now,
            )
            trial = await repo.get_trial(submission.trial_id)

    return {
        "feedback": state["feedback"],
        "plan_title": title,
        "plan_version": plan_version,
        "trial_status": trial.status if trial is not None else "",
        "conversation_id": (
            str(trial.conversation_id) if trial is not None else ""
        ),
        "feedback_created": created,
        # 记忆抽取要拿它溯源：这条观察是从哪次反馈来的
        "feedback_db_id": str(feedback_row.id if feedback_row is not None else ""),
    }


async def load_feedback_context(state: CoachState) -> dict[str, Any]:
    """读这次尝试的上下文：方案正文、孩子档案、长期记忆、历史尝试。

    只读节点。任何一块读不出来都降级成空，不阻断给家长下一步。
    """
    submission = FeedbackSubmission.model_validate(state["feedback"])
    trial = None
    plan = None
    feedback_row = None
    child_context: dict[str, Any] = {}
    history: list[dict[str, Any]] = []

    try:
        async with session_scope() as session:
            repo = Repository(session)
            trial = await repo.get_trial(submission.trial_id)
            if trial is None:
                return {
                    "memory_notes": _note(state, "反馈上下文：trial 不存在"),
                }
            plan = await repo.get_plan(str(trial.plan_id))
            feedback_row = await repo.get_feedback(
                trial_id=submission.trial_id, turn_id=submission.turn_id
            )
            if state.get("child_id"):
                child_context = await repo.child_context(state["child_id"])
            if state.get("child_id"):
                history = await repo.load_attempt_history(
                    child_id=state["child_id"],
                    exclude_trial_id=str(trial.id),
                )
    except Exception as e:  # noqa: BLE001 - 历史读不出来不耽误给下一步
        return {
            "memory_notes": _note(
                state, f"反馈上下文读取失败（本轮按空处理）：{type(e).__name__}: {e}"
            ),
            "feedback_history": history,
        }

    memory_context: dict[str, Any] = {}
    forbidden: list[str] = []
    if state.get("child_id") and get_settings().memory_enabled:
        try:
            ctx = await memory_service.load_memory_context(
                child_id=state["child_id"],
                query=_feedback_text(state.get("feedback") or {}),
            )
            child_context.update(ctx.get("child_context_patch") or {})
            memory_context = ctx.get("memory_context") or {}
            forbidden = ctx.get("forbidden_facts") or []
        except Exception as e:  # noqa: BLE001
            memory_context = {
                "degraded": True,
                "error": f"{type(e).__name__}: {e}",
            }

    cached_analysis = (
        dict(feedback_row.analysis_json or {}) if feedback_row is not None else {}
    )
    cached_next_step = (
        dict(feedback_row.next_step_json or {}) if feedback_row is not None else {}
    )

    return {
        "conversation_id": str(trial.conversation_id) if trial is not None else "",
        "plan_title": (
            (plan.plan_json or {}).get("title", "") if plan is not None else ""
        ),
        "plan_context": _plan_context(plan, trial),
        "plan_version": getattr(trial, "plan_version", 1),
        "trial_status": trial.status if trial is not None else "",
        "child_context": child_context,
        "memory_context": memory_context,
        "memory_forbidden_facts": forbidden,
        "feedback_history": history,
        "feedback_cached_analysis": cached_analysis,
        "feedback_cached_next_step": cached_next_step,
    }


async def analyze_feedback(state: CoachState) -> dict[str, Any]:
    """分析执行情况，并和过去的尝试对比。

    只描述观察到的关系，不下因果结论。已有缓存且本轮没有新追问答案时
    直接复用，避免重试同一个 turn_id 再跑一遍 LLM。
    """
    rounds = int(state.get("feedback_followup_rounds") or 0)
    cached = state.get("feedback_cached_analysis") or {}
    if cached and rounds == 0 and not state.get("feedback_followup_answers"):
        return {
            "feedback_analysis": cached,
            "feedback_analysis_from_cache": True,
        }

    # 不手动打点：下面 chat_json 的 stage 就是 feedback_analyze，
    # 再 emit 一次会推两条重复进度。
    system, user = P.analyze_feedback(
        plan_title=state.get("plan_title") or "",
        feedback=state.get("feedback") or {},
        plan_context=state.get("plan_context") or {},
        history=state.get("feedback_history") or [],
        child_context=state.get("child_context") or {},
        followup_answers=state.get("feedback_followup_answers") or [],
    )
    analysis, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FeedbackAnalysis,
        name="feedback_analysis",
        stage="feedback_analyze",
        temperature=0.3,
    )

    if state.get("feedback_db_id") and writes_allowed(state):
        try:
            async with session_scope() as session:
                await Repository(session).update_feedback_result(
                    feedback_id=state["feedback_db_id"],
                    analysis_json=analysis.model_dump(mode="json"),
                    followup_answers_json=state.get("feedback_followup_answers") or [],
                    followup_rounds=rounds,
                    analyzed=True,
                )
        except Exception as e:  # noqa: BLE001 - 落库失败不影响本轮回答
            return {
                "feedback_analysis": analysis.model_dump(mode="json"),
                "feedback_analysis_from_cache": False,
                "memory_notes": _note(
                    state, f"反馈分析落库失败（本轮仍可用）：{type(e).__name__}: {e}"
                ),
                "trace_metadata": {"feedback_analyze": trace.brief()},
            }

    return {
        "feedback_analysis": analysis.model_dump(mode="json"),
        "feedback_analysis_from_cache": False,
        "trace_metadata": {"feedback_analyze": trace.brief()},
    }


def _render_next_step(result: NextStep) -> str:
    """把下一步渲染成家长能读的话。"""
    lines: list[str] = []
    if result.acknowledgment:
        lines.append(result.acknowledgment)
    if result.decision == "followup":
        if result.decision_reason:
            lines.append(result.decision_reason)
        return "\n".join(lines)
    if result.next_steps:
        lines.append("")
        lines.append("接下来可以这样做：")
        lines.extend(f"- {x}" for x in result.next_steps)
    if result.observe_this_week:
        lines.append("")
        lines.append("这周留意：")
        lines.extend(f"- {x}" for x in result.observe_this_week)
    if result.decision == "stop":
        lines.append("")
        lines.append("这次先停一停，不用勉强继续。")
    return "\n".join(lines)


async def next_step(state: CoachState) -> dict[str, Any]:
    """给下一步：continue / adjust / followup / stop 四选一。

    缓存命中时直接复用上一次的结果（重试同一个 turn_id 不再调 LLM）。
    """
    cached = state.get("feedback_cached_next_step") or {}
    if (
        state.get("feedback_analysis_from_cache")
        and cached
        and not state.get("feedback_followup_answers")
    ):
        result = NextStep.model_validate(cached)
        return {
            "next_step": result.model_dump(mode="json"),
            "final_answer": _render_next_step(result),
        }

    rounds = int(state.get("feedback_followup_rounds") or 0)
    system, user = P.next_step(
        analysis=state.get("feedback_analysis") or {},
        plan_context=state.get("plan_context") or {},
        history=state.get("feedback_history") or [],
        child_context=state.get("child_context") or {},
        followup_rounds=rounds,
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=NextStep,
        name="next_step",
        stage="next_step",
        temperature=0.5,
    )
    # 老字段保持一致，方便旧前端/旧数据读。
    result = result.model_copy(
        update={"suggest_revisit_plan": result.decision == "adjust"}
    )
    return {
        "next_step": result.model_dump(mode="json"),
        "final_answer": _render_next_step(result),
        "trace_metadata": {"next_step": trace.brief()},
    }


def route_after_next_step(state: CoachState) -> str:
    """四选一之后往哪走。

    - followup 且还有追问额度 → 追问；
    - adjust → 重新查指南，但只出文字调整建议；
    - 其余（continue / stop / 追问用尽）→ 记忆 → 收尾。
    """
    decision = (state.get("next_step") or {}).get("decision") or "continue"
    rounds = int(state.get("feedback_followup_rounds") or 0)
    if decision == "followup" and rounds < MAX_FEEDBACK_FOLLOWUP_ROUNDS:
        return "followup"
    if decision == "adjust":
        return "retrieve"
    return "finish"


def route_after_feedback_followup(state: CoachState) -> str:
    """生成了追问就等家长；一个问题都生成不出来就直接收尾。"""
    if state.get("feedback_followup_questions"):
        return "await"
    return "finish"


async def generate_feedback_followup(state: CoachState) -> dict[str, Any]:
    """反馈太模糊时，生成最多 3 个会改变建议的问题。"""
    analysis = state.get("feedback_analysis") or {}
    priority = list(analysis.get("open_questions") or []) + list(
        analysis.get("missing_information") or []
    )
    system, user = P.generate_followup(
        case_summary={
            "observed_facts": [
                *(analysis.get("observed_changes") or []),
                *(analysis.get("execution_barriers") or []),
            ],
            "missing_information": priority,
        },
        priority_questions=priority,
        followup_rounds=int(state.get("feedback_followup_rounds") or 0),
        asked_questions=[
            str(item.get("question") or "")
            for item in (state.get("feedback_asked_qa") or [])
        ],
        asked_qa=list(state.get("feedback_asked_qa") or []),
        child_context=state.get("child_context"),
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FollowupQuestions,
        name="feedback_followup_questions",
        stage="feedback_followup",
        temperature=0.3,
    )
    return {
        "feedback_followup_questions": [
            q.model_dump(mode="json") for q in result.questions
        ],
        "trace_metadata": {"feedback_followup": trace.brief()},
    }


async def await_feedback_answer(state: CoachState) -> dict[str, Any]:
    """中断：等家长补充反馈里缺的信息。

    纯函数：只接 resume 值、只回 state。恢复时节点会重跑，
    任何写库都会重复发生。
    """
    progress.emit("await_feedback_answer")
    answer = interrupt(
        {
            "kind": "feedback_followup",
            "trial_id": (state.get("feedback") or {}).get("trial_id"),
            "round": int(state.get("feedback_followup_rounds") or 0) + 1,
            "questions": state.get("feedback_followup_questions") or [],
        }
    )
    text, answers = unpack_resume(answer)
    # 反馈追问的问答对也累积起来 —— 下一轮再追问时，
    # 模型要知道"问过什么、家长答了什么"，
    # 否则会换个说法把原问题重问一遍。
    asked_qa = list(state.get("feedback_asked_qa") or [])
    asked_qa.extend(
        build_asked_qa(state.get("feedback_followup_questions") or [], answers)
    )
    return {
        "user_message": text,
        # 记忆抽取读 followup_answers；反馈追问读自己的字段。
        "followup_answers": answers,
        "feedback_followup_answers": answers,
        "feedback_followup_questions": [],
        "feedback_asked_qa": asked_qa,
        "feedback_followup_rounds": int(
            state.get("feedback_followup_rounds") or 0
        )
        + 1,
    }


def _synthetic_case_summary(state: CoachState) -> dict[str, Any]:
    """把一次反馈拼成检索链认识的 ``CaseSummary`` 形状。

    ``rewrite_query`` 只认 ``observed_facts`` / ``parent_goals`` 这几个字段。
    反馈入口没有走 ``parse_parent_input``，所以这里用**代码**确定性拼一份 ——
    不调 LLM，同一份反馈永远得到同一个查询，评测和线上才对得上。
    """
    feedback = state.get("feedback") or {}
    analysis = state.get("feedback_analysis") or {}
    plan = state.get("plan_context") or {}
    history = state.get("feedback_history") or []
    child_context = state.get("child_context") or {}

    observed: list[str] = []
    sources: list[str] = []
    notes = str(feedback.get("implementation_notes") or "").strip()
    if notes:
        sources.append(f"家长执行情况：{notes}")
    for key in ("parent_observations", "difficulties"):
        for value in feedback.get(key) or []:
            text = str(value).strip()
            if text:
                sources.append(text)
    child_response = feedback.get("child_response") or {}
    if isinstance(child_response, dict):
        for value in child_response.values():
            if isinstance(value, str) and value.strip():
                sources.append(value.strip())
    for item in state.get("feedback_followup_answers") or []:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if answer:
            sources.append(
                f"家长对「{question}」的回答：{answer}" if question else answer
            )
    sources.extend(str(x).strip() for x in (analysis.get("observed_changes") or []))

    for text in sources:
        if text and text not in observed:
            observed.append(text)

    return {
        "observed_facts": observed,
        "parent_goals": list(child_context.get("family_goals") or []),
        "context": {
            "age_display": child_context.get("age_display") or "",
            "original_plan": plan.get("title") or "",
            "original_plan_version": str(state.get("plan_version") or 1),
        },
        "already_tried": [
            plan.get("title") or "",
            *(str(item.get("plan_title") or "") for item in history),
        ],
        "missing_information": list(analysis.get("missing_information") or []),
        # 检索概念只当线索，不拿假设去检索（计划 §5.4）
        "retrieval_concepts": [],
        "risk_signals": list(analysis.get("risk_signals") or []),
    }


async def prepare_feedback_retrieval(state: CoachState) -> dict[str, Any]:
    """把反馈整理成检索请求。纯规则，不调 LLM。"""
    case_summary = _synthetic_case_summary(state)
    plan = state.get("plan_context") or {}
    text = "\n".join(
        [
            _feedback_text(state.get("feedback") or {}),
            str((state.get("feedback_analysis") or {}).get("relation_to_plan") or ""),
            f"原来在执行的方案：{plan.get('title') or ''}",
        ]
    ).strip()
    return {
        "case_summary": case_summary,
        "user_message": text,
        "candidate_plans": [],
        "answer_mode": "",
        "evidence_followup_rounds": 0,
        # 反馈调整不出卡，渲染完直接结束，不进入方案选择中断。
        "route_after_render": "end",
    }


async def build_feedback_adjustment(state: CoachState) -> dict[str, Any]:
    """用检索到的证据生成一段调整建议（不出卡、不建 trial）。

    短编号（E1/E2）翻译回真实 evidence_id；翻译后仍不在本次证据包里的
    一律丢掉 —— 挂错证据和编造引用一样不能给家长看。
    """
    bundle = state.get("evidence_bundle") or {}
    evidence = list(bundle.get("evidence") or [])
    catalogue, mapping = build_evidence_catalogue(evidence)
    decision = state.get("evidence_decision") or {}

    system, user = P.build_feedback_adjustment(
        plan_context=state.get("plan_context") or {},
        feedback=state.get("feedback") or {},
        analysis=state.get("feedback_analysis") or {},
        history=state.get("feedback_history") or [],
        child_context=state.get("child_context") or {},
        evidence=evidence,
        evidence_status=str(decision.get("evidence_status") or "none"),
        evidence_gaps=list(bundle.get("evidence_gaps") or []),
        evidence_catalogue=catalogue,
        followup_answers=state.get("feedback_followup_answers") or [],
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FeedbackAdjustment,
        name="feedback_adjustment",
        stage="feedback_adjustment",
        temperature=0.5,
    )

    known = {str(item.get("evidence_id") or "") for item in evidence}
    for note in result.evidence_notes:
        resolved = resolve_short_ids(note.evidence_ids, mapping)
        note.evidence_ids = [x for x in resolved if x in known]

    # 和片段级引用同一把尺子：提到的指南术语必须出现在它引用的那条证据里。
    # 挂错证据的 note 直接丢掉，宁可少一句，不给家长看编出来的依据。
    bundle_obj = EvidenceBundle.model_validate(bundle) if bundle else EvidenceBundle(
        original_question="",
        case_summary={},
        evidence=[],
        retrieval_version="none",
    )
    require_binding = str(decision.get("evidence_status") or "none") in (
        "direct",
        "indirect",
    )
    kept_notes = []
    dropped = 0
    for note in result.evidence_notes:
        errors = validate_evidence_notes(
            [note], bundle_obj, require_binding=require_binding
        )
        if errors:
            dropped += 1
            continue
        kept_notes.append(note)
    result.evidence_notes = kept_notes

    note = trace.brief()
    if dropped:
        note += f"；丢弃 {dropped} 条挂错证据的 note"
    return {
        "feedback_adjustment": result.model_dump(mode="json"),
        "candidate_plans": [],
        "answer_mode": "adjustment",
        "final_answer": "",
        "route_after_render": "end",
        "trace_metadata": {"build_feedback_adjustment": note},
    }


def _derive_result_status(state: CoachState) -> str:
    """这次反馈最终落到什么状态（给前端和 result_json 用）。"""
    decision = state.get("evidence_decision") or {}
    if decision.get("high_risk") or decision.get("decision") == "professional_support":
        return "needs_professional_support"
    if state.get("answer_mode") == "risk":
        return "needs_professional_support"
    if state.get("answer_mode") == "adjustment":
        return "adjustment"
    if state.get("answer_mode") == "limited":
        return "limited_answer"
    if decision.get("decision") == "limited_answer":
        return "limited_answer"
    return "completed"


async def finalize_feedback(state: CoachState) -> dict[str, Any]:
    """把这一步的结果收尾落库。

    放在记忆提交之后：分析/下一步/调整建议都已经产出，再统一写
    ``result_json`` 和 ``completed_at``。API 看到 ``completed_at``
    就不再重跑 LLM。
    """
    if not writes_allowed(state):
        return skip_write(state, "finalize_feedback")
    if not state.get("feedback_db_id"):
        return {}

    result_json = {
        "status": _derive_result_status(state),
        "message": state.get("final_answer") or "",
        "answer_mode": state.get("answer_mode") or "",
        "next_step_decision": (state.get("next_step") or {}).get("decision"),
        "trial_status": state.get("trial_status") or "",
        "plan_version": state.get("plan_version") or 1,
        "evidence_bundle_id": state.get("evidence_bundle_db_id"),
        "evidence_decision": state.get("evidence_decision") or {},
    }
    try:
        async with session_scope() as session:
            await Repository(session).update_feedback_result(
                feedback_id=state["feedback_db_id"],
                next_step_json=state.get("next_step") or {},
                followup_answers_json=state.get("feedback_followup_answers") or [],
                followup_rounds=int(state.get("feedback_followup_rounds") or 0),
                result_json=result_json,
                completed=True,
            )
    except Exception as e:  # noqa: BLE001
        return {
            "memory_notes": _note(
                state, f"反馈收尾落库失败（本轮结果仍返回）：{type(e).__name__}: {e}"
            )
        }
    return {}
