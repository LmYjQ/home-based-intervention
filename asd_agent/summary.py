# -*- coding: utf-8 -*-
"""
日结 Agent
==========
把一天的结果折成一句话：**今天整体在往哪走，以及有什么值得家长注意的。**

★ 这个模块做的是"把已经死了的东西接活"
---------------------------------------
原先这些内容在 `evaluator.DailyEval` 里：`domain_updates`（能区方向）、
`flags`（跨天异常）、`parent_note`（给家长的一句话）。它们当时的情况是：

    只被 DailyEval.render() 的 print() 打印 → 然后丢掉。

grep 全项目，`store.py` 里没有 `daily_eval` 表，**这三个字段无人消费、不落库**。
也就是说，"这周社交在往上走、精细在原地打转"这句话从头到尾没进过记忆，
第二天的教练 Agent 读到的永远只是三个数字（refusal_counts / recent_scenes /
target_states）。

现在它落 `daily_summary` 表，`loop.build_request()` 会读，
**教练 Agent 第一次能拿到方向而不只是计数**。

为什么这部分需要 LLM，而 outcome/quality 不需要
------------------------------------------------
    outcome / quality   单条反馈就能定 —— 家长点了就是答案。模型再去猜一遍
                        是把一个确定的答案变成一个可能有错的答案。

    方向 / flags / 一句话  需要**跨样本归纳**。单条反馈推不出"这周在往上走"，
                        它得看今天 3 条 + 近 7 天的历史。这是真正的语义判断。

这与 statemachine 的分工是同一条原则：**计数交给 Python，语义交给 LLM。**
但方向仍然是"证据"而不是"决定" —— `DomainEvidence` 只给 direction + rationale，
不产出"所以应该升级到 mastered"。真正的转移仍然只由
`statemachine.propose_transition()` 的计数规则决定，**这里没有任何能力改判权**。

每天 1 次调用
-------------
这是本项目里 token 成本最值得的一笔：原链路每条活动一次评估调用，
逐条反馈下会变成 N 次；而这里固定 1 次/天，且它产出的东西是**唯一**
能跨天读到的判断。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import llm
import store
from vocab import ABILITY_DOMAIN

Domain = Literal[tuple(ABILITY_DOMAIN)]  # type: ignore[valid-type]


# ================================================================== 输出模型
class DomainEvidence(BaseModel):
    """★ 只给证据，判定留给 Python。（从 evaluator 搬过来，这里是它唯一的家。）

    magnitude 用"none/small/medium/large"这种**相对**描述而不是分数，
    因为分数会被下游误当成可以累加的量；"变化很大"到底多大，
    由 Python 按这个技能的样本量去解释。
    """

    model_config = ConfigDict(extra="forbid")

    domain: Domain
    direction: Literal["up", "flat", "down"]
    magnitude: Literal["none", "small", "medium", "large"]
    rationale: str = Field(max_length=60, description="≤60字，为什么是这个方向")
    evidence_activity_ids: list[str] = Field(
        default_factory=list, max_length=6, description="依据今天哪几条活动")


class DailySummary(BaseModel):
    """一天的方向判断。落 `daily_summary` 表，被明天的 build_request 读。"""

    model_config = ConfigDict(extra="forbid")

    date: str
    domain_updates: list[DomainEvidence] = Field(
        default_factory=list, max_length=5,
        description="只给今天**有活动记录**的能区。没有记录的能区不要报 —— "
                    "没练过的方向不能叫 up 也不能叫 flat"),
    flags: list[str] = Field(
        default_factory=list, max_length=5,
        description="需要家长注意的异常，一句话一条（如连续三天都拒绝同一个活动）。"
                    "「今天做了练习」这种不是 flag。最多 5 条。"),
    parent_note: str = Field(
        default="", max_length=120, description="给家长的一句话总结")
    degraded: bool = False

    def render(self) -> str:
        L = [f"【{self.date} 日结】"]
        for d in self.domain_updates:
            cn = ABILITY_DOMAIN.get(d.domain, d.domain)
            arrow = {"up": "↑", "flat": "→", "down": "↓"}[d.direction]
            mag = {"none": "无", "small": "轻微", "medium": "中等", "large": "明显"}[d.magnitude]
            L.append(f"  {cn} {arrow} {mag}：{d.rationale}")
        if self.flags:
            L.append("  ⚠ 需要注意：")
            for f in self.flags:
                L.append(f"    · {f}")
        if self.parent_note:
            L.append(f"  给家长：{self.parent_note}")
        if self.degraded:
            L.append("  （--no-llm：只报今天的事实，不猜方向）")
        return "\n".join(L)


# ================================================================== 输入
class SummaryInput(BaseModel):
    date: str
    today: list[dict] = Field(default_factory=list)    # 当天每条反馈的结果
    history: list[dict] = Field(default_factory=list)  # 近 7 天聚合
    observations: list[str] = Field(default_factory=list)  # 当天事件原话


# ================================================================== prompt
SYSTEM = """你是一名干预记录的分析员。你在看家长今天做了什么，需要给出**今天这一天的方向判断**。

【你管什么、不管什么】
你只给出方向、异常提示、给家长的一句话。
**你不决定能力升级** —— 那是另一个纯 Python 的规则引擎按计数做的事，它有最终决定权。
不要写"他应该升级到已掌握"这类话。

【direction 是你的判断，但 magnitude 不是】
up / flat / down 由你判断（这是语义判断）；magnitude 用相对说法（none/small/medium/large），
不要给分数 —— 分数会被误当成可以累加的量。

【direction 的判据】
- up   ：今天的结果**明显好于**这个孩子平时的水平，或连续几天在往上走
- flat ：和平时差不多
- down ：明显差于平时，或连续几次都不行
- **没有活动记录的能区不要报**。没练过不能算 flat，那只是没数据。

【flags 宁缺毋滥】
只有真正需要家长注意的异常才写。比如"连续三天拒绝同一个活动"、
"连续三天没有任何记录"。"今天做了练习"这种不是 flag。最多 5 条。"""


def _user(inp: SummaryInput) -> str:
    L = [f"【日期】{inp.date}", "", "【今天每条活动的结果】"]
    if inp.today:
        for r in inp.today:
            L.append(f"  {r.get('activity_id','')} {r.get('title','')}"
                     f" → {r.get('outcome','')}（质量 {r.get('quality',0):.2f}）")
    else:
        L.append("  （今天没有活动记录）")

    L += ["", "【最近 7 天的概况】"]
    if inp.history:
        for h in inp.history:
            L.append(f"  {h.get('date','')}：做 {h.get('n',0)} 条，"
                     f"成功 {h.get('success',0)}，抗拒 {h.get('refused',0)}")
    else:
        L.append("  （没有历史，这是第一天）")

    L += ["", "【家长今天写下的观察（原话）】"]
    if inp.observations:
        for i, o in enumerate(inp.observations, 1):
            L.append(f"  {i}. {o}")
    else:
        L.append("  （没有）")
    return "\n".join(L)


# ================================================================== 兜底
def _fallback_summary(inp: SummaryInput) -> DailySummary:
    """纯 Python 兜底：**只报事实，不猜方向。**

    不猜 direction、不猜 flags —— 猜错了会污染第二天的教练判断。
    只把今天确实发生的事按计数说一遍：哪个能区有活动、成功几条、抗拒几条。
    这是**纯 Python 从 sessions 表算出来的**，不是编的。
    """
    per_dom: dict[str, list[dict]] = {}
    for r in inp.today:
        per_dom.setdefault(r.get("domain", ""), []).append(r)

    updates: list[DomainEvidence] = []
    for dom, rows in per_dom.items():
        if not dom:
            continue
        n_ok = sum(1 for r in rows if r.get("outcome") in ("success", "partial"))
        n_ref = sum(1 for r in rows if r.get("outcome") == "refused")
        if n_ok and n_ref:
            d, mag, why = "flat", "small", "今天有成功也有抗拒"
        elif n_ok and not n_ref:
            d, mag, why = "up", "small", f"今天{n_ok}条都做出来了"
        elif n_ref and not n_ok:
            d, mag, why = "down", "small", f"今天{n_ref}条都不配合"
        else:
            # 全是 skipped —— 家长跳过一条不代表任何方向。
            # 早先的写法落进 else 分支报成"有成功也有抗拒"，那句话是假的：
            # skipped 既不是成功也不是抗拒，它只是没做。
            # （与 statemachine._COUNTED 同一口径：skipped 不参与方向判断。）
            continue
        updates.append(DomainEvidence(
            domain=dom, direction=d, magnitude=mag, rationale=why,
            evidence_activity_ids=[r.get("activity_id", "") for r in rows][:6]))

    done = len([r for r in inp.today if r.get("outcome") != "skipped"])
    note = "今天是简化模式，只记下了你填的部分"
    if done:
        note += f"；今天有 {done} 条活动的记录"
    return DailySummary(
        date=inp.date, domain_updates=updates[:5], flags=[],
        parent_note=note, degraded=True)


# ================================================================== 主入口
def summarize_day(conn: sqlite3.Connection, child_id: str, date: str, *,
                   sessions: list | None = None, observations: list | None = None,
                   no_llm: bool = False, save: bool = True,
                   on_step=None) -> DailySummary:
    """一天的 sessions + 观察 → DailySummary。**每天 1 次 LLM 调用。**

    输入全部从 store 读（sessions 由 feedback.apply_feedbacks 刚写进去），
    这样这个函数和调用方解耦 —— 明天想从别的入口日结也调它。
    """
    if sessions is None:
        sessions = _sessions_on(conn, child_id, date)
    if observations is None:
        observations = _observations_on(conn, child_id, date)

    inp = SummaryInput(
        date=date,
        today=[_row(s) for s in sessions],
        history=_history(conn, child_id, date),
        observations=observations)

    if no_llm or not sessions:
        # 今天一条记录都没有时也要给一份日结（"今天什么都没记"本身是信息），
        # 但不能编方向 —— _fallback_summary 只按计数报事实，而计数全为 0。
        out = _fallback_summary(inp)
        if not sessions:
            out = out.model_copy(update={
                "domain_updates": [],
                "parent_note": "今天还没有记录。",
            })
    else:
        try:
            draft, _ = llm.chat_json(
                system=SYSTEM, user=_user(inp), model_cls=DailySummary,
                name="daily_summary", stage="summary_day",
                max_tokens=1500, temperature=0.0,
                timeout_s=llm.TIMEOUT_S * 2, on_step=on_step)
            out = _repair(inp, draft)
        except llm.LLMError:
            out = _fallback_summary(inp)

    if save:
        store.save_summary(conn, child_id, out)
    return out


# ================================================================== 后置校验
def _repair(inp: SummaryInput, draft: DailySummary) -> DailySummary:
    """三条校验。**都是"不让幻觉进记忆"。**

    这套东西从 `evaluator._repair` 搬过来，但删掉了其中三条 ——
    那些是为"模型逐条判定 outcome"准备的，而逐条反馈下它们物理上不可能发生：
      · 丢弃不在计划里的 activity_id → outcome 现在由家长点，不存在幻觉 id
      · 漏条按 no_data 补 → 同上
      · quality 与 outcome 自洽 → quality 由 `feedback.quality_for()` 算
    留下的这三条全部针对 `domain_updates`，而它是**真的**可能出错的：
    模型完全可能报出一个今天压根没练过的能区。
    """
    # ---- ① 同一能区只保留第一条 ----
    # 模型常常对同一个能区连报两条方向不同的证据，而下游只会取第一条，
    # 留着等于埋一个"看起来有矛盾"的雷。
    seen: set[str] = set()
    doms: list[DomainEvidence] = []
    for d in draft.domain_updates:
        if d.domain in seen:
            continue
        seen.add(d.domain)
        doms.append(d)

    # ---- ② 今天没有活动记录的能区一律丢掉 ----
    # "没练过"不等于 flat。这是本模块最容易出错的地方：
    # 模型看到"今天只练了精细动作"，很可能顺手把社交适应也报一条 flat ——
    # 而那条 flat 明天的教练 Agent 会读成"社交这周在原地不动"，
    # 于是调整计划去练社交。**一次幻觉的方向判断改变了明天的整份计划。**
    active = {r["domain"] for r in inp.today if r.get("domain")}
    dropped = [d.domain for d in doms if active and d.domain not in active]
    doms = [d for d in doms if not active or d.domain in active]

    # ---- ③ flags 最多 5 条 ----
    flags = draft.flags[:5]

    return draft.model_copy(update={"domain_updates": doms[:5],
                                    "flags": flags})


# ================================================================== 从 store 读
def _row(s) -> dict:
    """SessionRecord → 日结用的一行。带上 domain（从活动库反查）。"""
    from activity_data import get as get_act
    a = get_act(s.activity_id)
    return {
        "activity_id": s.activity_id,
        "title": a.title if a else "",
        "domain": a.domains[0] if a and a.domains else "",
        "outcome": s.outcome,
        "quality": s.quality,
    }


def _sessions_on(conn: sqlite3.Connection, child_id: str, on_date: str) -> list:
    from statemachine import SessionRecord
    rows = conn.execute(
        "SELECT session_date,activity_id,outcome,quality,out_of_routine "
        "FROM sessions WHERE child_id=? AND session_date=? ORDER BY id",
        (child_id, on_date)).fetchall()
    return [SessionRecord(date=r["session_date"], activity_id=r["activity_id"],
                          outcome=r["outcome"], quality=r["quality"],
                          out_of_routine=bool(r["out_of_routine"]))
            for r in rows]


def _observations_on(conn: sqlite3.Connection, child_id: str,
                     on_date: str) -> list[str]:
    import json
    rows = conn.execute(
        "SELECT payload FROM observations WHERE child_id=? AND observed_at=? "
        "ORDER BY id", (child_id, on_date)).fetchall()
    out: list[str] = []
    for r in rows:
        try:
            q = json.loads(r["payload"]).get("raw_quote") or ""
            if q:
                out.append(q)
        except Exception:
            continue
    return out


def _history(conn: sqlite3.Connection, child_id: str, end_date: str,
             days: int = 7) -> list[dict]:
    """近 N 天的每日计数。**纯 SQL。**"""
    start = (date.fromisoformat(end_date) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT session_date, COUNT(*) AS n, "
        "  SUM(outcome='success') AS s, SUM(outcome='refused') AS r "
        "FROM sessions WHERE child_id=? AND session_date>=? AND session_date<=? "
        "GROUP BY session_date ORDER BY session_date",
        (child_id, start, end_date)).fetchall()
    return [{"date": r["session_date"], "n": r["n"],
             "success": r["s"] or 0, "refused": r["r"] or 0} for r in rows]


# ================================================================== 自检
def _cli() -> None:
    import os
    import feedback

    db = "/tmp/summary_check.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    conn = store.connect(db)
    cid = "sum-check"
    store.ensure_child(conn, cid, age_months=34)

    print("=" * 68)
    print("① 日结真的落库了（这是本次改动的核心断言）")
    print("=" * 68)
    feedback.apply_feedbacks(conn, cid, "2026-10-17", [
        feedback.ActivityFeedback("gro-007", "refused", prompt_level=3),
        feedback.ActivityFeedback("fin-001", "skipped"),
    ], no_llm=True)
    s1 = summarize_day(conn, cid, "2026-10-17", no_llm=True)
    print("  " + s1.render().replace("\n", "\n  "))

    n = conn.execute("SELECT COUNT(*) AS c FROM daily_summary WHERE child_id=?",
                     (cid,)).fetchone()["c"]
    print(f"\n  daily_summary 表行数 = {n}")
    assert n == 1, "日结必须落库"
    print("  ★ 改动前这些内容只被 print() 然后丢掉，不落任何表")

    print("\n" + "=" * 68)
    print("② ★ 明天的 build_request 能读到它")
    print("=" * 68)
    got = store.latest_summary(conn, cid, days=7, before="2026-10-18")
    print(f"  latest_summary(before=2026-10-18) 读到 {len(got)} 条")
    assert len(got) == 1, "今天写的日结明天必须读得到"
    print(f"  读到的 parent_note：「{got[0]['parent_note']}」")

    today_only = store.latest_summary(conn, cid, days=7, before="2026-10-17")
    print(f"  latest_summary(before=2026-10-17) 读到 {len(today_only)} 条")
    assert today_only == [], "今天的日结不该被今天的计划读到"
    print("  ✓ 反馈影响的是明天 —— 今天的卡片不会被同一天的反馈追溯修改")

    print("\n" + "=" * 68)
    print("③ _repair：没练过的能区不能报 flat")
    print("=" * 68)
    inp = SummaryInput(date="2026-10-18", today=[
        {"activity_id": "fin-004", "title": "翻书找东西", "domain": "fine_motor",
         "outcome": "success", "quality": 0.75}])
    bad = DailySummary(date="2026-10-18", domain_updates=[
        # 幻觉：今天只练了精细动作，模型却报了社会适应
        DomainEvidence(domain="social_adapt", direction="flat", magnitude="large",
                       rationale="社交这边没动静"),
        DomainEvidence(domain="fine_motor", direction="up", magnitude="small",
                       rationale="翻书比上次顺"),
        DomainEvidence(domain="fine_motor", direction="down", magnitude="large",
                       rationale="同能区的矛盾第二条"),
    ], flags=["a", "b", "c", "d", "e", "f"])
    fixed = _repair(inp, bad)
    print(f"  喂入 3 条 domain_updates（social_adapt + 两条 fine_motor）+ 6 条 flags")
    print(f"  修复后：{[d.domain for d in fixed.domain_updates]}，flags {len(fixed.flags)} 条")
    assert [d.domain for d in fixed.domain_updates] == ["fine_motor"]
    assert fixed.domain_updates[0].direction == "up"
    assert len(fixed.flags) == 5
    print("  ✓ social_adapt 被丢掉（今天没练 → 没数据，不是 flat）")
    print("  ✓ 同能区只留第一条（否则下游取到矛盾的方向）")
    print("  ✓ flags 封顶 5 条")

    print("\n" + "=" * 68)
    print("④ 兜底只报事实，不猜方向")
    print("=" * 68)
    fb_in = SummaryInput(date="2026-10-19", today=[
        {"activity_id": "gro-007", "title": "一二三木头人", "domain": "gross_motor",
         "outcome": "refused", "quality": 0.0},
        {"activity_id": "fin-004", "title": "翻书找东西", "domain": "fine_motor",
         "outcome": "partial", "quality": 0.35},
    ])
    fbs = _fallback_summary(fb_in)
    print("  " + fbs.render().replace("\n", "\n  "))
    assert not fbs.flags, "降级模式不猜 flags"
    assert fbs.degraded
    doms = {d.domain: d.direction for d in fbs.domain_updates}
    print(f"\n  方向 = {doms}")
    assert doms == {"gross_motor": "down", "fine_motor": "up"}
    print("  ✓ 方向是纯 Python 从 sessions 计数算出来的，不是模型给的")

    print("\n" + "=" * 68)
    print("⑤ 什么都没记的那天")
    print("=" * 68)
    empty = summarize_day(conn, cid, "2026-10-20", no_llm=True)
    print("  " + empty.render().replace("\n", "\n  "))
    assert not empty.domain_updates, "没有记录就没有方向"
    print("  ✓ 空的一天不编方向")


if __name__ == "__main__":
    _cli()
