"""证据门控的**规则层**（计划 §5.5 / §9.3）。

单独成文件是因为这块逻辑是硬约束，必须能脱离 LLM 单独测。

**规则优先，LLM 为辅。** 门控决定"给不给建议"——
一条错误的"有直接依据"会让家长拿到一份没有依据的建议。
所以下面这几条写死在代码里，LLM 说什么都不能推翻：

1. 证据包为空 → 不可能是"有直接依据"
2. 家长报告了风险信号 → 高风险，走专业支持
3. 没有任何一条证据达到"可作直接依据"的门槛 → 不能判 direct
4. 追问轮数用完了 → 不许再追问（防死循环）

"可作直接依据"的门槛（计划 §5.5 的"有直接相关且定位准确的证据"）：

    关键词路命中（精确术语）  **或者**  向量相似度 >= 1 - DIRECT_MAX_DISTANCE

为什么要两个信号：余弦距离分不开"崩溃点理论"（0.5341，相关）和
"红烧肉怎么做"（0.5345，无关）—— 标定数据见 ``evals/calibrate_threshold.py``。
加上"关键词路有没有命中这个精确术语"才分得开。
"""

from __future__ import annotations

from typing import Any

from rdi_agent.rag.search import DIRECT_MAX_DISTANCE
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceDecision, EvidenceItem

# 证据门控最多额外追问几轮。
#
# **为什么必须有上限**：阶段 1 的追问环回到 ``parse_parent_input``。
# 如果证据门控也路由到追问，两个环会互相打转 ——
# 门控说"缺信息" → 追问 → 回到 ``check_sufficiency``（模型说够了）
# → 检索 → 门控又说"缺信息" → 死循环。
# 第二次到门控时这个计数已经是 1，门控必然落到 limited_answer。
#
#
# **现在是 0**（2026-10-25 改，原值 1）。
#
# 家长只被允许回答**一轮**追问。门控的追问是第二个圈 ——
# 家长答完第一轮，流程进检索，门控再说"缺信息"就又问一轮，
# 那还是"答完还在追问"。所以门控不再追问：证据不够就直接给有限回答
# （``limited_answer``），把缺口写在 ``evidence_gaps`` 里让家长看见。
MAX_EVIDENCE_FOLLOWUP_ROUNDS = 0


def usable_evidence(bundle: EvidenceBundle) -> list[EvidenceItem]:
    """证据包里"可以当直接依据"的那些。

    规则和检索评测用的是同一把尺子（``evals/run_retrieval.py::_is_usable``），
    两边必须一致 —— 否则评测说"有可用证据"，门控却说"没有"，报告会自相矛盾。
    """
    floor = 1.0 - DIRECT_MAX_DISTANCE
    result: list[EvidenceItem] = []
    for item in bundle.evidence:
        if item.fts_matched:
            result.append(item)
        elif item.similarity is not None and item.similarity >= floor:
            result.append(item)
    return result


# 真的算"高风险"的信号。
#
# **为什么要逐条再判一次**：``CaseSummary.risk_signals`` 是模型填的，
# 实测它会把"到幼儿园门口大哭"也当风险信号 ——
# 而那一栏一旦非空就升级成"建议尽快就医"，
# 等于让每个分离焦虑的孩子都被吓一跳。
# 所以这里按内容再判一次，只有真的严重（自伤、伤到别人、意识异常、
# 拒绝进食进水）才升级。
#
# 词表刻意不含"哭""害怕""焦虑"这类词：它们是这次要处理的问题本身，
# 不是需要立刻就医的紧急情况。
SERIOUS_RISK_PATTERNS = (
    "自伤", "自残", "自杀", "伤自己", "伤害自己", "伤到别人", "伤害别人",
    "伤人", "打人", "咬人", "攻击", "暴力", "推倒", "摔打",
    "撞墙", "撞头", "撞自己", "掐自己", "掐脖子", "勒",
    "屏气", "憋气", "昏", "晕倒", "失去意识", "抽搐", "痉挛",
    "误食", "吞了", "中毒",
    "拒绝进食", "拒绝喝水", "不吃不喝", "无法进食", "无法喝水", "脱水",
    "高烧", "高热", "受伤", "流血不止", "伤口",
    "self-harm", "self harm", "self-injur", "suicid", "injur",
    "hitting", "aggressive", "violent", "seizure", "unconscious", "poison",
)

# "严重""剧烈"这类程度词单独出现太弱，要搭配危机词才算。
SEVERE_MARKERS = ("严重", "剧烈", "非常", "极度", "一直都", "反复")
CRISIS_MARKERS = (
    "崩溃", "失控", "情绪失控", "尖叫", "打人", "伤人", "自伤",
    "不吃", "不喝", "不睡", "无法平复", "平复不了", "停不下来",
)


def risk_signals(case_summary: dict[str, Any] | None) -> list[str]:
    """家长报告里的风险信号（**不判断严重程度**，见 serious_signals）。"""
    return list((case_summary or {}).get("risk_signals") or [])


def serious_signals(case_summary: dict[str, Any] | None) -> list[str]:
    """真正需要马上找专业人士的风险信号。"""
    result: list[str] = []
    for signal in risk_signals(case_summary):
        text = str(signal).lower()
        if any(p in text for p in SERIOUS_RISK_PATTERNS):
            result.append(signal)
        elif (
            any(m in text for m in SEVERE_MARKERS)
            and any(c in text for c in CRISIS_MARKERS)
        ):
            result.append(signal)
    return result


def forced_decision(
    bundle: EvidenceBundle,
    case_summary: dict[str, Any] | None = None,
) -> EvidenceDecision | None:
    """规则已经判死时返回裁决，否则返回 None（交给 LLM 判）。

    "判死"指两种情况：没有证据可用，或者有风险信号。
    这两种情况**不调 LLM** —— 让模型在空证据上"判断有没有依据"，
    正是我们要防的事。
    """
    signals = serious_signals(case_summary)
    if signals:
        return EvidenceDecision(
            evidence_status="risk",
            supported_claims=[],
            unsupported_claims=[],
            missing_information=list(signals),
            can_support_personalized_advice=False,
            high_risk=True,
            needs_followup=False,
            decision="professional_support",
        )
    if not bundle.evidence:
        return EvidenceDecision(
            evidence_status="none",
            supported_claims=[],
            unsupported_claims=[],
            missing_information=list(bundle.evidence_gaps) or ["知识库中没有找到相关片段"],
            can_support_personalized_advice=False,
            high_risk=False,
            needs_followup=False,
            decision="limited_answer",
        )
    return None


def enforce_rules(
    decision: EvidenceDecision,
    bundle: EvidenceBundle,
    case_summary: dict[str, Any] | None = None,
    *,
    evidence_followup_rounds: int = 0,
) -> EvidenceDecision:
    """把 LLM 的裁决按规则修正一遍。

    LLM 只能**加严**，不能放宽：它说 direct，但没有任何证据达到门槛，
    就必须降成 indirect。
    """
    data = decision.model_dump()
    usable = usable_evidence(bundle)
    signals = serious_signals(case_summary)

    # 规则 1：风险信号优先于一切
    if signals:
        data.update(
            evidence_status="risk",
            high_risk=True,
            can_support_personalized_advice=False,
            decision="professional_support",
        )
        return EvidenceDecision.model_validate(data)

    # 规则 2：没有可用证据就不能是 direct
    if not usable:
        if data.get("evidence_status") == "direct":
            data["evidence_status"] = "indirect"
        data["can_support_personalized_advice"] = False
        if data.get("decision") == "recommend":
            data["decision"] = "limited_answer"

    # 规则 3：追问轮数用完了就不许再追问
    if evidence_followup_rounds >= MAX_EVIDENCE_FOLLOWUP_ROUNDS:
        data["needs_followup"] = False

    return EvidenceDecision.model_validate(data)


def route_after_evidence_gate(state: dict[str, Any]) -> str:
    """证据门控之后的路由（计划 §9.3，原样照抄）。"""
    d = state.get("evidence_decision") or {}
    if d.get("high_risk"):
        return "professional_support"
    if d.get("needs_followup"):
        return "followup"
    if d.get("decision") == "recommend":
        return "recommend"
    return "limited_answer"
