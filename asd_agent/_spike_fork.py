"""spike 续：LangGraph 分叉到底该怎么写。

两个方法论错误，都是我自己犯的：
  1. `hasattr(r, "values")` 恒为真（dict.values 是绑定方法）→ 挂在方法上调 .get
  2. 所有实验共用一个 thread，测「有没有污染」时读的是前面实验的残留
     → 每个实验必须独立 thread
"""
from __future__ import annotations

import os
from typing import Any

from langgraph.types import Command

from _spike_lg import saver_for, build


def fresh(saver, tid: str):
    """跑一条完整的基线：interrupt → resume(10-17)。

    返回 (graph, cfg, 那个中间态 StateSnapshot)。
    ⚠️ thread_id 不可复用：同名 run 的 checkpoint 会累积（实测 4 → 8 → 10）。
    """
    g = build(saver)
    cfg = {"configurable": {"thread_id": tid}}
    g.invoke({"hits": []}, cfg)
    g.invoke(Command(resume="10-17"), cfg)
    snap = next(s for s in g.get_state_history(cfg)
                if (s.values or {}).get("hits") == ["before_interrupt"])
    return g, cfg, snap


def brief(r: Any) -> str:
    """invoke() 返回 dict，get_state() 返回 StateSnapshot —— 两者都从 .values 拿状态。
    ⚠️ 不能写 `hasattr(r, "values")`：dict 自己就有这个绑定方法。"""
    v = r if isinstance(r, dict) else getattr(r, "values", r)
    return f"answer={v.get('answer')!r:12s} hits={v.get('hits')}"


def main() -> None:
    RUN = os.environ.get("SPIKE_RUN", "r0")   # 每次运行唯一，否则 checkpoint 累积
    saver = saver_for("/tmp/spike_a.db")

    print("[基线] 每条实验都从一条全新的完整 run 开始（answer='10-17', 4 个 checkpoint）")

    # ---------- A. 同一 thread 上「改历史再重跑」 ----------
    print("\n[A] 同 thread_id，从历史 checkpoint 重跑 —— 看原 thread 会不会被改")
    g, cfg, past = fresh(saver, f"{RUN}-A")
    before = g.get_state(cfg)
    print(f"  重跑前：{brief(before)}  checkpoint 数={len(list(g.get_state_history(cfg)))}")
    try:
        g.invoke(Command(resume="99-99"), past.config)
        print("  invoke(Command(resume='99-99'), past_cfg) 成功")
    except Exception as e:
        print(f"  invoke 报错：{type(e).__name__}: {str(e)[:60]}")
    after = g.get_state(cfg)
    print(f"  重跑后：{brief(after)}  checkpoint 数={len(list(g.get_state_history(cfg)))}")
    same = after.values.get("answer") == "10-17"
    print(f"  → {'✓ 原 thread 未被改' if same else '✗ 原 thread 被改写了'}")

    # ---------- B. update_state 改历史 ----------
    print("\n[B] 同 thread_id：update_state 改历史快照后再跑")
    g, cfg, past = fresh(saver, f"{RUN}-B")
    try:
        newcfg = g.update_state(past.config, {"answer": "EDITED"})
        print(f"  update_state 返回 checkpoint_id={newcfg['configurable']['checkpoint_id'][:12]}")
        r = g.invoke(None, newcfg)
        print(f"  invoke(None, newcfg) → {brief(r)}")
    except Exception as e:
        print(f"  {type(e).__name__}: {str(e)[:80]}")
    print(f"  原 thread 现在：{brief(g.get_state(cfg))}")

    # ---------- C. 换 thread_id 分叉 ----------
    print("\n[C] ★ 换 thread_id 分叉（这才是方案 §5.5 要的「分叉」）")
    g, cfg, past = fresh(saver, f"{RUN}-C-base")
    n_before = len(list(g.get_state_history(cfg)))
    snap = dict(past.values or {})
    for tag, payload, tid in [
        ("原样 fork",                    dict(snap),                                   f"{RUN}-C-fork-a"),
        ("改一格后 fork（模拟「其实做了」）", {**snap, "answer": "做了", "hits": ["before_interrupt", "其实做了"]}, f"{RUN}-C-fork-b"),
    ]:
        try:
            r = g.invoke(payload, {"configurable": {"thread_id": tid}})
            print(f"  {tag:28s} → {brief(r)}")
        except Exception as e:
            print(f"  {tag:28s} → {type(e).__name__}: {str(e)[:60]}")
    after = g.get_state(cfg)
    print(f"\n  原 thread：{brief(after)}")
    print(f"  原 thread checkpoint 数 {n_before} → {len(list(g.get_state_history(cfg)))}")
    ok = (after.values.get("answer") == "10-17"
          and len(list(g.get_state_history(cfg))) == n_before)
    print(f"  → {'✓ 分叉完全隔离，原 thread 一行没动' if ok else '✗ 原 thread 受影响'}")

    # ---------- D. interrupt_id 定向 resume ----------
    print("\n[D] Command(resume={interrupt_id: 值})")
    g, cfg, _ = fresh(saver, f"{RUN}-D1")
    g2 = build(saver_for("/tmp/spike_a.db"))
    b2 = {"configurable": {"thread_id": f"{RUN}-D2"}}
    g2.invoke({"hits": []}, b2)
    st = g2.get_state(b2)
    ints = getattr(st, "interrupts", None) or []
    if ints:
        iid = ints[0].id
        r = g2.invoke(Command(resume={iid: "定向值"}), b2)
        print(f"  interrupt id={str(iid)[:16]} → {brief(r)}")
    else:
        print(f"  没取到 interrupt（state.interrupts={ints}）")

    # ---------- E. audit: 能不能拿到 checkpoint 的 step / 元数据 ----------
    print("\n[E] 审计线：历史快照里有什么")
    g, cfg, _ = fresh(saver, f"{RUN}-E")
    for s in list(g.get_state_history(cfg)):
        md = s.metadata or {}
        print(f"  step={md.get('step')} source={md.get('source')} "
              f"writes={list((md.get('writes') or {}).keys())} next={s.next}")


if __name__ == "__main__":
    main()
