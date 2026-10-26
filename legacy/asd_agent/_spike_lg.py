"""Phase 0 spike：验证 PostgresSaver 与现有 schema 隔离能共存。

**这个脚本是临时验证产物，验完即删。** 它回答四个问题：
  1. PostgresSaver.setup() 会把表建到哪个 schema？（跟不跟 SET search_path 走）
  2. interrupt / Command(resume=) 在 PG checkpointer 下能不能往返？
  3. get_state_history 能不能读出多个 checkpoint？
  4. store.reset() 的 DROP SCHEMA ... CASCADE 会不会把 checkpoint 一起清掉？
"""
from __future__ import annotations

import sys
import threading

from typing import TypedDict

import psycopg
from psycopg.rows import dict_row

import store
from langgraph.graph import START, StateGraph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command, interrupt


_SETUP_LOCK = threading.Lock()
_SETUP_DONE: set[str] = set()


def saver_for(path: str | None = None) -> PostgresSaver:
    """**不给 saver 用 store.connect() 的连接。**

    PostgresSaver.setup() 会跑 `CREATE INDEX CONCURRENTLY`，PG 不允许它在事务块里跑，
    而 store.connect() 是刻意非 autocommit 的（业务写入靠显式 commit()）。
    一条连接不能既 autocommit 又事务式 → saver 自己开一条 autocommit 连接，
    只对它 SET search_path，于是 checkpoint 表自然落在同一个 schema 里。
    store.py 一行都不用改。
    """
    sch = store._schema_of(path or store.DEFAULT_DB)
    conn = psycopg.connect(store.DEFAULT_DSN, autocommit=True)
    conn.row_factory = dict_row
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
    conn.execute(f'SET search_path TO "{sch}"')
    s = PostgresSaver(conn)
    with _SETUP_LOCK:                 # setup() 里有 CONCURRENTLY，重复跑很贵
        if sch not in _SETUP_DONE:
            s.setup()
            _SETUP_DONE.add(sch)
    return s


class S(TypedDict, total=False):
    hits: list[str]
    answer: str


def collect(state: S) -> dict:
    # ★ interrupt 之前绝不能有不可重复的副作用：resume 时本节点会从头重跑
    hits = state.get("hits", [])
    hits.append("before_interrupt")
    ans = interrupt({"type": "clarify", "question": "他是在哪一天出现的？"})
    hits.append("after_interrupt")      # 探针：resume 时不该再见到 before_interrupt
    return {"hits": hits, "answer": ans}


def finish(state: S) -> dict:
    return {"hits": state["hits"] + ["finish"]}


def build(saver):
    g = StateGraph(S)
    g.add_node("collect", collect)
    g.add_node("finish", finish)
    g.add_edge(START, "collect")
    g.add_edge("collect", "finish")
    return g.compile(checkpointer=saver)


def main() -> int:
    ok = True
    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    # ---------- 1. setup() 落在哪个 schema ----------
    print("\n[1] setup() 的建表位置")
    conn = store.reset("/tmp/spike_a.db")
    saver = saver_for("/tmp/spike_a.db")
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name LIKE 'checkpoint%' "
        "ORDER BY 1").fetchall()
    names = [r["table_name"] for r in rows]
    check("checkpoint 表建在当前 schema", len(names) >= 3, names)
    leak = conn.execute(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name LIKE 'checkpoint%'").fetchone()["n"]
    check("没有污染 public schema", leak == 0, f"public 里 {leak} 张")

    # ---------- 2. interrupt / resume ----------
    print("\n[2] interrupt → resume")
    g = build(saver)
    cfg = {"configurable": {"thread_id": "spike-a-1"}}
    out = g.invoke({"hits": []}, cfg)
    check("首次调用停在 interrupt", out.get("__interrupt__") is not None,
          type(out.get("__interrupt__")).__name__)
    check("interrupt 前节点确实跑过", "before_interrupt" in out.get("hits", []), out.get("hits"))
    check("interrupt 后节点没跑", "after_interrupt" not in out.get("hits", []))

    final = g.invoke(Command(resume="10-17"), cfg)
    print(f"     resume 后 hits = {final['hits']}  answer = {final.get('answer')!r}")
    check("resume 接上了后续节点", final["hits"][-1] == "finish")
    check("★ resume 时 interrupt 前节点被重跑",
          final["hits"].count("before_interrupt") == 2,
          "出现 2 次 → 证实副作用必须幂等")
    check("resume 值拿到了", final.get("answer") == "10-17")

    # ---------- 3. get_state_history ----------
    print("\n[3] time travel")
    snaps = list(g.get_state_history(cfg))
    check("能读出多个 checkpoint", len(snaps) >= 3, f"{len(snaps)} 个")
    for s in snaps[:5]:
        print(f"     id={s.config['configurable'].get('checkpoint_id','')[:12]} "
              f"next={s.next} hits={s.values.get('hits')}")
    mid = [s for s in snaps if s.next == ("collect",) or not s.next]
    check("存在可 fork 的中间态", bool(mid))

    # ★ 分叉的正确姿势（实测，见 _spike_fork.py）：必须换 thread_id。
    #   同 thread 重跑 → 状态不变但 checkpoint 被追加（污染历史）
    #   同 thread update_state → 直接改写原 thread 的状态
    past = next((s for s in snaps if (s.values or {}).get("hits") == ["before_interrupt"]), None)
    check("找到'刚 interrupt 完'那个快照", past is not None)
    if past:
        n0 = len(snaps)
        edited = {**(past.values or {}), "answer": "做了", "hits": ["before_interrupt", "其实做了"]}
        forked = g.invoke(edited, {"configurable": {"thread_id": "spike-a-1#fork"}})
        check("换 thread_id 分叉：编辑被带进新 thread", forked.get("answer") == "做了", forked.get("answer"))
        check("★ 换 thread_id 分叉：原 thread 一行没动",
              g.get_state(cfg).values.get("answer") == "10-17"
              and len(list(g.get_state_history(cfg))) == n0,
              f"原 checkpoint 数 {n0} → {len(list(g.get_state_history(cfg)))}")

    # ---------- 4. reset() 是否连带清 checkpoint ----------
    print("\n[4] reset() 与 checkpoint 的关系")
    conn2 = store.reset("/tmp/spike_b.db")
    n_before = conn2.execute(
        "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema=current_schema() "
        "AND table_name='checkpoints'").fetchone()["n"]
    check("新 schema 里还没有 checkpoint 表（被 CASCADE 掉了）", n_before == 0, f"{n_before} 张")
    sa2 = saver_for("/tmp/spike_b.db")
    g2 = build(sa2)
    g2.invoke({"hits": []}, {"configurable": {"thread_id": "spike-b-1"}})
    cnt = conn2.execute("SELECT count(*) AS n FROM checkpoints").fetchone()["n"]
    check("重建后 checkpoint 能写入", cnt >= 1, f"{cnt} 行")

    # ---------- 5. 多线程 ----------
    print("\n[5] 多线程（store.connect 是 thread-local）")
    errs: list[str] = []
    def worker(i: int):
        try:
            s = saver_for("/tmp/spike_a.db")
            gg = build(s)
            gg.invoke({"hits": []}, {"configurable": {"thread_id": f"spike-t-{i}"}})
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("4 线程并发写 checkpoint", not errs, errs[:2])

    print("\n" + ("✓ spike 全绿" if ok else "✗ 有失败项，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
