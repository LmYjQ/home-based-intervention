# -*- coding: utf-8 -*-
"""
评估 Agent
==========
把"今天做了什么 + 家长观察到了什么"折成**当天的结论**，并给出状态转移的**证据**。

★ 最重要的边界：状态转移不由这个 Agent 决定
----------------------------------------------
`DomainEvidence` 只产出「方向 + 幅度 + 理由 + 证据 id」，
**不产出"所以应该升级到 mastered"**。真正的转移由 `statemachine.propose_transition()`
用计数规则落地。

为什么非要这样切
----------------
`direction: up / flat / down` 是**语义判断**（"今天这次成功算不算泛化"），
7B 做得还行；而"该不该升级"是**计数判断**（最近 14 天成功 8 次、抗拒率 ≤0.2），
7B 不可靠。所以把前者交给模型、后者交给 Python。
两者的接口就是这个类：模型给证据，规则做决定。

这条边界是这个 demo 里最值得讲的一条工程原则，
它同时用在了教练的 impact 落地、planner 的时长校验上。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import llm
from activities import Activity
from vocab import ABILITY_DOMAIN

Domain = Literal[tuple(ABILITY_DOMAIN)]  # type: ignore[valid-type]
Outcome = Literal["success", "partial", "refused", "skipped", "no_data"]


# ================================================================== 输出模型
class ActivityEval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str = Field(description="必须是今天计划里的 activity_id")
    outcome: Outcome
    success_criteria_met: list[str] = Field(
        default_factory=list, max_length=3, description="达成了哪几条成功判据")
    refusal_behavior: str | None = Field(
        default=None, max_length=40,
        description="他是怎么不配合的，一句家长能看懂的话；没抗拒就填 null",
    )
    quality: float = Field(
        ge=0.0, le=1.0,
        description="完成质量 0–1。**这是会进状态机算式的数字，别给 0 或 1 敷衍了事**："
                    "抗拒时给 0，成功时按『做到了几步、要不要提示、提示到第几级』给分",
    )
    evidence_obs_ids: list[str] = Field(
        default_factory=list, max_length=6, description="依据哪几条观察记录")
    note: str = Field(default="", max_length=100)


class DomainEvidence(BaseModel):
    """★ 只给证据和建议，判定留给 Python。

    magnitude 用"none/small/medium/large"这种**相对**描述而不是分数，
    因为分数会被下游误当成可以累加的量；"变化很大"到底多���，
    由 Python 按这个技能的样本量去解释。
    """

    model_config = ConfigDict(extra="forbid")

    domain: Domain
    direction: Literal["up", "flat", "down"]
    magnitude: Literal["none", "small", "medium", "large"]
    rationale: str = Field(max_length=60, description="≤60字，为什么是这个方向")
    evidence_obs_ids: list[str] = Field(default_factory=list, max_length=6)


class DailyEval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    per_activity: list[ActivityEval] = Field(min_length=1, max_length=6)
    domain_updates: list[DomainEvidence] = Field(default_factory=list, max_length=5)
    flags: list[str] = Field(
        default_factory=list, max_length=5,
        description="需要家长注意的异常，一句话一条（如连续三天都拒绝同一个材料）",
    )
    parent_note: str = Field(default="", max_length=120, description="给家长的一句话总结")


class EvalOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: DailyEval | None = None
    sessions: list = Field(default_factory=list)   # 已落库的 SessionRecord
    degraded: bool = False
    traces: list[llm.LLMTrace] = Field(default_factory=list)
    redflag_triggered: bool = False

    def render(self) -> str:
        if self.eval is None:
            return "（评估降级，没有结论）"
        e = self.eval
        L = [f"【{e.date} 的评估】"]
        for a in e.per_activity:
            L.append(f"  {a.activity_id} → {a.outcome}（质量 {a.quality:.1f}）"
                     + (f"  {a.note}" if a.note else ""))
            if a.success_criteria_met:
                L.append(f"      达成：{'、'.join(a.success_criteria_met)}")
            if a.refusal_behavior:
                L.append(f"      抗拒方式：{a.refusal_behavior}")
        for d in e.domain_updates:
            L.append(f"  {ABILITY_DOMAIN[d.domain]}：{d.direction}/{d.magnitude}"
                     f" —— {d.rationale}")
        for f in e.flags:
            L.append(f"  ⚠ {f}")
        if e.parent_note:
            L.append(f"  给家长：{e.parent_note}")
        return "\n".join(L)


# ================================================================== 输入
class EvalActivityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str
    title: str
    success_criteria: list[str]
    refusal_behaviors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(
        default_factory=list,
        description="这条活动要用的材料。降级模式靠它把家长话里的东西对上活动 —— "
                    "因为家长说的是「小汽车」，不会说活动标题")


class EvalInput(BaseModel):
    date: str
    activities: list[EvalActivityInput]
    observations: list[str] = Field(default_factory=list)   # 家长原话
    parent_self_report: str = ""                            # 家长原话："今天他不太配合"
    prior_states: dict[str, str] = Field(default_factory=dict)
    history_summary: str = ""


# ================================================================== prompt
SYSTEM = """你是一名干预记录的分析员。你在看家长今天做了什么，需要给出当天的记录结论。

【quality 是会被算进公式的数字，不是随手填的】
后面系统会用它算这项能力的进展程度：成功一次加一点，抗拒一次减得更多。
所以请按实际情况给：
- 完全独立完成，不需要任何提示        → 0.8 - 1.0
- 做出来了，但要提示一两次             → 0.6 - 0.8
- 在持续提示下勉强完成                 → 0.3 - 0.6
- 只完成了一部分                     → 0.2 - 0.4
- 拒绝、没做                         → 0.0
**不要给中间值 0.5 敷衍。**

【outcome 怎么选】
- success：达成了成功判据
- partial：做了一部分
- refused：明确不配合
- skipped：没做成（生病、出门、太晚了）—— **这和 refused 不同，不要混**
- no_data：完全没有信息。宁可给 no_data，也不要编一个

【direction 是你的判断，但强度不是】
up / flat / down 由你判断（这是语义判断）；magnitude 用相对说法（none/small/medium/large），
不要给分数 —— 分数会被误当成可以累加的量。

【flags 宁缺毋滥】
只有真正需要家长注意的异常才写。比如"连续三天拒绝同一个材料""连续三天没有任何记录"。
"今天做了练习"这种不是 flag。最多 5 条。"""


def _user(inp: EvalInput) -> str:
    lines = [f"【日期】{inp.date}", "", "【今天安排了什么】"]
    for a in inp.activities:
        lines.append(f"  {a.activity_id} {a.title}")
        lines.append(f"      成功判据：{'、'.join(a.success_criteria)}")
        if a.refusal_behaviors:
            lines.append(f"      可能的抗拒表现：{'、'.join(a.refusal_behaviors)}")
    lines += ["", "【家长的观察记录（原话）】"]
    if inp.observations:
        for i, o in enumerate(inp.observations, 1):
            lines.append(f"  {i}. {o}")
    else:
        lines.append("  （没有）")
    lines += ["", f"【家长自己的说法】{inp.parent_self_report or '（没有）'}"]
    if inp.prior_states:
        st = "、".join(f"{ABILITY_DOMAIN.get(k, k)}={v}"
                       for k, v in inp.prior_states.items())
        lines += ["", f"【这些能区当前的状态】{st}"]
    if inp.history_summary:
        lines += ["", f"【最近的历史】{inp.history_summary}"]
    return "\n".join(lines)


def to_session_records(date: str, per_activity: list[ActivityEval]) -> list:
    """ActivityEval → SessionRecord。**两套 outcome 词表故意不一样，这里是接缝。**

    `ActivityEval.outcome` 有 5 个取值，`SessionRecord.outcome` 只有 4 个 ——
    差的那个是 `no_data`。这不是不一致，是刻意的：

    **"没有信息"不是一个观察结果，它是没有观察。**
    写成一条 session 等于凭空造了一次练习记录，而 sessions 表是状态机唯一的输入 ——
    一条被记成"做了但没成功"的假记录会实实在在扣掉一分能力。
    所以 no_data 直接不落库：没信息就是没记录。

    早先两个地方各自转换，其中一处把 no_data 也写进去了，
    Pydantic 当场拒绝 —— 这个词表差异就是那时候暴露的。
    现在收敛到这个函数里，调用方不会再踩。
    """
    import statemachine as sm
    return [sm.SessionRecord(date=date, activity_id=a.activity_id,
                             outcome=a.outcome, quality=a.quality)
            for a in per_activity if a.outcome != "no_data"]


# ================================================================== 后置校验
# 这里**没有** outcome 的别名归一化。曾经写过一张 {"成功": "success"} 的表，
# 但 outcome 是 Literal，服务端约束解码已经保证只会吐出那 5 个 code，
# 而一旦模型真吐出 "成功"，Pydantic 会在构造对象时就拒掉 —— 根本走不到归一化那步。
# 留着它是"看起来在兜底，实际永远不会执行"的假兜底，比没有更糟。


def _repair(inp: EvalInput, draft: DailyEval) -> tuple[DailyEval, list[str]]:
    """三处校验。

    ★ 最要紧的是第一条：**不在这份计划里的 activity_id 一律丢弃**。
    否则一个幻觉出来的 id 会去写 sessions 表，而状态机会把它算进
    某个技能的进展里 —— 一条凭空捏造的记录就这样变成了"证据"。
    """
    planned = {a.activity_id: a for a in inp.activities}
    dropped: list[str] = []
    kept: list[ActivityEval] = []
    for a in draft.per_activity:
        if a.activity_id not in planned:
            dropped.append(a.activity_id)
            continue
        kept.append(a)

    # 今天排了几条就必须有几条结论。少了的按 no_data 补上 ——
    # **默认"没信息"而不是默认"成功"**：默认成功会凭空造出进展。
    for aid, spec in planned.items():
        if not any(k.activity_id == aid for k in kept):
            kept.append(ActivityEval(activity_id=aid, outcome="no_data", quality=0.0,
                                     note="家长没有提到这条，系统不替它下结论"))

    # quality 与 outcome 自洽：抗拒/跳过的 quality 必须是 0
    for a in kept:
        if a.outcome in ("refused", "skipped", "no_data") and a.quality > 0:
            a.quality = 0.0

    # 同一能区只保留第一条：模型常常对同一个能区连报两条方向不同的证据，
    # 而下游只会取第一条，留着等于埋一个"看起来有矛盾"的雷。
    seen: set[str] = set()
    doms: list[DomainEvidence] = []
    for d in draft.domain_updates:
        if d.domain in seen:
            continue
        seen.add(d.domain)
        doms.append(d)

    return (draft.model_copy(update={
        "per_activity": kept[:len(planned)],
        "domain_updates": doms[:5],
    }), dropped)


# ================================================================== 降级
def _fallback_eval(inp: EvalInput) -> DailyEval:
    """纯 Python 兜底：**有记录才算成功，没有任何判断能力。**

    不猜 direction、不猜 flags —— 猜错了会污染状态机的输入。
    只把家长确实提到的活动按关键词对上，其余 no_data。
    """
    corpus = "\n".join(inp.observations) + "\n" + inp.parent_self_report
    per: list[ActivityEval] = []
    for a in inp.activities:
        hit = a.title in corpus or a.activity_id in corpus
        per.append(ActivityEval(
            activity_id=a.activity_id,
            outcome="success" if hit else "no_data",
            quality=0.6 if hit else 0.0,
            note="降级模式：只按家长是否提到这条来记，不做判断"))
    return DailyEval(date=inp.date, per_activity=per,
                     parent_note="今天是简化模式，只记下了你提到的活动。")


# ================================================================== 主入口
def evaluate_day(inp: EvalInput, *, on_step=None) -> EvalOutcome:
    """评估当天。**一次 LLM 调用 + 后置校验；失败则给一份"只记事实"的评估。**"""
    import statemachine as sm

    try:
        draft, trace = llm.chat_json(
            system=SYSTEM, user=_user(inp), model_cls=DailyEval,
            name="daily_eval", stage="evaluator_day",
            max_tokens=2500, temperature=0.0,
            timeout_s=llm.TIMEOUT_S * 2, on_step=on_step,
        )
        fixed, dropped = _repair(inp, draft)
        # 落成 SessionRecord —— 状态机唯一的输入（no_data 不落库，见 to_session_records）
        return EvalOutcome(eval=fixed, traces=[trace],
                           sessions=to_session_records(inp.date, fixed.per_activity))
    except llm.LLMError:
        fb = _fallback_eval(inp)
        return EvalOutcome(eval=fb, degraded=True,
                           sessions=to_session_records(inp.date, fb.per_activity))


# ================================================================== 状态转移落地
def apply_transitions(inp: EvalInput, ev: EvalOutcome, states: dict[str, dict],
                      profile, *, conn=None, child_id: str = "") -> list:
    """**把评估结果接到状态机上。转移由 Python 规则决定，不由模型决定。**

    每个技能细项只看**真正练到它**的那些 session。传 conn 时还会带上该细项的
    历史记录（不只是今天这几条）—— 状态机的规则全是时间窗统计
    （最近 7 天 / 14 天），只喂今天一条的话永远不会转移。

    ★ 这里曾经把当天的全部 session 无差别喂给每个技能，
    于是练语言得到的成功会算进"共同注意"的进展里 ——
    状态机看起来一直在正常推进，实际上是在拿别人的成绩往上爬。
    """
    import statemachine as sm

    try:
        from activity_data import get as get_activity
    except Exception:
        get_activity = lambda aid: None     # noqa: E731

    out: list = []
    for item in profile.assessments:
        # 只保留目标技能命中这条技能细项的 session
        todays = []
        for s in ev.sessions:
            a = get_activity(s.activity_id)
            if a is None or item.skill_item in a.target_skill_items:
                todays.append(s)

        if conn is not None and child_id:
            import store
            hist = store.sessions_for_skill(conn, child_id, item.skill_item,
                                            until=inp.date)
        else:
            hist = todays

        cur = states.get(item.skill_item, {}).get("state", "not_started")
        since = states.get(item.skill_item, {}).get("since")
        t = sm.propose_transition(
            skill_item=item.skill_item, domain=item.domain,
            current_state=cur,
            level_est=sm.estimate_level(item.level, hist),
            history=hist, end_date=inp.date,
            profile_confidence=profile.overall_confidence, since=since,
        )
        if t:
            out.append(t)
    return out


# ================================================================== 自检
def _cli() -> None:
    import statemachine as sm  # noqa: F401
    inp = EvalInput(
        date="2026-10-17",
        activities=[
            EvalActivityInput(activity_id="soc-001", title="指给你看他喜欢的",
                              success_criteria=["他主动指给你看"],
                              refusal_behaviors=["refusal"]),
            EvalActivityInput(activity_id="gro-007", title="一二三木头人",
                              success_criteria=["连续5轮听到口令能停"],
                              refusal_behaviors=["transition_stuck"]),
        ],
        observations=["今天他看到小汽车会自己跑过来指给我看，一共三次。",
                      "玩木头人的时候他不肯停下来，一直跑。"],
        parent_self_report="中间有点不耐烦，但整体还行。",
        prior_states={"social_adapt": "practicing"},
    )

    print("=== 校验：喂一个含幻觉 id、量纲矛盾、漏条的草稿 ===")
    bad = DailyEval(
        date="2026-10-17",
        per_activity=[
            ActivityEval(activity_id="soc-001", outcome="success",
                        quality=0.9, success_criteria_met=["主动指给你看"]),
            ActivityEval(activity_id="zzz-999", outcome="refused", quality=0.8),   # 幻觉 id + 抗拒却给高分
        ],
        domain_updates=[
            DomainEvidence(domain="social_adapt", direction="up", magnitude="medium",
                           rationale="他开始主动指给你看了"),
            DomainEvidence(domain="social_adapt", direction="up", magnitude="large",
                           rationale="重复的能区"),
        ],
        flags=["今天做了练习"],
        parent_note="",
    )
    fixed, dropped = _repair(inp, bad)
    print(f"  被丢弃的幻觉 id：{dropped}")
    for a in fixed.per_activity:
        print(f"  {a.activity_id} → {a.outcome:8s} quality={a.quality}"
              + ("  ← 抗拒却给了 0.8，已钳到 0" if a.activity_id == "zzz-999" else ""))
    print(f"  漏掉的 gro-007："
          f"{next(a.outcome for a in fixed.per_activity if a.activity_id == 'gro-007')}"
          f"  ← 默认 no_data 而不是默认成功")
    print(f"  重复能区去重后剩 {[d.domain for d in fixed.domain_updates]}")

    print("\n=== 降级路径（无 LLM）===")
    fb = _fallback_eval(inp)
    for a in fb.per_activity:
        print(f"  {a.activity_id} → {a.outcome:8s} quality={a.quality}")
    print(f"  {fb.parent_note}")
    print("  ★ 不猜 direction / flags —— 猜错会污染状态机的输入")

    print("\n=== session 与技能的关联：只把练到该技能的记录算进去 ===")
    from activity_data import get as get_act
    sessions = [sm.SessionRecord(date="2026-10-17", activity_id="soc-001",
                                 outcome="success", quality=0.8),
                sm.SessionRecord(date="2026-10-17", activity_id="gro-007",
                                 outcome="refused", quality=0.0)]
    for item in ["共同注意", "跑", "轮流等待"]:
        mine = [s.activity_id for s in sessions
                if item in (get_act(s.activity_id).target_skill_items
                            if get_act(s.activity_id) else [])]
        print(f"  {item:8s} ← 今天的记录 {[s.activity_id for s in sessions]}"
              f" → 只认 {mine or '（无）'}")


if __name__ == "__main__":
    _cli()
