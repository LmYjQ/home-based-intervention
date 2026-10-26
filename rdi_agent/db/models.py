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
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    #
    # 用 'simple' 而不是 'english'：知识库现在是中文指南，
    # 英文配置对中文无效（整段中文会变成一个 token）。
    # 'simple' 靠**空格**分词，而入库时 content_normalized 已经用
    # jieba 分好词了 —— 每个词因此都是独立的 lexeme。
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(content_normalized, ''))", persisted=True),
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
    """检索用术语表（B 层简化版）。首版只做 30–50 个核心术语。

    ``canonical_name`` 是**规范名** —— 知识库里真的用这个词。
    以前规范名是英文（书是英文的），所以叫 ``canonical_name_en``；
    现在知识库是中文指南，规范名是中文术语，列名去掉了 ``_en``。
    """

    __tablename__ = "concept_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    concept_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_zh_json: Mapped[list] = mapped_column(JSONB, default=list)
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
    # 称呼。**不限制长度**（TEXT）—— 手动测试台里这个字段实际被当成
    # "孩子的描述"用，有人会写一长串背景情况。见迁移 007。
    display_label: Mapped[str] = mapped_column(Text, nullable=False)
    age_band: Mapped[str | None] = mapped_column(String(32))
    # 年龄不存，读的时候按这个日期现算（见 rdi_agent/age.py）。
    # 老档案没有出生日期，继续用 age_band 兜底。
    birth_date: Mapped[date | None] = mapped_column(Date)
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
    # 产生这一版方案的那一轮（聊天 turn 或修订 turn）。
    # 修订请求按它判重：同一个 turn_id 不生成第二版。
    source_turn_id: Mapped[str | None] = mapped_column(String(64))
    # 分叉起点：生成这一版之前的那个 checkpoint。
    # 修订时从这里派生新分支，事实/证据/门控结论都沿用。
    fork_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
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
    # 这次尝试执行的是**哪个版本**的方案卡。方案可能被调整过，
    # 反馈必须能说清"当时执行的是第几版"，否则历史对比会把两版混在一起。
    plan_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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
    # 做了但中途停：和"没做"分开记，见 FeedbackSubmission.execution_status
    stopped: Mapped[bool] = mapped_column(default=False, nullable=False)
    implementation_notes: Mapped[str] = mapped_column(Text, default="")
    child_response_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    parent_observations_json: Mapped[list] = mapped_column(JSONB, default=list)
    difficulties_json: Mapped[list] = mapped_column(JSONB, default=list)
    # 分析/下一步/追问答案都落库：
    # 1. 重试同一个 turn_id 不必再跑一遍 LLM；
    # 2. 刷新页面能拿回上次的分析结果；
    # 3. completed_at 非空 = 这次反馈已经处理完。
    analysis_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    next_step_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    followup_answers_json: Mapped[list] = mapped_column(JSONB, default=list)
    result_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    followup_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Observation(Base):
    """情境观察（L2）。只写"发生了什么"，不写"孩子就是XX型"。"""

    __tablename__ = "observations"
    __table_args__ = (
        Index("idx_observations_child_time", "child_id", "observed_at"),
        # 同一条观察重复抽到只留一行。谓词把已作废/已删除的排除掉 ——
        # 以后同样的事再发生一次，是一条**新的**观察，不该被旧行挡住。
        Index(
            "uq_observations_child_content",
            "child_id",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL "
                "AND status IN ('active', 'pending_confirmation') "
                "AND content_hash IS NOT NULL"
            ),
        ),
        Index(
            "idx_observations_child_status_time",
            "child_id",
            "status",
            "observed_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

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

    # ---- 阶段 3：可追溯 / 可纠正 / 不重复 ----
    #
    # ``content_hash`` 是去重键（memory_type + 归一化内容），跨 turn 生效：
    # 追问恢复会换新的 turn_id，按 turn 去重挡不住重复抽取。
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    # 家长说"我们试过 X"记成观察时置 true。**不能**用系统给过的建议反推 ——
    # "建议过"不等于"做过"，那是 trials 的事。
    is_attempt: Mapped[bool] = mapped_column(default=False, nullable=False)
    source_interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interactions.id", ondelete="SET NULL")
    )
    source_feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("feedbacks.id", ondelete="SET NULL")
    )
    source_turn_id: Mapped[str | None] = mapped_column(String(64))
    # 家长原话，逐字。审核者靠它核对，没有它这条记忆不可信。
    source_quote: Mapped[str] = mapped_column(Text, default="", nullable=False)
    supersedes_memory_id: Mapped[str | None] = mapped_column(String(64))
    superseded_by_memory_id: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Hypothesis(Base):
    """待验证假设（L3）。模型生成默认 status='candidate'，不能自动升级为事实。"""

    __tablename__ = "hypotheses"
    __table_args__ = (
        Index("idx_hypotheses_child_status", "child_id", "status"),
        Index(
            "uq_hypotheses_child_content",
            "child_id",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL "
                "AND status IN ('candidate', 'active', 'weakened') "
                "AND content_hash IS NOT NULL"
            ),
        ),
        Index(
            "idx_hypotheses_child_status_updated",
            "child_id",
            "status",
            "updated_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

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

    # ---- 阶段 3 ----
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source_interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interactions.id", ondelete="SET NULL")
    )
    source_feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("feedbacks.id", ondelete="SET NULL")
    )
    source_turn_id: Mapped[str | None] = mapped_column(String(64))
    source_quote: Mapped[str] = mapped_column(Text, default="", nullable=False)
    supersedes_memory_id: Mapped[str | None] = mapped_column(String(64))
    superseded_by_memory_id: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryEvent(Base):
    """记忆变更审计。每次写入/修正/删除都留痕。"""

    __tablename__ = "memory_events"
    __table_args__ = (
        # 重放保护。**按动作而不是按内容**：同一个候选在同一轮里重放两次
        # 只该留一条事件；换一轮再写是新的动作，要留痕。
        Index(
            "uq_memory_events_turn_candidate",
            "child_id",
            "source_conversation_id",
            "source_turn_id",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "source_turn_id IS NOT NULL AND content_hash IS NOT NULL"
            ),
        ),
        Index("idx_memory_events_child_time", "child_id", "created_at"),
    )

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
    # 一次写入动作的身份。同一个候选在同一个 turn 里重放两次只留一条事件。
    content_hash: Mapped[str | None] = mapped_column(String(64))
    confirmed_by_parent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
