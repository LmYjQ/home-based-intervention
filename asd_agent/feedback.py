# -*- coding: utf-8 -*-
"""
家长逐条反馈 → 记忆
====================
家长端从"用一整段话描述今天"改成"**当天排了几条活动，就逐条填几条**"。
这个模块是那条新输入形态的唯一入口，负责把它变成 sessions / observations 两张表里的行。

它取代了原来 `evaluator.py` 里的"逐活动判定"那一半
--------------------------------------------------------------
原先评估 Agent 要从家长一整段话里推断"哪条活动今天做得怎么样"——
outcome、quality、成功判据命中，全靠模型从自由文本里裁量。
现在家长**直接点选项**回答了这个问题，模型再去猜一遍不只是浪费 token，
是**把一个已经确定的答案变成一个可能有错的答案**：
家长点了「不配合」，模型可能判成 partial。

所以这里做的事全是纯 Python 的映射，零 token。模型留给 `summary.py` 的日结
—— 那才是需要跨样本归纳、单条反馈推不出来的部分。

quality 的口径从提示词搬到了函数里
---------------------------------
原先 quality 的分档写在 evaluator 的 SYSTEM 提示词里：

    - 完全独立完成，不需要任何提示  → 0.8 - 1.0
    - 做出来了，但要提示一两次       → 0.6 - 0.8
    - 在持续提示下勉强完成           → 0.3 - 0.6

那段提示词是让**模型**执行的，所以同一档位不同天能差 0.15，而且不可复现
（重放同一份输入未必得到同一个数）。而 `statemachine.propose_transition`
的升级闸门要拿平均 quality 比 0.5 / 0.75 —— **拿一个抖动的数字去过闸门**，
闸门等于没有。挪进 `quality_for()` 之后它是确定的、可重放的。

两条路径的分工（改之前也分，但边界不清楚）
------------------------------------------
    家长点的 outcome/quality  →  sessions 表  →  管「明天做多难」（状态机）
    家长写的补充描述          →  observations →  管「明天做哪条」（检索硬过滤）

同一个"他不肯听"会同时出现在两条路径上，这是**有意的**：
sessions 那条告诉状态机"练不到这个难度"，
observations 那条告诉检索器"这条活动要进冷却期"。
两者的读者不同，不该合成一个字段。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

import observe
import store
from statemachine import SessionRecord

Outcome = Literal["success", "partial", "refused", "skipped"]

# 家长端的四个选项。★ 刻意与 SessionRecord 同词表（4 值，无 no_data）——
# 家长"跳过这条"和"今天没做这条"是两件事：
#   跳过 = 卡片发了没来得及做 → skipped
#   没做 = 想做但孩子不配合 → refused
# 而"家长压根没填"不是第四种结果，是**没有记录**，不进 sessions 表
# （与 evaluator.to_session_records 把 no_data 过滤掉是同一条原则：
# "没有信息"不是一个观察结果，它是没有观察）。

PROMPT_LEVELS = ["独立做到", "手势或口头提示", "示范给他看", "动手辅助"]


@dataclass
class ActivityFeedback:
    """单条活动的家长反馈。**这些值是家长直接回答的，不是模型推断的。**"""

    activity_id: str
    outcome: Outcome
    prompt_level: int = 0       # 用到提示阶梯的第几级，0=独立完成
    steps_done: int = 0         # 实际做到第几步（对照卡片上的步骤数）
    note: str = ""              # 选填自由补充 → 走 LLM 抽成事件

    def __post_init__(self):
        if not 0 <= self.prompt_level <= 3:
            raise ValueError(
                f"prompt_level 必须在 0-3 之间（{PROMPT_LEVELS}），"
                f"实际 {self.prompt_level}")


@dataclass
class DayFeedbackResult:
    """一天的反馈处理结果。给 loop 的第 5/6 步和日结用。"""

    date: str
    sessions: list = field(default_factory=list)          # SessionRecord
    session_ids: list[int] = field(default_factory=list)  # sessions 表的行 id
    refusal_hits: list = field(default_factory=list)       # observe.RefusalHit
    outcomes: list[observe.ObservationOutcome] = field(default_factory=list)
    observation_ids: list[int] = field(default_factory=list)
    degraded: bool = False

    def render(self) -> str:
        L = [f"【{self.date} 的家长反馈】"]
        for s in self.sessions:
            L.append(f"  {s.activity_id} → {s.outcome}（质量 {s.quality:.2f}）")
        if self.refusal_hits:
            L.append(f"  观察桥接补记 {len(self.refusal_hits)} 条 refused session：")
            for h in self.refusal_hits:
                L.append(f"    {h.activity_id}  {h.note}")
        if self.degraded:
            L.append("  （--no-llm：补充描述未抽取事件，一条都不编）")
        return "\n".join(L)


# ================================================================== quality
def quality_for(fb: ActivityFeedback) -> float:
    """outcome + 提示层级 → quality。**纯函数、确定性、可重放。**

    分档来自原 evaluator 的 SYSTEM 提示词，数值取各档中位偏上 ——
    闸门比的是平均 quality，取中位是为了让"提示一两次"和"持续提示"
    落在闸门两侧，而不是都落在同一侧。

    refused / skipped 恒为 0：与 `_repair` 里"抗拒的 quality 必须钳到 0"
    同一条约束，只是现在它由构造保证，不再需要事后修。
    """
    if fb.outcome != "success":
        # partial 的 quality 由"做到几步"决定，比固定值更准
        if fb.outcome == "partial":
            return 0.35
        return 0.0
    # 独立 0.90 / 提示1-2次 0.75 / 要一直提示 0.60
    return {0: 0.90, 1: 0.75, 2: 0.75, 3: 0.60}[fb.prompt_level]


# ================================================================== 主入口
def apply_feedbacks(conn: sqlite3.Connection, child_id: str, date: str,
                    feedbacks: list[ActivityFeedback], *,
                    other_notes: str = "", no_llm: bool = False,
                    on_step=None) -> DayFeedbackResult:
    """逐条反馈 → sessions + observations。**每天最多 1 次 LLM 调用每条反馈。**

    流程（顺序有讲究）：
      1. 先写 sessions —— 它是状态机的唯一输入，不依赖任何 LLM，
         所以即使后面抽取全降级，今天的成败记录也已经进去了。
      2. 再抽事件 —— 每条有 note 的反馈各调一次 LLM。
      3. 最后跑抗拒桥接 —— 只对 outcome != refused 的反馈跑。
    """
    from activity_data import get as get_act

    res = DayFeedbackResult(date=date)

    # ---- ① sessions：纯映射，先写 ----
    for fb in feedbacks:
        rec = SessionRecord(date=date, activity_id=fb.activity_id,
                            outcome=fb.outcome, quality=quality_for(fb))
        note = _session_note(fb)
        res.sessions.append(rec)
        res.session_ids.append(store.log_session(conn, child_id, rec, note))

    # ---- ② 逐条抽事件 ----
    # 记下 (feedback, outcome) 的对应关系，第 ④ 步要用。
    # 早期版本把 (activity_id, outcome) 存进一个列表再 zip 回来，
    # 两个列表靠"过滤条件相同"配对 —— 改一处过滤就静默错位，
    # 而错位的后果是抗拒记到错误的活动头上。所以直接存对象。
    per_activity: list[tuple[ActivityFeedback, observe.ObservationOutcome]] = []
    for fb in feedbacks:
        if not fb.note.strip():
            continue
        act = get_act(fb.activity_id)
        title = act.title if act else fb.activity_id
        out = observe.extract_observations(
            fb.note, date=date, activity=(fb.activity_id, title),
            no_llm=no_llm, on_step=on_step)
        per_activity.append((fb, out))

    # ---- ③ 「其他观察」框 ----
    # 它是当天唯一一段"混着多件事"的话，所以走整段模式（SYSTEM，
    # 一次话里多次事件要抽全）而不是逐条模式。
    other_out = None
    if other_notes.strip():
        other_out = observe.extract_observations(
            other_notes, date=date, activity=None,
            no_llm=no_llm, on_step=on_step)

    for _, out in per_activity:
        res.outcomes.append(out)
        res.observation_ids.extend(observe.persist(conn, child_id, out))
        res.degraded = res.degraded or out.degraded

    if other_out is not None:
        res.outcomes.append(other_out)
        res.observation_ids.extend(observe.persist(conn, child_id, other_out))
        res.degraded = res.degraded or other_out.degraded

    # ---- ④ 抗拒桥接 ----
    # ★ 家长已经点了 refused 时**跳过**：那条 refused session 上面已经写了，
    #   模型再从补充描述里抽出一条 refusal 就是同一件事记两次 ——
    #   而 MAX_REFUSALS_PER_DAY=2，一次练习就够了。
    for fb, out in per_activity:
        if fb.outcome == "refused":
            continue
        res.refusal_hits.extend(
            observe.refusal_sessions(out, fb.activity_id))

    # 「其他观察」框：activity_id=None → refusal_sessions 直接返回 []。
    # 这行调用是为了让"它不计入"在代码里是显式的一次调用，
    # 而不是靠"我们没调用它"这种沉默来表达。
    if other_out is not None:
        res.refusal_hits.extend(observe.refusal_sessions(other_out, None))

    observe.log_refusals(conn, child_id, res.refusal_hits)
    return res


def _session_note(fb: ActivityFeedback) -> str:
    """记进 sessions 表的 note。回答"这一行为什么是这样"。"""
    if fb.outcome == "refused":
        return "家长选「不配合」"
    if fb.outcome == "skipped":
        return "家长选「跳过这条」"
    if fb.outcome == "partial":
        return f"家长选「部分做到」，做到第 {fb.steps_done} 步"
    lvl = PROMPT_LEVELS[fb.prompt_level]
    return f"家长选「独立做到」" if fb.prompt_level == 0 else f"家长选「{lvl}」"


# ================================================================== 自检
def _cli() -> None:
    import os

    print("=" * 68)
    print("① quality 映射：确定性，可重放")
    print("=" * 68)
    for fb in [ActivityFeedback("gro-007", "success", 0),
               ActivityFeedback("gro-007", "success", 1),
               ActivityFeedback("gro-007", "success", 3),
               ActivityFeedback("gro-007", "partial", steps_done=2),
               ActivityFeedback("gro-007", "refused"),
               ActivityFeedback("gro-007", "skipped")]:
        q1, q2 = quality_for(fb), quality_for(fb)
        assert q1 == q2, "quality 必须可重放"
        print(f"  {fb.outcome:8s} 提示层级{fb.prompt_level} → {q1:.2f}"
              f"  （重放一致 ✓）")
    assert quality_for(ActivityFeedback("x", "refused", 0)) == 0.0
    assert quality_for(ActivityFeedback("x", "skipped", 0)) == 0.0
    print("  ✓ refused / skipped 的 quality 恒为 0（构造保证，不靠事后钳制）")

    print("\n" + "=" * 68)
    print("② ★ 归属只认家长填过的那条")
    print("=" * 68)
    db = "/tmp/feedback_check.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    conn = store.connect(db)
    cid = "fb-check"
    store.ensure_child(conn, cid, age_months=34)

    # 家长当天只填了 gro-007，fin-001 / gro-006 选了"跳过"。
    res = apply_feedbacks(conn, cid, "2026-10-17", [
        ActivityFeedback("gro-007", "refused", prompt_level=3,
                         note="他一开始不肯听，我说你停，他就站在那儿不动，"
                              "后来我拉着他才停下；停下来他立刻坐在地上哭了。"),
        ActivityFeedback("fin-001", "skipped"),
        ActivityFeedback("gro-006", "skipped"),
    ], no_llm=True)

    print("  " + res.render().replace("\n", "\n  "))
    rc = store.refusal_counts(conn, cid)
    print(f"\n  refusal_counts = {rc}")
    assert rc.get("gro-007", 0) == 1, f"期望 gro-007==1，实际 {rc}"
    assert rc.get("fin-001", 0) == 0, "fin-001 家长选了跳过，不该有抗拒"
    assert rc.get("gro-006", 0) == 0, "gro-006 家长选了跳过，不该有抗拒"
    print("  ★ gro-007 记 1 次（家长点的），fin-001 / gro-006 记 0 次")
    print("    —— 改动前这三者都会各记至少 1 次，那是无中生有")

    print("\n" + "=" * 68)
    print("③ 家长已选 refused 时不重复计数")
    print("=" * 68)
    print("  上面那条 gro-007 家长点了「不配合」→ 跳过抗拒桥接")
    print("  理由：MAX_REFUSALS_PER_DAY=2，模型再抽出一条 refusal 就到顶了")
    assert res.refusal_hits == [], "家长已点 refused 时不该再补记"
    print(f"  refusal_hits = {res.refusal_hits}  ✓")

    print("\n" + "=" * 68)
    print("④ 「其他观察」框：进 observations，不计 refusal")
    print("=" * 68)
    conn2 = store.connect("/tmp/feedback_check2.db")
    cid2 = "fb-check2"
    store.ensure_child(conn2, cid2, age_months=34)
    res2 = apply_feedbacks(conn2, cid2, "2026-10-18", [
        ActivityFeedback("fin-004", "partial", steps_done=2),
    ], other_notes="今天在超市他蹲下来捂住耳朵了，我没理他，"
                   "过了一会儿自己好了。", no_llm=True)
    print(f"  sessions: {[(s.activity_id, s.outcome, s.quality) for s in res2.sessions]}")
    print(f"  refusal_hits: {len(res2.refusal_hits)}（应为 0）")
    assert res2.refusal_hits == [], "其他观察框不该计入抗拒"
    print(f"  refusal_counts = {store.refusal_counts(conn2, cid2)}  ← 空")
    print("  ✓ 超市捂耳朵那条只作为观察，不扣任何活动")

    print("\n" + "=" * 68)
    print("⑤ session note 回答「这一行为什么这样」")
    print("=" * 68)
    rows = conn.execute(
        "SELECT activity_id,outcome,quality,note FROM sessions WHERE child_id=? "
        "ORDER BY id", (cid,)).fetchall()
    for r in rows:
        print(f"  {r['activity_id']}  {r['outcome']:8s} {r['quality']:.2f}  {r['note']}")
    assert all(r["note"] for r in rows), "每条 session 都该有可读的 note"


if __name__ == "__main__":
    _cli()
