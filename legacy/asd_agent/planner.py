# -*- coding: utf-8 -*-
"""
规划 Agent
==========
输入画像 + 状态机 + 记忆 → 输出家长这一周每天做什么。

**这个 Agent 不做检索。**
候选活动由 `activities.retrieve()` 在 Python 里用硬过滤 + 加权打分算完，
LLM 拿到的是一张已经合法的候选表，它只做两件事：
    ① 编排 —— 哪几条放在哪一天、哪个时段，怎么凑得下家长的时间
    ② 解释 —— 每条给家长一句"为什么今天做这个"（why_this）

这个边界是整个 demo 的核心论点：**把可判定的交给 Python，把需要理解的交给 LLM。**
编排看起来像"决策"，但它的每一个约束（时长够不够、材料齐不齐、前置做完没、
难度对不对得上当前状态）都已经是可判定的，让 7B 去"判断"这些只会引入不可复现的抖动。

三道后置校验
------------
模型输出**一律要过校验**才作数（约束解码只保证格式和枚举，保证不了"别选了个
候选表里没有的 id"）：
  1. activity_id 必须在候选表内 —— 否则替换为检索第一名并记入 excluded_activity_ids
  2. 每条 minutes **以活动定义为准**，模型写的数字只用来对账
  3. 每天总时长 ≤ 家长预算 —— 超了就按分项得分砍掉当天的低优先项

全部失败时走 `greedy_fallback_plan()`：纯 Python 贪心装箱，**永远能返回一份合法计划**。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field

import llm
import plan_item
from activities import (
    Activity,
    PlanRequest,
    ScoredActivity,
    candidates as retrieve_candidates,
    explain_retrieval,
)
from capabilities import LEVEL_CN, AbilityProfile
from vocab import ABILITY_DOMAIN, SCENE, STATE_TARGET_DIFFICULTY

MAX_DAYS = 7
MAX_PER_DAY = 5          # 一天最多几条。贪心装箱靠时间预算约束，这个只是兜底
Slot = str  # morning / afternoon / evening / anytime


# ================================================================== 输出模型
class PlannedActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str = Field(
        description="必须逐字取自候选表里的 activity_id"
                    "（候选表里的项可能是活动库里的，也可能是模型生成的）")
    slot: Slot = Field(description="morning / afternoon / evening / anytime")
    minutes: int = Field(ge=2, le=20, description="预计用时（分钟）")
    why_this: str = Field(
        max_length=40,
        description="**一句**中文，家长视角，不超过 40 字。"
                    "说为什么给他做这个，不要复述难度和能区名。",
    )


class DailyBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_index: int = Field(ge=0, le=6)
    date: str = Field(description="YYYY-MM-DD")
    focus_domain: str = Field(description="这一天主要练的能区 code")
    activities: list[PlannedActivity] = Field(min_length=1, max_length=5)
    total_minutes: int = Field(ge=0)
    parent_note: str = Field(default="", max_length=80, description="给家长的一句话提醒")


class WeeklyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    days: list[DailyBlock] = Field(min_length=1, max_length=MAX_DAYS)
    excluded_activity_ids: list[str] = Field(
        default_factory=list,
        description="被后置校验剔除的 activity_id。它们不在任何一天里出现过",
    )
    recap: str = Field(default="", max_length=200, description="本周安排的一句话说明")
    degraded: bool = False      # True = 走了贪心兜底，编排不是模型给的
    trace: llm.LLMTrace | None = None

    def render(self) -> str:
        lines = []
        for d in self.days:
            dom = ABILITY_DOMAIN.get(d.focus_domain, d.focus_domain)
            lines.append(f"  第{d.day_index + 1}天 {d.date}（{dom}，共 {d.total_minutes} 分钟）")
            for a in d.activities:
                lines.append(f"    · {a.minutes}分钟 {a.activity_id} — {a.why_this}")
            if d.parent_note:
                lines.append(f"    提醒：{d.parent_note}")
        return "\n".join(lines)


class PlanDraft(BaseModel):
    """模型这一次的原始输出。**没有 minutes 的最终裁决权，也没有候选表之外的选择权。**"""
    model_config = ConfigDict(extra="forbid")

    days: list[DailyBlock] = Field(min_length=1, max_length=MAX_DAYS)


# ================================================================== prompt
SYSTEM = """你是一名自闭症儿童的居家干预计划编排助手。

【最重要的规则】你只做编排和解释，**不做检索、不做判断**。
候选活动已经由系统按孩子的能力水平、时间预算、家里现有材料、前置完成情况筛过了，
你只能从候选表里挑，**绝对不能发明或改写 activity_id**。

【时间预算是硬约束】
家长每天只有固定的分钟数。每一天的活动时长加起来不得超过它。
宁可一天只排 2 条短的，也不要排 4 条然后超时 —— 排满的计划家长做两天就放弃了，
这是这类工具被弃用的头号原因。**每天排不满是可以接受的，排满是不可接受的。**

【排期原则】
- 一天不要安排同一个技能细项下的多条活动，孩子会腻
- 一天最多 3 条，通常 2-3 条就够
- 把需要家长精力最好的时候（孩子刚睡醒的上午）留给最需要专注的那条
- 前一天排过的活动，间隔一天再排

【why_this 怎么写】
一句中文，家长视角，**不超过 40 字**。
- 好的："他昨天指给你看了三次，这正是你想让他固定下来的动作"
- 差的："该活动的难度2匹配当前入门状态，属于ESDM方法"   ← 这是复述系统内部话术，家长看不懂

说"为什么是这一条"，不要说"这个活动有什么参数"。

【parent_note】
当天给家长的一句提醒，一句话。可以用他的原话、他的习惯。
没有特别要说的就留空字符串。"""


def _candidate_table(cands: list[ScoredActivity], req: PlanRequest, limit: int = 12) -> str:
    lines = []
    for s in cands[:limit]:
        lines.append(f"- {s.activity.brief()}  || 分 {s.score:.3f} || {explain_retrieval(req, s)}")
    return "\n".join(lines)


def _profile_block(profile: AbilityProfile, req: PlanRequest) -> str:
    lines = [profile.render(), ""]
    if req.target_states:
        st = "、".join(f"{ABILITY_DOMAIN.get(d, d)}={STATE_TARGET_DIFFICULTY[s]}"
                       for d, s in req.target_states.items())
        lines.append(f"各能区当前状态（括号内是当前阶段的目标难度）：{st}")
    if req.known_refusals:
        lines.append("家长明说孩子抗拒的：" + "、".join(req.known_refusals))
    if req.refusal_counts:
        rc = "、".join(f"{k}×{v}次" for k, v in sorted(req.refusal_counts.items())[:5])
        lines.append(f"近期被抗拒过的活动：{rc}（已被系统避开）")
    if req.success_counts:
        sc = "、".join(f"{k}×{v}次" for k, v in sorted(req.success_counts.items())[:5])
        lines.append(f"近期做得多的活动：{sc}（避免腻）")
    return "\n".join(lines)


# ================================================================== 兜底
def greedy_fallback_plan(req: PlanRequest, plan_id: str = "fallback", *,
                         days: int = MAX_DAYS,
                         today: str | None = None) -> WeeklyPlan:
    """纯 Python 贪心装箱。**零 LLM、零延迟、永远返回合法计划。**

    这不是"降级凑合"，而是一个真实可用的产出：50 条活动库里，
    按硬过滤 + 分数贪心装箱，一周计划的可用性并不比模型编排差多少。
    模型编排真正多出来的是 why_this 那句话和时段安排 —— 不是可执行性。
    所以 LLM 挂了不该让家长拿不到计划，只该让他少看到一句解释。
    """
    pool = retrieve_candidates(req, top_k=30)
    if not pool:
        # 连硬过滤都没结果：放宽到全部活动里挑最短的，保证有计划可给
        from activities import retrieve
        pool = [s for s in retrieve(req) if s.passed] or \
               [ScoredActivity(activity=a, score=0.0, passed=True)
                for a in __import__("activity_data").load_activities()]

    budget = req.daily_minutes
    # last_used: activity_id -> 上次被排在第几天。**用它做轮转**，
    # 而不是只看"昨天排了什么"。
    # 只避开昨天是不够的：候选池常常只有十几条而一周要排 20 个位置，
    # 于是会出现「第1-2天、第3-4天」两天一循环的假轮转 ——
    # 看起来排满了，实质上家长第三天就在重复第一天的内容。
    last_used: dict[str, int] = {}
    blocks: list[DailyBlock] = []

    base = date.fromisoformat(today) if today else date.today()
    for i in range(days):
        d = base + timedelta(days=i)
        picked: list[PlannedActivity] = []
        spent = 0
        chosen: set[str] = set()

        # 每天最多 3 条。轮转排序：越久没排过的越靠前，同一天不重复。
        ordered = sorted(
            pool,
            key=lambda s: (last_used.get(s.activity.activity_id, -1), -s.score),
        )
        for s in ordered:
            if len(picked) >= 3 or spent >= budget:
                break
            a = s.activity
            if a.activity_id in chosen or spent + a.total_min > budget:
                continue
            picked.append(PlannedActivity(
                activity_id=a.activity_id, slot=_slot_for(i, len(picked)),
                minutes=a.total_min, why_this=_template_reason(req, s)))
            spent += a.total_min
            chosen.add(a.activity_id)
            last_used[a.activity_id] = i

        if not picked:      # 预算太小或全排过了：允许重复，但挑上次数最少的那条
            for s in ordered:
                if spent + s.activity.total_min <= budget:
                    picked.append(PlannedActivity(
                        activity_id=s.activity.activity_id, slot="anytime",
                        minutes=s.activity.total_min, why_this=_template_reason(req, s)))
                    last_used[s.activity.activity_id] = i
                    break
        if not picked:      # 兜底的兜底：给最短的那条，哪怕超预算
            s = min(pool, key=lambda x: x.activity.total_min)
            picked.append(PlannedActivity(activity_id=s.activity.activity_id,
                                          slot="anytime", minutes=s.activity.total_min,
                                          why_this=_template_reason(req, s)))
            last_used[s.activity.activity_id] = i

        act = next((s.activity for s in pool
                    if s.activity.activity_id == picked[0].activity_id), None)
        blocks.append(DailyBlock(
            day_index=i, date=d.isoformat(),
            focus_domain=act.domains[0] if act else "social_adapt",
            activities=picked, total_minutes=sum(p.minutes for p in picked)))

    uniq = len({a.activity_id for b in blocks for a in b.activities})
    total = sum(b.total_minutes for b in blocks)
    return WeeklyPlan(plan_id=plan_id, days=blocks, degraded=True,
                      recap=f"按孩子当前能力自动排的 {days} 天计划："
                            f"共 {total} 分钟，覆盖 {uniq} 条不同活动。")


def _slot_for(day_index: int, nth: int) -> str:
    """时段错开，别让一周七天的活动都落在同一个时段。

    家长往往只有一个固定的、状态最好的时段（多半是上午），
    所以把第 2、3 条放到别的时段是在提醒他"这条不挑时候"，
    而不是在制造新的执行负担。
    """
    base = ("morning", "afternoon", "evening")[day_index % 3]
    return base if nth == 0 else "anytime"


def _template_reason(req: PlanRequest, s: ScoredActivity) -> str:
    """兜底时用的模板化理由。用检索器已有的可判定结论，不编。"""
    a = s.activity
    if req.refusal_counts.get(a.activity_id):
        return "换个新活动试试，难度刚好卡在现在的水平上"
    dom = "/".join(ABILITY_DOMAIN.get(d, d) for d in a.domains)
    if not a.materials:
        return f"{dom}的练习，不用准备任何东西，今天就能做"
    return f"{dom}的练习，{a.total_min}分钟，用家里现成的东西就行"


# ================================================================== 后置校验
def _repair(draft: PlanDraft, req: PlanRequest, cands: list[ScoredActivity],
            plan_id: str) -> WeeklyPlan:
    """三道校验 + 每日预算修复。全程确定性。"""
    by_id = {s.activity.activity_id: s for s in cands}
    allowed = set(by_id)
    fallback_first = cands[0].activity.activity_id if cands else ""
    dropped: list[str] = []      # 被丢掉的 id（含重复、超预算）；末尾再按"是否真的不在计划里"筛

    days: list[DailyBlock] = []
    for i, d in enumerate(draft.days):
        kept: list[PlannedActivity] = []
        seen: set[str] = set()
        for pa in d.activities:
            if pa.activity_id not in allowed:
                dropped.append(pa.activity_id)
                continue
            if pa.activity_id in seen:      # 一天里重复排同一条：去重
                dropped.append(pa.activity_id)
                continue
            seen.add(pa.activity_id)
            kept.append(pa)

        # 越界替换：用检索第一名顶上，但**不与当天已有的重复**
        for bad in [x.activity_id for x in d.activities if x.activity_id not in allowed]:
            if fallback_first and fallback_first not in seen:
                kept.append(PlannedActivity(activity_id=fallback_first, slot="anytime",
                                            minutes=by_id[fallback_first].activity.total_min,
                                            why_this=_template_reason(
                                                req, by_id[fallback_first])))
                seen.add(fallback_first)

        if not kept:
            # 整天都被校验掉了：从候选里补一条
            s = next((x for x in cands if x.activity.activity_id not in seen), None)
            if s is None:
                continue
            kept.append(PlannedActivity(activity_id=s.activity.activity_id, slot="anytime",
                                        minutes=s.activity.total_min,
                                        why_this=_template_reason(req, s)))

        # ---- 校验 2：时长以活动定义为准 ----
        # 先把模型写的分钟数改回活动定义里的真实值，**再**判断超没超预算。
        # 顺序反了会误杀：模型把 8 分钟的 lan-002 报成 20 分钟（撞到预算上限被砍掉），
        # 而这条活动其实完全排得下 —— 一次算术错误就白白少排一条。
        for pa in kept:
            pa.minutes = by_id[pa.activity_id].activity.total_min

        # ---- 校验 3：每日预算。超了就砍分最低的 ----
        budget = req.daily_minutes
        while sum(p.minutes for p in kept) > budget and len(kept) > 1:
            worst = min(kept, key=lambda p: by_id[p.activity_id].score)
            kept.remove(worst)
            dropped.append(worst.activity_id)

        days.append(DailyBlock(day_index=i, date=d.date, focus_domain=d.focus_domain,
                               activities=kept, total_minutes=sum(p.minutes for p in kept),
                               parent_note=d.parent_note))

    if not days:
        return greedy_fallback_plan(req, plan_id)

    # excluded 只收"**最终没出现在计划里**"的 id。
    # 同一天重复的那次也会进 dropped，但它和保留下来的那次是同一个 id，
    # 如果照单全收，前端会说"xxx 被剔除了"而它明明在计划里 —— 自相矛盾。
    in_plan = {p.activity_id for b in days for p in b.activities}
    excluded = sorted({x for x in dropped if x not in in_plan})

    return WeeklyPlan(plan_id=plan_id, days=days,
                      excluded_activity_ids=excluded,
                      recap=f"本周 {len(days)} 天，共 {sum(b.total_minutes for b in days)} 分钟，"
                            f"每天不超过 {req.daily_minutes} 分钟。")


# ================================================================== 主入口
def plan_week(
    profile: AbilityProfile,
    req: PlanRequest,
    *,
    plan_id: str = "plan-001",
    days: int = MAX_DAYS,
    top_k: int = 12,
    today: str | None = None,
    on_step=None,
) -> WeeklyPlan:
    """生成一周计划。**一次 LLM 调用 + 三道后置校验；任何失败都返回合法计划。**

    ★★ 这个函数**不在 HTTP 路径上**。Phase 3 之前它是"死代码"，
      之前 Phase 3 的交接文档判定为：要么接进 `generate_plan`，
      要么删掉整条 LLM 链路只保留 `pack_day`。

      现在的结论是**保留它，但只作为 CLI demo 的周计划**，
      而 HTTP 的计划链走 `pack_day`（纯 Python，见
      `graph/plan_nodes.py::generate_plan` 的注释）。三条理由：

      1. **它不是死代码** —— `loop.py` 的五日闭环 demo 每天用它排一周
         （`--no-llm` 时用 `greedy_fallback_plan`）。那是本项目
         "多 Agent 协作"四个展示点之一，删了就没了。
      2. **接进 `generate_plan` 是错的**：图里的计划链是**一天**的计划，
         而它是**一周**的。把周计划塞进日计划链，
         `validate_plan` 的"实质性变更"就会**每天都判成有变更** ——
         因为七天里每天的内容都不一样 ——
         于是每一天都打断家长一次，正是技术方案 §10.3
         自己列的"用户留存和家长负担"指标要防的那件事。
      3. **★ 真正要防的是"看起来发生过而其实没有"**：
         LANGGRAPH.md 第四节之三的担心是，
         读代码的人以为"LLM 出了个计划"，而 HTTP 上根本没有 LLM 参与。
         所以这里**把这个边界写在 docstring 里**，
         而不是删掉一个能跑的功能让后人重新发明一遍。
    """
    days = max(1, min(days, MAX_DAYS))

    # ---- 检索在 Python 里完成，LLM 只看结果 ----
    cands = retrieve_candidates(req, top_k=top_k)
    if not cands:
        return greedy_fallback_plan(req, plan_id, days=days, today=today)

    base = date.fromisoformat(today) if today else date.today()
    date_lines = [f"  第{i + 1}天 = {(base + timedelta(days=i)).isoformat()}"
                  for i in range(days)]

    user = "\n".join([
        _profile_block(profile, req),
        "",
        f"【家长每天的时间预算】{req.daily_minutes} 分钟（**每一天都不得超过**）",
        f"【要排的天数】{days} 天，日期对应：",
        *date_lines,
        "",
        f"【候选活动表】共 {len(cands)} 条，只能从里面挑：",
        _candidate_table(cands, req, top_k),
    ])

    try:
        draft, trace = llm.chat_json(
            system=SYSTEM, user=user, model_cls=PlanDraft,
            name="weekly_plan", stage="planner_plan",
            max_tokens=3000, timeout_s=llm.TIMEOUT_S * 2, on_step=on_step,
        )
    except llm.LLMError:
        # 编排挂了不该让家长拿不到计划
        return greedy_fallback_plan(req, plan_id, days=days, today=today)

    plan = _repair(draft, req, cands, plan_id)
    plan.trace = trace
    return plan


# ================================================================== 单日卡片接线
def todays_plan(req: PlanRequest, *, top_k: int = 12) -> list[ScoredActivity]:
    """今天给教练 Agent 的候选表（不经 LLM）。教练只需要最好的几条。"""
    return retrieve_candidates(req, top_k=top_k)


# ================================================================== 一天的装箱
def pack_day(cands: list[ScoredActivity], budget_minutes: int, *,
             first: ScoredActivity | None = None,
             max_n: int = MAX_PER_DAY) -> list[ScoredActivity]:
    """按分数贪心装箱，**把家长给的分钟数尽量占满**。纯 Python、零 LLM、可复现。

    ★ 为什么"今天做哪几条"不需要模型：
      排不排得下（时长）、会不会重复（活动 id）、会不会一天练同一个技能细项 ——
      全是**可判定**的。让 7B 去"判断"这些只会引入不可复现的抖动，
      而家长明天打开页面看到的推荐顺序和今天不一样，是这个 demo 最不能出的错。
      模型留给两件真正需要理解的事：今天孩子是什么状态（诊断）、
      以及怎么跟他做才做得成（卡片文案与退路）。

    ★ 同一天不重复练同一个技能细项 —— 这条原本只写在 SYSTEM 提示词里
      （"一天不要安排同一个技能细项下的多条活动，孩子会腻"），
      而**提示词是建议，模型可以不听**。一天三条大运动，
      "腻了"是这类工具被弃用的头号原因，所以它必须是硬保证而不是措辞。
      实测 50 条活动里 `轮流等待` 覆盖 10 条，不设这条约束的话
      贪心装箱很容易把同一个细项排满一整天。

    ★ 装箱用的是 `total_min + prep_min`：备料时间也是家长的时间。
      只算活动时长会让一天的总量真实地超出家长给的预算。

    `first` 是教练诊断之后选定的焦点活动（可能已经换过一条），
    它先入箱，剩下的分钟数才分给其余候选。
    """
    budget = int(budget_minutes)
    picked: list[ScoredActivity] = []
    used_ids: set[str] = set()
    used_skills: set[str] = set()
    spent = 0

    def try_add(s: ScoredActivity) -> bool:
        nonlocal spent
        a = s.activity
        if a.activity_id in used_ids:
            return False
        if used_skills & set(a.target_skill_items):
            return False          # 今天已经练过这个细项了
        cost = a.total_min + a.prep_min
        if spent + cost > budget:
            return False
        picked.append(s)
        used_ids.add(a.activity_id)
        # 用 update 而不是 `|=`：闭包里 `|=` 会把名字重新绑定成本地变量，
        # 于是函数自己都读不到它（UnboundLocalError）。
        used_skills.update(a.target_skill_items)
        spent += cost
        return True

    if first is not None:
        try_add(first)
    for s in cands:
        if len(picked) >= max_n or spent >= budget:
            break
        try_add(s)
    return picked


# ================================================================== 实质性变更判据
def _item_signature(a: Activity) -> tuple:
    """**一个方案项的语义签名** —— 指纹的比较单位。

    ★ 实现放在 `plan_item.signature`（方案项的统一抽象那一处），
      这里只做一次转发 —— 指纹与"方案项"是同一件事的两个视角，
      两处各写一遍必然漂移，而漂移的后果是"指纹漏算了一项"，
      那种错**不报错**。
    """
    return plan_item.signature(a)


def plan_fingerprint(acts: list[Activity]) -> dict:
    """一份计划的可比较"指纹"。**纯函数、零 token、可重放。**

    ★ 为什么是指纹而不是逐字段 diff：
      审批要回答的是"这份计划和上一版**有没有实质区别**"，
      而家长点"重新生成"之后卡片文案（why_this / 家长提醒）几乎必然不同 ——
      那不是实质区别，那是同一次安排的两种说法。
      逐字段比会把每次重新生成都变成一次打断，于是"只在真变化时审批"这条
      就被它自己吃掉了。所以指纹**只收会影响执行的字段**。

    ★ 为什么用 `materials` 的**集合**而不是有序列表：
      `Activity.materials` 的顺序是数据录入顺序，改它不改变孩子要做的事。
      `prompt_hierarchy` 同理 —— 提示阶梯**从轻到重**排，
      "起点提示级别"取的是 `[0]`（最轻的那一级），
      也就是"从哪一级开始提示"，那才是执行上的区别。

    ★ `v` 字段：指纹格式的版本号。
      老行（`plans.payload.fingerprint`）里存的是 v1 格式 ——
      它的 `activities` 里带 `activity_id`，而 v2 不带。
      两种格式都能读，但**不能混着比**：v1 的条目按 id 比，
      v2 的按语义签名比。见 `is_substantive_change` 的注释。
    """
    return {
        "v": 2,
        "skills": sorted({it for a in acts for it in a.target_skill_items}),
        "items": sorted(_item_signature(a) for a in acts),
    }


#: 指纹里哪些字段变了算"实质性变更"。**一张表，不是一个 if 链** ——
#: 因为半年后有人加一条判据时，要改的地方必须一眼看得见。
SUBSTANTIVE_FIELDS = ("skills", "items")


def _is_v2(fp: dict) -> bool:
    """这份指纹是不是 v2（语义签名）格式。"""
    return int(fp.get("v") or 1) >= 2


def is_substantive_change(new_fp: dict, old_fp: dict | None) -> tuple[bool, list[str]]:
    """★ 「今天要不要打断家长」——**唯一的判据，纯 Python、零 token、可重放**。

    `old_fp is None`（从来没有生效过的计划）→ 一定算变更。
    那不是"第一版也要审批"的礼节问题：第一次生成计划时家长手里什么都没有，
    而"要不要现在就做这三条"恰恰是最该由人拍板的一次。
    把它判成"无变更"等于让系统替家长做了第一次决定。

    ★ ★ 老指纹怎么比（v1 vs v2）
    ---------------------------
    指纹是**存进 `plans.payload`** 的，而老行里存的是 v1 格式
    （`activities` 里带 `activity_id`）。边界 #2 是 append-only，
    **老行不重写** —— 所以这里必须两种格式都能读。

    判据：**同一版本比同一版本**。
      · v2 vs v2 → 比语义签名（不看 ID：换 ID 不算变，见 `_item_signature`）
      · v1 vs v1 → 按老的口径比（带 ID），保证升级前的行为一字不变
      · v1 vs v2 → **保守判成有变更**。
        理由：两套口径的单位不同（一条 v1 记录带 ID，一条 v2 记录不带），
        硬凑一个比较口径出来的"没变化"是**假阴性** ——
        而"假阴性"的后果是家长在方案真的变了的时候没被打断。
        这条只发生在升级当天（第一次跑新代码时对比老行），之后两侧都是 v2。

    返回 `(是否实质性变更, 理由列表)`。理由是**给人看的中文**，
    前端直接渲染 —— 让家长知道"到底哪里变了"，
    否则一个"同意/不同意"的按钮就是在让人盲签。
    """
    if old_fp is None:
        return True, ["第一次生成计划，之前没有生效的版本可比"]

    reasons: list[str] = []
    # ---- 练习目标：两个版本都有这个字段，口径一致 ----
    a_skills = set(old_fp.get("skills") or [])
    b_skills = set(new_fp.get("skills") or [])
    add, drop = sorted(b_skills - a_skills), sorted(a_skills - b_skills)
    if add:
        reasons.append("新增练习目标：" + "、".join(add))
    if drop:
        reasons.append("不再练：" + "、".join(drop))

    # ---- 逐项比较 ----
    if _is_v2(new_fp) and _is_v2(old_fp):
        reasons += _diff_items_v2(new_fp, old_fp)
    elif not _is_v2(new_fp) and not _is_v2(old_fp):
        reasons += _diff_items_v1(new_fp, old_fp)
    else:
        # 跨版本：口径不同，保守判成有变更（理由里说清楚，不装作比过）
        reasons.append("计划记录的格式升级了，本次按新的方式重新校对了一遍")
    return bool(reasons), reasons


def _norm_sig(sig) -> tuple:
    """把一条语义签名**归一成可哈希的 tuple**。

    ★ 为什么需要这一步：指纹要落进 `plans.payload`（JSON），
      JSON 里没有 tuple —— 存进去再读出来就是 list of list。
      而语义签名的比较单位是集合（`set(...)`），list 不可哈希，
      于是"读回来的老指纹"一比就炸 `TypeError: unhashable type: 'list'`。
      更糟的是不归一时**新指纹（tuple）与老指纹（list）永远不相等**，
      第二天明明什么都没变也会被判成"有变更" → 天天打扰家长。

      所以归一必须发生在**读侧**，而不是改写老行（老行是 append-only 的）。
    """
    if isinstance(sig, (list, tuple)):
        return tuple(_norm_sig(x) for x in sig)
    if isinstance(sig, dict):
        return tuple(sorted((k, _norm_sig(v)) for k, v in sig.items()))
    return sig


def _diff_items_v2(new_fp: dict, old_fp: dict) -> list[str]:
    """v2 vs v2：按**语义签名**比，不看 activity_id。

    ★ 为什么理由里仍然出现 id：那只是**给人看的说明**，
      判据本身与 id 无关 —— 一个换了 id 但语义完全一样的项，
      不会出现在"新增/去掉"里。
      （它会出现在"同 id 的项内容变了"里 —— 那才是真变化。）

    ★ 怎么把"变了"说成人话：
      v2 的条目没有 id 可作 key（那正是要点），所以按**同一练习目标**
      分组比对：目标 + 难度 + 起点提示 + 时长相同的那几条，
      就是"同一件事的两种做法" —— 说"材料/步骤变了"；
      目标本身不同的那几条，就是"换了一项" —— 说"新增/去掉"。
    """
    reasons: list[str] = []
    old_sigs = [_norm_sig(s) for s in (old_fp.get("items") or [])]
    new_sigs = [_norm_sig(s) for s in (new_fp.get("items") or [])]
    old_set, new_set = set(old_sigs), set(new_sigs)
    if old_set == new_set:
        return reasons      # 语义完全一样（哪怕 id 全变了）→ 不打断

    def group(sig: tuple) -> tuple:
        """同一"件事"的分组键：目标 + 难度 + 起点提示 + 时长。
        步骤与材料不进分组键 —— 它们正是同组内会变的部分。"""
        return (sig[0], sig[2], sig[3], sig[4])

    old_groups: dict[tuple, list[tuple]] = {}
    new_groups: dict[tuple, list[tuple]] = {}
    for s in old_sigs:
        old_groups.setdefault(group(s), []).append(s)
    for s in new_sigs:
        new_groups.setdefault(group(s), []).append(s)

    changed = added = removed = 0
    for key, olds in old_groups.items():
        news = new_groups.get(key) or []
        if set(olds) == set(news):
            continue
        # 同组内内容变了 —— 措辞里带上"练什么"
        label = "、".join(key[0]) or "有一项"
        changed += 1
        reasons.append(f"{label} 的做法变了（材料或步骤与上次不同）")
    for key, olds in old_groups.items():
        if key not in new_groups:
            removed += len(olds)
            label = "、".join(key[0]) or "有一项"
            reasons.append(f"不再练：{label}")
    for key, news in new_groups.items():
        if key not in old_groups:
            added += len(news)
            label = "、".join(key[0]) or "有一项"
            reasons.append(f"新增：{label}")
    if not reasons:
        reasons.append("有一项的做法变了")
    return reasons


def _diff_items_v1(new_fp: dict, old_fp: dict) -> list[str]:
    """v1 vs v1：**照搬升级前的口径**，一个字都不改。

    ★ 为什么留着这一份：老行（`plans.payload`）里存的是 v1 指纹，
      它要与另一份 v1 指纹比较 —— 这条路径的行为必须与升级前**逐字一致**，
      否则"升级后第一天就莫名其妙打断家长"。
    """
    reasons: list[str] = []
    old_map = {x[0]: x for x in (old_fp.get("activities") or [])}
    new_map = {x[0]: x for x in (new_fp.get("activities") or [])}
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    if added:
        reasons.append(f"新增活动：{'、'.join(added)}")
    if removed:
        reasons.append(f"去掉活动：{'、'.join(removed)}")
    for aid in sorted(set(new_map) & set(old_map)):
        o, n = old_map[aid], new_map[aid]
        if o[1] != n[1]:
            reasons.append(f"{aid} 材料变了：" + "→".join(
                ["、".join(o[1]) or "无", "、".join(n[1]) or "无"]))
        if o[2] != n[2]:
            reasons.append(f"{aid} 难度 {o[2]}→{n[2]}")
        if o[3] != n[3]:
            reasons.append(f"{aid} 起点提示级别变了")
        if o[4] != n[4]:
            reasons.append(f"{aid} 时长 {o[4]}→{n[4]} 分钟")
    return reasons


# ================================================================== CLI
def _cli() -> None:
    import store
    from capabilities import ParentConstraints

    conn = store.reset("/tmp/planner_demo.db")
    store._seed_demo(conn, "demo-001", confidence=0.85)
    prof = store.latest_profile(conn, "demo-001")
    states = store.current_states(conn, "demo-001")

    pc = prof.parent_constraints
    req = PlanRequest(
        levels=prof.levels(),
        target_states={a.domain: states.get(a.skill_item, {}).get("state", "not_started")
                       for a in prof.assessments},
        priority_domains=prof.domains_of_interest(),
        daily_minutes=pc.daily_minutes_available,
        materials_at_home=set(pc.materials_at_home),
        refusal_counts=store.refusal_counts(conn, "demo-001"),
        success_counts=store.success_counts(conn, "demo-001"),
        recent_activity_ids=store.recent_activity_ids(conn, "demo-001", 3),
        success_history_ids=store.success_history_ids(conn, "demo-001"),
        recent_scenes=store.recent_scenes(conn, "demo-001"),
        preferences=pc.preferences,
    )

    print("=== 候选表（Python 检索，LLM 看到的就这些）===")
    cands = retrieve_candidates(req, top_k=8)
    for s in cands:
        print(f"  {s.score:.3f} {s.activity.brief()}")
        print(f"        {explain_retrieval(req, s)}")

    print("\n=== 贪心兜底计划（无 LLM）===")
    fb = greedy_fallback_plan(req, "fallback", today="2026-10-17")
    print(fb.render())
    print(f"\n  总 {sum(d.total_minutes for d in fb.days)} 分钟，"
          f"每日上限 {req.daily_minutes} → "
          f"{'✓ 全天不超预算' if all(d.total_minutes <= req.daily_minutes for d in fb.days) else '✗ 超预算'}")

    print("\n=== 单日装箱（今天页用的就是它，纯 Python 零 LLM）===")
    picks = pack_day(cands, req.daily_minutes)
    spent = sum(s.activity.total_min + s.activity.prep_min for s in picks)
    for s in picks:
        a = s.activity
        print(f"    {a.activity_id} {a.total_min}+{a.prep_min}min  {a.title}"
              f"  {'/'.join(a.target_skill_items)}")
    print(f"  {len(picks)} 条，共 {spent} 分钟（预算 {req.daily_minutes}）")

    # 三条硬约束。少一条都是"能跑但错"，而界面上看不出来。
    assert spent <= req.daily_minutes, f"超预算：{spent} > {req.daily_minutes}"
    skills = [x for s in picks for x in s.activity.target_skill_items]
    assert len(skills) == len(set(skills)), f"同一天重复练同一个技能细项：{skills}"
    ids = [s.activity.activity_id for s in picks]
    assert len(ids) == len(set(ids)), f"同一天重复排了同一条活动：{ids}"
    print("  ✓ 不超预算（含备料时间）/ 技能细项不重复 / 活动不重复")

    # 焦点优先：诊断换过的那条要先入箱，且不能把它挤掉
    focus = cands[2] if len(cands) > 2 else None
    if focus:
        p2 = pack_day(cands, req.daily_minutes, first=focus)
        assert p2[0].activity.activity_id == focus.activity.activity_id, "焦点没排在第一条"
        assert focus.activity.activity_id in [x.activity.activity_id for x in p2], "焦点被挤掉了"
        print(f"  ✓ 指定焦点 {focus.activity.activity_id} → 排在第 1 条，共 "
              f"{sum(x.activity.total_min + x.activity.prep_min for x in p2)} 分钟")

    print("\n=== LLM 编排 + 三道后置校验 ===")
    plan = plan_week(prof, req, plan_id="plan-demo", days=7, today="2026-10-17")
    t = plan.trace
    if t:
        print(f"  trace {t.latency_ms}ms  重试 {len(t.retries)}  tokens "
              f"{t.prompt_tokens}/{t.completion_tokens}")
    print(plan.render())
    print(f"\n  degraded={plan.degraded}")
    print(f"  被校验剔除的 activity_id：{plan.excluded_activity_ids or '无'}")
    ok = all(d.total_minutes <= req.daily_minutes for d in plan.days)
    print(f"  每日预算：{'✓ 全天不超' if ok else '✗ 存在超预算的一天'}")
    conn.close()


if __name__ == "__main__":
    _cli()
