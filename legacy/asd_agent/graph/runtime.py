# -*- coding: utf-8 -*-
"""
图的运行时
==========

**编译一次，按进程复用。**

为什么必须缓存：`saver_for()` 每次调用都会**开一条新的数据库连接**，
并在首次时跑一次 `PostgresSaver.setup()`（里面含 `CREATE INDEX CONCURRENTLY`）。
每个 HTTP 请求调一次，就意味着每个请求开一条连接、跑一次 CONCURRENTLY。
所以它是**按 schema 名缓存的进程级单例**。

多 worker（uvicorn --workers N）下每个进程各有一份 —— 那是正常的：
`setup()` 建表用 IF NOT EXISTS，重复跑幂等，而 checkpointer 表在 schema 里，
所有 worker 看到的是同一批表。**不要试图跨进程共享 saver 实例**：
连接不是线程安全的也不是进程安全的。

一个进程里 `ASD_DB_PATH` 变了会得到另一张图（key 是 schema 名）。
自检脚本正是靠这个拿到干净图的。
"""

from __future__ import annotations

import os
import sys
import threading

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import store                                          # noqa: E402

from .builder import build_graph                      # noqa: E402
from .checkpointer import saver_for                   # noqa: E402

_LOCK = threading.Lock()
_GRAPHS: dict[str, object] = {}        # schema 名 -> 观察链 CompiledStateGraph
_DRAFTS: dict[str, object] = {}        # schema 名 -> 草稿图
_PLANS: dict[str, object] = {}         # schema 名 -> 计划链 CompiledStateGraph
_RUNS: dict[str, object] = {}          # schema 名 -> 反馈链 CompiledStateGraph
_CASES: dict[str, object] = {}         # schema 名 -> 案例链 CompiledStateGraph


def _key(path: str | None) -> str:
    return store._schema_of(path or store.DEFAULT_DB)


def _cached(cache: dict, key: str, factory):
    with _LOCK:
        g = cache.get(key)
        if g is None:
            g = cache[key] = factory()
        return g


def get_graph(path: str | None = None):
    """干预主图。同一进程、同一 schema 只编译一次。"""
    return _cached(_GRAPHS, _key(path), lambda: build_graph(saver_for(path)))


def get_draft_graph(path: str | None = None):
    """草稿图（见 draft.py）。与主图分开的缓存，两者不会互相顶掉。"""
    from . import draft
    return _cached(_DRAFTS, _key(path), lambda: draft.build_draft_graph(saver_for(path)))


def get_plan_graph(path: str | None = None):
    """计划链图（Phase 3）。与观察链**分开缓存**。

    ★ 为什么必须分开而不是共用一个 dict：
      两张图的 saver 用的是**同一个连接**（`saver_for` 按 schema 缓存），
      而编译出来的 CompiledStateGraph 各自持有节点表。
      共用一个 key 的话，第二张图会把第一张覆盖掉，
      而 `/api/observe/run` 拿到的是计划链的图 ——
      症状是"观察端点返回了一个计划"，没有任何报错。
    """
    def _build(path=path):
        from .plan_graph import build_plan_graph
        return build_plan_graph(saver_for(path))
    return _cached(_PLANS, _key(path), _build)


def get_run_graph(path: str | None = None):
    """反馈链图（Phase 4）。与前两张**分开缓存**，理由同 `get_plan_graph`。

    ★ 为什么必须分开而不是共用一个 dict：
      三张图的 saver 用的是**同一个连接**（`saver_for` 按 schema 缓存），
      而编译出来的 CompiledStateGraph 各自持有节点表。
      共用一个 key 的话，第二张图会把第一张覆盖掉 ——
      症状是"反馈端点返回了一个计划"，没有任何报错。
    """
    def _build(path=path):
        from .run_graph import build_run_graph
        return build_run_graph(saver_for(path))
    return _cached(_RUNS, _key(path), _build)


def get_case_graph(path: str | None = None):
    """案例链图（阶段 2）。与前三张**分开缓存**，理由同 `get_plan_graph`。

    ★ 为什么必须分开而不是共用一个 dict：
      四张图的 saver 用的是**同一个连接**（`saver_for` 按 schema 缓存），
      而编译出来的 CompiledStateGraph 各自持有节点表。
      共用一个 key 的话，第二张图会把第一张覆盖掉 ——
      症状是"建案例的接口返回了一段观察"，没有任何报错。
    """
    def _build(path=path):
        from .case_graph import build_case_graph
        return build_case_graph(saver_for(path))
    return _cached(_CASES, _key(path), _build)


def close_all() -> None:
    """关掉本进程持有的 saver 连接 + 清缓存。**只给测试与关停用。**"""
    from .checkpointer import close_all as _close
    with _LOCK:
        _GRAPHS.clear()
        _DRAFTS.clear()
        _PLANS.clear()
        _RUNS.clear()
        _CASES.clear()
    _close()


if __name__ == "__main__":
    # 自检：图能拿到、且**确实是同一个对象**（即真的缓存了）
    g1, g2 = get_graph(), get_graph()
    print(f"schema        = {_key(None)}")
    print(f"同一实例      = {g1 is g2}")
    print(f"有 checkpointer= {g1.checkpointer is not None}")
    d1, d2 = get_draft_graph(), get_draft_graph()
    print(f"草稿同一实例  = {d1 is d2}")
    print(f"草稿≠主图     = {d1 is not g1}")
    p1, p2 = get_plan_graph(), get_plan_graph()
    print(f"计划图同一实例= {p1 is p2}")
    print(f"计划图≠观察图 = {p1 is not g1}")
    r1, r2 = get_run_graph(), get_run_graph()
    print(f"反馈图同一实例= {r1 is r2}")
    print(f"反馈图≠观察图 = {r1 is not g1}")
    print(f"反馈图≠计划图 = {r1 is not p1}")
    c1, c2 = get_case_graph(), get_case_graph()
    print(f"案例图同一实例= {c1 is c2}")
    print(f"案例图≠观察图 = {c1 is not g1}")
    close_all()
