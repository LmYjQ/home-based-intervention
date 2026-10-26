"""阶段 4：反馈迭代需要的列。

**为什么要落这些列**：反馈流程现在有三件事必须跨请求记住：

1. **执行状态**——``attempted`` / ``not_attempted`` / ``stopped`` 三种情况
   要分开，trial 不再是永远 ``accepted``；
2. **分析和下一步**——重试同一个 ``turn_id`` 不该再跑一遍 LLM，
   刷新页面也要能拿回上次的结果；
3. **追问**——反馈不足时会 interrupt 追问，答案要留痕。

``completed_at`` 非空 = 这次反馈已经处理完，API 直接返回缓存。
"""

from __future__ import annotations

from alembic import op

revision: str = "008_feedback_iteration"
down_revision: str | None = "007_display_label_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE feedbacks
            ADD COLUMN IF NOT EXISTS stopped BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS analysis_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS next_step_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS followup_answers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS followup_rounds INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedbacks_trial_submitted "
        "ON feedbacks (trial_id, submitted_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_feedbacks_trial_submitted;")
    op.execute(
        """
        ALTER TABLE feedbacks
            DROP COLUMN IF EXISTS stopped,
            DROP COLUMN IF EXISTS analysis_json,
            DROP COLUMN IF EXISTS next_step_json,
            DROP COLUMN IF EXISTS followup_answers_json,
            DROP COLUMN IF EXISTS result_json,
            DROP COLUMN IF EXISTS followup_rounds,
            DROP COLUMN IF EXISTS analyzed_at,
            DROP COLUMN IF EXISTS completed_at;
        """
    )
