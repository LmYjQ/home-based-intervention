"""证据相关契约。

EvidenceItem 描述"书里/研究里/观察里说了什么"，
EvidenceBundle 是一次检索的完整结果集，
EvidenceDecision 是证据门控（evidence_gate 节点）的裁决。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal[
    "book",
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
