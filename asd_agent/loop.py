# -*- coding: utf-8 -*-
"""
闭环演示：计划执行 → 家长逐条反馈 → 更新计划 → 第二天真的不一样
===============================================================
    python loop.py --no-llm --script hardcoded   # ★ 全离线确定性基线，<1s
    python loop.py                              # live，5 天 ≈ 13 次 LLM
    DEMO_MODE=replay python loop.py             # 现场保险，<1s

★ 家长端的形态：逐条，不是一段话
--------------------------------
当天排了几条活动，家长就逐条填几条（`feedback.ActivityFeedback`），
外加一个当天的「其他观察」框。事件抽取只跑在**家长写了补充描述**的那些条目上。

这么改的直接原因是修一个真 bug：旧的 `refusal_sessions(out, attempted_ids)`
收的是当天**计划**的全集，在其中做「事件 × 活动」的笛卡尔积匹配 ——
家长明说"今天玩一二三木头人"（只玩了 gro-007），fin-001 / gro-006 却各白得
1 次 refused 记录，只因为它们恰好也含 refusal 且都在 indoor_play。
实测 4 条 refused session 里 2 条是无中生有。
逐条之后归属不再需要推断，见 `DayInput` 与 `observe.refusal_sessions` 的说明。

★ 为什么是 5 天
--------------
冷却阈值 `refusal_counts >= 2` 的**语义变了**。逐条反馈下一条反馈最多产生
1 条 refused session，所以它从"同一天两次"变成了"**两天都拒**" ——
这在临床上更合理（隔两天各拒一次才是真该换掉它的信号），
代价是要多跑两天才看得到冷却。冷却是这条链上最有说服力的一屏，所以值得。

★ 为什么用「抗拒冷却」而不是「状态机」做演示
---------------------------------------------
状态机 `entry → practicing` 要 7 天内 3 次成功且平均 quality ≥0.5 —— 一轮演示跑不出来。
而 `--no-llm --script hardcoded` 用的是降级画像（`overall_confidence = 0.1`），
升级闸门会**冻结全部转移**，于是状态机整条链一次都跑不到。
要看真的转移，去掉 `--no-llm`（画像 18-47s）或用 `--seed-history`。

★ 日期策略：用真实过去日期，不用未来日期
----------------------------------------
`store.recent_activity_ids()` 用 `BETWEEN start AND end`（**有上界**，默认到今天），
而 `refusal_counts` / `recent_scenes` 用 `>= start`（**无上界**）——
两个函数对日期方向的假设不一致。写这条是因为将来一定会有人"顺手统一一下"，
统一之后 `recent_activity_ids` 查不到前一天的记录，演示会莫名其妙地失效。
Day1 = today-(days-1) 往回走，两个方向都对，不必改 store。

★ `--no-llm` 的诚实边界
-----------------------
家长点的 outcome / quality **完全生效**（`feedback.quality_for()` 是纯函数，零 token），
所以 sessions 表、状态机输入、检索记忆切片都是真的。
降级的是两处：从家长的补充描述里**抽事件**（`observe._fallback_batch` 一条都不编）
和日结的**方向判断**（只按计数报事实，不猜）。
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
from coach import (CoachOutcome, ObservationInput, card_from_activity,
                   coach_today, render_feedback_form)
from evaluator import TransitionsInput, apply_transitions
from feedback import ActivityFeedback, apply_feedbacks
import summary
from summary import summarize_day
from planner import WeeklyPlan, greedy_fallback_plan
from profile import extract_profile
from vocab import ABILITY_STATE, ABILITY_DOMAIN

_STATE_ORDER = {s: i for i, s in enumerate(ABILITY_STATE)}


# ================================================================== 演示剧本
@dataclass
class DayInput:
    """一天的家长输入。**家长排了几条活动，就逐条填几条。**

    ★ 形态从"一整段话"改成"逐条"，不是为了显得结构化，是修一个真 bug：
      原先 `refusal_sessions(out, set(plan_day_ids))` 收的是当天**计划**的全集，
      而它在这个集合内做「事件 × 活动」的笛卡尔积匹配。家长明说
      "今天玩一二三木头人"（只玩了 gro-007），fin-001 / gro-006 却各白得
      1 次 refused 记录 —— 只因为它们恰好也含 refusal 且都在 indoor_play。
      实测 4 条 refused session 里 2 条是无中生有。

      逐条之后归属不再需要推断：`feedback.ActivityFeedback.activity_id`
      就是家长亲手填的，"当天练过哪几条"从一个推测变成了一个事实。
    """

    date: str
    feedbacks: list[ActivityFeedback] = field(default_factory=list)
    other_notes: str = ""      # 当天的「其他观察」框，扣不到任何活动头上


# ★ Day 1 刻意只填 gro-007，另外两条选「跳过」—— 这就是改动要修的那个 bug。
#   改之前它们会因为"当天计划里有"而各得 1 次无中生有的抗拒记录。
#
# ★ 为什么是 5 天而不是 3 天：**冷却阈值 2 的语义变了。**
#   逐条反馈下，一条反馈最多产生 1 条 refused session（`feedback.apply_feedbacks`
#   在家长已点「不配合」时会跳过桥接）。所以 `refusal_counts >= 2` 从
#   "同一天两次"变成了"**两天都拒**" —— 这在临床上更合理：
#   一次练习里拒两次和隔两天各拒一次，后者才是真该换掉它的信号。
#   代价是演示要多跑两天才看得到冷却，而冷却是这条链上最有说服力的一屏，
#   所以值得。第 4 天再拒一次 gro-007，第 5 天它就被剔掉了。
DEMO_DAYS = [
    DayInput(
        date="",
        feedbacks=[
            ActivityFeedback("gro-007", "refused", prompt_level=3, steps_done=0,
                             note=("他一开始不肯听，我说你停，他就站在那儿不动，"
                                   "后来我拉着他才停下；停下来他立刻坐在地上哭了。")),
            ActivityFeedback("fin-001", "skipped"),
            ActivityFeedback("gro-006", "skipped"),
        ],
    ),
    DayInput(
        date="",
        feedbacks=[
            ActivityFeedback("fin-004", "partial", steps_done=2,
                             note="他一次翻好几页，我把书按慢了，一张一张来。"),
            ActivityFeedback("fin-008", "success", prompt_level=1, steps_done=4),
            ActivityFeedback("cog-001", "skipped"),
        ],
        # 「其他观察」框：它会照常抽成事件进 observations 表，
        # 但 refusal_counts 记 0 —— 超市那次扣不到翻书或涂鸦的头上。
        other_notes=("今天在超市他蹲下来捂住耳朵了，我没理他，过了一会儿自己好了。"
                     "出来以后他指着我们家的猫给我看，我说是猫呀，他就笑了。"),
    ),
    DayInput(
        date="",
        feedbacks=[
            ActivityFeedback("fin-004", "success", prompt_level=1, steps_done=4,
                             note="他今天自己翻完了大半本，还指着一页让我看。"),
            ActivityFeedback("fin-008", "partial", steps_done=2),
            ActivityFeedback("cog-001", "skipped"),
        ],
    ),
    # ── Day 4：冷却演示的第二次抗拒 ──
    # gro-007 已经过了"最近 3 天刚练过"的排重期，重新回到候选。
    # 家长这次又点了「不配合」→ refusal_counts 达到 2。
    DayInput(
        date="",
        feedbacks=[
            ActivityFeedback("gro-007", "refused", prompt_level=2,
                             note="第二次试了，还是不肯停下来，还是站住不动。"),
        ],
    ),
    # ── Day 5：gro-007 进了冷却期 ──
    DayInput(
        date="",
        feedbacks=[
            ActivityFeedback("soc-002", "success", prompt_level=1, steps_done=4,
                             note="推小车他居然自己递过来了三回。"),
        ],
    ),
]


# ================================================================== 一天的输入
@dataclass
class DayResult:
    day: int
    date: str
    obs: object                       # feedback.DayFeedbackResult
    hits: list = field(default_factory=list)   # observe.RefusalHit
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
    summary: object | None = None      # summary.DailySummary（★ 落库了）


# ================================================================== 灌历史
def seed_demo_history(conn, child_id: str, confidence: float) -> int:
    """给这个孩子灌一段"已经用了五周"的真实练习历史。

    ★ 这一段原先在 `pipeline_demo.py` 里（已删，合并进本文件）。
      它当时是那个 demo **唯一**独有的一屏：
          ⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足，升级闸门拦下了
      那个对照需要历史数据才能出现，而空库只会打出"没有任何技能状态变化"，
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
def build_request(conn, child_id: str, prof: AbilityProfile, *,
                  before: str | None = None) -> PlanRequest:
    """把画像 + 状态机 + 记忆库拼成检索请求。**这是整个系统的接缝。**

    ★ 必须像 pipeline_demo 当年那样**从 store 读记忆**。
      曾经在别处只传 minutes/materials 就断言冷却会触发，断言直接失败 ——
      检索器看的是 req.refusal_counts，而那段测试从没往里放东西。

    before 传给 latest_summary，用来排除"今天"：同一天里第 1 步发卡片、
    第 7 步重算明天的候选，两次读到的记忆必须不同 ——
    前者读到的是"含今天反馈前"的记忆，后者读到"含今天反馈后"的。
    不加这个参数，今天刚写的日结会回过头来解释今天要做什么，方向反了。
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
        recent_summary=store.latest_summary(conn, child_id, days=7, before=before),
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

    # ---- ① 每天的候选变化都要有据可查 ----
    #
    # ★ 索引：这里比的是 `days[i-1].cands_tomorrow`（第 i 天**反馈之后**重算的）
    #   与 `days[i].cands_in`（第 i+1 天**第 1 步**实际拿到的）。
    #   两者本该相同 —— 检索是纯函数，同一份记忆算出同一份候选。
    #   不同的地方必须能被第 i 天的剔除清单解释。
    #
    #   早先写成 `days[i-1].cands_in` 对 `days[i].cands_tomorrow`，
    #   那是拿**第 i 天的开头**去比**第 i+1 天的结尾**，跨了两次重算 ——
    #   于是三处该有的原因全都"查不到"，交叉核对直接误报。
    #   但一旦某天的剔除没有在候选里体现（例如"最近 3 天刚练过"
    #   已经因为跨天而失效），就会冒出一条无法解释的消失。
    changed_days = 0
    for i in range(1, len(days)):
        before = {x.activity.activity_id for x in days[i - 1].cands_tomorrow}
        after = {x.activity.activity_id for x in days[i].cands_in}
        if before != after:
            changed_days += 1
            gone = before - after
            reasons = dict(days[i - 1].cooled_out + days[i - 1].dropped_other)
            for aid in gone:
                if aid not in reasons:
                    problems.append(f"{aid} 退出候选，但没有任何剔除原因 —— 变化不可解释")
    # ★ changed_days 统计的是"重算结果与下一天实际拿到的不一致"，
    #   而检索是纯函数，所以它**正常情况下恒为 0** —— 这恰恰是好事：
    #   它证明第 7 步的重算和第 1 步的实际取用是同一套逻辑，没有偷换。
    #   闭环是否生效要看下一个数：真正因反馈被剔除的天数。
    fed_back = sum(1 for d in days if d.cooled_out or d.dropped_other)
    cooled_days = sum(1 for d in days if d.cooled_out)
    if fed_back == 0 and len(days) > 1:
        problems.append("这几天里候选一次都没被剔除 —— 闭环可能没有生效")

    # ---- ② 因抗拒冷却的，必须能反查回一条**真实的抗拒记录** ----
    #
    # ★ 这里换了个东西来反查。原先是「当天有没有事件归属到它」，
    #   那在旧形态下是对的（抗拒只从 observe 的事件桥接来）。
    #   逐条反馈下抗拒有两个来源，而且第一个是主要的：
    #     a) 家长自己点「不配合」 → feedback.apply_feedbacks 第 ① 步写的那条 session
    #     b) 桥接从补充描述里补记的（家长选了别的结果，但描述里提到他抗拒）
    #     Day 4 恰恰只有 (a)：家长点了「不配合」，于是第 ④ 步**跳过**了桥接，
    #   d.hits 是空的 —— 按旧断言会误报"冷却了但查不到事件"。
    #   正确的反查对象是 sessions 表里那天那条 outcome='refused' 的记录，
    #   它两个来源都包含。
    for d in days:
        refusals_today = {s.activity_id for s in (d.obs.sessions if d.obs else [])
                          if s.outcome == "refused"}
        for h in d.hits:            # 桥接补记的也要算进来
            refusals_today.add(h.activity_id)
        for aid, reason in d.cooled_out:
            if get_act(aid) is None:
                problems.append(f"Day{d.day} 冷却了不存在的活动 {aid}")
                continue
            if aid not in refusals_today:
                problems.append(f"Day{d.day} {aid} 以「{reason}」被冷却，"
                                f"但那天没有任何 refused 记录")
            act = get_act(aid)
            for h in d.hits:
                if h.activity_id == aid and h.behavior not in act.refusal_behaviors:
                    problems.append(f"Day{d.day} {aid} 被计入抗拒，"
                                    f"但行为 {h.behavior} 不在它的 refusal_behaviors 里")
        # 反向：当天被拒到**阈值以上**的活动，必须真的进了冷却清单。
        # 阈值是 2（activities.py 的硬过滤），所以被拒 1 次的不该被要求冷却 ——
        # 初版写成"有事件归属就该冷却"，第一次跑就误报了 fin-001 / gro-006。
        counts: dict[str, int] = {}
        for aid in refusals_today:
            counts[aid] = counts.get(aid, 0) + 1
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
        n_ev = sum(len(o.events) for o in d.obs.outcomes)
        for h in d.hits:
            if not (0 <= h.event_index < n_ev):
                problems.append(f"Day{d.day} 有一条 session 的 event_index "
                                f"{h.event_index} 越界")

    # ---- ⑤ ★ 本次改动的核心断言：归属只认家长填过的那条 ----
    # 旧签名收的是当天**计划**的全集（attempted_ids），于是一条
    # "他一开始不肯听"会同时给 gro-007 / fin-001 / gro-006 各记一次 ——
    # 后两条家长压根没提过。逐条反馈把这个推断变成了事实，所以现在能断言。
    for d in days:
        filled = {fb.activity_id for fb in DEMO_DAYS[d.day - 1].feedbacks}
        for h in d.hits:
            if h.activity_id not in filled:
                problems.append(
                    f"Day{d.day} {h.activity_id} 记了抗拒，"
                    f"但家长当天只填了 {sorted(filled)} —— 无中生有")
        # 「其他观察」框产出的必须是 0 条：家长自己标的 refused 之外
        # 不该再多出任何一条。
        marked_refused = sum(1 for fb in DEMO_DAYS[d.day - 1].feedbacks
                             if fb.outcome == "refused")
        if DEMO_DAYS[d.day - 1].other_notes and len(d.hits) > marked_refused:
            problems.append(f"Day{d.day} 抗拒 {len(d.hits)} 条多于家长标记的 "
                            f"{marked_refused} 条 —— 「其他观察」框不该贡献抗拒")

    # ---- ⑥ ★ 日结必须落库，而且明天读得到 ----
    n_sum = conn.execute("SELECT COUNT(*) AS c FROM daily_summary").fetchone()["c"]
    if n_sum < len(days):
        problems.append(f"daily_summary 表只有 {n_sum} 行，"
                        f"应为 {len(days)} 天各一行 —— 日结没落库")
    for d in days:
        if d.summary is None:
            problems.append(f"Day{d.day} 没有日结结果")
            continue
        # 日结里报的能区必须今天真的有活动记录（summary._repair 已保证，
        # 这里从外面再核一遍，防止那个修复被后人改掉）
        active = {s.activity_id for s in d.obs.sessions
                  if s.outcome != "skipped"}
        for ev in d.summary.domain_updates:
            dom_ok = any(get_act(a) and ev.domain in (get_act(a).domains or [])
                         for a in active)
            if not dom_ok:
                problems.append(f"Day{d.day} 日结报了 {ev.domain} 的方向，"
                                f"但今天没有练过该能区的任何活动")

    if problems:
        print("  ✗ 交叉核对未通过：")
        for x in problems:
            print(f"      {x}")
        raise AssertionError("演示数据自己没对上，不可信")
    print(f"  ✓ {changed_days} 天出现「重算结果与实际取用不一致」"
          f"（检索是纯函数，正常应为 0）")
    print(f"  ★ {fed_back} 天候选因家长反馈被剔除，其中 {cooled_days} 天进了冷却期")
    print("  ✓ 每个冷却都能反查回事件，且行为确实在该活动的 refusal_behaviors 里")
    print("  ✓ recent_scenes 里的场景都能在库里找到")
    print("  ✓ 每条 refused session 都可追溯到一条真实事件")
    print("  ★ 抗拒只记在家长亲手填过的那条活动上，「其他观察」框贡献 0 条")
    print(f"  ★ 日结落库 {n_sum} 行，且报的每个能区今天都真的练过")


# ★ 原先这里有一个 `_hardcoded_outcome()`：把手写的事件塞进 ObservationOutcome，
#   用来在 --no-llm 下演示闭环。它整个删掉了 —— 逐条反馈之后不需要了。
#   原因：它的存在本身就是"家长输入形态不对"的一个症状。
#   它要手写一份 (scene, behavior, antecedent, consequence, intensity, quote) 的
#   六元组表，只为了让 --no-llm 路径能产出事件。而现在家长点的 outcome
#   和写在 note 里的原话**本来就是**这次要处理的输入，--no-llm 下
#   feedback.apply_feedbacks 照样能把 sessions 全部写进去（那部分零 token）。
#   唯一降级的是"从 note 里抽事件"，而 observe._fallback_batch 一条都不编 ——
#   这与原话里写的诚实边界一致：闭环有效，抽取质量未验证。


# ================================================================== 跑一天
def run_day(conn, child_id: str, prof: AbilityProfile, day: DayInput, *,
            n: int, plan_day_ids: list[str], no_llm: bool = False,
            hardcoded: bool = False,
            out_prev: list[ObservationInput] | None = None) -> tuple[DayResult, PlanRequest]:
    c = time.time()
    print(f"\n{'=' * 70}\n第 {n} 天 · {day.date}\n{'=' * 70}")
    # 教练诊断读的是**之前**攒下的观察，不是今天刚说的那段 ——
    # 今天的话是今天情况的报告，它驱动的是**明天**的调整。
    out_prev = []

    # ---------- 1. 今天的卡片（先用当前记忆生成）----------
    # ★ 顺序很重要：先生成今天的卡片，再收今天的反馈。
    # 反过来做（先收反馈再出卡片）会让 Day 1 出现荒谬的一幕 ——
    # 周计划刚建议 gro-007，同一天的反馈立刻把它冷却掉，于是"今天的建议"
    # 当场失效。用户要的是「计划执行 → 反馈 → 更新计划」，
    # 那么反馈影响的是**明天**，今天已经发出去的卡片不该被追溯修改。
    print("\n【1】检索候选 → 今天的卡片")
    req = build_request(conn, child_id, prof, before=day.date)
    cands = retrieve_candidates(req, top_k=12)
    print(f"  记忆切片：refusal_counts={req.refusal_counts or '{}'}  "
          f"recent_scenes={req.recent_scenes or '[]'}  "
          f"target_states={ {k: v for k, v in list(req.target_states.items())[:2]} }")
    print(f"  日结（上次的方向判断）："
          f"{[s.get('date') for s in req.recent_summary] or '[]'}"
          f"  ← 教练 Agent 读得到方向，不只是计数")
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
        return DayResult(day=n, date=day.date, obs=None, cands_in=[],
                         suggested_id=suggested), req
    if no_llm or hardcoded:
        cout = CoachOutcome(card=card_from_activity(
            pick.activity, prof.parent_constraints.preferences),
            activity=pick.activity, degraded=True,
            adjustment="（--no-llm / hardcoded：使用活动定义直接生成的兜底卡片）")
    else:
        cout = coach_today(cands, today_id=pick.activity.activity_id,
                           observations=observe.to_coach_inputs(out_prev),
                           req=req, recent_summary=req.recent_summary)
    if cout.analysis:
        for sg in cout.analysis.signals:
            print(f"  诊断：{sg.kind}｜{sg.interpretation} → {sg.impact}"
                  f"（功能 {sg.function}）")
    if cout.adjustment:
        print(f"  调整：{cout.adjustment}")
    print()
    print("  " + cout.card.render().replace("\n", "\n  "))

    # ---------- 3. 家长逐条填反馈 ----------
    print("\n【3】家长逐条反馈 → sessions + observations")
    print("  家长端今天会看到这几个输入框：")
    for fb in day.feedbacks:
        a = _get(fb.activity_id)
        if a is not None:
            print("\n" + render_feedback_form(a, min(len(a.steps), 6)).replace("\n", "\n  "))
    if day.other_notes:
        print("\n  ── 当天的「其他观察」（选填）──")
        print(f"  {day.other_notes}")

    fbr = apply_feedbacks(conn, child_id, day.date, day.feedbacks,
                          other_notes=day.other_notes,
                          no_llm=no_llm or hardcoded)
    print("\n  " + fbr.render().replace("\n", "\n  "))

    # ---------- 4. 抗拒归属核对（已经并进 feedback.apply_feedbacks）----------
    print("\n【4】抗拒归属核对（★ 本次改动的核心）")
    if fbr.refusal_hits:
        for h in fbr.refusal_hits:
            print(f"  {h.activity_id}  {h.note}")
        print(f"  ★ {len(fbr.refusal_hits)} 条，全部来自家长填过的那条活动")
    else:
        print("  今天没有可归属的抗拒")
    filled = {fb.activity_id for fb in day.feedbacks}
    rc = store.refusal_counts(conn, child_id)
    print(f"  家长今天填了 {sorted(filled)}")
    print(f"  refusal_counts = {rc or '{}'}")
    if day.other_notes:
        print("  ★「其他观察」框（如超市捂耳朵）扣不到任何活动头上 —— 它只进 observations")

    # ---------- 5. 状态转移（纯 Python，零 token） ----------
    print("\n【5】状态机 → 能力状态转移（纯 Python，不调模型）")
    ti = TransitionsInput(date=day.date, sessions=fbr.sessions)
    ts = apply_transitions(ti, store.current_states(conn, child_id), prof,
                           conn=conn, child_id=child_id)
    for t in ts:
        store.append_state(conn, child_id, t)
        print(f"  状态转移：{t.render()}")
    if not ts:
        print("  今天没有转移 —— 升级闸门要求 profile_confidence ≥ 0.5，"
              "降级画像下全关（这正是它该关的时候）")

    # ---------- 6. 日结 Agent ----------
    print("\n【6】日结 Agent → 今天整体在往哪走（每天 1 次 LLM 调用）")
    if hardcoded:
        print("  （--script hardcoded：日结也走降级，只报计数事实，不猜方向）")
    summ = summarize_day(conn, child_id, day.date, sessions=fbr.sessions,
                         no_llm=no_llm or hardcoded)
    print("  " + summ.render().replace("\n", "\n  "))

    # ---------- 7. 明天的候选 ----------
    req2 = build_request(conn, child_id, prof, before=day.date)
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
    return (DayResult(day=n, date=day.date, obs=fbr, hits=fbr.refusal_hits,
                      cands_in=cands, card=cout, transitions=ts,
                      suggested_id=suggested, picked_id=pick.activity.activity_id,
                      cands_tomorrow=cands2, cooled_out=cooled,
                      dropped_other=dropped_other, summary=summ), req2)


def _get(aid: str):
    from activity_data import get as get_act
    return get_act(aid)


# ================================================================== 跑整个循环
def run_loop(child_id: str, *, days: int = 5, no_llm: bool = False,
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
        # 教练诊断读的是**昨天**攒下的观察。今天的话是今天情况的报告，
        # 它驱动的是明天的调整 —— 所以这里攒的是上一次的结果，不是这次的。
        # 逐条反馈下 `obs.outcomes` 是"每条反馈 + 其他观察框"各一个 outcome。
        prev_inputs = []
        for out in (res.obs.outcomes if res.obs else []):
            prev_inputs.extend(observe.to_coach_inputs(out))

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

    print("\n" + "=" * 70)
    print("★ 日结：这三天的方向判断落库了（改动前它们只被打印然后丢掉）")
    print("=" * 70)
    for row in store.latest_summary(conn, child_id, days=30,
                                    before=date.today().isoformat()):
        for ev in row.get("domain_updates", []):
            cn = ABILITY_DOMAIN.get(ev.get("domain", ""), ev.get("domain", ""))
            arrow = {"up": "↑", "flat": "→", "down": "↓"}.get(ev.get("direction", ""), "")
            print(f"  {row.get('date')}  {cn} {arrow}  {ev.get('rationale','')}")
        print(f"  {row.get('date')}  给家长：{row.get('parent_note','')}")
    print("\n  ★ 明天 build_request 会读这些（recent_summary），")
    print("    教练 Agent 第一次能拿到「方向」，而不只是三个数字。")

    if hardcoded and not no_llm:
        print("\n" + "!" * 70)
        print("! --script hardcoded：家长填的 outcome/quality 全部生效（那部分零 token），")
        print("! 但从补充描述里抽事件、以及日结的方向判断都走了降级。")
        print("! 闭环咬合是验证过的；**抽取准确率没有验证**，那需要 live 或 capture。")
        print("!" * 70)
    if not no_llm and not hardcoded:
        print("\n  live 模式耗时提醒：画像 1 次 + 逐条抽取 3 次 + 教练 3×2 次 + 日结 3 次")
        print("  ≈ 13 次 LLM，现场请用 DEMO_MODE=replay。")

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
    ap.add_argument("--days", type=int, default=5,
                    help="演示天数（最多 5，有剧本；默认 5 才能看到冷却）")
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
