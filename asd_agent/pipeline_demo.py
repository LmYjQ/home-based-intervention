# -*- coding: utf-8 -*-
"""
端到端 demo CLI
===============
    python pipeline_demo.py                 # 完整链路（含 LLM）
    python pipeline_demo.py --no-llm        # 全离线，确定性，<1 秒
    python pipeline_demo.py --child demo-001 --days 7 --trace

链路：
    评估报告 → 能力画像 → 状态机 → 一周计划 → 今天的家长卡片
             → 家长记录 → 日评估 → 状态转移 → 周回顾

`--no-llm` 不是玩具模式：它让整条链路可以在**零网络**下确定性重放，
所以它同时是阶段 6 的回放基线和现场断网的保险。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm
import store
from activities import PlanRequest, candidates as retrieve_candidates
from capabilities import ParentConstraints
from coach import CoachOutcome, ObservationInput, card_from_activity, coach_today
from evaluator import (ActivityEval, DailyEval, EvalActivityInput, EvalInput,
                       apply_transitions, evaluate_day)
from planner import WeeklyPlan, plan_week
from profile import SAMPLE_REPORT, ProfileExtraction, extract_profile
from vocab import ABILITY_STATE, ABILITY_DOMAIN

DEMO_REPORT = SAMPLE_REPORT


class Console:
    """极简的分段计时输出。阶段 4 的 Agent Trace 面板是它的 UI 版。"""

    def __init__(self, trace: bool = False):
        self.trace = trace
        self.t0 = time.time()
        self.n = 0

    def step(self, title: str, fn):
        self.n += 1
        head = f"\n{'=' * 66}\n[{self.n}] {title}"
        print(head)
        t = time.time()
        out = fn()
        dt = (time.time() - t) * 1000
        print(f"    —— {dt:.0f}ms" + (f"   累计 {time.time() - self.t0:.1f}s"
                                     if self.trace else ""))
        return out

    def note(self, text: str) -> None:
        print(f"    {text}")


def build_plan_request(conn, child_id: str, profile) -> PlanRequest:
    """把画像 + 状态机 + 记忆库拼成检索请求。**这是整个系统的接缝。**"""
    pc = profile.parent_constraints
    states = store.current_states(conn, child_id)
    # 能区的当前状态取该能区下任一细项的状态（都用 mastered 的例子）
    by_domain: dict[str, str] = {}
    for a in profile.assessments:
        st = states.get(a.skill_item, {}).get("state", "not_started")
        # 取该能区里"最靠前"的状态：能区的目标难度取决于最需要练的那一项
        if a.domain not in by_domain or _STATE_ORDER[st] > _STATE_ORDER[by_domain[a.domain]]:
            by_domain[a.domain] = st
    return PlanRequest(
        levels=profile.levels(),
        target_states=by_domain,
        priority_domains=profile.domains_of_interest(),
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


_STATE_ORDER = {s: i for i, s in enumerate(ABILITY_STATE)}


PARENT_TODAY = [
    ObservationInput(
        obs_id="o1", date="", scene="indoor_play", behavior="refusal",
        raw="今天拿出小汽车他就不高兴，扔到地上，让我自己玩。最后我自己玩了十分钟他才过来。"),
    ObservationInput(
        obs_id="o2", date="", scene="mealtime", behavior="joint_attention",
        raw="吃饭的时候他突然指着窗户叫了一声，我看他指的方向是鸟，我说是鸟呀，他就笑了。"),
]
PARENT_SELF_REPORT = "今天有点累，但他整体还算配合。中间有一次不高兴，很快就过去了。"


def run(child_id: str = "demo-child", *, days: int = 7, trace: bool = False,
        no_llm: bool = False, db_path: str | None = None) -> int:
    c = Console(trace)

    if no_llm:
        llm.configure(mode="live")      # 不改模式，改为在下面跳过所有 LLM 调用

    db = db_path or f"/tmp/pipeline_{child_id}.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    conn = store.connect(db)

    # ---------------- 1. 画像 ----------------
    def _profile():
        if no_llm:
            c.note("（--no-llm：使用内置降级画像，可信度 0.1，状态机将冻结升级）")
            from profile import _fallback_profile
            prof = _fallback_profile(child_id, 34, ParentConstraints(
                daily_minutes_available=30,
                materials_at_home=["杯子", "绘本", "小汽车", "积木"],
                avoid_load_sensory=True))
            return ProfileExtraction(profile=prof, redflags=__import__(
                "redflag").scan(DEMO_REPORT), degraded=True,
                note="--no-llm 模式")
        return extract_profile(DEMO_REPORT, child_id, 34,
                               parent_constraints=ParentConstraints(
                                   daily_minutes_available=30,
                                   materials_at_home=["杯子", "绘本", "小汽车", "积木"],
                                   avoid_load_sensory=True))

    ex = c.step("评估报告 → 能力画像（1 次 LLM）", _profile)
    prof = ex.profile
    if ex.redflags.triggered:
        print("    ★ 红旗：")
        for h in ex.redflags.hits:
            print(f"        [{h.severity}] {h.group} —— {h.quote}")
    print(prof.render())
    c.note(f"可信度 {prof.overall_confidence:.2f} · 降级 {ex.degraded}")
    if ex.unverified:
        c.note(f"引文未通过原文核对（已降级为推断）：{'、'.join(ex.unverified)}")

    store.ensure_child(conn, child_id, name="小宇", age_months=34)
    store.save_profile(conn, prof)

    # ---------------- 2. 检索候选 ----------------
    req = build_plan_request(conn, child_id, prof)
    cands = c.step("状态机 + 记忆 → 检索候选（纯 Python）", lambda: retrieve_candidates(req, top_k=12))
    from activities import explain_retrieval
    print(f"    {len(cands)} 条通过硬过滤，前 3 条：")
    for s in cands[:3]:
        print(f"      {s.score:.3f} {s.activity.brief()}")
        print(f"            {explain_retrieval(req, s)}")
    c.note(f"每日预算 {req.daily_minutes} 分钟 · 目标状态 {req.target_states}")

    # ---------------- 3. 一周计划 ----------------
    plan = c.step("规划 Agent → 一周计划（1 次 LLM）",
                  lambda: plan_week(prof, req, plan_id=f"plan-{child_id}",
                                    days=days, today=date.today().isoformat())
                  if not no_llm else __import__("planner").greedy_fallback_plan(
                      req, f"plan-{child_id}", days=days,
                      today=date.today().isoformat()))
    print(plan.render())
    c.note(f"degraded={plan.degraded}"
           + (f" · 剔除 {plan.excluded_activity_ids}" if plan.excluded_activity_ids else ""))
    store.save_plan(conn, child_id, date.today().isoformat(), plan)

    # ---------------- 4. 今天的卡片 ----------------
    today_block = plan.days[0]
    first = cands[0].activity.activity_id if cands else None
    pick = next((s for s in cands
                 if s.activity.activity_id in {a.activity_id for a in today_block.activities}),
                cands[0] if cands else None)

    def _coach():
        if no_llm:
            c.note("（--no-llm：使用活动定义直接生成的兜底卡片）")
            return CoachOutcome(card=card_from_activity(pick.activity, prof.parent_constraints.preferences),
                                activity=pick.activity, degraded=True)
        return coach_today(cands, today_id=pick.activity.activity_id,
                           observations=PARENT_TODAY, req=req)

    out = c.step("教练 Agent → 家长卡片（2 次 LLM）", _coach)
    if out.analysis:
        print("    诊断：")
        for s in out.analysis.signals:
            print(f"      · {s.kind}｜{s.interpretation} → {s.impact}")
        if out.analysis.free_reasoning:
            c.note(f"（模型自述：{out.analysis.free_reasoning[:60]}…）")
    if out.adjustment:
        c.note(f"调整：{out.adjustment}")
    if out.unverified_quotes:
        c.note(f"引文未通过核对已作废：{out.unverified_quotes}")
    print()
    print("    " + out.card.render().replace("\n", "\n    "))
    c.note(f"degraded={out.degraded}  trace={out.render_trace()}")

    # ---------------- 5. 日评估 ----------------
    today = date.today().isoformat()
    planned_ids = [a.activity_id for a in today_block.activities]

    def _eval():
        from activity_data import get as get_act
        acts = []
        for aid in planned_ids:
            a = get_act(aid)
            if a is None:
                continue
            acts.append(EvalActivityInput(
                activity_id=a.activity_id, title=a.title,
                success_criteria=a.success_criteria,
                refusal_behaviors=a.refusal_behaviors,
                materials=[m.name for m in a.materials]))
        if no_llm:
            c.note("（--no-llm：使用简化评估，只记事实不猜方向）")
            from evaluator import _fallback_eval
            inp = EvalInput(date=today, activities=acts,
                            observations=[o.raw for o in PARENT_TODAY],
                            parent_self_report=PARENT_SELF_REPORT)
            from evaluator import EvalOutcome, to_session_records
            fb = _fallback_eval(inp)
            return inp, EvalOutcome(eval=fb, degraded=True,
                                    sessions=to_session_records(today, fb.per_activity))
        inp = EvalInput(date=today, activities=acts,
                        observations=[o.raw for o in PARENT_TODAY],
                        parent_self_report=PARENT_SELF_REPORT,
                        prior_states=req.target_states)
        return inp, evaluate_day(inp)

    inp, ev = c.step("评估 Agent → 日评估（1 次 LLM）", _eval)
    print("    " + ev.render().replace("\n", "\n    "))
    c.note(f"degraded={ev.degraded}")

    # ---------------- 6. 状态转移（Python 落地）----------------
    def _transit():
        # --no-llm 时阶段 5 不产出任何 session（降级模式拒绝编造结果），
        # 于是状态机面前是空历史、必然零转移，后面两阶段就没东西可展示。
        # 这里的做法是**灌入真实的练习历史**，而不是让降级模式假装"今天成功了"——
        # 让状态机在真实记录上给出结论，比伪造一次成功有意义得多。
        if no_llm:
            import statemachine as sm
            hist = sm._demo_history()
            for h in hist:
                store.log_session(conn, child_id, h)
            c.note(f"（--no-llm：灌入 {len(hist)} 条真实练习历史，"
                   f"让状态机在真数据上运行）")

        store_states = store.current_states(conn, child_id)
        ts = apply_transitions(inp, ev, store_states, prof, conn=conn, child_id=child_id)
        for a in ev.sessions:
            store.log_session(conn, child_id, a)
        for t in ts:
            store.append_state(conn, child_id, t)
        return ts

    ts = c.step("状态机 → 转移判定（纯 Python，零 token）", _transit)
    if ts:
        for t in ts:
            print("    " + t.render())
    else:
        from statemachine import RULES
        if prof.overall_confidence < RULES["upgrade_min_confidence"]:
            c.note(f"没有转移：画像可信度 {prof.overall_confidence:.2f} 低于升级闸门 "
                   f"{RULES['upgrade_min_confidence']}，全部升级被拦下")
            c.note("（降级路径的画像刻意把闸门关死 —— 数据不够时不动，比瞎动安全）")
        else:
            c.note("没有转移：状态机的规则要连续多天的记录才动得了一下子，今天一条不够")

    # ---------------- 7. 周回顾 ----------------
    c.step("记忆库 → 周回顾（纯 SQL）", lambda: (
        print("    " + store.what_last_week(conn, child_id, today).render()),
        print("    " + store.what_progressed(
            conn, child_id, 30, profile_confidence=prof.overall_confidence).render()),
    )[0])

    print(f"\n{'=' * 66}\n全链路用时 {time.time() - c.t0:.1f}s")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", default="demo-child")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--trace", action="store_true", help="打印每步累计耗时与 LLM 轨迹")
    ap.add_argument("--no-llm", action="store_true", help="全离线，确定性，不调模型")
    args = ap.parse_args()
    return run(args.child, days=args.days, trace=args.trace, no_llm=args.no_llm)


if __name__ == "__main__":
    sys.exit(main())
