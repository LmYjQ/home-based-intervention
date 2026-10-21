"""加宽 conversation_id / thread_id。

**为什么需要**：阶段 0 把这两列定成 VARCHAR(64)，当时假设 id 是短的。
阶段 1 改成 ``family_id.child_id.随机后缀`` 之后，一个 UUID 是 36 字符，
两段 UUID 加分隔符再加 12 位后缀 = **86 字符**，64 不够，写入直接报
``StringDataRightTruncation``。

这不是"临时凑合"：``conversation_id`` 要在 k8s 里长期当业务主键用，
URL 里也要出现，所以宁可一次给够（128），也不要再迁一次。

``checkpoints`` 那些表是 LangGraph 自己的，不归本迁移管。
"""

from __future__ import annotations

from alembic import op

revision: str = "002_widen_conversation_id"
down_revision: str | None = "001_init"

# 阶段 1 的 conversation_id 实际会用到的列
_TABLES = [
    "interactions",
    "evidence_bundles",
    "plans",
    "trials",
    "observations",
]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN conversation_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE interactions ALTER COLUMN thread_id TYPE VARCHAR(128)")


def downgrade() -> None:
    # 缩回去会截断已有的长 id，所以只在确认表是空的时候才允许回退
    for table in _TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM {table} WHERE length(conversation_id) > 64) THEN
                    RAISE EXCEPTION
                        '{table} 里有 conversation_id 超过 64 字符，收窄会截断数据';
                END IF;
            END $$
            """
        )
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN conversation_id TYPE VARCHAR(64)"
        )
    op.execute("ALTER TABLE interactions ALTER COLUMN thread_id TYPE VARCHAR(64)")