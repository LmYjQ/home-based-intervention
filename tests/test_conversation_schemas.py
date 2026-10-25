"""阶段 1 流程契约的测试。

这里测的是"结构性拒绝"：模型多给一张 recommended、追问超过 3 个、
两张卡用了同一个 plan_id —— 这些都要在校验层直接失败，
不能等到运行期才发现。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rdi_agent.schemas.conversation import (
    MAX_FOLLOWUP_QUESTIONS,
    FollowupQuestion,
    FollowupQuestions,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.recommendation import ParentAction, RecommendationCard


def _card(plan_id: str = "plan_1", recommended: bool = True) -> RecommendationCard:
    return RecommendationCard(
        plan_id=plan_id,
        title=f"方案 {plan_id}",
        observed_facts=["早上出门会哭"],
        explanation="先缩短难受的时长。",
        parent_actions=[
            ParentAction(action="提前十分钟出门", context="早上出门前"),
        ],
        success_indicators=["出门时的哭持续时间变短"],
        stop_conditions=["连续一周完全没有变化就停下来重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不清楚是不是分离焦虑"],
        recommended=recommended,
    )


# ------------------------------------------------------------------ 追问
def test_followup_allows_three_questions():
    result = FollowupQuestions(
        questions=[
            FollowupQuestion(question="a?", options=["x", "y"]),
            FollowupQuestion(question="b?"),
            FollowupQuestion(question="c?"),
        ]
    )
    assert len(result.questions) == MAX_FOLLOWUP_QUESTIONS


def test_followup_rejects_four_questions():
    """一轮追问最多 3 个。家长被砸四个问题会直接放弃。"""
    with pytest.raises(ValidationError):
        FollowupQuestions(
            questions=[FollowupQuestion(question=q) for q in ("a?", "b?", "c?", "d?")]
        )


def test_followup_allows_zero_questions():
    """零个问题是合法的 —— generate_followup 靠它跳过中断直接出方案。"""
    assert FollowupQuestions(questions=[]).questions == []


def test_followup_question_allows_empty_options():
    """选项可以为空 —— 想不出好选项时退化成纯自由输入。

    比硬凑几个"其他""不确定"这种废话选项好。
    """
    q = FollowupQuestion(question="在园更自在吗？", options=[])
    assert q.options == []


def test_options_is_required_in_json_schema():
    """模型必须显式给出 options 字段（哪怕是空数组）。

    ``options`` 在 pydantic 上有默认值 []，所以它默认**不在** json_schema
    的 required 里 —— 结构化输出允许模型整题都不写这个字段。
    实测模型会直接省略，于是每一题都退化成纯文本输入，
    点选按钮形同虚设。

    所以断言的是 **json_schema 里它必须在 required**，
    而"空数组"仍然是合法答案（对应"想不出好选项就给空数组"）。
    """
    schema = FollowupQuestions.model_json_schema()
    card_schema = schema["$defs"]["FollowupQuestion"]
    assert "options" in card_schema["required"]


def test_followup_question_rejects_five_options():
    """选项太多就成了问卷，家长会直接放弃。"""
    with pytest.raises(ValidationError):
        FollowupQuestion(question="?", options=["a", "b", "c", "d", "e"])


def test_followup_question_drops_fallback_options():
    """"其他"这种兜底选项要丢掉。

    家长点"其他"等于没回答，但会让整组选项看起来都没说中。
    家长本来就可以自己写，兜底选项只有坏处。
    """
    q = FollowupQuestion(
        question="?", options=["换过老师", "其他", "以上皆是", "在园更自在"]
    )
    assert q.options == ["换过老师", "在园更自在"]


def test_followup_question_drops_diagnostic_options():
    """带诊断判断的选项要丢掉。

    家长点一个选项等于在心里认领一个说法 —— 点"分离焦虑"
    和点"换过老师"不一样。系统只描述事实，不给孩子贴标签。
    """
    q = FollowupQuestion(
        question="?", options=["换过老师", "分离焦虑", "疑似自闭症", "在园更自在"]
    )
    assert q.options == ["换过老师", "在园更自在"]


def test_followup_question_keeps_meaningful_negatives():
    """"没有""这种**有信息量**的选项要留着。

    它明确表达了"没有这个情况"，比"其他"有用得多。
    """
    q = FollowupQuestion(question="?", options=["没有这种情况", "不清楚"])
    assert q.options == ["没有这种情况", "不清楚"]


def test_followup_question_counts_after_cleaning():
    """先清洗再查上限。

    模型给了 5 个选项（其中 1 个是"其他"），
    清洗之后其实只剩 4 个合法的 —— 不该报错。
    """
    q = FollowupQuestion(
        question="?", options=["a", "b", "c", "d", "其他"]
    )
    assert q.options == ["a", "b", "c", "d"]


def test_followup_question_all_diagnostic_options_becomes_empty():
    """全被清洗掉就退化成纯自由输入 —— 也是合法形态。"""
    q = FollowupQuestion(question="?", options=["分离焦虑", "自闭症"])
    assert q.options == []


def test_followup_question_rejects_blank_question():
    with pytest.raises(ValidationError):
        FollowupQuestion(question="   ", options=["a"])


def test_followup_question_rejects_extra_field():
    """多塞一个字段要报错 —— 不让模型偷偷加东西。"""
    with pytest.raises(ValidationError):
        FollowupQuestion(question="?", options=["a"], diagnosis="自闭症")


# ------------------------------------------------------------------ 建议卡组
def test_recommendation_set_rejects_zero_recommended():
    """一张都不推荐等于让家长自己挑 —— 那不是"选择建议"，是把活推回去。"""
    with pytest.raises(ValidationError):
        RecommendationSet(plans=[_card("plan_1", False), _card("plan_2", False)])


def test_recommendation_set_rejects_two_recommended():
    with pytest.raises(ValidationError):
        RecommendationSet(plans=[_card("plan_1", True), _card("plan_2", True)])


def test_recommended_is_required_in_json_schema():
    """模型必须显式表态推荐哪张卡。

    ``recommended`` 在 pydantic 上有默认值 False，所以它默认**不在**
    json_schema 的 required 里 —— 结构化输出允许模型整张卡都不写它。
    漏写之后三张卡全是 False，"必须恰好一张卡 recommended=True" 的校验
    就抛错，整轮生成被打成 500（实测大约三次里撞上一次）。

    所以这里断言的是 **json_schema 里它必须在 required**，
    而不是 pydantic 层面拒绝构造 —— pydantic 侧保留默认值，
    是为了让代码构造这张卡时不用每次都写。
    """
    schema = RecommendationSet.model_json_schema()
    card_schema = schema["$defs"]["RecommendationCard"]
    assert "recommended" in card_schema["required"]


def test_source_citations_is_required_in_json_schema():
    """模型必须显式表态这张卡有没有引用。

    ``source_citations`` 在 pydantic 上有默认值 []，所以它默认**不在**
    json_schema 的 required 里 —— 结构化输出允许模型整张卡都不写它。

    实测模型会这么干：它把 ``application_label`` 填成 ``source_summary``
    （"这是指南里说的"），却不给出任何引用。然后
    ``source_summary_needs_citation`` 校验器抛错，
    **三张卡一起废掉**，整轮退化成有限回答 ——
    家长答完三轮追问，最后只拿到一段没有出处的文字。

    要求"字段必须出现"不等于"必须有引用" —— 空数组是合法答案，
    对应"这张卡是应用转化，不是指南原文"。
    """
    schema = RecommendationSet.model_json_schema()
    card_schema = schema["$defs"]["RecommendationCard"]
    assert "source_citations" in card_schema["required"]


def test_recommendation_set_rejects_duplicate_plan_ids():
    """plan_id 重复会导致 select-plan 选错卡。"""
    with pytest.raises(ValidationError):
        RecommendationSet(plans=[_card("plan_1", True), _card("plan_1", False)])


def test_recommendation_set_rejects_more_than_three_plans():
    with pytest.raises(ValidationError):
        RecommendationSet(plans=[_card(f"plan_{i}", i == 0) for i in range(4)])


def test_recommendation_set_accepts_three_cards():
    result = RecommendationSet(
        plans=[_card("plan_1", True), _card("plan_2", False), _card("plan_3", False)]
    )
    assert [p.recommended for p in result.plans] == [True, False, False]


def test_recommendation_set_rejects_extra_field():
    """多塞一个 diagnostic_conclusion 要报错，和 CaseSummary 同理。"""
    payload = _card().model_dump(mode="json")
    payload["diagnostic_conclusion"] = "分离焦虑症"
    with pytest.raises(ValidationError):
        RecommendationSet(plans=[payload], comparison_note="")


# ------------------------------------------------------------------ 充分性
def test_sufficiency_requires_flag():
    """requires_followup 是必填的。默认成 True 或 False 都可能悄悄改变流程。"""
    with pytest.raises(ValidationError):
        SufficiencyDecision(reasons=["信息不全"])


def test_sufficiency_accepts_sparse():
    d = SufficiencyDecision(requires_followup=False)
    assert d.priority_questions == []