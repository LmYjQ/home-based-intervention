"""给长期记忆加"类目"和"类目小结"。

**为什么加 category**：计划 §10.2 说 L1 稳定档案是"一份'当前怎么看这个
孩子'的快照"，但从来没定义过这份快照长什么样。结果事实越攒越多，
每次回答都只能靠关键词重合度从一堆散条里挑 —— 越攒越噪。

有了类目，同类的记录才能合成一句"现状"，那就是"合并"。

**为什么小结单独一列**：小结是**系统归纳**出来的，家长确认过的事实是
**家长说的**。两者语义不同、状态不同、生命周期不同 ——
混在一列里，代码就得靠字段猜这条是家长说的还是系统推的。
分成两列，读取侧一眼就分清，纠正/删除也能分别处理。

**老数据不回填 category**：分类是给"新抽的"明细用的，
让模型给三个月前的记录补类目只会得到一堆编的分类。
老 observation 的 category 留 NULL，按 ``other`` 处理。
"""

from __future__ import annotations

from alembic import op

revision: str = "011_memory_categories"
down_revision: str | None = "010_plan_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. observations 加 category ----
    #
    # 允许 NULL：老行没有类目，按"未归类"处理（等价于 other）。
    # 加 CHECK 是为了挡住拼错的类目名 —— 枚举写死在
    # ``memory/categories.py``，这里和它保持一致。
    op.execute(
        """
        ALTER TABLE observations
            ADD COLUMN IF NOT EXISTS category VARCHAR(32);
        """
    )
    op.execute(
        """
        ALTER TABLE observations
            ADD CONSTRAINT ck_observations_category
            CHECK (
                category IS NULL OR category IN (
                    'separation', 'communication', 'social', 'emotion',
                    'sensory_motor', 'daily', 'family', 'other'
                )
            );
        """
    )
    # 合并时要按"类目 + 状态"捞明细，给它一个索引。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_child_category
            ON observations (child_id, category, observed_at DESC)
            WHERE deleted_at IS NULL;
        """
    )

    # ---- 2. children 加 memory_summaries_json ----
    op.execute(
        """
        ALTER TABLE children
            ADD COLUMN IF NOT EXISTS memory_summaries_json JSONB NOT NULL DEFAULT '[]'::jsonb;
        """
    )
    op.execute(
        """
        ALTER TABLE children
            ADD CONSTRAINT ck_children_memory_summaries_is_array
            CHECK (jsonb_typeof(memory_summaries_json) = 'array');
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE children "
        "DROP CONSTRAINT IF EXISTS ck_children_memory_summaries_is_array;"
    )
    op.execute("ALTER TABLE children DROP COLUMN IF EXISTS memory_summaries_json;")

    op.execute("DROP INDEX IF EXISTS idx_observations_child_category;")
    op.execute(
        "ALTER TABLE observations "
        "DROP CONSTRAINT IF EXISTS ck_observations_category;"
    )
    op.execute("ALTER TABLE observations DROP COLUMN IF EXISTS category;")
