"""数据库引擎与连接池。

业务数据、向量、全文检索和 LangGraph Checkpointer 都在同一个 PostgreSQL 实例里，
但 Checkpointer 用独立 schema 隔离（见计划 §12.2）。

应用 Pod 无状态：连接池随进程建立，不落任何本地文件。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rdi_agent.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def get_engine() -> AsyncEngine:
    """取全局连接池。

    ``_engine_url`` 记录当前池是按哪个 URL 建的：改了 ``DATABASE_URL``
    之后必须重建，否则会拿着旧连接去连新地址 —— 这在测试里尤其致命，
    "数据库连不上要降级"那条用例会因为上一个用例留下的连接池而假通过。
    """
    global _engine, _engine_url, _sessionmaker
    settings = get_settings()
    if _engine is not None and _engine_url != settings.database_url:
        # URL 变了，旧池直接丢弃。不 await 它的 close：这里不能阻塞，
        # 旧连接会在空闲后由 pool 自己回收。
        _engine = None
        _sessionmaker = None
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            echo=False,
            future=True,
        )
        _engine_url = settings.database_url
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务边界：正常退出提交，异常回滚。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """关掉连接池。测试和优雅退出时调用。"""
    global _engine, _sessionmaker, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _engine_url = None
