"""会话流转契约（阶段 1）。

为什么单独一个文件：``CaseSummary`` / ``RecommendationCard`` 是**内容**契约，
跨阶段复用；这里的三个是**流程**契约，只描述"流程走到哪、下一步问什么"，
阶段 1 新增，后续阶段基本不动。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rdi_agent.schemas.recommendation import RecommendationCard

MAX_FOLLOWUP_QUESTIONS = 3
# 每个问题最多给几个点选选项。4 个是"看得过来又不至于像问卷"的量。
MAX_FOLLOWUP_OPTIONS = 4

# **兜底选项** —— 这些选项没有信息量。
#
# 家长点"其他"等于没回答，但所有选项看起来都没说中 ——
# 一个"其他"把整组选项的可信度拉低了。家长本来就可以自己写，
# 所以兜底选项只有坏处。
#
# 不拦"没有""没有这种情况""不清楚" —— 这些**有信息量**：
# 它们明确表达了"没有这个情况"，比"其他"有用得多。
_FOLLOWUP_FALLBACK_OPTIONS = frozenset({
    "其他", "其它", "以上皆是", "以上都有", "其他情况", "别的", "别的情况",
})

# **带诊断/病因判断的选项**。
#
# 家长点一个选项，等于在心里认领了一个说法。
# 点"分离焦虑"和点"换过老师"不一样 —— 前者是让他替我们下判断，
# 后者是描述观察到的情况。系统只描述事实，不给孩子贴标签。
#
# 和 ``graph/safety.py`` 的诊断规则是同一个道理，
# 但这里更严：safety 是拦整张卡，这里是拦单个选项。
_FOLLOWUP_DIAGNOSTIC_OPTIONS = (
    "自闭症", "孤独症", "自闭", "谱系", "发育迟缓", "智力障碍", "唐氏",
    "分离焦虑", "抑郁症", "焦虑症", "多动", "ADHD", "语言障碍",
)


def _require_options(schema: dict) -> None:
    """把 ``options`` 塞进 json_schema 的 required 列表。

    和 ``RecommendationCard`` 的 ``recommended`` 同理：
    有默认值的字段不在 required 里，结构化输出就允许模型整题省略它。
    """
    required = schema.get("required")
    if isinstance(required, list) and "options" not in required:
        required.append("options")


class SufficiencyDecision(BaseModel):
    """信息够不够生成建议。"""

    model_config = ConfigDict(extra="forbid")

    requires_followup: bool
    reasons: list[str] = Field(default_factory=list)
    # 这里叫 priority_questions：它是"该问什么"的候选清单，还不是给家长看的问法。
    # 给家长看的问法由 generate_followup 重新措辞，避免把内部术语直接抛出去。
    priority_questions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class FollowupQuestion(BaseModel):
    """一个追问 + 它的点选选项。

    **为什么要选项**：家长面对一个空输入框不知道该写什么，
    写出来的东西质量差异很大 —— 有人写三行流水账，有人只写"不知道"。
    给几个贴合场景的短选项，家长点一下就有话说了；
    实在不认同这些选项，还能自己写。

    选项是**建议**不是**限制**：``options`` 可以是空的数组
    （模型想不出好选项时），此时这一题退化成纯自由输入。
    """

    model_config = ConfigDict(
        extra="forbid",
        # 强制 options 进 json_schema 的 required。
        #
        # **为什么**：它在 pydantic 上有默认值 []，所以**不在** required 里 ——
        # 结构化输出允许模型整题都不写这个字段。
        # 实测模型会直接省略（options 变成空数组），
        # 于是每一题都退化成纯文本输入，点选按钮形同虚设。
        #
        # 要求"必须出现"不等于"必须非空" —— 空数组是合法答案，
        # 对应 prompt 里"想不出好选项就给空数组"。
        json_schema_extra=_require_options,
    )

    # 给家长看的问法
    question: str
    # 点选选项。每个都是**家长会说的原话**，不是"其他""以上皆是"这种废话。
    options: list[str] = Field(default_factory=list)
    # 为什么问这个 —— 给家长看一眼，让他知道这不是随便问的。
    # 调试和前端提示都用得上。
    why_asking: str = ""

    @field_validator("options")
    @classmethod
    def clean_options(cls, value: list[str]) -> list[str]:
        """清洗选项：先丢没用的，再查数量上限。

        丢兜底选项（"其他"）和带诊断判断的选项（"分离焦虑"）。
        实测模型会顽固地给出这两类 —— prompt 说了"不要给其他"，
        它还是给。所以这里用代码兜住，跟本仓库"硬规则靠代码不靠 prompt"的做法一致。

        **为什么不报错**：一个坏选项不值得让整轮生成作废。
        丢掉它，这一题少一个按钮而已，家长还能自己写。
        全被丢光就退化成纯自由输入 —— 也是合法形态。

        **顺序很重要**：先清洗再查上限。反过来的话，模型给了 5 个选项
        （其中 1 个是"其他"）会因为超限直接报错，
        而清洗之后其实只剩 4 个合法的。
        """
        kept: list[str] = []
        for opt in value:
            text = (opt or "").strip()
            if not text:
                continue
            if text in _FOLLOWUP_FALLBACK_OPTIONS:
                continue
            if any(term in text for term in _FOLLOWUP_DIAGNOSTIC_OPTIONS):
                continue
            kept.append(text)
        if len(kept) > MAX_FOLLOWUP_OPTIONS:
            raise ValueError(f"一个问题最多 {MAX_FOLLOWUP_OPTIONS} 个选项")
        return kept

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class FollowupQuestions(BaseModel):
    """一轮追问。硬上限 3 个（计划 §8.4）。

    每题带自己的选项（见 :class:`FollowupQuestion`）。
    """

    model_config = ConfigDict(extra="forbid")

    questions: list[FollowupQuestion] = Field(default_factory=list)
    why_asking: str = ""

    @field_validator("questions")
    @classmethod
    def at_most_three(cls, value: list[FollowupQuestion]) -> list[FollowupQuestion]:
        if len(value) > MAX_FOLLOWUP_QUESTIONS:
            raise ValueError(f"一轮追问最多 {MAX_FOLLOWUP_QUESTIONS} 个问题")
        return value


class RecommendationSet(BaseModel):
    """一轮生成的 2–3 张建议卡，必须恰好有一张被推荐。"""

    model_config = ConfigDict(extra="forbid")

    plans: list[RecommendationCard] = Field(min_length=1, max_length=3)
    comparison_note: str = ""

    @field_validator("plans")
    @classmethod
    def exactly_one_recommended(cls, value: list[RecommendationCard]) -> list[RecommendationCard]:
        n = sum(1 for p in value if p.recommended)
        if n != 1:
            raise ValueError(f"必须恰好一张卡 recommended=True，实际 {n} 张")
        return value

    @field_validator("plans")
    @classmethod
    def unique_plan_ids(cls, value: list[RecommendationCard]) -> list[RecommendationCard]:
        ids = [p.plan_id for p in value]
        if len(set(ids)) != len(ids):
            raise ValueError("plan_id 不能重复")
        return value


class NextStep(BaseModel):
    """反馈之后给家长的下一步。刻意不含"方案是否有效"的结论。"""

    model_config = ConfigDict(extra="forbid")

    acknowledgment: str = ""
    next_steps: list[str] = Field(default_factory=list)
    observe_this_week: list[str] = Field(default_factory=list)
    # 只描述"观察到的关系"，不下因果结论
    relation_to_plan: str = ""
    suggest_revisit_plan: bool = False

class SafetyReview(BaseModel):
    """安全审查结论（计划 §9.3）。

    三选一：

    - ``pass``：没问题，可以给家长看
    - ``blocked``：命中硬规则，走安全兜底话术
    - ``needs_human_review``：命中软规则。首版不建审核队列，
      降级为有限回答并标记。
    """

    model_config = ConfigDict(extra="forbid")

    result: Literal["pass", "blocked", "needs_human_review"]
    findings: list[str] = Field(default_factory=list)
    rules_hit: list[str] = Field(default_factory=list)
    # LLM 复核有没有跑（跑了但失败也算 False）
    llm_checked: bool = False
    llm_concerns: list[str] = Field(default_factory=list)


class SafetyReviewCheck(BaseModel):
    """LLM 复核的结果。规则层是硬闸，这里只能加严不能放宽。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    severity: Literal["none", "minor", "major"] = "none"
    concerns: list[str] = Field(default_factory=list)


class LimitedAnswer(BaseModel):
    """证据不足时的有限回答。

    **刻意不包含建议卡**：证据不够还给一张"看起来很专业"的卡，
    等于用格式唬人。所以这一路只给文字：小动作、观察什么、
    什么时候该找专业人士。
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = ""
    low_risk_actions: list[str] = Field(default_factory=list)
    observe: list[str] = Field(default_factory=list)
    when_to_seek_professional_help: list[str] = Field(default_factory=list)
    # 说清楚"为什么这次没有书中依据"。空着会让家长以为是系统坏了。
    knowledge_gap_note: str = ""
