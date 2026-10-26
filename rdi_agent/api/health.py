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
from rdi_agent.rag import keyword_backends


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


async def _check_memory_schema(conn: AsyncConnection) -> dict[str, Any]:
    """阶段 3 的记忆表结构是不是最新的。

    **为什么要单独查**：005 迁移加的是"溯源 / 软删 / 去重"的列。
    没跑迁移的话，记忆节点会在第一次写库时炸 —— 而它被设计成**失败降级**，
    于是线上表现是"记忆悄无声息地不生效"，不是报错。
    这种静默失败必须在 /health 上暴露出来。
    """
    try:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE (table_name = 'observations' AND column_name IN ('content_hash', 'deleted_at', 'status'))
                       OR (table_name = 'hypotheses' AND column_name IN ('content_hash', 'deleted_at'))
                       OR (table_name = 'memory_events' AND column_name = 'content_hash')
                    """
                )
            )
        ).all()
        found = {(r[0], r[1]) for r in rows}
        required = {
            ("observations", "content_hash"),
            ("observations", "deleted_at"),
            ("observations", "status"),
            ("hypotheses", "content_hash"),
            ("hypotheses", "deleted_at"),
            ("memory_events", "content_hash"),
        }
        missing = sorted(f"{t}.{c}" for t, c in required - found)
        if missing:
            return {
                "ok": False,
                "error": f"缺少阶段 3 的列：{', '.join(missing)}；先跑 alembic upgrade head",
            }
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _check_checkpointer(conn: AsyncConnection) -> dict[str, Any]:
    """Checkpointer 的表在不在。

    **为什么单独查**：这些表由 ``AsyncPostgresSaver.setup()`` 在启动时创建，
    不归 Alembic 管。有人为了"清干净"把 ``langgraph`` schema 删掉之后，
    服务能启动、业务表也能用，但**所有会话恢复都会失败** ——
    症状是"重启后接着聊，系统却从零开始了"。这种静默失败必须在 /health 上暴露。
    """
    try:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'langgraph'
                    """
                )
            )
        ).scalars().all()
        found = set(rows)
        required = {
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        }
        missing = sorted(required - found)
        if missing:
            return {
                "ok": False,
                "error": f"langgraph schema 缺少 {', '.join(missing)}；"
                "重启服务跑一次 saver.setup()",
            }
        return {"ok": True, "tables": sorted(found)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def check_all() -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {
        "status": "ok",
        "database": {"ok": False, "error": "not checked"},
        "pgvector": {"ok": False, "error": "not checked"},
        "fulltext": {"ok": False, "error": "not checked"},
        "memory_schema": {"ok": False, "error": "not checked"},
        "checkpointer": {"ok": False, "error": "not checked"},
        # 报**实际生效**的后端，不只是配置里写的那个。
        # 配置写错了（名字拼错、后端没注册）时，实际跑的是默认后端 ——
        # 这个差别必须看得见，否则线上出问题时查的是错的方向。
        "keyword_backend": keyword_backends.get_backend(
            settings.keyword_backend
        ).name,
        "keyword_backend_configured": settings.keyword_backend,
        "keyword_backends_available": keyword_backends.available_backends(),
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
            result["memory_schema"] = await _check_memory_schema(conn)
            result["checkpointer"] = await _check_checkpointer(conn)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        result["database"] = {"ok": False, "error": msg}
        result["pgvector"] = {"ok": False, "error": msg}
        result["fulltext"] = {"ok": False, "error": msg}
        result["memory_schema"] = {"ok": False, "error": msg}
        result["checkpointer"] = {"ok": False, "error": msg}

    # LLM 自检要发网络请求，replay 模式下跳过
    result["llm"] = await llm.health()

    if not all(
        result[k].get("ok")
        for k in (
            "database",
            "pgvector",
            "fulltext",
            "memory_schema",
            "checkpointer",
        )
    ):
        result["status"] = "degraded"
    return result
