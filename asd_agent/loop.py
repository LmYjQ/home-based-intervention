# -*- coding: utf-8 -*-
"""
闭环演示：计划执行 → 家长反馈 → 更新计划 → 第二天真的不一样
=============================================================
    python loop.py --no-llm --script hardcoded   # ★ 全离线确定性基线，<1s
    python loop.py                              # live，3 天 ≈ 16 次 LLM，5-7 分钟
    DEMO_MODE=replay python loop.py             # 现场保险，<1s

★ 现场绝对不能用 live 跑完整循环
--------------------------------
3 天 = 1 次 planner + 3 次 observe + 3×2 次 coach + 3 次 eval = **16 次 LLM**，
按 7B 实测约 5–7 分钟。必须先 `DEMO_MODE=capture` 录成 fixture 再 replay。

★ 为什么用「抗拒冷却」而不是「状态机」做演示
---------------------------------------------
状态机 `entry → practicing` 要 7 天内 3 次成功且平均质量 ≥0.5 —— 一轮演示跑不出来。
冷却判定（`activities.py` 的 `refusal_counts >= 2`）一天两次就能凑够。
所以 Day 1 家长报一次「他不肯听 + 他站住不动」两条事件，
Day 2 检索器就会把那条活动剔掉，附一个家长能看懂的原因。

★ 日期策略：用真实过去日期，不用未来日期
----------------------------------------
`store.recent_activity_ids()` 用 `BETWEEN start AND end`（**有上界**，默认到今天），
而 `refusal_counts` / `recent_scenes` 用 `>= start`（**无上界**）——
两个函数对日期方向的假设不一致。写这条是因为将来一定会有人"顺手统一一下"，
统一之后 Day 2 的 `recent_activity_ids` 查不到 Day 1 的记录，
演示会莫名其妙地失效。Day1 = today-(days-1) 往回走，两个方向都对，不必改 store。

★ `--no-llm` 的诚实边界
-----------------------
观察降级时**一条都不写**（见 observe._fallback_batch），所以 `--no-llm` 跑不出冷却。
`--script hardcoded` 用硬编码的 ObservationOutcome 直接走 persist + refusal_sessions，
跳过 LLM 但走**完全相同**的下游代码路径 —— 闭环验证有效，**抽取质量未验证**。
这两件事会在输出里明说，不要让看的人以为"抽取准不准"也被验证过了。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm
import observe
import store
from activities import (PlanRequest, ScoredActivity, candidates as retrieve_candidates,
                       retrieve)
from activities import explain_retrieval
from capabilities import AbilityProfile, ParentConstraints
from coach import CoachOutcome, ObservationInput, card_from_activity, coach_today
from evaluator import EvalActivityInput, EvalInput, apply_transitions, evaluate_day
from planner import WeeklyPlan, greedy_fallback_plan
from profile import extract_profile
from vocab import ABILITY_STATE, ABILITY_DOMAIN

_STATE_ORDER = {s: i for i, s in enumerate(ABILITY_STATE)}


# ================================================================== 演示剧本
@dataclass
class DayInput:
    """一天的家长输入。真实使用时就是一段话。"""

    date: str
    parent_text: str = ""
    self_report: str = ""
    executed: bool = True


DEMO_DAYS = [
    DayInput(
        date="",
        parent_text=("今天玩一二三木头人，他一开始不肯听，我说你停，他就站在那儿不动，"
                     "后来我拉着他才停下；停下来他立刻坐在地上哭了。我一看都六点了，就没再玩了。"),
        self_report="他今天状态一般。",
    ),
    DayInput(
        date="",
        parent_text=("今天在超市他蹲下来捂住耳朵了，我没理他，过了一会儿自己好了。"
                     "出来以后他指着我们家的猫给我看，我说是猫呀，他就笑了。"),
        self_report="超市那段有点难熬，但回家之后还不错。",
    ),
    DayInput(
        date="",
        parent_text="今天他自己玩了半个小时积木，中间还跑过来给我看他搭的。",
        self_report="挺好的。",
    ),
]


# ================================================================== 一天的输入
@dataclass
class DayResult:
    day: int
    date: str
    obs: observe.ObservationOutcome
    hits: list = field(default_factory=list)
    cands_in: list[ScoredActivity] = field(default_factory=list)
    card: CoachOutcome | None = None
    transitions: list = field(default_factory=list)
    suggested_id: str = ""
    picked_id: str = ""
    cands_tomorrow: list[ScoredActivity] = field(default_factory=list)
    # (id, 原因)。分开两类的理由见 assert_loop_closed ②：
    # 抗拒导致的冷却必须能反查回事件，而"最近 3 天刚练过"本来就与事件无关。
    cooled_out: list[tuple[str, str]] = field(default_factory=list)      # 因抗拒冷却
    dropped_other: list[tuple[str, str]] = field(default_factory=list)   # 其他硬过滤


# ================================================================== 灌历史
def seed_demo_history(conn, child_id: str, confidence: float) -> int:
    """给这个孩子灌一段"已经用了五周"的真实练习历史。

    ★ 这一段原先在 `pipeline_demo.py` 里（已删，合并进本文件）。
      它当时是那个 demo **唯一**独有的一屏：
          ⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足，升级闸门拦下了
      那个对照需要历史数据才能出现，而空库只��打出"没有任何技能状态变化"，
      于是"升级闸门"这个卖点整个看不见。

    语义上也说得通：真实用户用了几周之后当然是有历史的，
    这个开关就是"从第五周开始演"。

    刻意**不写画像** —— 画像由调用方给（可能是可信度不同的两版，
    闸门对照正是靠这个差别跑出来的）。
    """
    import statemachine as sm

    history = sm._demo_history()
    for h in history:
        store.log_session(conn, child_id, h)
    dates = sorted({r.date for r in history})
    for t in sm.walk(skill_item="共同注意", domain="social_adapt",
                     initial_state="not_started", initial_level=1,
                     history=history, dates=dates, profile_confidence=confidence):
        store.append_state(conn, child_id, t)
    return len(history)


# ================================================================== 请求装配
def build_request(conn, child_id: str, prof: AbilityProfile) -> PlanRequest:
    """把画像 + 状态机 + 记忆库拼成检索请求。**这是整个系统的接缝。**

    ★ 必须像 pipeline_demo 当年那样**从 store 读记忆**。
      曾经在别处只传 minutes/materials 就断言冷却会触发，断言直接失败 ——
      检索器看的是 req.refusal_counts，而那段测试从没往里放东西。
    """
    pc = prof.parent_constraints
    states = store.current_states(conn, child_id)
    by_domain: dict[str, str] = {}
    for a in prof.assessments:
        st = states.get(a.skill_item, {}).get("state", "not_started")
        if a.domain not in by_domain or _STATE_ORDER[st] > _STATE_ORDER[by_domain[a.domain]]:
            by_domain[a.domain] = st
    return PlanRequest(
        levels=prof.levels(),
        target_states=by_domain,
        priority_domains=prof.domains_of_interest(),
        daily_minutes=pc.daily_minutes_available,
        materials_at_home=set(pc.materials_at_home),
        avoid_load_sensory=pc.avoid_load_sensory,
        known_refusals=pc.known_refusals,
        refusal_counts=store.refusal_counts(conn, child_id),
        success_counts=store.success_counts(conn, child_id),
        recent_activity_ids=store.recent_activity_ids(conn, child_id, 3),
        success_history_ids=store.success_history_ids(conn, child_id),
        recent_scenes=store.recent_scenes(conn, child_id),
        successes_outside_home=store.successes_outside_routine(conn, child_id),
        preferences=pc.preferences,
    )


# ================================================================== ★ 交叉核对
def assert_loop_closed(days: list[DayResult], reqs: list[PlanRequest], conn) -> None:
    """演示数据必须自己先经得起交叉核对 —— `statemachine.py` 的 soc-004 教训。

    当时 `_demo_history()` 写死了 `soc-004 看他的眼睛`，而那条活动的
    target_skill_items 是眼神回应/轮流等待、不含共同注意，于是状态机拿到空历史
    却照样输出一条完整转移链 —— 一切看着正常，实则全是幻觉。

    ★ 这一版的断言改过一次：初版有三条是**按直觉写的，不是按代码行为写的**，
    结果第一次跑就误报了 3 条 —— 它们挡住的不是 bug，是正确行为：
      · "Day2 候选必须和 Day1 不同" → Day3 没有新反馈，候选本来就应该稳定。
        不变是**正确**的；真正要保证的是"变化时能被归因"，不是"每天都变"。
      · "recent_scenes 里的场景必须出现在当天事件里" → recent_scenes 查的是
        **14 天滚动窗口**，Day2 出现 Day1 的 indoor_play 是对的。
        该查的是"出现在**某一天**的事件里"，不是"当天"。
    写断言前先读一遍被断言的函数，这两条都是没读就写出来的。
    """
    import json as _json

    problems: list[str] = []

    # ---- 收集库里真实存在的场景（任何一天）----
    all_scenes: set[str] = set()
    for row in conn.execute("SELECT payload FROM observations").fetchall():
        try:
            all_scenes.add(_json.loads(row["payload"]).get("scene"))
        except Exception:
            pass
    all_scenes.discard(None)

    from activity_data import get as get_act

    # ---- ① 至少有一天候选真的变了，且每次变化都有据可查 ----
    changed_days = 0
    for i in range(1, len(days)):
        before = {x.activity.activity_id for x in days[i - 1].cands_in}
        after = {x.activity.activity_id for x in days[i].cands_tomorrow}
        if before != after:
            changed_days += 1
            gone = before - after
            # ★ 索引：days[i].cands_tomorrow 是**第 i 天**算出来的，
            #   而它反映的是**第 i-1 天**的反馈造成的剔除。
            #   早先写成 days[i].cooled_out，于是三条变化全都"查不到原因"。
            reasons = dict(days[i - 1].cooled_out + days[i - 1].dropped_other)
            for aid in gone:
                if aid not in reasons:
                    problems.append(f"{aid} 退出候选，但没有任何剔除原因 —— 变化不可解释")
    if changed_days == 0 and len(days) > 1:
        problems.append("三天里候选一次都没变 —— 闭环可能没有生效")

    # ---- ② 因抗拒冷却的，必须能反查回某条事件，且行为在该活动的 refusal_behaviors 里 ----
    for d in days:
        for aid, reason in d.cooled_out:
            act = get_act(aid)
            if act is None:
                problems.append(f"Day{d.day} 冷却了不存在的活动 {aid}")
                continue
            hit = [h for h in d.hits if h.activity_id == aid]
            if not hit:
                problems.append(f"Day{d.day} {aid} 以「{reason}」被冷却，"
                                f"但当天没有任何事件归属到它")
                continue
            for h in hit:
                if h.behavior not in act.refusal_behaviors:
                    problems.append(f"Day{d.day} {aid} 被计入抗拒，"
                                    f"但行为 {h.behavior} 不在它的 refusal_behaviors 里")
        # 反向：当天被拒到**阈值以上**的活动，必须真的进了冷却清单。
        # 阈值是 2（activities.py 的硬过滤），所以被拒 1 次的不该被要求冷却 ——
        # 初版写成"有事件归属就该冷却"，第一次跑就误报了 fin-001 / gro-006。
        counts: dict[str, int] = {}
        for h in d.hits:
            counts[h.activity_id] = counts.get(h.activity_id, 0) + 1
        cooled_ids = {aid for aid, _ in d.cooled_out}
        for aid, n in counts.items():
            if n >= 2 and aid not in cooled_ids:
                problems.append(f"Day{d.day} {aid} 当天被拒 {n} 次（已过阈值），"
                                f"却没进冷却清单")

    # ---- ③ recent_scenes 里每个场景都必须真的存在于库中某条观察 ----
    for d, req in zip(days, reqs):
        for sc in req.recent_scenes:
            if sc not in all_scenes:
                problems.append(f"Day{d.day} recent_scenes 含 {sc}，"
                                f"但库里任何一天的观察里都没有这个场景")

    # ---- ④ 每条 refused session 都要能追溯到一条真实事件 ----
    for d in days:
        for h in d.hits:
            if not (0 <= h.event_index < len(d.obs.events)):
                problems.append(f"Day{d.day} 有一条 session 的 event_index "
                                f"{h.event_index} 越界")

    if problems:
        print("  ✗ 交叉核对未通过：")
        for x in problems:
            print(f"      {x}")
        raise AssertionError("演示数据自己没对上，不可信")
    print(f"  ✓ 候选在 {changed_days} 天里发生过可归因的变化")
    print("  ✓ 每个冷却都能反查回事件，且行为确实在该活动的 refusal_behaviors 里")
    print("  ✓ recent_scenes 里的场景都能在库里找到")
    print("  ✓ 每条 refused session 都可追溯到一条真实事件")


# ================================================================== 跑一天
def run_day(conn, child_id: str, prof: AbilityProfile, day: DayInput, *,
            n: int, plan_day_ids: list[str], no_llm: bool = False,
            hardcoded: bool = False,
            out_prev: list[ObservationInput] | None = None) -> tuple[DayResult, PlanRequest]:
    c = time.time()
    print(f"\n{'=' * 70}\n第 {n} 天 · {day.date}\n{'=' * 70}")
    # 教练诊断读的是**之前**攒下的观察，不是今天刚说的那段 ——
    # 今天的话是今天情况的报告，它驱动的是明天的调整。
    out_prev: list[ObservationInput] = []

    # ---------- 1. 今天的卡片（先用当前记忆生成）----------
    # ★ 顺序很重要：先生成今天的卡片，再收今天的反馈。
    # 反过来做（先收反馈再出卡片）会让 Day 1 出现荒谬的一幕 ——
    # 周计划刚建议 gro-007，同一天的反馈立刻把它冷却掉，于是"今天的建议"
    # 当场失效。用户要的是「计划执行 → 反馈 → 更新计划」，
    # 那么反馈影响的是**明天**，今天已经发出去的卡片不该被追溯修改。
    print("\n【1】检索候选 → 今天的卡片")
    req = build_request(conn, child_id, prof)
    cands = retrieve_candidates(req, top_k=12)
    print(f"  记忆切片：refusal_counts={req.refusal_counts or '{}'}  "
          f"recent_scenes={req.recent_scenes or '[]'}  "
          f"target_states={ {k: v for k, v in list(req.target_states.items())[:2]} }")
    print(f"  {len(cands)} 条通过硬过滤，前 3 条：")
    for sc in cands[:3]:
        print(f"    {sc.score:.3f} {sc.activity.activity_id} {sc.activity.title}")
        print(f"          {explain_retrieval(req, sc)}")

    print("\n【2】教练 Agent → 家长卡片")
    suggested = plan_day_ids[0] if plan_day_ids else (cands[0].activity.activity_id if cands else "")
    pick = next((x for x in cands if x.activity.activity_id == suggested), None)
    if pick is None and cands:
        pick = cands[0]
        print(f"  ★ 周计划建议 {suggested}，但它今天过不了硬过滤 → 换 {cands[0].activity.activity_id}")
    if pick is None:
        print("  候选表为空，今天不出卡片")
        return DayResult(day=n, date=day.date,
                         obs=observe._fallback_batch(day.parent_text, day.date,
                                                     observe.redflag.CLEAN),
                         cands_in=[], suggested_id=suggested), req
    if no_llm or hardcoded:
        cout = CoachOutcome(card=card_from_activity(
            pick.activity, prof.parent_constraints.preferences),
            activity=pick.activity, degraded=True,
            adjustment="（--no-llm / hardcoded：使用活动定义直接生成的兜底卡片）")
    else:
        cout = coach_today(cands, today_id=pick.activity.activity_id,
                           observations=observe.to_coach_inputs(out_prev),
                           req=req)
    if cout.analysis:
        for sg in cout.analysis.signals:
            print(f"  诊断：{sg.kind}｜{sg.interpretation} → {sg.impact}"
                  f"（功能 {sg.function}）")
    if cout.adjustment:
        print(f"  调整：{cout.adjustment}")
    print()
    print("  " + cout.card.render().replace("\n", "\n  "))

    # ---------- 3. 家长执行完，反馈一段话 ----------
    print("\n【3】家长执行完 → 一段话 → 结构化事件")
    if hardcoded:
        out = _hardcoded_outcome(day)
        print("  （--script hardcoded：硬编码事件，跳过 LLM 抽取；"
              "下游代码路径与真实抽取完全相同）")
    else:
        out = observe.extract_observations(day.parent_text, date=day.date, no_llm=no_llm)
    print("  " + out.render().replace("\n", "\n  "))
    if out.trace:
        print(f"  trace: {out.trace.latency_ms}ms  重试 {len(out.trace.retries)}")

    print("\n【4】写入 observations 表")
    ids = observe.persist(conn, child_id, out)
    print(f"  写入 {len(ids)} 条（噪声事件不写）")

    # ---------- 5. 抗拒桥接 ★ 闭环咬合点 ----------
    print("\n【5】观察 → refused session（★ 闭环咬合点，纯 Python）")
    attempted = set(plan_day_ids)
    hits = observe.refusal_sessions(out, attempted)
    if hits:
        for h in hits:
            print(f"  {h.activity_id}  {h.note}")
        observe.log_refusals(conn, child_id, hits)
        print(f"  ★ 写入 {len(hits)} 条 refused session")
    else:
        print("  今天没有可归属到已练活动的抗拒（不计入 refusal_counts，"
              "但仍作为观察影响场景与诊断）")

    # ---------- 6. 日评估 ----------
    print("\n【6】评估 Agent → 日评估")
    if day.executed:
        acts = []
        for aid in plan_day_ids:
            a = _get(aid)
            if a is None:
                continue
            # 观察桥接已经产出 refused 的活动，不重复让评估再判一次
            if any(h.activity_id == aid for h in hits):
                continue
            acts.append(EvalActivityInput(
                activity_id=a.activity_id, title=a.title,
                success_criteria=a.success_criteria,
                refusal_behaviors=a.refusal_behaviors,
                materials=[m.name for m in a.materials]))
        if acts:
            inp = EvalInput(date=day.date, activities=acts,
                            observations=[o.raw for o in observe.to_coach_inputs(out)],
                            parent_self_report=day.self_report,
                            prior_states=req.target_states)
            if no_llm or hardcoded:
                from evaluator import EvalOutcome, _fallback_eval, to_session_records
                fb = _fallback_eval(inp)
                ev = EvalOutcome(eval=fb, degraded=True,
                                 sessions=to_session_records(day.date, fb.per_activity))
            else:
                ev = evaluate_day(inp)
            print("  " + ev.render().replace("\n", "\n  "))
            for rec in ev.sessions:
                store.log_session(conn, child_id, rec)
            ts = apply_transitions(inp, ev, store.current_states(conn, child_id),
                                  prof, conn=conn, child_id=child_id)
            for t in ts:
                store.append_state(conn, child_id, t)
                print(f"  状态转移：{t.render()}")
        else:
            ts = []
            print("  今天的活动都由观察桥接覆盖，评估只补空 —— 避免同一天两个 verdict")
            print("  （否则 refusal_rate 的分母翻倍，状态机的回退阈值全部失真）")
    else:
        ts = []

    # ---------- 7. 明天的候选 ----------
    req2 = build_request(conn, child_id, prof)
    cands2 = retrieve_candidates(req2, top_k=12)
    ids2 = {x.activity.activity_id for x in cands2}
    # ★ 必须从**全量** retrieve() 里取剔除原因。早先写的是从 cands 里取，
    # 而 cands 全是通过的 passed 对象 —— 它们按定义没有 hard_filter_reason，
    # 于是打印出来是空的"没推荐："，看起来像理由丢了。
    full2 = retrieve(req2)
    reason_of = {x.activity.activity_id: x.hard_filter_reason
                 for x in full2 if not x.passed}
    cooled: list[tuple[str, str]] = []
    dropped_other: list[tuple[str, str]] = []
    for x in cands:
        aid = x.activity.activity_id
        if aid in ids2:
            continue
        reason = reason_of.get(aid, "（未进入候选，可能只是排名掉出前 12）")
        (cooled if "冷却" in reason else dropped_other).append((aid, reason))

    print("\n【7】明天的候选（重新检索）")
    if cooled:
        print("  ★ 因家长反馈而退出候选：")
        for aid, reason in cooled:
            print(f"    ✗ {aid} —— 没推荐：{reason}")
    if dropped_other:
        print("  因其他硬过滤退出（与今天的反馈无关）：")
        for aid, reason in dropped_other:
            print(f"    · {aid} —— {reason}")
    if not cooled and not dropped_other:
        print("  （今天的候选没有变化）")

    print(f"\n  —— 本天用时 {(time.time() - c) * 1000:.0f}ms")
    return (DayResult(day=n, date=day.date, obs=out, hits=hits, cands_in=cands,
                      card=cout, transitions=ts, suggested_id=suggested,
                      picked_id=pick.activity.activity_id,
                      cands_tomorrow=cands2, cooled_out=cooled,
                      dropped_other=dropped_other), req2)


def _get(aid: str):
    from activity_data import get as get_act
    return get_act(aid)


def _hardcoded_outcome(day: DayInput) -> observe.ObservationOutcome:
    """硬编码事件。**抽取质量未验证，只验证闭环。**

    事件是手写的，但走的是与真实抽取**完全相同**的下游路径
    （verify → dedupe → 落库 → 桥接），所以闭环验证是有效的。
    每一条的 behavior 都必须真的在当天所练活动的 refusal_behaviors 里 ——
    `assert_loop_closed()` 的第 ② 条断言专门盯这个。
    """
    from schema import Observation

    spec = {
        0: [("indoor_play", "refusal", "adult_demand", "adult_relented", 3,
             "他一开始不肯听"),
            ("indoor_play", "transition_stuck", "no_consent", "left_scene", 3,
             "他就站在那儿不动"),
            ("indoor_play", "tantrum", "activity_ending", "continued_unchanged", 4,
             "坐在地上哭了")],
        1: [("shopping", "sensory_avoid", "sensory_overload", "left_scene", 3,
             "蹲下来捂住耳朵"),
            ("other", "joint_attention", "unclear", "unclear", 1,
             "他指着我们家的猫给我看")],
        2: [("indoor_play", "task_completion", "unclear", "unclear", 1,
             "自己玩了半个小时积木"),
            ("indoor_play", "joint_attention", "unclear", "unclear", 1,
             "跑过来给我看他搭的")],
    }
    idx = DEMO_DAYS.index(day)
    events = [
        Observation(scene=sc, behavior=bh, antecedent=an, consequence=co,
                    intensity=it, raw_quote=q, confidence=0.75)
        for (sc, bh, an, co, it, q) in spec.get(idx, [])
    ]
    return observe.ObservationOutcome(
        source_text=day.parent_text, date=day.date, events=events,
        redflags=observe.redflag.scan(day.parent_text))


# ================================================================== 跑整个循环
def run_loop(child_id: str, *, days: int = 3, no_llm: bool = False,
             hardcoded: bool = False, trace: bool = False,
             minutes: int = 30, materials: str = "杯子,绘本,小汽车,积木",
             seed_history: bool = False, db_path: str | None = None) -> int:
    t0 = time.time()
    db = db_path or f"/tmp/loop_{child_id}.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    conn = store.connect(db)

    constraints = ParentConstraints(
        daily_minutes_available=minutes,
        materials_at_home=[m for m in materials.split(",") if m],
        avoid_load_sensory=True,
    )

    print("=" * 70)
    print("评估报告 → 能力画像")
    print("=" * 70)
    if no_llm or hardcoded:
        from profile import _fallback_profile
        from profile import ProfileExtraction
        from redflag import scan
        prof = _fallback_profile(child_id, 34, constraints)
        ex = ProfileExtraction(profile=prof, redflags=scan(""), degraded=True,
                               note="降级画像：可信度 0.1，状态机将冻结全部升级")
        print(f"  （降级画像，可信度 {prof.overall_confidence:.2f} —— "
              f"低于升级闸门 0.5，状态机会冻结所有升级）")
        if ex.redflags.triggered:
            print("  ★ 报告中的红旗：")
            for h in ex.redflags.hits:
                print(f"      [{h.severity}] {h.group} —— {h.quote}")
    else:
        from profile import SAMPLE_REPORT
        ex = extract_profile(SAMPLE_REPORT, child_id, 34, parent_constraints=constraints)
        print(f"  可信度 {ex.profile.overall_confidence:.2f}  降级 {ex.degraded}")
        if ex.redflags.triggered:
            print("  ★ 报告中的红旗：")
            for h in ex.redflags.hits:
                print(f"      [{h.severity}] {h.group} —— {h.quote}")
        if ex.unverified:
            print(f"  引文未通过原文核对（已降级为推断）：{'、'.join(ex.unverified)}")
    print("  " + prof.render().replace("\n", "\n  "))
    store.ensure_child(conn, child_id, name="小宇", age_months=34)
    store.save_profile(conn, prof)

    if seed_history:
        n = seed_demo_history(conn, child_id, prof.overall_confidence)
        print(f"\n  ★ 已灌入 {n} 条练习历史（模拟这个孩子已经用了五周）")
        print("    —— 升级闸门的那一屏对照需要历史数据才出得来")

    # ---- 周计划只跑一次（决策 A）----
    print("\n" + "=" * 70)
    print("规划 Agent → 一周计划（★ 全程只跑这一次，之后每天只重算候选）")
    print("=" * 70)
    req0 = build_request(conn, child_id, prof)
    base = date.today() - timedelta(days=days - 1)
    if no_llm or hardcoded:
        plan = greedy_fallback_plan(req0, f"plan-{child_id}", days=days,
                                    today=base.isoformat())
    else:
        from planner import plan_week
        plan = plan_week(prof, req0, plan_id=f"plan-{child_id}", days=days,
                         today=base.isoformat())
    print(plan.render())
    print(f"\n  degraded={plan.degraded}"
          + (f"  剔除 {plan.excluded_activity_ids}" if plan.excluded_activity_ids else ""))

    # ---- 逐日循环 ----
    results: list[DayResult] = []
    reqs: list[PlanRequest] = []
    prev_inputs: list[ObservationInput] = []   # 教练诊断读的是之前攒下的观察
    inputs = list(DEMO_DAYS[:days])
    for i, d in enumerate(inputs):
        d.date = (base + timedelta(days=i)).isoformat()
    for i, din in enumerate(inputs, 1):
        blk = plan.days[min(i - 1, len(plan.days) - 1)]
        ids = [a.activity_id for a in blk.activities]
        res, req = run_day(conn, child_id, prof, din, n=i, plan_day_ids=ids,
                           no_llm=no_llm, hardcoded=hardcoded, out_prev=prev_inputs)
        results.append(res)
        reqs.append(req)
        prev_inputs = observe.to_coach_inputs(res.obs) if res.obs.events else []

    # ---- 收尾对照 ----
    print("\n" + "=" * 70)
    print("收尾：反馈到底改变了什么")
    print("=" * 70)
    _show_diff(results)

    print("\n" + "=" * 70)
    print("★ 交叉核对（演示数据自己必须对得上）")
    print("=" * 70)
    assert_loop_closed(results, reqs, conn)

    print("\n" + "=" * 70)
    print("记忆库查询（纯 SQL，零 token）")
    print("=" * 70)
    today = date.today().isoformat()
    print("  " + store.what_last_week(conn, child_id, today).render())
    print()
    print("  " + store.what_progressed(conn, child_id, 30,
                                     profile_confidence=prof.overall_confidence).render())
    print()
    print("  " + store.what_child_refuses(conn, child_id, 30).render())

    if hardcoded and not no_llm:
        print("\n" + "!" * 70)
        print("! --script hardcoded：用硬编码事件跳过了 LLM 抽取。")
        print("! 闭环咬合是验证过的；**抽取准确率没有验证**，那需要 live 或 capture。")
        print("!" * 70)
    if not no_llm and not hardcoded:
        print("\n  live 模式耗时提醒：16 次 LLM 调用，现场请用 DEMO_MODE=replay。")

    print(f"\n全链路用时 {time.time() - t0:.1f}s")
    conn.close()
    return 0


def _show_diff(results: list[DayResult]) -> None:
    """Day 1 与 Day 2 的候选对照 —— 这是"反馈改变了计划"的直接证据。"""
    if len(results) < 2:
        print("  （只有一天，没有对照）")
        return
    a, b = results[0], results[1]
    ids_a = [s.activity.activity_id for s in a.cands_in][:5]
    ids_b = [s.activity.activity_id for s in b.cands_tomorrow][:5]
    print(f"  Day1 候选前 5：{'  '.join(ids_a)}")
    print(f"  Day2 候选前 5：{'  '.join(ids_b)}")
    # ★ 用 Day1 自己的 dropped 清单，不是 Day2 的。
    #   ids_b（Day2 的候选）正是由 Day1 的反馈淘汰出来的，
    #   所以原因要去 a（Day1）那里取 —— 早先写成 b.cooled_out，
    #   而 Day2 自己没踢掉任何东西，于是**整个演示最关键的那两行消失了**，
    #   只剩"新进候选"，看起来像系统自己换了一批活动。
    cooled = {aid for aid, _ in a.cooled_out}
    other = {aid for aid, _ in a.dropped_other}
    gone = [i for i in ids_a if i not in ids_b]
    came = [i for i in ids_b if i not in ids_a]
    if cooled:
        print(f"  ★ 因家长 Day1 的反馈退出：{'、'.join(sorted(cooled))}")
        for aid, reason in a.cooled_out:
            print(f"      {aid} —— {reason}")
    if other:
        print(f"  因其他硬过滤退出（与反馈无关）：{'、'.join(sorted(other))}")
        for aid, reason in a.dropped_other:
            print(f"      {aid} —— {reason}")
    if came:
        print(f"  新进候选：{'、'.join(came)}")
    if not gone and not came:
        print("  ⚠ 两天的候选没有变化 —— 闭环可能没有生效")
    print()
    print("  家长反馈 → observations → 抗拒计数 / 场景分布 → 检索硬过滤，")
    print("  这条链上没有任何一步需要重新跑排期 LLM。")


# ================================================================== CLI
def main() -> int:
    ap = argparse.ArgumentParser(
        description="闭环演示：计划执行 → 家长反馈 → 更新计划",
        epilog="现场请用 DEMO_MODE=replay：live 模式 3 天约 16 次 LLM 调用，5-7 分钟。")
    ap.add_argument("--child", default="loop-child")
    ap.add_argument("--days", type=int, default=3, help="演示天数（最多 3，有剧本）")
    ap.add_argument("--no-llm", action="store_true", help="全离线，不调模型")
    ap.add_argument("--script", choices=["hardcoded"], default=None,
                    help="hardcoded = 用硬编码事件跳过抽取，但走相同的下游路径")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--minutes", type=int, default=30, help="家长每天的时间预算")
    ap.add_argument("--materials", default="杯子,绘本,小汽车,积木",
                    help="家里现成的材料，逗号分隔")
    ap.add_argument("--like", action="append", default=[],
                    help="孩子喜欢的材料/场景/活动，可多次")
    ap.add_argument("--seed-history", action="store_true",
                    help="先灌一段五周练习历史（升级闸门的那一屏需要它）")
    args = ap.parse_args()
    return run_loop(args.child, days=args.days, no_llm=args.no_llm,
                    hardcoded=args.script == "hardcoded", trace=args.trace,
                    minutes=args.minutes, materials=args.materials,
                    seed_history=args.seed_history)


if __name__ == "__main__":
    sys.exit(main())
