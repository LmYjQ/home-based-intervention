"""给 evidence_bundles 加唯一约束。

**为什么需要**：阶段 2C 开始把证据包落库（``evidence_bundles`` 表在
``001_init`` 里就有了，但一直没人写）。这张表没有唯一约束的话，
同一个 (conversation_id, turn_id) 重试一次就会多写一条 ——
之后追溯"当时检索到了什么"就会看到好几份不一样的记录。

仓库的幂等约定一直是"靠数据库唯一约束挡重放"，不是靠先查后写
（``interactions`` 的 UNIQUE(conversation_id, turn_id, role)、
``feedbacks`` 的 UNIQUE(trial_id, turn_id) 都是这么做的）。
先查后写在并发下不成立 —— 两个请求同时查、同时写，就都写进去了。

所以给 ``evidence_bundles`` 加 ``UNIQUE(conversation_id, turn_id)``，
写入用 ``ON CONFLICT DO NOTHING``。

**注意**：表里已经有阶段 2B 之前遗留的行吗？没有 —— 这张表在 2C 之前
没有任何写入方。加约束不会撞上重复数据。
"""

from __future__ import annotations

from alembic import op

revision: str = "003_evidence_bundle_unique"
down_revision: str | None = "002_widen_conversation_id"

_CONSTRAINT = "uq_evidence_bundles_conversation_turn"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE evidence_bundles
        ADD CONSTRAINT {_CONSTRAINT}
        UNIQUE (conversation_id, turn_id)
        """
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE evidence_bundles DROP CONSTRAINT IF EXISTS {_CONSTRAINT}"
    )
