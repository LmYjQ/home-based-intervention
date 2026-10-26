"""阶段 3：给长期记忆补"可追溯 / 可纠正 / 不重复"需要的列和索引。

**为什么 001 建的表不够用**：001 按"四层记忆"的语义建了表，但只想着
"存下来"，没想着：

1. **可追溯** —— 每条记忆要知道自己是从哪一轮、哪句原话来的。
   没有 ``source_quote``，"家长可以核对"就是空话。
2. **可纠正** —— 计划 §10.5 要求旧记录标 ``superseded``、新记录写
   ``supersedes_memory_id``，不能直接覆盖历史。001 没有这些列。
3. **不重复** —— 追问恢复会换新的 ``turn_id``，同一段事实会被反复抽到。
   去重必须按**内容哈希**，不能按 turn。
4. **可删除** —— 软删除需要 ``deleted_at``，删掉的记忆不能再进 prompt。

**顺带修一个 001 就埋下的坑**：``memory_events.source_conversation_id``
是 ``VARCHAR(64)``，但真实会话 id 是 ``family_uuid.child_uuid.后缀`` =
86 字符。002 加宽了其它五张表，漏了这张 —— 因为在这之前**没有任何代码
写过 memory_events**，所以一直没暴露。阶段 3 第一次写审计就会撞上
``StringDataRightTruncation``。

**部分唯一索引的谓词为什么带 status**：已经作废/删除的内容以后可能被
重新提起，不该被唯一索引挡住；只有"活着的"记忆之间才算重复。
"""

from __future__ import annotations

from alembic import op

revision: str = "005_memory_provenance"
down_revision: str | None = "004_chinese_guideline_source"
branch_labels = None
depends_on = None


# 两张记忆表要加的列完全一样（observations 多一个 is_attempt）。
#
# 单独写成函数而不是两条手抄的 SQL：抄漏一列要到运行时才发现，
# 而两张表的列一旦不对称，共享的读取代码就会在某一类记忆上莫名报错。
_MEMORY_COLUMNS = """
    ADD COLUMN content_hash            VARCHAR(64),
    ADD COLUMN source_interaction_id   UUID REFERENCES interactions(id) ON DELETE SET NULL,
    ADD COLUMN source_feedback_id      UUID REFERENCES feedbacks(id) ON DELETE SET NULL,
    ADD COLUMN source_turn_id          VARCHAR(64),
    ADD COLUMN source_quote            TEXT NOT NULL DEFAULT '',
    ADD COLUMN supersedes_memory_id    VARCHAR(64),
    ADD COLUMN superseded_by_memory_id VARCHAR(64),
    ADD COLUMN deleted_at              TIMESTAMPTZ,
    ADD COLUMN updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
"""

_MEMORY_DROP_COLUMNS = """
    DROP COLUMN IF EXISTS content_hash,
    DROP COLUMN IF EXISTS source_interaction_id,
    DROP COLUMN IF EXISTS source_feedback_id,
    DROP COLUMN IF EXISTS source_turn_id,
    DROP COLUMN IF EXISTS source_quote,
    DROP COLUMN IF EXISTS supersedes_memory_id,
    DROP COLUMN IF EXISTS superseded_by_memory_id,
    DROP COLUMN IF EXISTS deleted_at,
    DROP COLUMN IF EXISTS updated_at
"""


def upgrade() -> None:
    # ---- 1. 修 002 漏掉的列宽 ----
    op.execute(
        "ALTER TABLE memory_events "
        "ALTER COLUMN source_conversation_id TYPE VARCHAR(128);"
    )

    # ---- 2. observations：溯源 + 软删 + 状态 ----
    op.execute(f"ALTER TABLE observations {_MEMORY_COLUMNS};")
    op.execute(
        """
        ALTER TABLE observations
            ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active',
            ADD COLUMN is_attempt BOOLEAN NOT NULL DEFAULT false;
        """
    )
    op.execute(
        """
        ALTER TABLE observations
            ADD CONSTRAINT ck_observations_status
            CHECK (status IN ('active', 'pending_confirmation', 'superseded', 'deleted'));
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_observations_child_content
            ON observations(child_id, content_hash)
            WHERE deleted_at IS NULL
              AND status IN ('active', 'pending_confirmation')
              AND content_hash IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_observations_child_status_time
            ON observations(child_id, status, observed_at DESC)
            WHERE deleted_at IS NULL;
        """
    )

    # ---- 3. hypotheses：同样的溯源列 + status 取值收窄 ----
    op.execute(f"ALTER TABLE hypotheses {_MEMORY_COLUMNS};")
    # 老库里 status 只有默认值 'candidate'，但保险起见把越界的先归一。
    op.execute(
        """
        UPDATE hypotheses SET status = 'candidate'
        WHERE status NOT IN ('candidate', 'active', 'weakened', 'superseded', 'deleted');
        """
    )
    op.execute(
        """
        ALTER TABLE hypotheses
            ADD CONSTRAINT ck_hypotheses_status
            CHECK (status IN ('candidate', 'active', 'weakened', 'superseded', 'deleted'));
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_hypotheses_child_content
            ON hypotheses(child_id, content_hash)
            WHERE deleted_at IS NULL
              AND status IN ('candidate', 'active', 'weakened')
              AND content_hash IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_hypotheses_child_status_updated
            ON hypotheses(child_id, status, updated_at DESC)
            WHERE deleted_at IS NULL;
        """
    )

    # ---- 4. memory_events：重放保护 ----
    #
    # 唯一键是**一次写入动作**的身份，不是记忆内容的身份：
    # 同一个候选在同一个 turn 里重放两次只该留一条事件；
    # 换成另一个 turn 再写（比如家长后来真的纠正了）是新的动作，要留痕。
    op.execute("ALTER TABLE memory_events ADD COLUMN content_hash VARCHAR(64);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_memory_events_turn_candidate
            ON memory_events(
                child_id, source_conversation_id, source_turn_id, content_hash
            )
            WHERE source_turn_id IS NOT NULL
              AND content_hash IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_memory_events_child_time
            ON memory_events(child_id, created_at DESC);
        """
    )

    # ---- 5. 事实仍然存在 children 的 JSONB 里 ----
    #
    # 元素从"字符串"升级成"对象"（带 id / 状态 / 原话），但外层必须还是
    # 数组 —— 读取侧兼容旧字符串，写入侧只写对象。加个 CHECK 是为了
    # 让"有人手滑写成对象"在入库时就报错，而不是到 prompt 拼接时才炸。
    op.execute(
        """
        ALTER TABLE children
            ADD CONSTRAINT ck_children_confirmed_facts_is_array
            CHECK (jsonb_typeof(confirmed_facts_json) = 'array');
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE children DROP CONSTRAINT IF EXISTS ck_children_confirmed_facts_is_array;"
    )

    op.execute("DROP INDEX IF EXISTS idx_memory_events_child_time;")
    op.execute("DROP INDEX IF EXISTS uq_memory_events_turn_candidate;")
    op.execute("ALTER TABLE memory_events DROP COLUMN IF EXISTS content_hash;")

    op.execute("DROP INDEX IF EXISTS idx_hypotheses_child_status_updated;")
    op.execute("DROP INDEX IF EXISTS uq_hypotheses_child_content;")
    op.execute("ALTER TABLE hypotheses DROP CONSTRAINT IF EXISTS ck_hypotheses_status;")
    op.execute(f"ALTER TABLE hypotheses {_MEMORY_DROP_COLUMNS};")

    op.execute("DROP INDEX IF EXISTS idx_observations_child_status_time;")
    op.execute("DROP INDEX IF EXISTS uq_observations_child_content;")
    op.execute(
        "ALTER TABLE observations DROP CONSTRAINT IF EXISTS ck_observations_status;"
    )
    op.execute(
        "ALTER TABLE observations DROP COLUMN IF EXISTS is_attempt, "
        "DROP COLUMN IF EXISTS status;"
    )
    op.execute(f"ALTER TABLE observations {_MEMORY_DROP_COLUMNS};")

    # 收窄会截断超长 id，所以只在确认没有超长数据时才允许回退。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM memory_events WHERE length(source_conversation_id) > 64
            ) THEN
                RAISE EXCEPTION
                    'memory_events 里有 source_conversation_id 超过 64 字符，收窄会截断数据';
            END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE memory_events "
        "ALTER COLUMN source_conversation_id TYPE VARCHAR(64);"
    )
