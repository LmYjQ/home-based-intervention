# -*- coding: utf-8 -*-
"""
评估报告 → 能力画像
===================
流程（一次 LLM 调用，不多不少）
--------------------------------
    ① redflag.scan() 前置扫报告              ← 零 token，规则判定
    ② 报告超长先摘要                          ← 7B 的上下文撑不住 8000 字报告
    ③ 一次 chat_json()，system 里塞 skill_items_block()
    ④ llm.check_quote() 逐条校验 evidence_quote  ← 回指校验
    ⑤ 校验不过的条目 source 降级为 inferred、confidence 压到 0.35
    ⑥ Python 算 overall_confidence（**不交给模型**）

为什么只调一次
--------------
多轮"抽取 → 追问 → 补抽取"在 7B 上会把每一轮的上下文都带偏，
而 7B × 3 轮 × 8s 就是面试现场的 24 秒。更重要的是：
**一次受限解码 + 校验 + 降级**的失败模式是可预测的，
而多轮循环的失败模式是随机的 —— 现场卡死的代价不对称。

⑤ 这一步是本模块的核心。模型很容易顺手给一句"合理"的 evidence_quote，
但那条 quote 未必是原文里的连续子串。校验不过的处理方式**不是丢弃**，
而是把"报告直述"降级成"推断"并把置信度压到 0.35 —— 因为
"证据无法核实"和"报告没写"在可信度上是一回事，都意味着状态机不该拿它当升级依据。
丢弃则会让画像出现空洞，而空洞会被下游误读成 level=0（他不会）。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, model_validator

import llm
from capabilities import (
    AbilityProfile,
    Domain,
    ParentConstraints,
    SkillAssessment,
    default_overall_confidence,
)
from redflag import RedFlagReport, scan
from vocab import skill_items_block

MAX_REPORT_CHARS = 4000     # 超过这个长度先摘要再抽取
UNVERIFIED_CONFIDENCE = 0.35   # 回指校验不过时的置信度，仍 <0.4，保持 inferred 语义

# 报告抽取的专用超时。实测 7B 出 ~1100 token 需要 45-50s，
# 用每日链路的 20s 上限去卡它，结果不是"慢一点"而是直接进降级路径。
# 它是**一次性的建档操作**，不走每日回路，阶段 4 挂后台 job + SSE。
EXTRACT_TIMEOUT_S = float(os.getenv("LLM_EXTRACT_TIMEOUT_S", "90"))
SUMMARY_TIMEOUT_S = 60.0


# ================================================================== 模型输出
# 注意这里**没有** overall_confidence 和 parent_constraints：
# 前者是要算的，后者是家长填的，都不该由一个读报告的模型产出。
# 模型只回答它真正擅长的一件事 —— "报告里说了什么能力"。
class ProfileDraft(BaseModel):
    """模型这一次的原始输出。**不含** overall_confidence —— 那是算出来的。"""

    model_config = ConfigDict(extra="forbid")

    assessments: list[SkillAssessment] = Field(
        min_length=1, max_length=40,
        description="评估结论。**只写报告真正写到的细项**。"
                    "报告没提到的细项要缺席，不要填 level=0 —— "
                    "'没评估'和'评估为不会'在下游是完全不同的两件事",
    )
    priority_domains: list[Domain] = Field(
        default_factory=list, max_length=3,
        description="优先干预的能区 code，最多 3 个。依据报告里的落后程度判断",
    )
    report_date: str | None = Field(
        default=None, max_length=20, description="报告上的日期 YYYY-MM-DD；没写就填 null",
    )
    needs_clarification: bool = Field(
        default=False, description="报告不足以支撑可靠画像时置 true",
    )
    clarifying_question: str | None = Field(
        default=None, max_length=200,
        description="needs_clarification 为 true 时，向家长追问的那一个最关键的问题",
    )
    report_summary: str = Field(
        default="", max_length=300,
        description="两三句话概括报告结论。给家长看，不要复述检测数据",
    )

    @model_validator(mode="after")
    def _no_duplicate_items(self) -> "ProfileDraft":
        seen = [a.skill_item for a in self.assessments]
        dup = {x for x in seen if seen.count(x) > 1}
        if dup:
            raise ValueError(f"同一技能细项出现了多次：{'、'.join(sorted(dup))}")
        return self


class ProfileExtraction(BaseModel):
    """抽取的完整产物：画像 + 红旗 + LLM 轨迹 + 降级记录。"""

    model_config = ConfigDict(extra="forbid")

    profile: AbilityProfile
    redflags: RedFlagReport
    trace: llm.LLMTrace | None = None
    report_summary: str = ""
    # evidence_quote 回指校验没过的条目：skill_item -> 模型给的那句 quote
    unverified: dict[str, str] = Field(default_factory=dict)
    degraded: bool = False        # True = 整份画像走了降级路径（LLM 不可用）
    note: str = ""

    def render_for_prompt(self) -> str:
        """给下游 Agent（规划器/教练）看的组合视图。"""
        parts = [self.profile.render()]
        if self.report_summary:
            parts.append(f"报告概括：{self.report_summary}")
        if self.redflags.triggered:
            parts.append("【报告中的红旗信号】\n" + self.redflags.render())
        if self.unverified:
            items = "、".join(self.unverified)
            parts.append(f"【证据未通过原文核对的细项】{items} —— "
                         f"这些是推断，不要当作报告的明确结论")
        return "\n\n".join(parts)


# ================================================================== prompt
SYSTEM = f"""你是一名儿童康复评估报告的结构化助手。你的唯一任务是：把这份评估报告里
**明确写到的**能力情况，整理成结构化条目。

【可用的能区与技能细项】你只能从下面这份清单里选，一个字都不能自创：
{skill_items_block()}

【最关键的一条规则】区分两种来源，并在 source 字段里如实标注：

  report_stated —— 报告里**针对这个细项有明确描述**。
                    例："能听懂简单的名词指令" → 听懂单词
                    这时 evidence_quote 必须逐字复制报告里的那段话。

  inferred —— 报告只给了笼统结论，你据此推断出来的。
                    例：报告只写"语言发育落后"，并没有说能不能听懂指令。
                    **这时 evidence_quote 必须填空串**，confidence 必须 <0.4。

  编造 evidence_quote 是这个任务最严重的错误：系统在抽取后会拿你给的这句话
  去报告原文里做逐字匹配，对不上就把这条降级成 inferred。所以写了也白写，
  还不如老实填空串。

【只写报告真正涉及的细项】
报告没提到的细项，请让它在结果里缺席。**不要填 level=0** ——
"报告没评估这一项"和"评估结论是他不会"在下游是完全不同的两件事：
前者意味着未知（值得去探一下），后者会直接把检索推去难度最低的活动。

【等级的标定】
0=从来不做；1=全身协助下能做；2=部分协助下能做；3=能独立完成但慢；
4=能独立稳定完成；5=换到新场景/新形式也能用。

拿不准就往低了给，但 0 要特别当心。**只有报告明确说"从不""完全没有""不会"
才给 0。** 报告写"有短暂的眼神回应，但无法持续" —— 这不是 0，这是 1。
把"偶有""有时""短暂""不稳定"判成 0，后果是系统会认为这条技能他完全不会，
于是把所有进阶活动都挡掉，从此只推最简单的 —— 这比判高一级糟糕得多。

几个容易判错的例子：
  "能听懂简单指令，双指令需要示范一次"   → 听懂单词 3 / 听懂双指令 1
  "有短暂的眼神回应，但无法持续"         → 眼神回应 1（不是 0）
  "能配对相同颜色，需要大人示范"         → 配对 2（不是 1 也不是 3）
  "全天几乎无主动语言"                   → 主动表达需要 0

【其他】
- priority_domains 最多 3 个。超过 3 个等于没有优先级。
- age_equivalent_months 只在报告明确写了等效月龄时才填，否则填 null，不要自己折算。
- report_summary 用两三句话概括结论，给家长看，不要复述检测数据和量表分数。
- needs_clarification 只在报告确实不足以支撑画像时置 true，
  并且 clarifying_question 要问那**一个**最关键的问题，而不是罗列一堆。"""


def _user_prompt(report_text: str, child_id: str, age_months: int,
                 parent_constraints: ParentConstraints | None) -> str:
    lines = [f"孩子标识：{child_id}    月龄：{age_months}个月", ""]
    if parent_constraints is not None:
        lines.append(
            "【家长已提供的现实条件（供你判断可行性，不影响你记录什么）】\n"
            f"  每天可支配时间：{parent_constraints.daily_minutes_available} 分钟\n"
            f"  家里现成材料：{'、'.join(parent_constraints.materials_at_home) or '未提供'}\n"
            f"  是否只有一位家长能带：{'是' if parent_constraints.alone_only else '否'}")
        lines.append("")
    lines.append("【评估报告原文】")
    lines.append(report_text)
    return "\n".join(lines)


# ================================================================== 摘要
_SUMMARY_SYSTEM = (
    "你在压缩一份儿童康复评估报告。保留：每个能区的结论、等级判断、"
    "关键的行为描述原句、以及任何提到具体技能的句子。"
    "删掉：量表分数、检测数据、机构信息、套话。不要评价，不要补充。"
)


def summarize_report(report_text: str) -> str:
    """长报告先压到 MAX_REPORT_CHARS 以内。

    7B 的注意力是有限的：把 8000 字报告原样塞进去，模型会从中间开始漏，
    而且**漏得毫无规律** —— 漏掉的往往是细节最具体的那几条。
    压缩的目标不是省 token，是让每一条都还在模型的注意力里。
    """
    if len(report_text) <= MAX_REPORT_CHARS:
        return report_text
    try:
        s, _ = llm.chat_json(
            system=_SUMMARY_SYSTEM,
            user=("请把下面的评估报告压缩，控制在 2500 字以内，逐条保留结论：\n\n" + report_text),
            model_cls=_SummaryOut,
            name="report_summary",
            stage="report_summary",
            max_tokens=1800,
            timeout_s=SUMMARY_TIMEOUT_S,
        )
        return s.text or report_text[:MAX_REPORT_CHARS]
    except llm.LLMError:
        # 摘要失败就截断。宁可漏掉报告后半段，也不能让整份画像拿不到
        return report_text[:MAX_REPORT_CHARS]


class _SummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="压缩后的报告正文")


# ================================================================== 校验与降级
def _verify_quotes(draft: ProfileDraft, source: str) -> tuple[ProfileDraft, dict[str, str]]:
    """逐条回指校验。返回 (降级后的 draft, 被降级的条目)。

    ★ 降级而不是丢弃。丢弃会让画像出现空洞，而空洞会被下游读成 level=0。
    降级保留了"我们认为他在这项上大概是什么样"的可用信息，
    只是把它老老实实标成 inferred、置信度压到 0.35 ——
    于是状态机的升级闸门会挡住它，检索的 state_match 也不会被它带偏。
    """
    unverified: dict[str, str] = {}
    fixed: list[SkillAssessment] = []
    for a in draft.assessments:
        if llm.check_quote(source, a.evidence_quote):
            fixed.append(a)
            continue
        unverified[a.skill_item] = a.evidence_quote
        fixed.append(a.model_copy(update={
            "source": "inferred",
            "confidence": min(a.confidence, UNVERIFIED_CONFIDENCE),
            "evidence_quote": "",      # 存下来只为了让人看见"模型原本编了什么"
        }))
    return draft.model_copy(update={"assessments": fixed}), unverified


# ================================================================== 降级路径
def _fallback_profile(child_id: str, age_months: int,
                      constraints: ParentConstraints) -> AbilityProfile:
    """LLM 不可用时的画像。**不是空画像，而是一份诚实的画像。**

    只给"未知"级别的占位会失败：检索需要至少一个能区才知道往哪找。
    这里给全部细项 level=0、source=inferred、confidence=0.1 ——
    含义是"还没有任何信息"，检索会据此推最低难度的入门活动，
    家长拿到的是一份"先从最简单的开始试试"的计划，而不是一句报错。
    整体可信度 0.1 < 0.5，状态机随即冻结全部升级。
    """
    from vocab import ABILITY_DOMAIN, SKILL_ITEMS

    items: list[SkillAssessment] = []
    for dom in ABILITY_DOMAIN:
        for item in SKILL_ITEMS[dom][:2]:     # 每能区取两项作代表，够检索起步即可
            items.append(SkillAssessment(domain=dom, skill_item=item, level=0,
                                         source="inferred", confidence=0.1))
    return AbilityProfile(
        child_id=child_id,
        child_age_months=age_months,
        assessments=items,
        priority_domains=list(ABILITY_DOMAIN)[:2],
        parent_constraints=constraints,
        overall_confidence=0.1,
        needs_clarification=True,
        clarifying_question="请把评估报告的原文贴进来，或至少说明孩子在语言和社交上的具体表现。",
    )


# ================================================================== 主入口
def extract_profile(
    report_text: str,
    child_id: str,
    age_months: int,
    *,
    parent_constraints: ParentConstraints | None = None,
    max_attempts: int = 2,
    on_step=None,
) -> ProfileExtraction:
    """把评估报告转成能力画像。**一次 LLM 调用 + 回指校验 + 确定性降级。**"""
    constraints = parent_constraints or ParentConstraints()

    # ① 红旗先行。规则判定，零 token，且不依赖模型是否可用 ——
    #    这正是不把它做成 schema 字段的第二个理由：模型挂了红旗照样能报。
    redflags = scan(report_text)

    # ② 长报告先压
    prepared = summarize_report(report_text)
    truncated = prepared is not report_text

    try:
        # ③ 一次受限解码
        draft, trace = llm.chat_json(
            system=SYSTEM,
            user=_user_prompt(prepared, child_id, age_months, constraints),
            model_cls=ProfileDraft,
            name="ability_profile",
            stage="profile_extract",
            max_tokens=3000,
            max_attempts=max_attempts,
            timeout_s=EXTRACT_TIMEOUT_S,
            on_step=on_step,
        )
    except llm.LLMError as e:
        # ④ 降级：仍然返回一份可用的画像，让演示能跑完
        profile = _fallback_profile(child_id, age_months, constraints)
        return ProfileExtraction(
            profile=profile, redflags=redflags, degraded=True,
            note=f"画像抽取降级：{type(e).__name__}。当前使用的是全 0 起始画像，"
                 f"状态机已冻结全部升级（整体可信度 0.1）。",
        )

    # ⑤ 回指校验 + 降级
    verified, unverified = _verify_quotes(draft, prepared)

    # ⑥ 整体可信度由 Python 算，不让模型算
    profile = AbilityProfile(
        child_id=child_id,
        child_age_months=age_months,
        report_date=verified.report_date,
        assessments=verified.assessments,
        priority_domains=verified.priority_domains,
        parent_constraints=constraints,
        overall_confidence=default_overall_confidence(verified.assessments),
        needs_clarification=verified.needs_clarification or bool(unverified),
        clarifying_question=(
            verified.clarifying_question
            or (f"报告里关于「{'、'.join(list(unverified)[:3])}」的描述比较笼统，"
                f"方便把这几项的实际表现再说具体些吗？" if unverified else None)
        ),
    )

    note = ""
    if truncated:
        note = f"报告超过 {MAX_REPORT_CHARS} 字，已摘要后抽取，后半段细节可能丢失。"
    if unverified:
        note = (note + "  " if note else "") + \
            f"{len(unverified)} 条 evidence_quote 未通过原文逐字核对，已降级为推断。"

    return ProfileExtraction(profile=profile, redflags=redflags, trace=trace,
                             report_summary=verified.report_summary,
                             unverified=unverified, note=note)


# ================================================================== 自检
SAMPLE_REPORT = """姓名：小宇  性别：男  评估日期：2026-09-20  月龄：34个月

一、语言
能听懂简单的名词性指令（"把鞋子拿过来"），但双指令需要示范一次才能执行。
不会主动表达需要，需要什么都是拉大人的手或直接走过去拿。
不会用词或短句表达，全天几乎无主动语言。

二、社会适应
有短暂的眼神回应，但无法持续。很少出现共同注意，
叫他的名字偶尔会转头，抱着他看东西时不会顺着大人的手指方向看。
家长反馈平时几乎不指给他看想要的东西，替代沟通需求非常强烈。

三、认知
能配对相同颜色的积木，需要大人示范。分类完全不能。
能记住两步指令，三步及以上不行。

四、大运动
独走稳，能跑但停不稳，不会双脚跳，不会踢球。

评估意见：语言与社会适应显著落后，建议尽早介入。
另：家长提及孩子有时会咬自己的手指。"""


def _cli() -> None:
    import json

    print("=== 红旗前置扫描（零 token）===")
    print(scan(SAMPLE_REPORT).render())

    print("\n=== 抽取画像（1 次 LLM 调用）===")
    ex = extract_profile(SAMPLE_REPORT, "xiaoyu", 34,
                         parent_constraints=ParentConstraints(
                             daily_minutes_available=30,
                             materials_at_home=["杯子", "绘本", "积木", "小汽车"],
                         ))
    t = ex.trace
    if t:
        print(f"  trace: {t.latency_ms}ms  model={t.model}  "
              f"tokens={t.prompt_tokens}/{t.completion_tokens}  重试={len(t.retries)}")
    print(f"  降级：{ex.degraded}    未通过回指：{list(ex.unverified) or '无'}")
    if ex.note:
        print(f"  备注：{ex.note}")

    print("\n" + ex.profile.render())
    print(f"\n  priority_domains = {ex.profile.domains_of_interest()}")
    print(f"  levels = {json.dumps(ex.profile.levels(), ensure_ascii=False)}")
    print(f"  needs_clarification = {ex.profile.needs_clarification}")
    if ex.profile.clarifying_question:
        print(f"  追问：{ex.profile.clarifying_question}")

    # 状态机闸门
    from statemachine import RULES
    gate = RULES["upgrade_min_confidence"]
    print(f"\n  状态机升级闸门 = {gate}，当前画像可信度 = {ex.profile.overall_confidence:.2f}"
          f" → {'放行' if ex.profile.overall_confidence >= gate else '冻结全部升级'}")

    print("\n=== 降级路径（LLM 不可用时）===")
    fb = _fallback_profile("xiaoyu", 34, ParentConstraints())
    print(f"  assessments {len(fb.assessments)} 项，overall_confidence={fb.overall_confidence}"
          f" → 闸门{'放行' if fb.overall_confidence >= gate else '冻结全部升级'}")
    print(f"  {fb.clarifying_question}")


if __name__ == "__main__":
    _cli()
