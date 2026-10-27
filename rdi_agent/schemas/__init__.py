"""Pydantic 契约集合。所有节点输出都必须先过这里的校验。"""

from rdi_agent.schemas.case import CaseSummary
from rdi_agent.schemas.conversation import (
    MAX_FOLLOWUP_OPTIONS,
    MAX_FOLLOWUP_QUESTIONS,
    FollowupQuestion,
    FollowupQuestions,
    NextStep,
    RecommendationSet,
    SufficiencyDecision,
)
from rdi_agent.schemas.evidence import (
    EvidenceBundle,
    EvidenceDecision,
    EvidenceItem,
)
from rdi_agent.schemas.feedback import (
    ChildResponse,
    FeedbackAnalysis,
    FeedbackSubmission,
)
from rdi_agent.schemas.memory import (
    ConsolidationResult,
    DecisionType,
    MemoryAction,
    MemoryCandidate,
    MemoryCandidateDraft,
    MemoryDecision,
    MemoryExtractionResult,
    MemoryUpdateResult,
    SourceKind,
    SummaryDraft,
)
from rdi_agent.schemas.recommendation import (
    ApplicationLabel,
    ParentAction,
    RecommendationCard,
    SourceCitation,
)

__all__ = [
    "MAX_FOLLOWUP_OPTIONS",
    "MAX_FOLLOWUP_QUESTIONS",
    "ApplicationLabel",
    "CaseSummary",
    "ChildResponse",
    "ConsolidationResult",
    "EvidenceBundle",
    "EvidenceDecision",
    "EvidenceItem",
    "FeedbackAnalysis",
    "FeedbackSubmission",
    "FollowupQuestion",
    "FollowupQuestions",
    "DecisionType",
    "MemoryAction",
    "MemoryCandidate",
    "MemoryCandidateDraft",
    "MemoryDecision",
    "MemoryExtractionResult",
    "MemoryUpdateResult",
    "SourceKind",
    "SummaryDraft",
    "NextStep",
    "ParentAction",
    "RecommendationCard",
    "RecommendationSet",
    "SourceCitation",
    "SufficiencyDecision",
]
