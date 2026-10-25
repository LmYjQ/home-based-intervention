"""检索与证据节点（计划 §9.3）。

这一段把 2B 的检索能力接进图：

    rewrite_query → retrieve_knowledge → rerank_and_expand → evidence_gate
                                                              ├─ recommend → build_recommendation
                                                              ├─ limited_answer → build_limited_answer
                                                              ├─ professional_support → build_limited_answer
                                                              └─ followup → generate_evidence_followup

**和阶段 1 的分工**：阶段 1 的 ``parse_parent_input`` / ``check_sufficiency``
管"家长说了什么、够不够问"，这一段管"知识库里有没有依据、够不够给建议"。

**State 里不放原文**：``retrieval_candidates`` 只放 chunk_id + 分数 + 页码 +
一小段摘要（计划 §4.2）。下一棒要完整原文时按 id 回库取
（``search.load_candidates_by_ids``）。
"""

from __future__ import annotations

import re
from typing import Any

from rdi_agent import llm
from rdi_agent.config import get_settings
from rdi_agent import progress
from rdi_agent.graph import evidence_gate as gate
from rdi_agent.graph.evidence_gate import route_after_evidence_gate  # noqa: F401
from rdi_agent.graph.safety import BLOCKED_FALLBACK, review_safety
from rdi_agent.graph.state import CoachState
from rdi_agent.prompts import chat as P
from rdi_agent.rag import retrieval as rag_retrieval
from rdi_agent.rag import search as search_mod
from rdi_agent.rag.query_rewrite import (
    RetrievalQueries,
    build_query_from_case_summary,
)

# **必须起别名**：下面有一个同名的节点函数 ``rewrite_query``。
# 直接 import 会把导入的名字盖掉，节点里的 ``await rewrite_query(text)``
# 就变成在调用自己 —— 传进去的是查询文本（str），不是 state。
from rdi_agent.rag.query_rewrite import rewrite_query as rag_rewrite_query
from rdi_agent.schemas.conversation import (
    FollowupQuestions,
    LimitedAnswer,
    SafetyReview,
    SafetyReviewCheck,
)
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceDecision
from rdi_agent.schemas.recommendation import RecommendationCard

# 候选池上限。够重排用就行，不必把整个召回池塞进 state。
CANDIDATE_POOL = 20


# --------------------------------------------------------------- 工具
def _queries_of(state: CoachState) -> RetrievalQueries:
    """从 state 里还原检索参数。"""
    data = dict(state.get("retrieval_queries") or {})
    if not data:
        return RetrievalQueries(
            original_query="", semantic_query="", keyword_query=""
        )
    return RetrievalQueries(
        original_query=data.get("original_query", ""),
        semantic_query=data.get("semantic_query", ""),
        keyword_query=data.get("keyword_query", ""),
        concept_ids=list(data.get("concept_ids") or []),
        filters=dict(data.get("filters") or {}),
        bridged_terms=list(data.get("bridged_terms") or []),
    )


def _bundle_of(state: CoachState) -> EvidenceBundle | None:
    raw = state.get("evidence_bundle")
    if not raw:
        return None
    try:
        return EvidenceBundle.model_validate(raw)
    except Exception:  # noqa: BLE001 - state 里的东西坏了不该让整条链崩
        return None


def _cards_of(state: CoachState) -> list[RecommendationCard]:
    result: list[RecommendationCard] = []
    for raw in state.get("candidate_plans") or []:
        try:
            result.append(RecommendationCard.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
    return result


# --------------------------------------------------------------- 检索
async def rewrite_query(state: CoachState) -> dict[str, Any]:
    """从 case_summary 生成检索查询。

    **只用已确认事实**（计划 §5.4）：``observed_facts`` / ``parent_goals``。
    ``retrieval_concepts`` 不进检索词 —— 它是模型抽的线索，不是事实，
    拿假设去检索等于用假设"验证"自己。

    这一步是纯规则，不调 LLM：同一份 case_summary 必须永远得到同一个查询，
    否则评测的 Recall 变化说不清是检索变好了还是改写随机了。
    """
    text = build_query_from_case_summary(
        state.get("case_summary") or {},
        state.get("user_message", ""),
    )
    progress.emit("rewrite_query")
    queries = await rag_rewrite_query(text)
    return {
        "retrieval_queries": queries.to_dict(),
        "trace_metadata": {
            "rewrite_query": (
                f"查询={queries.semantic_query[:80]!r} "
                f"桥接={[b['canonical_name'] for b in queries.bridged_terms]}"
            )
        },
    }


async def retrieve_knowledge(state: CoachState) -> dict[str, Any]:
    """双路召回（向量 + 关键词），RRF 合并。

    输出**精简**候选：state 里不放完整原文（计划 §4.2）。
    """
    queries = _queries_of(state)
    if not queries.semantic_query:
        return {"retrieval_candidates": [], "trace_metadata": {"retrieve": "查询为空，跳过召回"}}

    # 召回要走 embedding + 数据库查询，实测好几秒 —— 不打点的话
    # 家长会以为卡死了。
    progress.emit("retrieve")
    recall = await rag_retrieval.recall_candidates(
        queries, candidate_k=rag_retrieval.DEFAULT_CANDIDATE_K, use_vector=True
    )
    candidates = [
        {
            "chunk_id": c.chunk_id,
            "score": round(c.score, 6),
            "similarity": c.similarity,
            "vector_rank": c.vector_rank,
            "fts_rank": c.fts_rank,
            "section_code": c.section_code,
            "section_title": c.section_title,
            "pdf_page_start": c.pdf_page_start,
            "pdf_page_end": c.pdf_page_end,
            "snippet": c.snippet[:160],
        }
        for c in recall.merged[:CANDIDATE_POOL]
    ]
    return {
        "retrieval_candidates": candidates,
        "trace_metadata": {"retrieve": "；".join(recall.notes)},
    }


async def rerank_and_expand(state: CoachState) -> dict[str, Any]:
    """去重、重排、父块扩展，组装证据包。

    完整片段按 id 回库取（``load_candidates_by_ids``）——
    state 里只留了摘要，重排和引用都要原文。
    """
    queries = _queries_of(state)
    slim = state.get("retrieval_candidates") or []
    if not slim:
        bundle = EvidenceBundle(
            original_question=queries.original_query,
            case_summary=state.get("case_summary") or {},
            evidence=[],
            evidence_gaps=["知识库中没有找到相关片段"],
            retrieval_version=rag_retrieval.RETRIEVAL_VERSION,
        )
        return {"evidence_bundle": bundle.model_dump(mode="json")}

    fetched = await search_mod.load_candidates_by_ids(
        [c["chunk_id"] for c in slim if c.get("chunk_id")]
    )
    by_id = {c.chunk_id: c for c in fetched}
    # 恢复 RRF 顺序，并补回召回路的名次和相似度
    ordered = []
    for item in slim:
        candidate = by_id.get(item["chunk_id"])
        if candidate is None:
            continue
        candidate.score = item.get("score", candidate.score)
        candidate.vector_rank = item.get("vector_rank")
        candidate.fts_rank = item.get("fts_rank")
        candidate.similarity = item.get("similarity")
        ordered.append(candidate)

    settings = get_settings()
    # 重排要调外部 API（rerank 开了的话），是最慢的一步之一。
    progress.emit("rerank")
    built = await rag_retrieval.build_evidence_bundle(
        ordered,
        queries,
        original_question=queries.original_query,
        case_summary=state.get("case_summary") or {},
        top_k=rag_retrieval.DEFAULT_FINAL_EVIDENCE,
        # 图里默认不重排：重排要把书里片段发给外部 API。
        use_rerank=settings.rerank_in_graph,
        expand_parents=True,
    )
    bundle = built.bundle

    if settings.summary_zh_enabled:
        from rdi_agent.rag import summary_zh

        await summary_zh.ensure_summaries(bundle)

    usable = gate.usable_evidence(bundle)
    gaps = list(bundle.evidence_gaps)
    if bundle.evidence and not usable:
        gaps.append("检索到的片段相关度不够高，只能作参考，不能当作直接依据")
    bundle.evidence_gaps = gaps

    return {
        "evidence_bundle": bundle.model_dump(mode="json"),
        "trace_metadata": {
            "rerank_and_expand": "；".join(built.notes)
            + f"；可用证据 {len(usable)}/{len(bundle.evidence)} 条"
        },
    }


# --------------------------------------------------------------- 门控
async def evidence_gate(state: CoachState) -> dict[str, Any]:
    """判断证据够不够、是不是高风险。

    **规则优先**：没有证据或者有风险信号时，压根不调 LLM ——
    让模型在空证据上判断"有没有依据"，正是要防的事。
    """
    bundle = _bundle_of(state)
    if bundle is None:
        bundle = EvidenceBundle(
            original_question="",
            case_summary=state.get("case_summary") or {},
            evidence=[],
            evidence_gaps=["没有检索到证据"],
            retrieval_version=rag_retrieval.RETRIEVAL_VERSION,
        )
    case_summary = state.get("case_summary") or {}
    rounds = int(state.get("evidence_followup_rounds") or 0)

    forced = gate.forced_decision(bundle, case_summary)
    if forced is not None:
        decision = forced
        trace = f"规则判定：{decision.evidence_status}（未调用模型）"
        # 规则判死时**不调 LLM**，所以 chat_json 的打点不会触发 ——
        # 得在这儿自己打一个。
        #
        # **注意别打两次**：走 else 分支时 chat_json 会打，
        # 这里再打就是重复事件了。所以打点只放在这一支。
        progress.emit("evidence_gate")
    else:
        system, user = P.evidence_gate(
            case_summary=case_summary,
            evidence=[e.model_dump(mode="json") for e in bundle.evidence],
            evidence_gaps=bundle.evidence_gaps,
        )
        raw, llm_trace = await llm.chat_json(
            system=system,
            user=user,
            model_cls=EvidenceDecision,
            name="evidence_decision",
            stage="evidence_gate",
            temperature=0.0,
        )
        decision = gate.enforce_rules(
            raw, bundle, case_summary, evidence_followup_rounds=rounds
        )
        trace = f"模型判定 {raw.evidence_status} → 规则修正为 {decision.evidence_status}；{llm_trace.brief()}"

    # 追问轮数：门控说"要追问"才 +1，用完了强制不再问
    next_rounds = rounds
    if decision.needs_followup and rounds < gate.MAX_EVIDENCE_FOLLOWUP_ROUNDS:
        next_rounds = rounds + 1

    return {
        "evidence_decision": decision.model_dump(mode="json"),
        "evidence_followup_rounds": next_rounds,
        "trace_metadata": {"evidence_gate": trace},
    }


async def generate_evidence_followup(state: CoachState) -> dict[str, Any]:
    """证据不足导致的追问。问的是"缺什么信息会让建议变样"。"""
    decision = state.get("evidence_decision") or {}
    system, user = P.generate_followup(
        case_summary=state.get("case_summary") or {},
        priority_questions=list(decision.get("missing_information") or []),
        followup_rounds=int(state.get("evidence_followup_rounds") or 0),
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=FollowupQuestions,
        name="evidence_followup_questions",
        stage="evidence_followup",
        temperature=0.3,
    )
    return {
        # 转成 dict 再存：state 要能序列化，前端拿到的也是 dict。
        "followup_questions": [q.model_dump(mode="json") for q in result.questions],
        "trace_metadata": {"evidence_followup": trace.brief()},
    }


# --------------------------------------------------------------- 生成
async def build_limited_answer(state: CoachState) -> dict[str, Any]:
    """证据不足 / 高风险时的有限回答。

    这一路**不出建议卡** —— 证据不够还给一张"看起来很专业"的卡，
    等于用格式唬人。只给文字。
    """
    bundle = _bundle_of(state)
    decision = state.get("evidence_decision") or {}
    mode = "risk" if decision.get("high_risk") else "limited"
    system, user = P.build_limited_answer(
        case_summary=state.get("case_summary") or {},
        evidence=[e.model_dump(mode="json") for e in (bundle.evidence if bundle else [])],
        evidence_gaps=list(bundle.evidence_gaps) if bundle else [],
        mode=mode,
    )
    result, trace = await llm.chat_json(
        system=system,
        user=user,
        model_cls=LimitedAnswer,
        name="limited_answer",
        stage="build_limited_answer",
        temperature=0.5,
    )
    return {
        "candidate_plans": [],
        "answer_parts": result.model_dump(mode="json"),
        "answer_mode": mode,
        # 标记"已经降级过一次"。
        # 没有这个标记，safety_review → fallback → build_limited_answer
        # → safety_review → fallback 会绕成死循环。
        "safety_fallback_used": True,
        "trace_metadata": {"build_limited_answer": trace.brief()},
    }


async def safety_review(state: CoachState) -> dict[str, Any]:
    """安全审查。规则层是硬闸，LLM 复核只能加严不能放宽。"""
    cards = _cards_of(state)
    bundle = _bundle_of(state)
    answer_text = state.get("final_answer") or ""

    review = review_safety(cards, bundle, answer_text)

    # 规则层直接判"blocked"/"needs_human_review"时**不调 LLM** ——
    # 那一支得自己打点，否则家长看不到"安全检查"这一步。
    #
    # **注意别打两次**：走 pass 分支时 chat_json 自己会打。
    if review.result != "pass":
        progress.emit("safety_review")

    if review.result == "pass":
        content = _review_content(state, cards, answer_text)
        system, user = P.safety_review(content=content)
        try:
            check, trace = await llm.chat_json(
                system=system,
                user=user,
                model_cls=SafetyReviewCheck,
                name="safety_review_check",
                stage="safety_review",
                temperature=0.0,
                # 复核的输入是全部建议卡的正文，和 build_recommendation 一样重。
                timeout_s=90.0,
            )
        except llm.LLMError as exc:
            # 复核失败不阻断流程：规则层已经放行了。
            return {
                "safety_review": review.model_copy(
                    update={"llm_checked": False}
                ).model_dump(mode="json"),
                "trace_metadata": {"safety_review": f"规则通过；复核失败：{exc}"},
            }
        if not check.passed:
            result = "blocked" if check.severity == "major" else "needs_human_review"
            review = SafetyReview(
                result=result,
                findings=review.findings + list(check.concerns),
                rules_hit=review.rules_hit + ["llm_review"],
                llm_checked=True,
                llm_concerns=list(check.concerns),
            )
        else:
            review = review.model_copy(
                update={"llm_checked": True, "llm_concerns": list(check.concerns)}
            )

    return {
        "safety_review": review.model_dump(mode="json"),
        "trace_metadata": {"safety_review": f"结果 {review.result}；命中规则 {review.rules_hit}"},
    }


def _review_content(
    state: CoachState,
    cards: list[RecommendationCard],
    answer_text: str,
) -> str:
    """给 LLM 复核看的文本。"""
    parts: list[str] = []
    if answer_text:
        parts.append(answer_text)
    for card in cards:
        parts.append(f"[{card.plan_id}] {card.title}\n{card.explanation}")
        for action in card.parent_actions:
            parts.append(f"  动作：{action.action}（{action.context}）")
            parts.extend(f"    - {s}" for s in action.steps)
            parts.extend(f"    - {s}" for s in action.reduce_or_stop_if)
        parts.append("  停止条件：" + "；".join(card.stop_conditions))
    return "\n".join(parts)


# --------------------------------------------------------------- 渲染
def render_answer(state: CoachState) -> dict[str, Any]:
    """渲染给家长看的回答。**纯函数**，不调 LLM。

    输出确定、可复现，也省一次模型往返。

    **注意**：这是纯函数（不调 LLM），所以 ``llm.chat_json`` 的进度钩子
    抓不到它。要让前端看到"正在整理最终回复"这一步，得在这儿手动打点。
    """
    progress.emit("render")
    review = state.get("safety_review") or {}
    if review.get("result") == "blocked":
        return {
            "final_answer": BLOCKED_FALLBACK,
            "route_after_render": "end",
            "trace_metadata": {"render_answer": "安全审查未通过，走兜底话术"},
        }

    cards = _cards_of(state)
    bundle = _bundle_of(state)
    parts = state.get("answer_parts") or {}

    if cards:
        text = _render_cards(cards, bundle)
    else:
        text = _render_limited(parts)

    return {
        "final_answer": text,
        "route_after_render": "plans" if cards else "end",
        "trace_metadata": {"render_answer": f"渲染 {len(cards)} 张卡" if cards else "渲染有限回答"},
    }


def _render_cards(cards: list[RecommendationCard], bundle: EvidenceBundle | None) -> str:
    recommended = next((c for c in cards if c.recommended), cards[0])
    others = [c for c in cards if c is not recommended]
    lines: list[str] = []

    lines.append(f"【建议先做这个】{recommended.title}")
    if recommended.explanation:
        lines.append(recommended.explanation)
    lines.append("")
    lines.append("怎么做：")
    for i, action in enumerate(recommended.parent_actions, start=1):
        lines.append(f"{i}. {action.action}（{action.context}）")
        for step in action.steps:
            lines.append(f"   - {step}")
        for observe in action.observe:
            lines.append(f"   - 留意：{observe}")
        for stop in action.reduce_or_stop_if:
            lines.append(f"   - 减量或停：{stop}")
    if recommended.success_indicators:
        lines.append("")
        lines.append("怎么算有效果：" + "；".join(recommended.success_indicators))
    if recommended.stop_conditions:
        lines.append("什么时候停：" + "；".join(recommended.stop_conditions))
    if recommended.uncertainty_notes:
        lines.append("还不确定的地方：" + "；".join(recommended.uncertainty_notes))

    if recommended.source_citations:
        lines.append("")
        lines.append("这次的指南依据：")
        index = {e.evidence_id: e for e in (bundle.evidence if bundle else [])}
        for citation in recommended.source_citations:
            item = index.get(citation.source_id)
            if item is None:
                continue
            pages = ""
            if item.pdf_page_start is not None:
                pages = f"，PDF 第 {item.pdf_page_start}-{item.pdf_page_end} 页"
            lines.append(
                f"- 《{item.section_title or item.title}》{pages}：{item.summary}"
            )
    else:
        lines.append("")
        lines.append("这次的建议没有直接引用指南里的原文，是根据你描述的情况给出的。")

    if others:
        lines.append("")
        lines.append("另外两个可以试的方向：")
        for card in others:
            lines.append(f"- {card.title}：{card.explanation}")

    return "\n".join(lines)


# 模型有时会把字段名写进内容里（"when_to_seek_professional_help：建议就医"）。
# 渲染的时候字段名已经有了，再重复一遍读起来很怪 —— 这里剥掉。
_FIELD_PREFIX = re.compile(
    r"^(low_risk_actions|observe|when_to_seek_professional_help|"
    r"knowledge_gap_note|answer)\s*[:：]\s*",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return _FIELD_PREFIX.sub("", str(text or "")).strip()


def _render_limited(parts: dict[str, Any]) -> str:
    lines: list[str] = []
    if parts.get("answer"):
        lines.append(_clean(parts["answer"]))
    if parts.get("low_risk_actions"):
        lines.append("")
        lines.append("可以先做的：")
        lines.extend(f"- {_clean(a)}" for a in parts["low_risk_actions"])
    if parts.get("observe"):
        lines.append("")
        lines.append("这周留意：")
        lines.extend(f"- {_clean(o)}" for o in parts["observe"])
    if parts.get("when_to_seek_professional_help"):
        lines.append("")
        lines.append("什么时候该找专业人士：")
        lines.extend(f"- {_clean(w)}" for w in parts["when_to_seek_professional_help"])
    if parts.get("knowledge_gap_note"):
        lines.append("")
        lines.append(_clean(parts["knowledge_gap_note"]))
    return "\n".join(lines) if lines else "这次我没能给出有依据的建议。"


# --------------------------------------------------------------- 路由
def route_after_safety_review(state: CoachState) -> str:
    """审查通过就往下走；不通过就退回有限回答。

    已经降级过一次就不再降 —— 否则
    ``safety_review → build_limited_answer → safety_review`` 会绕成死循环。
    """
    review = state.get("safety_review") or {}
    if review.get("result") in ("blocked", "needs_human_review"):
        if state.get("safety_fallback_used"):
            return "present"
        return "fallback"
    return "present"


def route_after_render(state: CoachState) -> str:
    """有建议卡就去让家长选，没有就结束。"""
    if state.get("route_after_render") == "plans" and state.get("candidate_plans"):
        return "plans"
    return "end"
