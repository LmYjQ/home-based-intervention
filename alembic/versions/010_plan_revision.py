"""方案修订：记录"这一版是哪一轮产生的、从哪个 checkpoint 分叉"。

**为什么要落这两列**：家长在反馈里补充新情况后，系统从"生成建议卡"
之前的 checkpoint 派生新分支，只重跑生成及之后的安全审查/渲染/落库。
回溯点必须精确到**这一版方案**对应的那个 checkpoint ——
多轮会话里"最后一个待生成检查点"未必是这一轮的，
定位错了就会拿错的事实和证据去生成新卡。

``source_turn_id`` 同时是修订请求的幂等依据：同一个 ``turn_id``
重复提交时，靠它认出"这一版已经生成过了"。
"""

from __future__ import annotations

from alembic import op

revision: str = "010_plan_revision"
down_revision: str | None = "009_trial_plan_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE plans
            ADD COLUMN IF NOT EXISTS source_turn_id VARCHAR(64),
            ADD COLUMN IF NOT EXISTS fork_checkpoint_id VARCHAR(255);
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plans_conversation_source_turn "
        "ON plans (conversation_id, source_turn_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_plans_conversation_source_turn;")
    op.execute(
        """
        ALTER TABLE plans
            DROP COLUMN IF EXISTS source_turn_id,
            DROP COLUMN IF EXISTS fork_checkpoint_id;
        """
    )
