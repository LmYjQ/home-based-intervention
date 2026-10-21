# -*- coding: utf-8 -*-
"""
反馈链节点体
============

    START → record_execution → analyze_outcome → next_action → apply_transitions → END

★ 它为什么是**第三张**图，而不是观察图后面接一段
-------------------------------------------------
和 Phase 3 的计划链同一个理由，但这里更硬：

1. **触发时机不同。** 观察链由「家长写了一段话」触发，
   反馈链由「家长填完今天的逐条反馈」触发。后者要落 sessions、
   要跑状态转移、要日结 —— 这些都不该被"家长写了一段话"顺带触发。
2. **thread_id 会打架。** 实测（LANGGRAPH.md ③）：同 thread 的 `invoke`
   会**接着上一次跑**。三条链共用一个 thread 的话，家长写一段话
   就会把反馈链的 run 续下去 —— 而反馈链的 run 是**要落库**的。
   → 反馈链用 `run:` 前缀，与 `plan:` / `draft:` 一样是**命名空间隔离**。
3. **写库节点会变成两个。** 观察图的 `publish` 写 observations + red_flags，
   计划链的 `publish_plan` 写 plans，反馈链的 `apply_transitions` 写
   sessions + skill_state + daily_summary。三条链各有各的末尾写库节点。

★ 副作用的分布（与另外两张图同一条约束）
----------------------------------------
    record_execution → analyze_outcome → next_action → apply_transitions

前三个节点**一个字节都不写业务表**：
  · `record_execution` 算落库计划（可能调 LLM 抽事件，但不碰表）
  · `analyze_outcome` 算当天的结果统计与日结（LLM 一次，不碰表）
  · `next_action` 调 `statemachine.propose_transition` 判定（纯函数，零 token）
最后一个节点 `apply_transitions` 落库。

★ 为什么这条链**没有 interrupt**
--------------------------------
反馈链没有"要等人拍板"的地方：家长已经点完了每一项。
而 interrupt 是有代价的 —— 恢复时会重跑所在节点，
于是写库必须排在它后面（本图已经这么排了）。
所以这张链的价值不在 HITL，在**幂等**与**可追溯**：
  · 重复提交同一天 → 落库那半幂等，checkpoint 那半直接返回上次结果
  · 家长更正 → 覆盖当天那一条，而不是多出一行
  · "这次执行属于哪一版安排" → sessions 挂在生效的 plans 下面

★ `next_action` 的四个取值
--------------------------
`continue` / `adjust` / `pause` / `escalate` —— **全部由 Python 判**。
判据只有三样，都不来自模型：
  · 今天有没有新的 urgent 红旗 → escalate
  · 有没有未关闭的 urgent（= `store.plan_paused`）→ pause
  · 有没有回退型状态转移 → adjust
  · 其余 → continue
理由见 `next_action` 的 docstring。
"""

from __future__ import annotations

import os
import sys
from typing import Any

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import redflag                                      # noqa: E402
import store                                        # noqa: E402
from feedback import (                             # noqa: E402
    ActivityFeedback,
    plan_feedbacks,
    plan_from_state,
    plan_to_state,
)

#: `next_action` 的四个取值。**刻意是一份可枚举的元组**：
#: 判据在前端也要用（决定显示哪句话），两处各写一遍字面量迟早漂移。
ACTIONS = ("continue", "adjust", "pause", "escalate")


def _mark(state, name: str) -> list[str]:
    return list(state.get("trace") or []) + [name]


# ================================================================== 1. 收反馈
def record_execution(state) -> dict:
    """家长今天点的那些条 → **落库计划**（纯计算，一行都没写）。

    ★ 它做三件事：
      1. 把家长的逐条反馈归一化成 `SessionRecord`（quality 由
         `feedback.quality_for` 算 —— 确定性的，可重放）。
      2. 每条带 note 的反馈抽一次事件（`observe.extract_observations`），
         抽出来的事件是**证据**，不是结论（模型给证据，Python 做判定）。
      3. 扫一遍家长的**原话**找红旗。

    ★ 第 ③ 件事为什么在这里（而不是观察链里）：
      家长的原话有两个入口 —— 「其他观察」框（走观察链）与
      **逐条活动的补充描述**（走这里）。以前只有前者被扫过，
      于是"他在玩木头人时咬了自己的手臂"这句话**从来没有被扫过** ——
      不是扫了没命中，是压根没扫。
      红旗是 fail-closed 的东西（漏扫的后果是漏掉一次自伤），
      所以两个入口都要扫。

    ★ 没有 feedbacks 时走「重算模式」：从库里读那天已经落了什么。
      `/api/close` 用它（"把今天重新算一遍"），而家长真的什么都没填时
      它给出的也是"今天没有记录"这个诚实答案。
    """
    from feedback import feedback_digest

    child_id, day = state["child_id"], state["date"]
    raw = list(state.get("feedbacks") or [])

    # ★ 当天生效的那版计划里的**活动定义**。
    #   生成项（`gen:` 前缀）不在活动库里 —— 没有这张表，
    #   它的标题取不到、抗拒桥接做不了（桥接要读 refusal_behaviors）。
    #   来源是**计划**而不是活动库：家长填的反馈是针对今天排出来的那几条，
    #   而"那几条是什么"就记在计划里。
    conn = store.connect()
    acts = _plan_activities(conn, child_id, day)

    if raw:
        fbs = [ActivityFeedback(**f) for f in raw]
        plan = plan_feedbacks(fbs, date=day,
                              no_llm=bool(state.get("no_llm", False)),
                              activities=acts)
    else:
        plan = _plan_from_db(child_id, day)

    out = plan_to_state(plan)
    out["redflag_reports"] = _scan_notes(plan)
    out["input_digest"] = state.get("input_digest") or feedback_digest(
        [ActivityFeedback(**f) for f in raw], day)

    # 当天生效的那一版计划（练习记录要挂在它下面）。
    cur = store.active_plan(conn, child_id, on=day)
    out["plan_id"] = int(cur["id"]) if cur else None
    out["plan_version"] = int(cur["version"]) if cur else 0
    out["trace"] = _mark(state, "record_execution")
    return out


def _plan_activities(conn, child_id: str, day: str) -> dict:
    """当天生效的那版计划里的活动定义：`{activity_id: Activity}`。

    ★ 为什么走"计划"而不是"活动库"：
      生成项（`gen:` 前缀）不在活动库里 —— 家长对它填的反馈
      要能取到标题（给模型看的那句）与行为词表（抗拒桥接用），
      就只能从计划里取。计划里有完整内容（阶段 1 写进去的）。
    """
    cur = store.active_plan(conn, child_id, on=day)
    return {a.activity_id: a for a in store.plan_items((cur or {}).get("payload"))}


def _plan_from_db(child_id: str, day: str):
    """从库里读那天的记录，还原成 `FeedbackPlan`（重算模式）。

    ★ 为什么能从库里还原：Phase 4 给 `sessions` 补了
      `prompt_level` / `steps_done` 两列 —— 它们以前只以中文形式
      塞在 `note` 里，结构化数据取不回来。
      补了列之后，"家长那天到底说了什么"就能被机器读出来，
      而不用去解析中文。
    """
    from statemachine import SessionRecord
    from feedback import FeedbackPlan

    conn = store.connect()
    rows = conn.execute(
        "SELECT activity_id,outcome,quality,out_of_routine,note,"
        "prompt_level,steps_done,source,seq FROM sessions "
        "WHERE child_id=%s AND session_date=%s ORDER BY id",
        (child_id, day)).fetchall()

    plan = FeedbackPlan(date=day)
    for r in rows:
        rec = SessionRecord(date=day, activity_id=r["activity_id"],
                            outcome=r["outcome"], quality=r["quality"],
                            out_of_routine=bool(r["out_of_routine"]))
        if r["source"] == "bridge":
            plan.refusal_hits.append({
                "session": rec, "activity_id": r["activity_id"],
                "note": r["note"], "event_index": 0, "behavior": "",
                "seq": int(r["seq"]),
            })
            continue
        plan.sessions.append(rec)
        plan.rows.append({
            "note": r["note"], "raw_note": "", "prompt_level": int(r["prompt_level"]),
            "steps_done": int(r["steps_done"]), "source": r["source"],
            "seq": int(r["seq"]),
        })
    return plan


def _scan_notes(plan) -> list[dict]:
    """逐条扫描家长的原话。**每条 note 单独扫**（各自的 source 各自幂等）。"""
    out: list[dict] = []
    for row in plan.rows:
        raw = (row.get("raw_note") or "").strip()
        if not raw:
            continue
        rep = redflag.scan(raw)
        if not rep.hits:
            continue
        out.append({"source": raw, "report": redflag.dump_report(rep)})
    return out


# ================================================================== 2. 判结果
def analyze_outcome(state) -> dict:
    """这次执行的结果怎么样。**纯 Python 的统计 + 一次日结。**

    ★ 统计部分零 token：成功/部分/抗拒/跳过各几条、平均质量多少。
      它是"今天这一天的账"，而账是数出来的，不是总结出来的。
    ★ 日结（`summary.summarize_day`）是**唯一**调模型的地方：
      "给家长的一句话"与"跨天的 flags"确实需要归纳，
      单条反馈推不出来（这正是当初把它从评估 Agent 里拆出来的理由）。
      它是纯函数（不落库），落库在 `apply_transitions`。

    ★ 日结读的是**内存里**这一份 sessions，而不是库里的 ——
      因为这个节点排在写库节点**之前**，库里还没有今天这些行。
      顺序不是随意的：算完再写，写的那半才是幂等的那一半。
    """
    from summary import summarize_day
    from statemachine import SessionRecord

    child_id, day = state["child_id"], state["date"]
    sessions = [SessionRecord.model_validate(s) for s in (state.get("sessions") or [])]

    n = len(sessions)
    stats = {
        "n": n,
        "success": sum(1 for s in sessions if s.outcome == "success"),
        "partial": sum(1 for s in sessions if s.outcome == "partial"),
        "refused": sum(1 for s in sessions if s.outcome == "refused"),
        "skipped": sum(1 for s in sessions if s.outcome == "skipped"),
        "mean_quality": round(
            sum(s.quality for s in sessions) / n, 3) if n else 0.0,
    }

    conn = store.connect()
    # 今天的观察：库里已落的（观察链写的）+ 今天抽出来还没落的。
    # 后者此刻只在内存里 —— 落库在下一个节点之后。
    quotes = [q for q in _observations_quotes(conn, child_id, day)]
    for o in (state.get("outcomes") or []):
        for e in (o.get("events") or []):
            if e.get("raw_quote"):
                quotes.append(e["raw_quote"])

    summ = summarize_day(conn, child_id, day, sessions=sessions,
                         observations=quotes, save=False,
                         no_llm=bool(state.get("no_llm", False)))
    return {
        "outcome_stats": stats,
        "daily_summary": summ.model_dump(mode="json"),
        "trace": _mark(state, "analyze_outcome"),
    }


def _observations_quotes(conn, child_id: str, day: str) -> list[str]:
    """那天 observations 里的引文。纯 SQL + 一点 JSON。"""
    import json as _json
    rows = conn.execute(
        "SELECT payload FROM observations WHERE child_id=%s AND observed_at=%s "
        "ORDER BY id", (child_id, day)).fetchall()
    out: list[str] = []
    for r in rows:
        try:
            q = (_json.loads(r["payload"]) or {}).get("raw_quote") or ""
        except Exception:
            continue
        if q:
            out.append(q)
    return out


# ================================================================== 3. 下一步
def next_action(state) -> dict:
    """接下来怎么办。**纯 Python，主体调 `statemachine.propose_transition`。**

    ★ 判据只有三样，一样都不来自模型：
      1. **今天有没有新的 urgent 红旗** → `escalate`。
         新的 = 今天扫出来的（不是历史上没关掉的）。
      2. **有没有未关闭的 urgent** → `pause`。
         这一条**只调 `store.plan_paused`** —— 它是全项目
         「暂停计划生成」的唯一口径（LANGGRAPH.md 第四节之三）。
         这里绝不自己写一遍 `exists urgent ...`，那就是第二处定义。
      3. **有没有回退型状态转移** → `adjust`（难度该往回调）。
      4. 其余 → `continue`。

    ★ 为什么 escalate 排在 pause 前面：
      两者都来自红旗，但家长要做的**动作**不同 ——
      escalate 是"今天刚出的事，现在就处理"（转诊指引），
      pause 是"之前那件事还没处理完，先别排新计划"。
      把它们合成一个动作，家长就不知道该做什么了。
    """
    from evaluator import TransitionsInput, apply_transitions
    from statemachine import SessionRecord

    child_id, day = state["child_id"], state["date"]
    conn = store.connect()
    prof = store.latest_profile(conn, child_id)
    sessions = [SessionRecord.model_validate(s) for s in (state.get("sessions") or [])]

    ts: list = []
    if prof is not None:
        # ★ 读的是**append 之前**的 current_states：边算边写会让
        #   同一天的后一条转移看到前一条的结果（顺序不变量）。
        ts = apply_transitions(TransitionsInput(date=day, sessions=sessions),
                               store.current_states(conn, child_id), prof,
                               conn=conn, child_id=child_id)

    action, reason = _decide(state, ts)

    return {
        "transitions": [{
            "skill_item": t.skill_item, "domain": t.domain,
            "from_state": t.from_state, "to_state": t.to_state,
            "reason": t.reason, "evidence": dict(t.evidence),
            "at_date": t.at_date,
        } for t in ts],
        "next_action": action,
        "action_reason": reason,
        "trace": _mark(state, "next_action"),
    }


def _decide(state, transitions: list) -> tuple[str, str]:
    """`continue` / `adjust` / `pause` / `escalate` 的判定。**纯函数。**

    ★ 它是纯的（不查库、不调模型）：红旗与暂停都由调用方算好传进来 ——
      `state["redflag_reports"]` 与 `state["paused"]`。
      判据写成纯函数才能被单测，而"接下来怎么办"正是最该被测的一句。
    """
    reports = state.get("redflag_reports") or []
    new_urgent = [r for r in reports
                  if (r.get("report") or {}).get("highest") == "urgent"]
    if new_urgent:
        codes = sorted({h.get("code") for r in new_urgent
                        for h in ((r.get("report") or {}).get("hits") or [])
                        if h.get("severity") == "urgent"})
        return "escalate", ("今天的记录里出现了需要立刻处理的情况（"
                            + "、".join(codes) + "），先按转诊指引处理，今天先不排新活动")

    if state.get("paused"):
        return "pause", "有还没处理完的紧急情况，明天的计划先不出"

    back = [t for t in transitions
            if t.get("from_state") and t.get("to_state")
            and _is_downgrade(t.get("from_state"), t.get("to_state"))]
    if back:
        names = "、".join(t.get("skill_item", "") for t in back)
        return "adjust", (f"{names} 的难度要往回调一点（今天有回退型转移），"
                          "明天的安排会按更松的提示来")

    # ★ 抗拒**不**直接判成 adjust：一条没配合是常态，
    #   而它造成的后果（那条活动进冷却、明天不再推）由检索器负责 ——
    #   那是"候选变了"，不是"难度要调"。两件事混成一个标签，
    #   adjust 就会天天响，而它响的时候反而没人当回事。
    stats = state.get("outcome_stats") or {}
    if stats.get("refused"):
        return "continue", (f"今天照常（{stats['refused']} 条没配合，"
                            "那几条明天的候选会往后排）")
    return "continue", "今天照常，明天的安排按状态机算出来的来"


def _is_downgrade(frm: str, to: str) -> bool:
    """是不是回退（难度往下调）。**顺序从词表推导**，不另写一张表。"""
    from vocab import ABILITY_STATE
    if frm not in ABILITY_STATE or to not in ABILITY_STATE:
        return False
    return ABILITY_STATE.index(to) < ABILITY_STATE.index(frm)


# ================================================================== 4. 落库
def apply_transitions(state) -> dict:
    """★ 反馈链**唯一**写业务表的地方，且排在所有计算之后。

    写四样东西：
      · sessions   —— 家长点的那些条 + 桥接补记的抗拒（幂等：唯一键 + upsert）
      · observations —— 从补充描述里抽出来的事件（幂等：按语料判）
      · red_flags  —— 今天扫出来的红旗（幂等：唯一索引）
      · skill_state —— 状态转移（幂等：run_id + from/to 唯一键）
      · daily_summary —— 日结（幂等：child + date + run_id 唯一键）

    ★ 全部幂等，所以这张图**可以重跑**：重复提交不会多写一行，
      而家长更正了某一条会**覆盖**当天那一条。
      幂等不是这里顺带加的，是 Phase 4 步 1 在存储层铺好的 ——
      这里只是**用它**。
    """
    from feedback import write_feedbacks
    from statemachine import StateTransition
    from summary import DailySummary

    child_id, day = state["child_id"], state["date"]
    run_id = state.get("run_id") or store.day_run_id(child_id, day)
    conn = store.connect()

    plan = plan_from_state(
        date=day,
        sessions=state.get("sessions") or [],
        rows=state.get("session_rows") or [],
        outcomes=state.get("outcomes") or [],
        refusal_hits=state.get("refusal_hits") or [],
    )
    w = write_feedbacks(conn, child_id, plan, run_id=run_id)

    # 状态转移：append-only，幂等靠 run_id + (from, to)。
    state_ids = []
    for t in (state.get("transitions") or []):
        tr = StateTransition(
            skill_item=t.get("skill_item", ""), domain=t.get("domain", ""),
            from_state=t.get("from_state", ""), to_state=t.get("to_state", ""),
            reason=t.get("reason", ""), evidence=dict(t.get("evidence") or {}),
            at_date=t.get("at_date", day))
        sid = store.append_state(conn, child_id, tr, run_id=run_id)
        if sid:
            state_ids.append(sid)

    # 日结
    summary_id = 0
    if state.get("daily_summary"):
        summ = DailySummary.model_validate(state["daily_summary"])
        summary_id = store.save_summary(conn, child_id, summ, run_id=run_id)

    # 红旗（今天扫出来的那些）
    red_flag_ids = _write_redflags(state, conn, child_id, day)

    return {
        "session_ids": list(w.session_ids),
        "observation_ids": list(w.observation_ids),
        "red_flag_ids": red_flag_ids,
        "state_ids": state_ids,
        "summary_id": summary_id,
        "trace": _mark(state, "apply_transitions"),
    }


def _write_redflags(state, conn, child_id: str, day: str) -> list[int]:
    """今天扫出来的红旗落库。**每条 note 各自一个 source**。"""
    ids: list[int] = []
    for item in (state.get("redflag_reports") or []):
        rep = redflag.report_from_dump(item.get("report") or {})
        if not rep.hits:
            continue
        got = store.append_red_flags(
            conn, child_id, day, redflag.rows_for(rep),
            source=item.get("source") or "", source_kind="note")
        ids.extend(got)
    return ids
