"""测试账号接口的入参校验。

**为什么单独测这个**：姓名和出生年月改成必填之后，前端按钮已经禁用了，
但接口本身也得拦住 —— 前端能被绕过，接口不能。
这些校验都在碰数据库**之前**做，所以不需要连库。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import Text

from rdi_agent.api.children import CreateChildRequest, create_child
from rdi_agent.db.models import Child


@pytest.mark.asyncio
async def test_missing_name_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await create_child(CreateChildRequest(birth_date="2022-05"))
    assert exc.value.status_code == 400
    assert "名字" in exc.value.detail


@pytest.mark.asyncio
async def test_blank_name_is_rejected():
    """只填空格也不行 —— 存进去就是个空名字。"""
    with pytest.raises(HTTPException) as exc:
        await create_child(CreateChildRequest(display_label="   ", birth_date="2022-05"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_birth_date_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await create_child(CreateChildRequest(display_label="小明"))
    assert exc.value.status_code == 400
    assert "出生年月" in exc.value.detail


@pytest.mark.asyncio
async def test_future_birth_date_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await create_child(
            CreateChildRequest(display_label="小明", birth_date="2999-01")
        )
    assert exc.value.status_code == 400
    assert "不能晚于今天" in exc.value.detail


def test_display_label_column_is_text():
    """姓名不限制长度 —— 列必须是 TEXT，不能还是 VARCHAR(128)。

    这个字段实际被当"孩子的描述"用：有人会写一长串背景情况。
    存不下会直接报 value too long。
    """
    assert isinstance(Child.__table__.c.display_label.type, Text)
