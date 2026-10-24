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


class SufficiencyDecision(BaseModel):
    """信息够不够生成建议。"""

    model_config = ConfigDict(extra="forbid")

    requires_followup: bool
    reasons: list[str] = Field(default_factory=list)
    # 这里叫 priority_questions：它是"该问什么"的候选清单，还不是给家长看的问法。
    # 给家长看的问法由 generate_followup 重新措辞，避免把内部术语直接抛出去。
    priority_questions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class FollowupQuestions(BaseModel):
    """一轮追问。硬上限 3 个（计划 §8.4）。"""

    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(default_factory=list)
    why_asking: str = ""

    @field_validator("questions")
    @classmethod
    def at_most_three(cls, value: list[str]) -> list[str]:
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
