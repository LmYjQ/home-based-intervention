"""SQLAlchemy ORM 模型。

对应计划 §4.4 的表设计。要点：

- 全部用 UUID 主键 + JSONB 存 JSON + TIMESTAMPTZ 存时间。
- ``knowledge_chunks.embedding`` 是 pgvector 的 ``vector(1024)``。
- ``content_tsv`` 是生成列（STORED），由 Postgres 维护，迁移里用
  ``GENERATED ALWAYS AS ... STORED`` 建，ORM 这边只是普通列。
- 不建"什么都能装"的 memory_items：四类记忆落四张语义表。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ------------------------------------------------------------------ 知识库
class KnowledgeSource(Base):
    """来源登记。``source_id`` 固定如 ``the_rdi_book_2009``。"""

    __tablename__ = "knowledge_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    source_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors_json: Mapped[list] = mapped_column(JSONB, default=list)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(16), default="en")
    # 当前 RDI 书的授权未核实，一律 internal_review_only
    rights_status: Mapped[str] = mapped_column(
        String(32), default="internal_review_only", nullable=False
    )
    source_uri: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sections: Mapped[list["BookSection"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class BookSection(Base):
    """章节树。PDF 页码与印刷页码分开存，印刷页码无法确认就置空。"""

    __tablename__ = "book_sections"
    __table_args__ = (UniqueConstraint("source_id", "section_code"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id"), nullable=False
    )
    parent_section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_sections.id")
    )
    section_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title_original: Mapped[str] = mapped_column(Text, nullable=False)
    title_zh: Mapped[str | None] = mapped_column(Text)
    section_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pdf_page_start: Mapped[int | None] = mapped_column(Integer)
    pdf_page_end: Mapped[int | None] = mapped_column(Integer)
    print_page_start: Mapped[int | None] = mapped_column(Integer)
    print_page_end: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped[KnowledgeSource] = relationship(back_populates="sections")


class KnowledgeChunk(Base):
    """父子块。父块承载完整语义，子块用于精确召回。"""

    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id"), nullable=False
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_sections.id")
    )
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_chunks.id")
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_original: Mapped[str] = mapped_column(Text, nullable=False)
    content_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    # 生成列：值由 Postgres 算，不能也不能往里写。
    # 必须声明成 Computed，否则 SQLAlchemy 会把它塞进 INSERT，PG 直接报错
    # "cannot insert a non-DEFAULT value into column content_tsv"。
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(content_normalized, ''))", persisted=True),
    )
    embedding: Mapped[Any] = mapped_column(Vector(1024))
    summary_zh: Mapped[str | None] = mapped_column(Text)
    knowledge_type: Mapped[str] = mapped_column(String(32), default="unknown")
    concept_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    pdf_page_start: Mapped[int | None] = mapped_column(Integer)
    pdf_page_end: Mapped[int | None] = mapped_column(Integer)
    print_page_start: Mapped[int | None] = mapped_column(Integer)
    print_page_end: Mapped[int | None] = mapped_column(Integer)
    parse_confidence: Mapped[float | None] = mapped_column()
    review_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_chunks_section", "section_id"),
        Index("idx_chunks_pages", "pdf_page_start", "pdf_page_end"),
    )


class ConceptAlias(Base):
    """检索用术语表（B 层简化版）。首版只做 30–50 个核心术语。"""

    __tablename__ = "concept_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    concept_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    canonical_name_en: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_zh_json: Mapped[list] = mapped_column(JSONB, default=list)
    aliases_en_json: Mapped[list] = mapped_column(JSONB, default=list)
    source_chunk_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    is_explicit_in_source: Mapped[bool] = mapped_column(default=False)
    limitations_json: Mapped[list] = mapped_column(JSONB, default=list)
    review_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    version: Mapped[str] = mapped_column(String(32), default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ------------------------------------------------------------------ 业务表
class Child(Base):
    """稳定档案。不要求真实姓名，只存对建议有帮助的信息。"""

    __tablename__ = "children"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    display_label: Mapped[str] = mapped_column(String(128), nullable=False)
    age_band: Mapped[str | None] = mapped_column(String(32))
    communication_profile_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    strengths_interests_json: Mapped[list] = mapped_column(JSONB, default=list)
    support_preferences_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    family_goals_json: Mapped[list] = mapped_column(JSONB, default=list)
    known_contexts_json: Mapped[list] = mapped_column(JSONB, default=list)
    confirmed_facts_json: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Interaction(Base):
    """原始交互。UNIQUE(conversation_id, turn_id, role) 保证幂等。"""

    __tablename__ = "interactions"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_id", "role"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    child_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id")
    )
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceBundleRecord(Base):
    """证据包审计：回答后追溯"当时检索到了什么"。"""

    __tablename__ = "evidence_bundles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    retrieval_version: Mapped[str] = mapped_column(String(64), default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Plan(Base):
    """候选/选中方案。一张建议卡一行。"""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    child_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id")
    )
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    plan_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_bundles.id")
    )
    application_label: Mapped[str] = mapped_column(String(32), nullable=False)
    recommended: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Trial(Base):
    """家庭尝试。"""

    __tablename__ = "trials"
    __table_args__ = (Index("idx_trials_child_time", "child_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    planned_context_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Feedback(Base):
    """反馈提交。UNIQUE(trial_id, turn_id) 保证重试不重复写。"""

    __tablename__ = "feedbacks"
    __table_args__ = (UniqueConstraint("trial_id", "turn_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    trial_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trials.id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempted: Mapped[bool] = mapped_column(default=False, nullable=False)
    implementation_notes: Mapped[str] = mapped_column(Text, default="")
    child_response_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    parent_observations_json: Mapped[list] = mapped_column(JSONB, default=list)
    difficulties_json: Mapped[list] = mapped_column(JSONB, default=list)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Observation(Base):
    """情境观察（L2）。只写"发生了什么"，不写"孩子就是XX型"。"""

    __tablename__ = "observations"
    __table_args__ = (Index("idx_observations_child_time", "child_id", "observed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id"), nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    setting: Mapped[str | None] = mapped_column(String(128))
    participants_json: Mapped[list] = mapped_column(JSONB, default=list)
    antecedent: Mapped[str] = mapped_column(Text, default="")
    observable_behavior: Mapped[str] = mapped_column(Text, default="")
    adult_response: Mapped[str] = mapped_column(Text, default="")
    child_response: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(Text, default="")
    source_kind: Mapped[str] = mapped_column(String(32), default="parent_report")
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Hypothesis(Base):
    """待验证假设（L3）。模型生成默认 status='candidate'，不能自动升级为事实。"""

    __tablename__ = "hypotheses"
    __table_args__ = (Index("idx_hypotheses_child_status", "child_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id"), nullable=False
    )
    hypothesis_text: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_observation_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    contradicting_observation_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    confidence_level: Mapped[str] = mapped_column(String(16), default="low")
    scope_conditions_json: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    created_by: Mapped[str] = mapped_column(String(32), default="llm")
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryEvent(Base):
    """记忆变更审计。每次写入/修正/删除都留痕。"""

    __tablename__ = "memory_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("children.id"), nullable=False
    )
    memory_table: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    after_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_conversation_id: Mapped[str | None] = mapped_column(String(128))
    source_turn_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_by_parent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
