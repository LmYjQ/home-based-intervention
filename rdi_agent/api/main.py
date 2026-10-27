"""FastAPI 入口。

``/health`` 是逐项自检；业务接口在 :mod:`rdi_agent.api.conversations`，
按计划 附录 A 的清单逐步挂上。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from rdi_agent.api.children import router as children_router
from rdi_agent.api.conversations import router as conversations_router
from rdi_agent.api.debug import router as debug_router
from rdi_agent.api.health import check_all
from rdi_agent.api.memory import router as memory_router
from rdi_agent.api.revision import router as revision_router
from rdi_agent.api.settings import router as settings_router
from rdi_agent.config import get_settings
from rdi_agent.logging_setup import get_logger, setup_logging

# 尽早配置日志 —— 导入期出的问题（配置读不到、模型连不上）
# 也要能落到 logs/rdi.log，不能只出现在启动那一屏。
setup_logging()


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

app.include_router(children_router)
app.include_router(conversations_router)
app.include_router(memory_router)
app.include_router(revision_router)
app.include_router(settings_router)
app.include_router(debug_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """没接住的异常，返回**能看懂的 JSON**，不是 "Internal Server Error"。

    **为什么**：默认的 500 返回一段 HTML 或一句纯文本，
    前端只能显示"HTTP 500"—— 家长看到的是"Internal Server Error"，
    什么都不知道；调试的人还得去翻 logs/rdi.log 才知道是模型超时了。

    这里把异常类型和消息原样带回去（**只给本地手动测试台用**，
    线上不能这么干 —— 异常消息里可能有孩子的信息）。
    """
    logger = get_logger("api.error")
    logger.error(
        "未处理的异常 | %s %s | %s: %s",
        request.method, request.url.path,
        type(exc).__name__, exc, exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "type": type(exc).__name__,
        },
    )


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
