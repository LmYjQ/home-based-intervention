"""手动测试台的"账号"接口：新建 + 列出。

一个"账号"在这里就是 **一个家庭 + 一个孩子** —— 项目还没有登录体系
（阶段 5 再说），长期记忆挂在孩子上，所以想对比几种情况，
最快的办法是每种情况建一个孩子，每个孩子配一个新的 family_id。

这两个接口只服务于 ``frontend_rdi/`` 的手动测试台，**不是产品接口**：

- 列表不按 family 过滤，能看到本机所有测试账号
- 新建时不给 ``family_id`` 就随机生成一个（等于开了一个新账号）
- 不提供删除 —— 测试数据要留着对比；真要清理直接进库删

**姓名和出生年月都是必填**（2026-10-26 起）：

之前"留空自动叫测试账号 N"看着方便，但**自动命名出来的账号在列表里
长得一模一样**，根本分不清谁是谁；而出生年月不给的话，年龄只能靠
``age_band`` 兜底，算出来的年龄段经常是错的。

**姓名不限制长度**：这个字段实际被当成"孩子的描述"用 ——
有人会写一长串背景情况（诊断、语言状况、平时怎么沟通）当背景写给模型。
所以列是 ``TEXT``（迁移 007），前端也不做长度校验。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from rdi_agent.age import describe_age, parse_birth_date
from rdi_agent.db.engine import session_scope
from rdi_agent.db.repositories.case import Repository

router = APIRouter(prefix="/api", tags=["children"])


class CreateChildRequest(BaseModel):
    display_label: str | None = None
    birth_date: str | None = None
    age_band: str | None = None
    family_id: str | None = None


def _child_view(child: Any) -> dict[str, Any]:
    # 年龄现算，不读存量字段 —— 出生日期才是真的，年龄每天都在变。
    age = describe_age(child.birth_date)
    return {
        "child_id": str(child.id),
        "family_id": str(child.family_id),
        "display_label": child.display_label,
        "birth_date": age.get("birth_date"),
        "age_display": age.get("age_display"),
        "age_years": age.get("age_years"),
        "age_months": age.get("age_months"),
        "age_band": age.get("age_band") or child.age_band,
    }


@router.get("/children")
async def list_children(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    prefix: Annotated[
        str | None,
        Query(description="只看称呼以此开头的账号；测试台传「测试账号」"),
    ] = None,
) -> dict[str, Any]:
    """列出账号（新建的在前）。"""
    async with session_scope() as session:
        rows = await Repository(session).list_children(
            limit=limit, label_prefix=(prefix or "").strip() or None
        )
    return {"children": [_child_view(x) for x in rows]}


@router.post("/children")
async def create_child(req: CreateChildRequest) -> dict[str, Any]:
    """新建一个测试账号（默认新家庭 + 新孩子）。

    ``birth_date`` 接受 ``YYYY-MM``（前端只精确到月）或 ``YYYY-MM-DD``。
    只存出生日期，年龄由 :func:`rdi_agent.age.describe_age` 每次现算。

    **姓名和出生年月必填**：见模块说明。错误信息用中文说清楚缺什么。
    """
    family_id = req.family_id or str(uuid.uuid4())
    try:
        uuid.UUID(family_id)
    except ValueError:
        raise HTTPException(400, "family_id 不是合法 UUID") from None

    label = (req.display_label or "").strip()
    if not label:
        raise HTTPException(400, "请填写孩子的名字或描述") from None

    if not (req.birth_date or "").strip():
        raise HTTPException(400, "请选择出生年月") from None

    try:
        birth = parse_birth_date(req.birth_date)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if birth is not None and birth > date.today():
        raise HTTPException(400, "出生年月不能晚于今天")

    async with session_scope() as session:
        repo = Repository(session)
        child = await repo.ensure_child(
            family_id=family_id,
            display_label=label,
            age_band=(req.age_band or "").strip() or None,
            birth_date=birth,
        )
        return _child_view(child)
