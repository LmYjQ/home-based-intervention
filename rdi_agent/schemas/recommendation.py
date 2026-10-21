"""建议卡契约。

硬规则（由 Pydantic 强制，不靠 prompt 自觉）：

1. success_indicators / stop_conditions 不得为空 —— 家长要能看出"怎么算有效果"和"什么时候该停"。
2. application_label == "book_summary" 时必须带真实引用 —— 没有直接证据就不许标成"书里说的"。
3. expert_reviewed 第一阶段不实现，出现即拒绝。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ApplicationLabel = Literal["book_summary", "ai_adaptation", "expert_reviewed"]


class SourceCitation(BaseModel):
    source_id: str
    source_type: Literal[
        "book",
        "external_research",
        "professional_review",
        "application_adaptation",
    ]
    title: str
    chapter: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    claim_supported: str


class ParentAction(BaseModel):
    action: str
    context: str
    steps: list[str] = Field(default_factory=list)
    observe: list[str] = Field(default_factory=list)
    reduce_or_stop_if: list[str] = Field(default_factory=list)


class RecommendationCard(BaseModel):
    # 同 CaseSummary：多余字段直接报错，避免模型偷偷加"诊断结论"字段
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    title: str
    observed_facts: list[str]
    tentative_interpretations: list[str] = Field(default_factory=list)
    explanation: str
    parent_actions: list[ParentAction]
    success_indicators: list[str]
    stop_conditions: list[str]
    source_citations: list[SourceCitation] = Field(default_factory=list)
    application_label: ApplicationLabel
    uncertainty_notes: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    recommended: bool = False

    @field_validator("success_indicators", "stop_conditions")
    @classmethod
    def must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("建议卡必须包含观察指标和停止条件")
        return value

    @model_validator(mode="after")
    def book_summary_needs_citation(self):
        if self.application_label == "book_summary" and not self.source_citations:
            raise ValueError("book_summary 必须至少有一条真实引用")
        return self
