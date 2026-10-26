"""阶段 4：trial 记录自己执行的是哪一版方案。

**为什么不直接 join plans.plan_version**：方案可能被后续调整覆盖，
反馈必须能说清"当时执行的是第几版"。把版本写在 trial 上，
历史尝试不会因为方案表更新而改变含义。
"""

from __future__ import annotations

from alembic import op

revision: str = "009_trial_plan_version"
down_revision: str | None = "008_feedback_iteration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials "
        "ADD COLUMN IF NOT EXISTS plan_version INTEGER NOT NULL DEFAULT 1;"
    )
    # 存量 trial 从它关联的方案回填一次。
    op.execute(
        """
        UPDATE trials t
        SET plan_version = p.plan_version
        FROM plans p
        WHERE p.id = t.plan_id AND t.plan_version = 1;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_child_status_time "
        "ON trials (child_id, status, created_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_child_status_time;")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS plan_version;")
