"""记忆改成两层：短句 + 类目合并。

**这次改的是记忆的数据模型本身**。之前是三层 ——
L1 事实（``children.confirmed_facts_json``）、L2 观察（``observations``）、
L3 假设（``hypotheses``）。三层让"这条记忆怎么用"取决于它被归成哪一类，
而不是取决于它说了什么；而且抽取那一步要模型做类型判断，
实测它几乎总是判成"事实"，于是另外两层永远是空的。

现在只有两层：

1. **拆句** —— 家长的一段话拆成几个短句，每句只讲一件事。
2. **分类** —— 每个短句归一个类目。

同类合并之后，每个类目一条小结，存在 ``children.memory_summaries_json``。
**进 prompt 的是小结，短句只留在库里给家长看。**

**一张表装下所有短句**：``memory_items``。
原来"事实/偏好/观察/假设"的区别变成两个开关 ——
``is_preference``（家长的硬约束，不参与合并）和
``inference``（系统自己猜的，不参与合并）。
两个都关掉的才参与类目合并。

**老数据不迁移**：三层的老记录语义和新短句对不上，
硬搬过来只会得到一堆归错类的句子。开发阶段直接丢掉。
"""

from __future__ import annotations

from alembic import op

revision: str = "012_memory_items"
down_revision: str | None = "011_memory_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. 新建 memory_items ----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            id UUID PRIMARY KEY,
            child_id UUID NOT NULL REFERENCES children (id),
            conversation_id VARCHAR(128),
            observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            content TEXT NOT NULL,
            category VARCHAR(32),
            is_preference BOOLEAN NOT NULL DEFAULT FALSE,
            inference BOOLEAN NOT NULL DEFAULT FALSE,
            scope_conditions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_kind VARCHAR(32) NOT NULL DEFAULT 'parent_report',
            confidence DOUBLE PRECISION,
            content_hash VARCHAR(64),
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            source_interaction_id UUID REFERENCES interactions (id) ON DELETE SET NULL,
            source_feedback_id UUID REFERENCES feedbacks (id) ON DELETE SET NULL,
            source_turn_id VARCHAR(64),
            source_quote TEXT NOT NULL DEFAULT '',
            supersedes_memory_id VARCHAR(64),
            superseded_by_memory_id VARCHAR(64),
            deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        ALTER TABLE memory_items
            ADD CONSTRAINT ck_memory_items_category
            CHECK (
                category IS NULL OR category IN (
                    'separation', 'communication', 'social', 'emotion',
                    'sensory_motor', 'daily', 'family', 'other'
                )
            );
        """
    )
    # 同一条短句重复抽到只留一行。作废/删除的排除掉 ——
    # 以后同样的事再发生一次，是一条**新的**短句。
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_items_child_content
            ON memory_items (child_id, content_hash)
            WHERE deleted_at IS NULL
              AND status = 'active'
              AND content_hash IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_child_time
            ON memory_items (child_id, observed_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_child_status_time
            ON memory_items (child_id, status, observed_at DESC)
            WHERE deleted_at IS NULL;
        """
    )

    # ---- 2. 删掉旧的三层存储 ----
    op.execute("DROP TABLE IF EXISTS hypotheses;")
    op.execute("DROP TABLE IF EXISTS observations;")
    op.execute("ALTER TABLE children DROP COLUMN IF EXISTS confirmed_facts_json;")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE children
            ADD COLUMN IF EXISTS confirmed_facts_json JSONB NOT NULL DEFAULT '[]'::jsonb;
        """
    )
    op.execute("DROP TABLE IF EXISTS memory_items;")
