"""安全审查规则测试（计划 §9.3 验收点：安全用例中不出现诊断、强迫或忽视不适）。

规则层是硬闸，所以它必须被钉死 —— 漏判一条就是一次真实的伤害。

**知识库换成中文指南之后，诊断规则改成了语境判定**（见
``safety._hits_diagnostic``）：中文的"孤独症""自闭症"是**指南自己的
词汇**，按词判会把所有正经的卡都拦掉。所以这里除了"要拦的"，
还要钉死"**不该拦的**" —— 那些是指南原话里的正常表述。
"""

from __future__ import annotations

from rdi_agent.graph.safety import review_safety
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem
from rdi_agent.schemas.recommendation import ParentAction, RecommendationCard

EXCERPT = (
    "挑战性行为干预应坚持预防为主，通过功能分析明确行为的功能，"
    "针对不同功能采用前因干预、替代行为训练、后果管理等策略。"
    "实施前因干预可以通过环境结构化、任务分解、提供选择、调整物理环境"
    "等方式减少引发问题行为的诱因；替代行为训练要教孩子在原有行为"
    "发生之前用更可取的行为来达到同样的功能目的。"
)

EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"


def _item(excerpt: str = EXCERPT) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=EVIDENCE_ID,
        source_type="guideline",
        title="针对挑战性行为的干预",
        section_title="（四）针对挑战性行为的干预",
        pdf_page_start=7,
        pdf_page_end=8,
        summary="挑战性行为干预应坚持预防为主，通过功能分析明确行为的功能",
        source_excerpt=excerpt,
        relevance_reason="向量召回第 1 名",
        review_status="unreviewed",
        may_quote_to_user=False,
        similarity=0.61,
        fts_matched=True,
    )


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(
        original_question="孩子发脾气怎么办",
        case_summary={},
        evidence=list(items),
        evidence_gaps=[],
        retrieval_version="2c-v1",
    )


def _card(**kw) -> RecommendationCard:
    base = dict(
        plan_id="plan_1",
        title="先缩短出门这件事",
        observed_facts=["早上出门会哭"],
        explanation="先让孩子有时间准备好。",
        parent_actions=[
            ParentAction(
                action="提前十分钟出门",
                context="早上",
                steps=["出门前先陪他看一会儿要带的东西"],
                observe=["他有没有主动拿东西"],
                reduce_or_stop_if=["他明显抗拒就停下来"],
            )
        ],
        success_indicators=["哭的时间变短"],
        stop_conditions=["一周没有变化就停下重新评估"],
        application_label="ai_adaptation",
        uncertainty_notes=["还不确定原因"],
        recommended=True,
    )
    base.update(kw)
    return RecommendationCard(**base)


# ------------------------------------------------------------------ 正常通过
def test_clean_card_passes():
    review = review_safety([_card()], _bundle(_item()))
    assert review.result == "pass"
    assert review.findings == []


def test_clean_answer_passes():
    review = review_safety([], _bundle(_item()), answer_text="可以先试试提前十分钟出门。")
    assert review.result == "pass"


# ------------------------------------------------------------------ 诊断：要拦的
def test_diagnostic_language_is_blocked():
    card = _card(explanation="这孩子可能有自闭症谱系障碍。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "diagnostic_language" in review.rules_hit


def test_diagnosis_english_is_blocked():
    card = _card(explanation="The child may have autism.")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"


def test_diagnostic_in_parent_action_is_blocked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="先确认他是不是确诊了",
                context="诊断",
                steps=[],
                observe=[],
                reduce_or_stop_if=[],
            )
        ]
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"


def test_labelling_the_child_is_blocked():
    """"他就是自闭症"这种直接贴标签必须拦。"""
    card = _card(explanation="他就是个自闭症孩子，我们说什么都没用。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "diagnostic_language" in review.rules_hit


# ------------------------------------------------------------------ 诊断：不该拦的
#
# 这一组是这次改动的核心回归测试。知识库是中文指南，指南正文里
# 这些表述是**正常的** —— 按词判的话每一条都会被误拦。


def test_mentioning_autism_in_guideline_context_is_allowed():
    """引用指南讲孤独症儿童 —— 不是给孩子下诊断。"""
    card = _card(
        explanation="指南指出，孤独症儿童的缺陷行为应减少，增加适应性行为。"
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


def test_guideline_term_challenging_behavior_is_allowed():
    """指南的核心术语"挑战性行为"不能被当成诊断词。"""
    card = _card(explanation="针对挑战性行为的干预，指南建议先做功能分析。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


def test_quoting_confirmed_diagnosis_guideline_is_allowed():
    """指南原文"确诊患儿应立即干预"是知识库内容，不是给这个孩子下诊断。"""
    card = _card(explanation="指南里写：确诊患儿应立即干预，早期干预越早越好。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


def test_health_education_mention_is_allowed():
    card = _card(explanation="健康教育这一节讲的是孤独症相关知识的家长宣教。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


def test_suspected_only_is_blocked():
    """"疑似" + 病名 —— 对着孩子说的，要拦。"""
    card = _card(explanation="你描述的情况看起来疑似孤独症。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"


# ------------------------------------------------------------------ 强迫
def test_coercion_is_blocked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="要求他必须看着你的眼睛",
                context="打招呼的时候",
                steps=[],
                observe=[],
                reduce_or_stop_if=[],
            )
        ]
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "coercion" in review.rules_hit


def test_suppressing_refusal_is_blocked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="他拒绝就按住他",
                context="练习",
                steps=[],
                observe=[],
                reduce_or_stop_if=[],
            )
        ]
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"


# ------------------------------------------------------------------ 惩罚
#
# 指南**自己就讲惩罚**（附件 2 有"负惩罚"策略的完整说明），
# 所以不能"见到罚站就拦" —— 那会把指南里最有用的一节封掉。
# 要拦的是**建议家长用惩罚**。


def test_punishment_advice_is_blocked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="他再闹就罚站十分钟",
                context="在外面",
                steps=[],
                observe=[],
                reduce_or_stop_if=[],
            )
        ]
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "punishment_advice" in review.rules_hit


def test_quoting_guideline_about_negative_punishment_is_allowed():
    """复述指南里的负惩罚策略 —— 是知识库内容，不是建议。"""
    card = _card(
        explanation="指南附件 2 讲负惩罚策略，说应谨慎使用，如罚站、扣玩游戏时间。"
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


# ------------------------------------------------------------------ 忽视不适
def test_ignoring_distress_is_blocked():
    card = _card(
        parent_actions=[
            ParentAction(
                action="他难受就忽视，不理他",
                context="情绪上来的时候",
                steps=[],
                observe=[],
                reduce_or_stop_if=[],
            )
        ]
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "ignore_distress" in review.rules_hit


def test_ignore_without_distress_word_is_not_blocked():
    """"别忽视孩子的情绪"这种正面用法不该被误判。"""
    card = _card(explanation="不要忽视孩子已经表达出来的情绪。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


# ------------------------------------------------------------------ 大段原文
def test_long_verbatim_quote_is_blocked():
    card = _card(explanation=EXCERPT)
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "long_quote" in review.rules_hit


def test_short_quote_is_allowed():
    card = _card(explanation="指南里提到 消退 这个策略。")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


# ------------------------------------------------------------------ 误标
def test_ai_adaptation_claiming_guideline_quote_is_blocked():
    card = _card(explanation="指南说这个阶段要慢慢来。", application_label="ai_adaptation")
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"
    assert "misattribution" in review.rules_hit


def test_source_summary_may_mention_the_guideline():
    card = _card(
        explanation="指南讲的挑战性行为干预……",
        application_label="source_summary",
        source_citations=[
            {
                "source_id": EVIDENCE_ID,
                "source_type": "guideline",
                "title": "针对挑战性行为的干预",
                "chapter": "（四）针对挑战性行为的干预",
                "pdf_page_start": 7,
                "pdf_page_end": 8,
                "claim_supported": "指南讲的是挑战性行为的干预",
            }
        ],
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "pass"


# ------------------------------------------------------------------ 软规则
def test_vague_stop_conditions_need_human_review():
    card = _card(stop_conditions=["视情况而定"])
    review = review_safety([card], _bundle(_item()))
    assert review.result == "needs_human_review"
    assert "vague_stop_conditions" in review.rules_hit


def test_hard_rule_wins_over_soft_rule():
    card = _card(
        stop_conditions=["视情况而定"],
        explanation="这孩子可能有自闭症。",
    )
    review = review_safety([card], _bundle(_item()))
    assert review.result == "blocked"


# ------------------------------------------------------------------ 正文
def test_answer_text_is_also_reviewed():
    review = review_safety([], _bundle(_item()), answer_text="你应该强迫他看着你。")
    assert review.result == "blocked"
