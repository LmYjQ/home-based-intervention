"""孩子的出生日期（用于实时计算年龄）。

**只加出生日期，不加年龄字段**：年龄会随时间变化，存下来就得有人负责更新；
出生日期不变，读的时候按 ``date.today()`` 现算。

``age_band`` 列保留：老档案（以及只按年龄段建的档案）还要用。
有 ``birth_date`` 时由代码算出的段位优先。
"""

from __future__ import annotations

from alembic import op

revision: str = "006_child_birth_date"
down_revision: str | None = "005_memory_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE children ADD COLUMN IF NOT EXISTS birth_date DATE;")


def downgrade() -> None:
    op.execute("ALTER TABLE children DROP COLUMN IF EXISTS birth_date;")
