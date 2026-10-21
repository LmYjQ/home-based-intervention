"""/health 的实现。

拆成独立模块是因为这个接口要做的事比"返回 200"多：
它要真的连一次数据库、确认 pgvector 扩展在、全文检索后端可用、
模型配置对不对、是不是 replay 模式。demo 现场最怕的就是
"以为在用真模型，其实 key 是空的"。

单项检查失败不抛异常，逐项返回状态，让一眼看出是哪一环坏了。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from rdi_agent import llm
from rdi_agent.config import get_settings
from rdi_agent.db.engine import get_engine


async def _check_db() -> dict[str, Any]:
    try:
        async with get_engine().connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar_one()
        return {
            "ok": True,
            "server": version.split(",")[0] if version else "unknown",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _check_pgvector(conn: AsyncConnection) -> dict[str, Any]:
    try:
        ext = (
            await conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one_or_none()
        if ext is None:
            return {"ok": False, "error": "vector 扩展未安装，先跑 alembic upgrade head"}
        probe = (
            await conn.execute(text("SELECT '[1,2,3]'::vector"))
        ).scalar_one()
        return {"ok": True, "version": ext, "probe": str(probe)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _check_fulltext(conn: AsyncConnection) -> dict[str, Any]:
    """检查内置全文检索是否真的可用（生成列 + GIN 索引）。"""
    try:
        probe = (
            await conn.execute(
                text("SELECT to_tsvector('english', 'guided participation')")
            )
        ).scalar_one()
        return {"ok": True, "probe": str(probe)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def check_all() -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {
        "status": "ok",
        "database": {"ok": False, "error": "not checked"},
        "pgvector": {"ok": False, "error": "not checked"},
        "fulltext": {"ok": False, "error": "not checked"},
        "keyword_backend": settings.keyword_backend,
        "embedding_model": settings.embedding_model,
        "rerank_enabled": settings.rerank_enabled,
        "rag_enabled": settings.rag_enabled,
        "memory_enabled": settings.memory_enabled,
        "llm": {},
    }

    try:
        async with get_engine().connect() as conn:
            result["database"] = await _check_db()
            result["pgvector"] = await _check_pgvector(conn)
            result["fulltext"] = await _check_fulltext(conn)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        result["database"] = {"ok": False, "error": msg}
        result["pgvector"] = {"ok": False, "error": msg}
        result["fulltext"] = {"ok": False, "error": msg}

    # LLM 自检要发网络请求，replay 模式下跳过
    result["llm"] = await llm.health()

    if not all(
        result[k].get("ok")
        for k in ("database", "pgvector", "fulltext")
    ):
        result["status"] = "degraded"
    return result
