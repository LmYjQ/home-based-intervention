"""001 init：知识库 + 业务表

Revision ID: 001_init
Revises:
Create Date: 2026-10-21

用原生 SQL 建表而不是 op.create_table，原因有两个：

1. ``content_tsv`` 必须是生成列（GENERATED ALWAYS AS ... STORED），
   生成列只能靠原生 DDL 表达。
2. ``embedding vector(1024)`` 和 HNSW 索引需要 pgvector 扩展。

建表前先 CREATE EXTENSION vector。扩展不存在时迁移会直接失败，
这是刻意的：静默降级成普通列会让检索悄悄变慢，比启动失败更难查。
"""

from __future__ import annotations

from alembic import op

revision: str = "001_init"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # ------------------------------------------------------------ 知识库
    op.execute(
        """
        CREATE TABLE knowledge_sources (
            id                UUID PRIMARY KEY,
            source_id         VARCHAR(128) NOT NULL UNIQUE,
            source_type       VARCHAR(32)  NOT NULL,
            title             TEXT         NOT NULL,
            authors_json      JSONB        NOT NULL DEFAULT '[]'::jsonb,
            publication_year  INTEGER,
            language          VARCHAR(16)  NOT NULL DEFAULT 'en',
            rights_status     VARCHAR(32)  NOT NULL DEFAULT 'internal_review_only',
            source_uri        TEXT,
            content_hash      VARCHAR(128),
            version           VARCHAR(32)  NOT NULL DEFAULT 'v1',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE book_sections (
            id                UUID PRIMARY KEY,
            source_id         UUID NOT NULL REFERENCES knowledge_sources(id),
            parent_section_id UUID REFERENCES book_sections(id),
            section_code      VARCHAR(64)  NOT NULL,
            title_original    TEXT         NOT NULL,
            title_zh          TEXT,
            section_order     INTEGER      NOT NULL DEFAULT 0,
            pdf_page_start    INTEGER,
            pdf_page_end      INTEGER,
            print_page_start  INTEGER,
            print_page_end    INTEGER,
            parse_status      VARCHAR(32)  NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_book_sections_source_code UNIQUE (source_id, section_code)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE knowledge_chunks (
            id                UUID PRIMARY KEY,
            source_id         UUID NOT NULL REFERENCES knowledge_sources(id),
            section_id        UUID REFERENCES book_sections(id),
            parent_chunk_id   UUID REFERENCES knowledge_chunks(id),
            chunk_index       INTEGER      NOT NULL DEFAULT 0,
            content_original  TEXT         NOT NULL,
            content_normalized TEXT        NOT NULL,
            content_tsv       tsvector GENERATED ALWAYS AS (
                                to_tsvector('english', coalesce(content_normalized, ''))
                            ) STORED,
            embedding         vector(1024),
            summary_zh        TEXT,
            knowledge_type    VARCHAR(32)  NOT NULL DEFAULT 'unknown',
            concept_ids_json  JSONB        NOT NULL DEFAULT '[]'::jsonb,
            pdf_page_start    INTEGER,
            pdf_page_end      INTEGER,
            print_page_start  INTEGER,
            print_page_end    INTEGER,
            parse_confidence  DOUBLE PRECISION,
            review_status     VARCHAR(32)  NOT NULL DEFAULT 'unreviewed',
            embedding_model   VARCHAR(128),
            embedding_version VARCHAR(32),
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_chunks_section ON knowledge_chunks(section_id);")
    op.execute(
        "CREATE INDEX idx_chunks_pages ON knowledge_chunks(pdf_page_start, pdf_page_end);"
    )
    op.execute("CREATE INDEX idx_chunks_tsv ON knowledge_chunks USING GIN (content_tsv);")
    op.execute(
        """
        CREATE INDEX idx_chunks_embedding ON knowledge_chunks
            USING hnsw (embedding vector_cosine_ops);
        """
    )

    op.execute(
        """
        CREATE TABLE concept_aliases (
            id                    UUID PRIMARY KEY,
            concept_id            VARCHAR(64) NOT NULL UNIQUE,
            canonical_name_en     TEXT        NOT NULL,
            aliases_zh_json       JSONB       NOT NULL DEFAULT '[]'::jsonb,
            aliases_en_json       JSONB       NOT NULL DEFAULT '[]'::jsonb,
            source_chunk_ids_json JSONB       NOT NULL DEFAULT '[]'::jsonb,
            is_explicit_in_source BOOLEAN     NOT NULL DEFAULT false,
            limitations_json      JSONB       NOT NULL DEFAULT '[]'::jsonb,
            review_status         VARCHAR(32) NOT NULL DEFAULT 'unreviewed',
            version               VARCHAR(32) NOT NULL DEFAULT 'v1',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # ------------------------------------------------------------ 业务表
    op.execute(
        """
        CREATE TABLE children (
            id                        UUID PRIMARY KEY,
            family_id                 UUID NOT NULL,
            display_label             VARCHAR(128) NOT NULL,
            age_band                  VARCHAR(32),
            communication_profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            strengths_interests_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
            support_preferences_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            family_goals_json         JSONB NOT NULL DEFAULT '[]'::jsonb,
            known_contexts_json       JSONB NOT NULL DEFAULT '[]'::jsonb,
            confirmed_facts_json      JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE interactions (
            id             UUID PRIMARY KEY,
            family_id      UUID NOT NULL,
            child_id       UUID REFERENCES children(id),
            conversation_id VARCHAR(64) NOT NULL,
            thread_id      VARCHAR(64) NOT NULL,
            turn_id        VARCHAR(64) NOT NULL,
            role           VARCHAR(16) NOT NULL,
            content        TEXT NOT NULL,
            trace_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_interactions_turn_role UNIQUE (conversation_id, turn_id, role)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE evidence_bundles (
            id               UUID PRIMARY KEY,
            conversation_id  VARCHAR(64) NOT NULL,
            turn_id          VARCHAR(64) NOT NULL,
            bundle_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
            retrieval_version VARCHAR(64) NOT NULL DEFAULT 'v1',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE plans (
            id                UUID PRIMARY KEY,
            family_id         UUID NOT NULL,
            child_id          UUID REFERENCES children(id),
            conversation_id   VARCHAR(64) NOT NULL,
            plan_version      INTEGER NOT NULL DEFAULT 1,
            plan_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_bundle_id UUID REFERENCES evidence_bundles(id),
            application_label VARCHAR(32) NOT NULL,
            recommended       BOOLEAN NOT NULL DEFAULT false,
            status            VARCHAR(16) NOT NULL DEFAULT 'proposed',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE trials (
            id                   UUID PRIMARY KEY,
            plan_id              UUID NOT NULL REFERENCES plans(id),
            child_id             UUID NOT NULL REFERENCES children(id),
            conversation_id      VARCHAR(64) NOT NULL,
            target               TEXT NOT NULL,
            planned_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            status               VARCHAR(16) NOT NULL DEFAULT 'accepted',
            started_at           TIMESTAMPTZ,
            ended_at             TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_trials_child_time ON trials(child_id, created_at DESC);"
    )

    op.execute(
        """
        CREATE TABLE feedbacks (
            id                      UUID PRIMARY KEY,
            trial_id                UUID NOT NULL REFERENCES trials(id),
            turn_id                 VARCHAR(64) NOT NULL,
            attempted               BOOLEAN NOT NULL DEFAULT false,
            implementation_notes    TEXT NOT NULL DEFAULT '',
            child_response_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
            parent_observations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            difficulties_json       JSONB NOT NULL DEFAULT '[]'::jsonb,
            submitted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_feedbacks_trial_turn UNIQUE (trial_id, turn_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE observations (
            id                  UUID PRIMARY KEY,
            child_id            UUID NOT NULL REFERENCES children(id),
            conversation_id     VARCHAR(64),
            observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            setting             VARCHAR(128),
            participants_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
            antecedent          TEXT NOT NULL DEFAULT '',
            observable_behavior TEXT NOT NULL DEFAULT '',
            adult_response      TEXT NOT NULL DEFAULT '',
            child_response      TEXT NOT NULL DEFAULT '',
            outcome             TEXT NOT NULL DEFAULT '',
            source_kind         VARCHAR(32) NOT NULL DEFAULT 'parent_report',
            confidence          DOUBLE PRECISION,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_observations_child_time
            ON observations(child_id, observed_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE hypotheses (
            id                                UUID PRIMARY KEY,
            child_id                          UUID NOT NULL REFERENCES children(id),
            hypothesis_text                   TEXT NOT NULL,
            supporting_observation_ids_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
            contradicting_observation_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence_level                  VARCHAR(16) NOT NULL DEFAULT 'low',
            scope_conditions_json             JSONB NOT NULL DEFAULT '[]'::jsonb,
            status                            VARCHAR(16) NOT NULL DEFAULT 'candidate',
            created_by                        VARCHAR(32) NOT NULL DEFAULT 'llm',
            last_reviewed_at                  TIMESTAMPTZ,
            created_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_hypotheses_child_status ON hypotheses(child_id, status);"
    )

    op.execute(
        """
        CREATE TABLE memory_events (
            id                    UUID PRIMARY KEY,
            child_id              UUID NOT NULL REFERENCES children(id),
            memory_table          VARCHAR(32) NOT NULL,
            memory_id             VARCHAR(64) NOT NULL,
            event_type            VARCHAR(32) NOT NULL,
            before_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
            after_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_conversation_id VARCHAR(64),
            source_turn_id        VARCHAR(64),
            confirmed_by_parent   BOOLEAN NOT NULL DEFAULT false,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    for table in (
        "memory_events",
        "hypotheses",
        "observations",
        "feedbacks",
        "trials",
        "plans",
        "evidence_bundles",
        "interactions",
        "children",
        "concept_aliases",
        "knowledge_chunks",
        "book_sections",
        "knowledge_sources",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table};")
    # 扩展不删：别的 schema 可能还在用，且 pgvector 的删除会连带索引一起掉
