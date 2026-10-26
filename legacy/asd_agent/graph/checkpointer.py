# -*- coding: utf-8 -*-
"""
checkpointer
============

把 `PostgresSaver` 挂到 `store.py` 的 schema 隔离上。
**下面每一行都来自实测**（`_spike_lg.py` / `_spike_fork.py`，langgraph 1.2.16
+ langgraph-checkpoint-postgres 3.2.0），不是从文档抄的 —— 1.x 与网上 0.x 的
示例有出入，别去查文档改这里。

四条实测结论，改任何一条之前先想清楚：

① **`PostgresSaver` 没有 `schema=` 参数。** 签名是 `(conn, pipe=None, serde=None)`。
   表建在哪取决于那条连接的 `search_path`。所以下面显式 `SET search_path`。

② **不能用 `store.connect()` 的连接。** `setup()` 里有
   `CREATE INDEX CONCURRENTLY`，在事务块里会抛
   `ActiveSqlTransaction`。一条连接不能既 autocommit 又事务式。
   → saver 自己开一条 autocommit 连接，生命周期与业务连接解耦。
   （`store.py:100` 现在也是 autocommit 了，但那是**两件事**：
   那个改动是为了不让长连接停在 idle in transaction 把 CONCURRENTLY 卡死，
   这里则是为了让 saver 根本不需要那个长连接。别把它们合并成一个。）

③ **`setup()` 会被「idle in transaction」的后端无限期阻塞。**
   `CREATE INDEX CONCURRENTLY` 等的是**整个 database** 的 virtualxid，
   **与 schema 无关**。所以 `_SETUP_DONE` 缓存必须留着 —— 它让
   "每个进程只跑一次 setup()" 成立，配合 (二) 的 autocommit 双保险。

④ **`thread_id` 不可复用**：同名 run 的 checkpoint 会累积（实测 4 → 8 → 10）。
   生产上 `thread_id = f"{child_id}:{date}"` 天然唯一；测试必须带唯一后缀。

表与业务表同 schema，所以 `store.reset()` 的 `DROP SCHEMA ... CASCADE`
会连 checkpoint 一起清掉 —— 语义正好等于「删掉这个临时库」，实测确认。
"""

from __future__ import annotations

import os
import sys
import threading

import psycopg
from psycopg.rows import dict_row

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import store                                          # noqa: E402
from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

_SETUP_LOCK = threading.Lock()
_SETUP_DONE: set[str] = set()


def saver_for(path: str | None = None) -> PostgresSaver:
    """给 `path` 对应的 schema 拿一个已 setup 好的 saver。

    `path` 不传 = 跟随 `ASD_DB_PATH`（`store.DEFAULT_DB`），
    与业务连接**同一个 schema** —— 这样 reset/隔离的口径只有一个。
    """
    sch = store._schema_of(path or store.DEFAULT_DB)
    conn = psycopg.connect(store.DEFAULT_DSN, autocommit=True)   # 必须 autocommit
    conn.row_factory = dict_row
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
    conn.execute(f'SET search_path TO "{sch}"')       # ← 表自然落在这个 schema
    s = PostgresSaver(conn)
    with _SETUP_LOCK:                 # setup() 含 CONCURRENTLY，重复跑很贵
        if sch not in _SETUP_DONE:
            s.setup()
            _SETUP_DONE.add(sch)
    return s


def close_all() -> None:
    """关掉本进程所有 saver 持有的连接。**只给测试与脚本用。**

    `PostgresSaver` 在 3.2.0 里没有公开的 close 入口（`__exit__` 只在
    `from_conn_string` 的 contextmanager 上有），而 `saver_for` 返回的实例
    只把连接藏在 `.conn` 里。测试进程退出时连接由 GC 回收，但一个进程里
    反复 reset schema 时，先关掉更干净。
    """
    import gc
    for obj in gc.get_objects():
        if isinstance(obj, PostgresSaver):
            conn = getattr(obj, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# ================================================================== 自检
def _cli() -> int:
    """★ 这里只验一件事：**checkpoint 表跟着 schema 走，且不污染 public。**

    Phase 0 的验收不靠"图跑起来了"（那由 selfcheck.py 验），
    而是靠这一屏：表建错地方的后果是分叉历史被别的临时库看到。
    """
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    p = "/tmp/graph_ckpt_check.db"
    conn = store.reset(p)
    sa = saver_for(p)

    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name LIKE 'checkpoint%' "
        "ORDER BY 1").fetchall()
    names = [r["table_name"] for r in rows]
    check("4 张 checkpoint 表建在当前 schema", len(names) == 4, names)

    leak = conn.execute(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name LIKE 'checkpoint%'").fetchone()["n"]
    check("public schema 零污染", leak == 0, f"{leak} 张")

    check("autocommit 连接（否则 CONCURRENTLY 跑不了）",
          sa.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)

    check("PostgresSaver 确实没有 schema= 参数（实测结论 ①）",
          "schema" not in __import__("inspect").signature(PostgresSaver).parameters)

    # reset 会 CASCADE 掉 checkpoint 表 —— 这正是我们要的语义
    store.reset(p)
    n = conn.execute(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name='checkpoints'"
    ).fetchone()["n"]
    check("store.reset() 连 checkpoint 一起清（= 删掉这个临时库）", n == 0, f"{n} 张")

    # 重建后还能拿到 saver（表回来）
    sa2 = saver_for(p)
    check("重建后 saver 可用（表已回来）", sa2 is not None)

    close_all()
    print("\n" + ("✓ checkpointer 自检全绿" if ok else "✗ 有失败项，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
