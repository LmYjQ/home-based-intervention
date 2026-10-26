"""证据门控的路由：**代码决定走哪条路**（2026-10-26 简化）。

**这个文件测的是一条真实的线上事故**：

同一份证据包（8 条、8 条可用、章节页码对得上，其中一条正是
「（四）针对挑战性行为的干预」，里面有"前因干预""替代行为训练"
"后果管理"），两次门控给出**相反**的结论：

- 一次出了卡；
- 一次只给了家长一段话，前端还显示"证据不够"。

差别不在证据，在于 ``decision`` 和 ``can_support_personalized_advice``
这两个**模型自由填的字段**。它们和 ``supported_claims`` 互相矛盾
（主张写得很具体，can_support 却填 false）。

所以现在：

- ``can_support_personalized_advice`` 字段已删除；
- ``decision`` 不再由模型填 —— 由 ``decide_flow`` 按硬规则算；
- **只有 ``high_risk`` 能挡住建议卡**。

门控不再追问（``MAX_EVIDENCE_FOLLOWUP_ROUNDS = 0``），
所以 "followup" 在这里根本不是合法出口。
"""

from __future__ import annotations

from rdi_agent.graph.evidence_gate import (
    decide_flow,
    enforce_rules,
    route_after_evidence_gate,
)
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceDecision, EvidenceItem

EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"


def _bundle(with_evidence: bool = True) -> EvidenceBundle:
    """有可用证据的证据包（fts 命中就算可用）。"""
    evidence = []
    if with_evidence:
        evidence.append(
            EvidenceItem(
                evidence_id=EVIDENCE_ID,
                source_type="guideline",
                title="针对挑战性行为的干预",
                section_title="（四）针对挑战性行为的干预",
                pdf_page_start=7,
                pdf_page_end=8,
                summary="挑战性行为干预应坚持预防为主",
                source_excerpt="挑战性行为干预应坚持预防为主，通过功能分析明确行为的功能。",
                relevance_reason="向量召回第 1 名",
                review_status="unreviewed",
                may_quote_to_user=False,
                similarity=0.61,
                fts_matched=True,
            )
        )
    return EvidenceBundle(
        original_question="孩子不愿意去幼儿园",
        case_summary={},
        evidence=evidence,
        evidence_gaps=[],
        retrieval_version="2c-v1",
    )


def _decision(**kw) -> EvidenceDecision:
    base = dict(
        evidence_status="direct",
        supported_claims=["挑战性行为干预可以用前因干预减少触发因素"],
        unsupported_claims=["孩子抗拒入园是因为缺乏共同注意"],
        missing_information=[],
        high_risk=False,
        needs_followup=False,
    )
    base.update(kw)
    return EvidenceDecision(**base)


# ------------------------------------------------------------------ decide_flow
def test_evidence_available_goes_to_recommend():
    """有可用证据 → 出卡。"""
    out = decide_flow(_decision(), bundle=_bundle(), case_summary={})
    assert out == "recommend"


def test_no_usable_evidence_goes_to_limited_answer():
    """一条可用证据都没有 → 有限回答（没东西可引）。"""
    out = decide_flow(_decision(), bundle=_bundle(with_evidence=False), case_summary={})
    assert out == "limited_answer"


def test_high_risk_goes_to_professional_support():
    """高风险 → 找专业人士，不出卡。"""
    out = decide_flow(_decision(high_risk=True), bundle=_bundle(), case_summary={})
    assert out == "professional_support"


def test_serious_risk_signal_goes_to_professional_support():
    """家长报告了严重风险信号 → 同样转专业支持。"""
    case_summary = {"risk_signals": ["孩子会撞墙"]}
    out = decide_flow(_decision(), bundle=_bundle(), case_summary=case_summary)
    assert out == "professional_support"


def test_evidence_status_does_not_affect_flow():
    """``evidence_status`` 只是描述，**不决定**给不给卡。

    以前模型的 ``decision`` 和证据状态对不上就掉进 limited_answer，
    家长拿到什么成了掷骰子。现在证据状态再差，只要不是高风险、
    也有可引用的证据，一律出卡 ——
    够不够由引用校验把关。
    """
    for status in ("direct", "indirect", "conflicting"):
        out = decide_flow(
            _decision(evidence_status=status), bundle=_bundle(), case_summary={}
        )
        assert out == "recommend", status


# ------------------------------------------------------------------ 路由
def test_route_uses_the_computed_flow():
    """路由只认 state 里那个**代码算出来的** decision。"""
    state = {
        "evidence_decision": {
            "evidence_status": "direct",
            "high_risk": False,
            "decision": "recommend",
            "needs_followup": False,
        }
    }
    assert route_after_evidence_gate(state) == "recommend"


def test_route_respects_limited_answer():
    state = {
        "evidence_decision": {
            "evidence_status": "none",
            "high_risk": False,
            "decision": "limited_answer",
            "needs_followup": False,
        }
    }
    assert route_after_evidence_gate(state) == "limited_answer"


def test_route_respects_professional_support():
    state = {
        "evidence_decision": {
            "evidence_status": "risk",
            "high_risk": True,
            "decision": "professional_support",
            "needs_followup": False,
        }
    }
    assert route_after_evidence_gate(state) == "professional_support"


def test_feedback_entrypoint_still_goes_to_adjustment():
    """反馈入口不受影响：出的是文字调整建议，不是方案卡。"""
    state = {
        "entrypoint": "feedback",
        "evidence_decision": {
            "evidence_status": "direct",
            "high_risk": False,
            "decision": "recommend",
            "needs_followup": False,
        },
    }
    assert route_after_evidence_gate(state) == "feedback_adjustment"


# ------------------------------------------------------------------ 规则层
def test_serious_risk_signal_forces_high_risk():
    """家长报告了严重风险信号 → 规则层直接判死。"""
    case_summary = {"risk_signals": ["孩子会撞墙"]}
    out = enforce_rules(
        _decision(high_risk=False), bundle=_bundle(), case_summary=case_summary
    )
    assert out.high_risk is True
    assert out.evidence_status == "risk"


def test_no_usable_evidence_cannot_be_direct():
    """一条可用证据都没有 → 不能判 direct。"""
    out = enforce_rules(
        _decision(evidence_status="direct"),
        bundle=_bundle(with_evidence=False),
        case_summary={},
    )
    assert out.evidence_status == "indirect"


def test_model_cannot_invent_a_flow_decision():
    """模型**不能**再自己填 decision —— 那个字段已经不在契约里了。

    以前它能填错，填错了就掉进 limited_answer。
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvidenceDecision(
            evidence_status="direct",
            high_risk=False,
            needs_followup=False,
            decision="limited_answer",
        )
