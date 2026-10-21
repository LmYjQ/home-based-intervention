"""FastAPI 入口。

``/health`` 是逐项自检；业务接口在 :mod:`rdi_agent.api.conversations`，
按计划 附录 A 的清单逐步挂上。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from rdi_agent.api.conversations import router as conversations_router
from rdi_agent.api.health import check_all
from rdi_agent.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动预热：把 TLS 握手的延迟挡在第一次业务调用之前
    from rdi_agent import llm
    from rdi_agent.graph.runtime import setup_graph, shutdown_graph

    await llm.warmup()
    # Checkpointer 建不起来就别硬撑：服务起来了但恢复不了会话，
    # 比启动直接失败难查得多。
    await setup_graph()
    yield
    await shutdown_graph()

    from rdi_agent.db.engine import dispose_engine

    await dispose_engine()


app = FastAPI(
    title="RDI 家长干预建议智能体",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(conversations_router)


@app.get("/health")
async def health() -> JSONResponse:
    """返回数据库、pgvector、全文检索后端、模型、replay 状态。

    任何一项检查失败都返回 503，body 里逐项说明是哪一环坏了。
    """
    result = await check_all()
    status_code = 200 if result.get("status") == "ok" else 503
    return JSONResponse(status_code=status_code, content=result)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "app": "rdi-agent",
        "docs": "/docs",
        "health": "/health",
        "demo_mode": get_settings().demo_mode,
    }
