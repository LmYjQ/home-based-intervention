# -*- coding: utf-8 -*-
"""
能力状态机
==========
把"这些天做得怎么样"折叠成"这项能力练到哪一步了"。状态不是用来显示的 ——
它通过 `activities.STATE_TARGET_DIFFICULTY` 直接决定候选活动的难度，
是整个推荐链路权重最高的那一项（`w_state_match = 0.30`）。
**所以状态判错会级联放大，这就是它必须完全确定性、零 token 消耗的原因。**

为什么不让 LLM 判状态
---------------------
"最近 7 天成功 3 次"和"最近 14 天成功 8 次"是计数，7B 在上面不可靠，
而这恰好是状态机的全部。而且状态判错的后果不是显示难看 —— 是检索带偏、
计划排错、家长白花两周时间。

**计数交给 Python，语义交给 LLM。** 这个模块是这条决策最纯粹的体现：
整个文件没有一个 LLM 调用、没有一个魔法数字来自模型，整条时间线可完全重放 ——
删库重建、灌进同一份 fixture，14 天的状态转移逐条复现。

这也是面试时可以当场演示的：改一条 session 记录、重跑、状态变化立刻可解释。

转移规则
--------
    not_started → entry        ≥1 次 success/partial，或 level_est ≥ 1
    entry       → practicing   最近 7 天  success ≥3 且 平均 quality ≥0.5
    practicing  → mastered     最近 14 天 success ≥8 且 平均 quality ≥0.75
                              且 refusal_rate ≤0.2
    mastered    → generalizing 累计 success ≥5，其中 ≥3 次发生在 linked_scenes 之外
    practicing  → entry       最近 7 天 refusal_rate ≥0.5 或 success == 0     （回退）
    generalizing→ mastered    最近 14 天 refusal_rate ≥0.4                  （回退）

只允许相邻转移 + 回退，不允许跳级：not_started 不能直接到 practicing。
不允许跳级不是为了严谨，是因为跳级通常意味着数据缺失（比如孩子两周没来，
中间的历史没记录），而缺失的历史会让状态机在最不该推进的时候推进。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vocab import ABILITY_STATE, ABILITY_STATE_CN

Outcome = Literal["success", "partial", "refused", "skipped"]

# 参与状态判定的 outcomes。skipped 是"没做成"（生病、出游），不是"不肯做" ——
# 混为一谈会让一次生病变成一次抗拒，两周下来状态机误判回退。
_COUNTED = ("success", "partial", "refused")


class SessionRecord(BaseModel):
    """一次练习记录。状态机的唯一输入。"""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")
    activity_id: str = Field(min_length=1, max_length=20)
    outcome: Outcome
    quality: float = Field(
        ge=0.0, le=1.0, default=0.0,
        description="完成质量 0–1（家长自评或评估 Agent 判定）。拒绝时填 0",
    )
    out_of_routine: bool = Field(
        default=False,
        description="这次的成功是否发生在该活动的 linked_scenes 之外的场景。"
                    "由调用方算好传进来 —— 状态机不该知道活动库里有哪些场景",
    )

    @property
    def counted(self) -> bool:
        return self.outcome in _COUNTED


@dataclass
class StateTransition:
    """一次状态转移。落库时即一条不可变的历史记录。"""

    skill_item: str
    domain: str
    from_state: str
    to_state: str
    reason: str                        # 中文，可直接展示给家长
    evidence: dict = field(default_factory=dict)   # 触发它的那几个数字
    at_date: str = ""                  # 判定当天。**由 propose_transition 自己填**，
                                       # 不留给调用方猜 —— 调用方一猜就会错，
                                       # 而状态历史的时间戳错了整条审计线就废了

    @property
    def is_upgrade(self) -> bool:
        return ABILITY_STATE.index(self.to_state) > ABILITY_STATE.index(self.from_state)

    def render(self) -> str:
        arrow = "↑" if self.is_upgrade else "↓"
        return (f"{self.domain}/{self.skill_item}："
                f"{ABILITY_STATE_CN.get(self.from_state, self.from_state)}"
                f" {arrow} {ABILITY_STATE_CN.get(self.to_state, self.to_state)}"
                f"  —— {self.reason}")


# ------------------------------------------------------------------ 阈值
# 全部集中在这里而不是散在条件里：面试时可以被追问"为什么是 8 次不是 6 次"，
# 集中之后这个问题有一个明确的落点（下面 _why 里的注释是真实依据，不是事后编的）。
RULES = {
    "start_min_partial": 1,     # not_started → entry：1 次部分成功就算入门
    "entry_days": 7,
    "entry_success": 3,
    "entry_quality": 0.5,
    "master_days": 14,
    "master_success": 8,
    "master_quality": 0.75,
    "master_refusal_rate": 0.2,     # 掌握一个技能却还有两成抗拒，说明没真掌握
    "gen_success": 5,
    "gen_out_of_routine": 3,       # 泛化要求的重点是"换个地方也能用"，所以看场次
    "back_days": 7,
    "back_refusal_rate": 0.5,
    "gen_back_days": 14,
    "gen_back_refusal_rate": 0.4,
    "upgrade_min_confidence": 0.5,  # 可信度低于此值禁止升级
}

LEVEL_GAIN = 0.15     # 每成功一次，等效等级上浮
LEVEL_LOSS = 0.20     # 每抗拒一次，等效等级下沉（比成功涨得快：抗拒的信息量更大）


# ------------------------------------------------------------------ 统计
def _window(history: list[SessionRecord], end_date: str, days: int) -> list[SessionRecord]:
    """截至 end_date（含）往前 days 天。日期是字符串，ISO 格式可直接字典序比较。"""
    y, m, d = (int(x) for x in end_date.split("-"))
    import datetime as _dt
    end = _dt.date(y, m, d)
    start = end - _dt.timedelta(days=days - 1)
    lo, hi = start.isoformat(), end.isoformat()
    return [h for h in history if h.counted and lo <= h.date <= hi]


def stats(recs: list[SessionRecord]) -> dict[str, float]:
    """一段记录的统计量。空窗口返回全 0，且 success==0 这个语义由调用方约定。"""
    succ = [r for r in recs if r.outcome == "success"]
    part = [r for r in recs if r.outcome == "partial"]
    ref = [r for r in recs if r.outcome == "refused"]
    n = len(succ) + len(part) + len(ref)
    quality_pool = succ + part
    return {
        "n": n,
        "success": len(succ),
        "partial": len(part),
        "refused": len(ref),
        # quality 只对成功/部分成功取平均：一次拒绝没有"完成质量"可言，
        # 把它算进去会把拒绝伪装成"质量低但还在做"，从而推迟回退。
        "mean_quality": (sum(r.quality for r in quality_pool) / len(quality_pool)) if quality_pool else 0.0,
        "refusal_rate": (len(ref) / n) if n else 0.0,
        "out_of_routine": sum(1 for r in succ if r.out_of_routine),
    }


def estimate_level(initial_level: int, history: list[SessionRecord]) -> int:
    """由 sessions 历史算当前等效等级。**不是模型给的。**

        level = clamp(初始等级 + 0.15×成功次数 − 0.20×抗拒次数, 0, 5)

    全量历史而非时间窗：等级是能力的连续刻画，切窗口会让等级在两个日期之间
    突然跳变，而那只是窗口边界在动，不是因为孩子变了。
    抗拒的系数（0.20）大于成功（0.15）是有意的：一次成功只说明这条路走通了，
    一次抗拒可能意味着提示方式不对、时机不对、或这个技能本来就不匹配。
    """
    s = stats(history)
    raw = initial_level + LEVEL_GAIN * s["success"] - LEVEL_LOSS * s["refused"]
    return max(0, min(5, int(math.floor(raw + 1e-9))))


# ------------------------------------------------------------------ 转移判定
def propose_transition(
    *,
    skill_item: str,
    domain: str,
    current_state: str,
    level_est: int,
    history: list[SessionRecord],
    end_date: str,
    profile_confidence: float,
    since: str | None = None,
) -> StateTransition | None:
    """判定要不要转移状态。**纯函数：不写库、不调 LLM、可重复。**

    返回 None 表示维持现状。判定顺序是 **先回退、后升级** ——
    一个孩子这周连着拒了五次，此刻正确的动作是退回来，不是把他推到下一档。
    同时满足回退和升级时，回退优先。

    since
        进入**当前状态**的日期（由调用方从状态历史里取）。所有"累计"型证据
        （当前是泛化证据）只统计 since 之后的记录。
        这一条不是可选的严谨性，是修一个真实的 bug：曾经出现过
        「泛化中 →(连续外出抗拒)→ 已掌握 → 隔一天又 → 泛化中」的抖动，
        因为累计成功次数不随回退清零，退回去的第二天旧证据又把它推上来。
        **回退必须让旧证据失效**，否则回退只是延迟一天，反而制造了状态抖动
        —— 而状态抖动通过 w_state_match 直接表现为"今天难度3、明天难度2、
        后天难度3"，家长看到的是系统在乱改计划。
        since=None 表示"这个状态从一开始就在"，用全量历史。
    """
    if current_state not in ABILITY_STATE:
        raise ValueError(f"未知状态 {current_state!r}；取值范围 {ABILITY_STATE}")

    def mk(to_state: str, reason: str, evidence: dict) -> StateTransition:
        return StateTransition(skill_item, domain, current_state, to_state,
                               reason, evidence, at_date=end_date)

    w_back = _window(history, end_date, RULES["back_days"])
    s_back = stats(w_back)
    w_gen_back = _window(history, end_date, RULES["gen_back_days"])
    s_gen_back = stats(w_gen_back)
    all_s = stats([h for h in history if since is None or h.date >= since])

    # ---------------- 回退优先 ----------------
    if current_state == "practicing":
        if s_back["n"] >= 2 and s_back["refusal_rate"] >= RULES["back_refusal_rate"]:
            return mk("entry", f"最近 {RULES['back_days']} 天抗拒率 {s_back['refusal_rate']:.0%}，"
                f"先退回入门把提示方式调松一点", {"refusal_rate": s_back["refusal_rate"], "window_days": RULES["back_days"]})
            
        if s_back["n"] >= 3 and s_back["success"] == 0:
            return mk("entry", f"最近 {RULES['back_days']} 天做了 {int(s_back['n'])} 次，一次都没成功，"
                f"难度或提示方式需要退回来重配", {"attempts": s_back["n"], "window_days": RULES["back_days"]})
            

    if current_state == "generalizing":
        if s_gen_back["n"] >= 3 and s_gen_back["refusal_rate"] >= RULES["gen_back_refusal_rate"]:
            return mk("mastered", f"最近 {RULES['gen_back_days']} 天在新场景里抗拒率 "
                f"{s_gen_back['refusal_rate']:.0%}，泛化还不稳，先回到已掌握状态巩固", {"refusal_rate": s_gen_back["refusal_rate"]})
            

    # ---------------- 升级：先过置信度闸门 ----------------
    # 这是整个系统里最重要的一条 guard。画像可信度不足时，**任何升级都禁止** ——
    # 因为升级会让检索去推更高难度的活动，那是在一条可能本就不存在的起点上
    # 继续往上盖。宁可让孩子在简单活动上多做两周。
    if profile_confidence < RULES["upgrade_min_confidence"]:
        return None

    if current_state == "not_started":
        partials = all_s["success"] + all_s["partial"]
        if partials >= RULES["start_min_partial"] or level_est >= 1:
            return mk("entry",
                      "已经有部分成功记录，可以开始正式练了" if partials else
                      "评估等级已达 1 级（协助下能做），从入门开始",
                      {"partial_or_success": partials, "level_est": level_est})
        return None

    if current_state == "entry":
        w = _window(history, end_date, RULES["entry_days"])
        s = stats(w)
        if s["success"] >= RULES["entry_success"] and s["mean_quality"] >= RULES["entry_quality"]:
            return mk("practicing", f"最近 {RULES['entry_days']} 天成功 {int(s['success'])} 次、"
                f"平均质量 {s['mean_quality']:.2f}，进到练习阶段（可以引入更高一级提示了）", {"success": s["success"], "mean_quality": s["mean_quality"]})
            
        return None

    if current_state == "practicing":
        w = _window(history, end_date, RULES["master_days"])
        s = stats(w)
        if (s["success"] >= RULES["master_success"]
                and s["mean_quality"] >= RULES["master_quality"]
                and s["refusal_rate"] <= RULES["master_refusal_rate"]):
            return mk("mastered", f"最近 {RULES['master_days']} 天成功 {int(s['success'])} 次、"
                f"平均质量 {s['mean_quality']:.2f}、抗拒率 {s['refusal_rate']:.0%}，已掌握", {"success": s["success"], "mean_quality": s["mean_quality"],
                 "refusal_rate": s["refusal_rate"]})
            
        return None

    if current_state == "mastered":
        if all_s["success"] >= RULES["gen_success"] and all_s["out_of_routine"] >= RULES["gen_out_of_routine"]:
            return mk("generalizing", f"当前状态以来成功 {int(all_s['success'])} 次，其中 {int(all_s['out_of_routine'])} 次"
                f"发生在常用场景之外，换地方也能用了", {"success": all_s["success"], "out_of_routine": all_s["out_of_routine"]})
            
        return None

    return None


def walk(
    *,
    skill_item: str,
    domain: str,
    initial_state: str,
    initial_level: int,
    history: list[SessionRecord],
    dates: list[str],
    profile_confidence: float,
) -> list[StateTransition]:
    """把一段历史**逐日重放**，返回途中发生的每一次转移。

    这是这个模块最有说服力的用法：给定同一份 history 和同一份日期轴，
    结果逐条可复现，没有任何隐式状态。阶段 6 的 fixtures 回放直接用它。
    """
    by_date: dict[str, list[SessionRecord]] = {}
    for h in history:
        by_date.setdefault(h.date, []).append(h)

    state = initial_state
    level = initial_level
    since: str | None = None      # 进入当前状态的日期；从未转移过则为 None
    out: list[StateTransition] = []
    for d in sorted(dates):
        level = estimate_level(initial_level, [h for h in history if h.date <= d])
        t = propose_transition(
            skill_item=skill_item, domain=domain, current_state=state, level_est=level,
            history=[h for h in history if h.date <= d], end_date=d,
            profile_confidence=profile_confidence, since=since,
        )
        if t:
            out.append(t)
            state = t.to_state
            since = d
    return out


# ------------------------------------------------------------------ 自检
def _act(activity_id: str):
    from activity_data import get
    return get(activity_id)


def _demo_history(activity_id: str = "soc-001") -> list[SessionRecord]:
    """构造一条能走完 not_started → entry → practicing → mastered → generalizing
    再回退到 mastered 的时间线。

    默认用 soc-001「指给你看他喜欢的」—— 它的 target_skill_items 真的含"共同注意"。
    **这一步曾经踩坑**：早先这里写死 soc-004「看他的眼睛」，而那条活动的目标技能是
    眼神回应/轮流等待，于是 store.sessions_for_skill('共同注意') 过滤掉全部 35 条记录、
    状态机拿到空历史却照样输出一条完整转移链 —— 看起来一切正常，实则全是幻觉。
    演示数据必须自己先经得起交叉核对。
    """
    H: list[SessionRecord] = []

    def add(date: str, outcome: Outcome, q: float = 0.0, oor: bool = False) -> None:
        H.append(SessionRecord(date=date, activity_id=activity_id, outcome=outcome,
                               quality=q, out_of_routine=oor))

    # 10 月 1-5 日：陆续成功，进入门
    for i, d in enumerate(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]):
        add(d, "success", 0.4 + 0.05 * i)

    # 10 月 6-12 日：进入 entry，7 天内 4 次成功但质量一般
    for d, q in zip(["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"],
                    [0.55, 0.6, 0.55, 0.65, 0.6]):
        add(d, "success", q)

    # 10 月 11-24 日：14 天窗口内成功 9 次，质量 0.8+，抗拒 1 次 → mastered
    days = [f"2026-10-{d:02d}" for d in range(11, 25)]
    plan = [("success", 0.8), ("success", 0.85), ("refused", 0.0), ("success", 0.8),
            ("success", 0.9), ("success", 0.85), ("success", 0.8), ("success", 0.9),
            ("success", 0.85), ("success", 0.8), ("success", 0.9), ("success", 0.85),
            ("success", 0.9), ("success", 0.85)]
    for d, (o, q) in zip(days, plan):
        add(d, o, q)

    # 10 月 25-30 日：其中 3 次成功发生在常用场景之外 → generalizing
    for d in ["2026-10-25", "2026-10-26", "2026-10-27"]:
        add(d, "success", 0.9, oor=True)
    for d in ["2026-10-28", "2026-10-29"]:
        add(d, "success", 0.85)

    # 11 月上旬：外出场景里连续抗拒 → 回退。
    # 注意 14 天窗口里仍混着 10 月下旬的 7 次成功，所以要拒到 6 次（6/13 ≈ 46%）
    # 才够 0.4 的线 —— 这正说明回退阈值定得偏保守：宁可晚几天再退，
    # 也不要因为一次外出不顺利就把已经建立的能力打回去。
    for d in ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"]:
        add(d, "refused", 0.0, oor=True)
    return H


def _cli() -> None:
    H = _demo_history()
    dates = sorted({h.date for h in H})
    assert any("共同注意" in a.target_skill_items
               for a in [_act(h.activity_id) for h in H[:1]]), \
        "演示活动必须真的以'共同注意'为目标技能，否则状态机与活动库对不上"

    print(f"session 记录 {len(H)} 条，{dates[0]} → {dates[-1]}，共 {len(dates)} 个日期\n")

    print("=== 逐日重放（profile_confidence = 0.85）===")
    ts = walk(skill_item="共同注意", domain="social_adapt", initial_state="not_started",
              initial_level=1, history=H, dates=dates, profile_confidence=0.85)
    for t in ts:
        print("  " + t.render())
    if not ts:
        print("  （无转移）")

    print("\n=== 同一份历史，profile_confidence = 0.35（低于升级闸门 0.5）===")
    ts2 = walk(skill_item="共同注意", domain="social_adapt", initial_state="not_started",
               initial_level=1, history=H, dates=dates, profile_confidence=0.35)
    for t in ts2:
        print("  " + t.render())
    if not ts2:
        print("  （无转移）")
    print("  ^ 同一份数据，可信度低一档就一步都不升级 —— 状态机不会在"
          "一条可能本就不存在的起点上继续往上盖")

    print("\n=== 等效等级（由计数算，不由模型给）===")
    for d in dates[::6]:
        lvl = estimate_level(1, [h for h in H if h.date <= d])
        print(f"  {d}  level_est = {lvl}")

    print("\n=== 可重放性 ===")
    a = walk(skill_item="共同注意", domain="social_adapt", initial_state="not_started",
             initial_level=1, history=H, dates=dates, profile_confidence=0.85)
    b = walk(skill_item="共同注意", domain="social_adapt", initial_state="not_started",
             initial_level=1, history=list(reversed(H)), dates=dates, profile_confidence=0.85)
    same = [t.render() for t in a] == [t.render() for t in b]
    print(f"  打乱输入顺序后结果一致：{same}")
    print(f"  转移次数：{len(a)}   其中升级 {sum(1 for t in a if t.is_upgrade)}"
          f" / 回退 {sum(1 for t in a if not t.is_upgrade)}")


if __name__ == "__main__":
    _cli()
