"""建议卡契约。

硬规则（由 Pydantic 强制，不靠 prompt 自觉）：

1. success_indicators / stop_conditions 不得为空 —— 家长要能看出"怎么算有效果"和"什么时候该停"。
2. application_label == "source_summary" 时必须带真实引用 —— 没有直接证据就不许标成"指南里说的"。
3. expert_reviewed 第一阶段不实现，出现即拒绝。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ``source_summary`` = 这张卡的主要依据是知识库原文（现在是指南）。
# 以前叫 ``book_summary`` —— 知识库从英文书换成中文指南之后，
# "book" 这个词就不准了，但语义没变：有原文依据 vs 应用转化。
ApplicationLabel = Literal["source_summary", "ai_adaptation", "expert_reviewed"]


def _require_recommended(schema: dict) -> None:
    """把 ``recommended`` 塞进 json_schema 的 required 列表。

    在 ``RecommendationCard.model_json_schema()`` 上原地改。
    pydantic 只在顶层模型生成 schema，但嵌套的 card 也会带上这个 hook
    —— 所以 RecommendationSet 里嵌着 card 时同样生效。
    """
    required = schema.get("required")
    if isinstance(required, list) and "recommended" not in required:
        required.append("recommended")


def _require_source_citations(schema: dict) -> None:
    """把 ``source_citations`` 也塞进 required。

    **为什么**：这个字段有默认值（空列表），所以结构化输出允许模型整张卡
    都不写它。实测模型会这么干 —— 它把 ``application_label`` 填成
    ``source_summary``（"这是指南里说的"），却不给出任何引用。

    然后 ``source_summary_needs_citation`` 校验器直接抛错，
    **三张卡一起废掉**，整轮退化成有限回答：家长辛辛苦苦答完三轮追问，
    最后只拿到一段没有出处的文字。

    要求"字段必须出现"不等于"必须有引用" —— 空数组是合法答案，
    对应"这张卡是应用转化，不是指南原文"。
    """
    required = schema.get("required")
    if isinstance(required, list) and "source_citations" not in required:
        required.append("source_citations")


class SourceCitation(BaseModel):
    source_id: str
    source_type: Literal[
        "guideline",
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
    #
    # json_schema_extra 强制把 ``recommended`` 放进 required 列表。
    #
    # **为什么**：这个字段在 pydantic 上有默认值 False，所以它**不在**
    # json_schema 的 required 里 —— 结构化输出允许模型整张卡都不写它。
    # 漏写之后三张卡全是 False，``RecommendationSet`` 的
    # "必须恰好一张卡 recommended=True" 校验就抛错，整轮生成被打成 500
    # （实测大约三次里撞上一次）。
    #
    # pydantic 侧保留默认值，是为了让**代码构造**这张卡时不用每次都写；
    # 只在 json_schema 里要求模型必须显式表态。
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=lambda schema: (
            _require_recommended(schema),
            _require_source_citations(schema),
        ),
    )

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
    def source_summary_needs_citation(self):
        if self.application_label == "source_summary" and not self.source_citations:
            raise ValueError("source_summary 必须至少有一条真实引用")
        return self
