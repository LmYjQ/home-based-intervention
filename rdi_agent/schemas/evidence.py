"""证据相关契约。

EvidenceItem 描述"指南里/研究里/观察里说了什么"，
EvidenceBundle 是一次检索的完整结果集，
EvidenceDecision 是证据门控（evidence_gate 节点）的裁决。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal[
    "guideline",
    "external_research",
    "professional_review",
    "child_observation",
    "application_pattern",
]


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_type: SourceType
    title: str
    section_title: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    print_page_start: int | None = None
    print_page_end: int | None = None
    summary: str
    source_excerpt: str | None = None
    relevance_reason: str
    limitations: list[str] = Field(default_factory=list)
    review_status: str
    may_quote_to_user: bool = False
    # 向量路的余弦相似度（1 - 距离）。只有 FTS 命中、没进向量候选池时为 None。
    # 门控层用它判断"这条能不能当直接依据"（见 search.DIRECT_MAX_DISTANCE）。
    similarity: float | None = None
    # 是否被关键词路命中。精确术语命中是比距离更硬的信号：
    # 距离分不开"崩溃点理论"（0.5341，相关）和"红烧肉"（0.5345，无关），
    # FTS 命中能分开。
    fts_matched: bool = False


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_question: str
    case_summary: dict[str, object] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    retrieval_version: str


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_status: Literal["direct", "indirect", "conflicting", "none", "risk"]
    supported_claims: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    can_support_personalized_advice: bool = False
    high_risk: bool = False
    needs_followup: bool = False
    decision: Literal[
        "recommend", "limited_answer", "followup", "professional_support"
    ]
