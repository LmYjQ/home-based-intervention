"""运行时可调的设置（模型名、超时）。

**为什么这几个能改、别的不能**：模型名和超时是最常被临时改的两项 ——
provider 排队时把超时调短试试、换个模型对比一下效果。
改 ``.env`` 要重启服务，改代码更糟。

**别的为什么不给**：数据库地址、检索后端这些改了就是换一套系统，
不是"调一下参数"。它们留在 ``.env`` 里。

**这是本地手动测试台用的**：进程内存里的覆盖，重启就没了。
线上要么走环境变量，要么走配置中心。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from rdi_agent.config import llm_overrides, reset_llm_overrides, set_llm_overrides

router = APIRouter(prefix="/api", tags=["settings"])


class LLMSettingsRequest(BaseModel):
    """改模型名和超时。

    **两个字段都可以不传** —— 不传的那个保持原样。
    （前端只填了超时时，不该把模型名清掉。）
    """

    llm_model: str | None = None
    llm_timeout_s: float | None = Field(default=None, gt=0)
    # 回到 .env 里的默认值（清掉运行时覆盖）
    reset: bool = False


@router.get("/settings/llm")
async def get_llm_settings() -> dict[str, Any]:
    """当前生效的 LLM 配置（前端回显用）。"""
    return llm_overrides()


@router.post("/settings/llm")
async def update_llm_settings(req: LLMSettingsRequest) -> dict[str, Any]:
    """改模型名和超时，立刻生效（不用重启）。

    **改超时必须重置 client**：client 是按超时缓存的，
    不重置的话等于没改。
    """
    if req.reset:
        return reset_llm_overrides()
    try:
        return set_llm_overrides(
            llm_model=req.llm_model, llm_timeout_s=req.llm_timeout_s
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
