"""图运行时：Checkpointer 生命周期 + 单例图。

**Checkpointer 与业务表同库不同 schema**（计划 §12.2）。分 schema 的理由不是洁癖：
迁移 Job 会 ``alembic downgrade`` 业务表，如果检查点在 ``public``，
回滚时容易连带把会话状态清掉，用户会发现"咨询到一半服务端重启，前面的追问没了"。
"""

from __future__ import annotations

import contextlib
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from rdi_agent.config import get_settings
from rdi_agent.graph.builder import build_graph

CHECKPOINT_SCHEMA = "langgraph"

_graph: Any | None = None
_saver_ctx: Any | None = None
_saver: AsyncPostgresSaver | None = None


def _dsn_with_search_path() -> str:
    """给 Checkpointer 的连接串加上 ``search_path``。

    这样做而不是在业务连接上设置：业务查询要能看见两张 schema 的表，
    而 Checkpointer 自己只需要 langgraph schema，用独立连接更干净。
    """
    settings = get_settings()
    base = settings.database_url
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-csearch_path%3D{CHECKPOINT_SCHEMA},public"


async def _ensure_schema() -> None:
    """建 schema 并跑 Checkpointer 自己的 DDL。

    幂等：``CREATE SCHEMA IF NOT EXISTS`` + saver.setup() 都是可重复的。
    迁移 Job 里也会跑一次，两边撞上不报错。
    """
    from rdi_agent.db.engine import get_engine
    from sqlalchemy import text

    async with get_engine().begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"'))


async def setup_graph() -> Any:
    """建 Checkpointer、编译图、缓存起来。返回编译好的图。"""
    global _graph, _saver, _saver_ctx
    if _graph is not None:
        return _graph

    await _ensure_schema()
    # from_conn_string 是个 asynccontextmanager，yield 出来的是 saver 本身，
    # 所以这里接的是单个值，不是 (saver, ctx) 元组。
    ctx = AsyncPostgresSaver.from_conn_string(_dsn_with_search_path())
    saver = await ctx.__aenter__()
    await saver.setup()
    _saver, _saver_ctx = saver, ctx
    _graph = build_graph(checkpointer=saver)
    return _graph


def get_graph() -> Any:
    """取已编译的图。没 setup 过就抛错 —— 静默编一个无 Checkpointer 的图
    会让"服务重启后恢复会话"这条验收项悄悄失败，比直接报错难查得多。"""
    if _graph is None:
        raise RuntimeError("图还没 setup，先 await setup_graph()")
    return _graph


def config_for(conversation_id: str, **extra: Any) -> dict[str, Any]:
    """LangGraph 的运行配置。

    ``thread_id = conversation_id``：一个会话一条线，重启后用同一个
    conversation_id 就能接着上次中断的地方跑。
    """
    return {"configurable": {"thread_id": conversation_id}, **extra}


async def shutdown_graph() -> None:
    """关掉 Checkpointer 连接。lifespan 退出时调。"""
    global _graph, _saver, _saver_ctx
    _graph, _saver, _saver_ctx = None, None, None
    if _saver_ctx is not None:
        with contextlib.suppress(Exception):
            await _saver_ctx.__aexit__(None, None, None)