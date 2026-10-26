"""孩子的称呼改成 TEXT（不限制长度）。

**为什么**：手动测试台里"称呼"这个字段实际被当成**孩子的描述**用 ——
有人会写一长串"4 岁男孩，确诊自闭症，合并语言迟缓，平时用图片交流……"
当背景写给模型。``VARCHAR(128)`` 存不下会直接报错。

**降级可能失败**：有超长数据时降回 ``VARCHAR(128)`` 会报错。
这是故意的 —— 强行截断会把家长写的描述弄坏，宁可降级失败也不要静默截断。
"""

from __future__ import annotations

from alembic import op

revision: str = "007_display_label_text"
down_revision: str | None = "006_child_birth_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE children ALTER COLUMN display_label TYPE TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE children ALTER COLUMN display_label TYPE VARCHAR(128);")
