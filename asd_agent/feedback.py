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

import psycopg
from dataclasses import dataclass, field
from typing import Literal

import observe
import store
from statemachine import SessionRecord

Outcome = Literal["success", "partial", "refused", "skipped"]

#: outcome 的四个合法取值。**刻意是一份可枚举的元组**：
#: 它同时给 `Literal` 用、给构造时的校验用、给前端的选项用 ——
#: 三处各写一遍字面量，迟早有一处落后，而落后的那一处不报错。
OUTCOMES = ("success", "partial", "refused", "skipped")

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
        # ★ Phase 4：outcome 也要在**构造时**就拒。
        #   它以前是裸 str，非法值能一路走到 INSERT —— 于是库里躺着一行
        #   outcome='succeess'，而状态机的统计只认四个词，
        #   那行既不计入成功也不计入抗拒，**安静地不存在**。
        #   判据放在构造里，是因为这里是唯一一处所有入口都必经的地方
        #   （HTTP 的 Pydantic 校验它、图还原它时也校验它）。
        if self.outcome not in OUTCOMES:
            raise ValueError(
                f"outcome 必须是 {OUTCOMES} 之一，实际 {self.outcome!r}")


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
        return render_day(self.date, self.sessions, self.refusal_hits,
                          self.degraded)


def render_day(date: str, sessions: list, refusal_hits: list,
               degraded: bool = False) -> str:
    """一天的反馈那段人话。**函数形式** —— 反馈链的 state 里没有对象，
    只有 dump，所以渲染得能吃 dump。

    ★ 为什么把它从 `DayFeedbackResult.render` 里拆出来：
      以前它是方法，而方法的宿主是个 dataclass；
      反馈链走完图之后手里只有**纯 dict**（checkpoint 的形状），
      于是渲染也得能吃 dict —— 两处各写一遍文案必然漂移。
    """
    L = [f"【{date} 的家长反馈】"]
    for s in sessions:
        q = s.quality if hasattr(s, "quality") else (s.get("quality") or 0.0)
        aid = s.activity_id if hasattr(s, "activity_id") else s.get("activity_id")
        out = s.outcome if hasattr(s, "outcome") else s.get("outcome")
        L.append(f"  {aid} → {out}（质量 {q:.2f}）")
    if refusal_hits:
        L.append(f"  观察桥接补记 {len(refusal_hits)} 条 refused session：")
        for h in refusal_hits:
            aid = h["activity_id"] if isinstance(h, dict) else h.activity_id
            note = h["note"] if isinstance(h, dict) else h.note
            L.append(f"    {aid}  {note}")
    if degraded:
        L.append("  （--no-llm：补充描述未抽取事件，一条都不编）")
    return "\n".join(L)


@dataclass
class FeedbackPlan:
    """★ 一次提交的**纯计算**产物。**一个字节都不写。**

    为什么要有这一层（Phase 4）
    --------------------------
    反馈链是第三张图，而图的结构有一条硬约束：
    **写库只能发生在最后一个节点**（`interrupt()` 恢复会重跑所在节点，
    写在 interrupt 之前的副作用会被做两遍）。

    以前的 `apply_feedbacks` 是"算 + 写"混在一起的：
    写 sessions → 抽事件 → 写 observations → 桥接 → 再写 sessions。
    图没法用它 —— 中间那些写就落在"算"的位置上了。

    拆成两半之后：
      · `plan_feedbacks` —— 算（可能调 LLM，但不碰业务表）
      · `write_feedbacks` —— 写
    而 `apply_feedbacks` = 两半接起来，**给 CLI（loop.py）保留**。

    ★ 拆分的边界按"能不能重算"划，不按"函数多长"划：
      算的部分重算一遍只是浪费 token，写的那部分重算一遍是数据错乱。
    """

    date: str
    #: 家长点的那些条（SessionRecord，quality 由 quality_for 算）。
    sessions: list = field(default_factory=list)
    #: 与 sessions 一一对应的元信息：note / prompt_level / steps_done /
    #: source / seq。**seq 是这一行在幂等键里的位置**（见 store.log_session）。
    rows: list[dict] = field(default_factory=list)
    #: 从每条 note 里抽出来的事件（ObservationOutcome）。
    outcomes: list = field(default_factory=list)
    #: 从 note 里桥接出来的抗拒（RefusalHit）—— 它们也要落成 sessions 行。
    refusal_hits: list = field(default_factory=list)
    degraded: bool = False


@dataclass
class FeedbackWriteResult:
    """写库的结果。`session_ids` 与 `observation_ids` 给出**这次真正落下的行**。"""

    session_ids: list[int] = field(default_factory=list)
    observation_ids: list[int] = field(default_factory=list)
    bridge_session_ids: list[int] = field(default_factory=list)
    written_sessions: int = 0
    written_observations: int = 0


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
def plan_feedbacks(feedbacks: list[ActivityFeedback], *, date: str,
                   other_notes: str = "", no_llm: bool = False,
                   on_step=None) -> FeedbackPlan:
    """逐条反馈 → **纯计算**的落库计划。**一个字节都不写。**

    流程（顺序有讲究）：
      1. 先算 sessions —— 它是状态机的唯一输入，不依赖任何 LLM，
         所以即使后面抽取全降级，今天的成败记录也算得出来。
      2. 再抽事件 —— 每条有 note 的反馈各调一次 LLM。
      3. 最后跑抗拒桥接 —— 只对 outcome != refused 的反馈跑。

    ★ `other_notes`（「其他观察」框）在这里**仍然处理**，
      但调用方（HTTP 层）传空 —— Phase 1 起「其他观察」改走
      `POST /api/observe/run` 的图，两处都做 = 抽两次、写两行、付两次 LLM。
      `loop.py` 的 CLI 还在用它那条路径。
    """
    from activity_data import get as get_act

    plan = FeedbackPlan(date=date)

    # ---- ① sessions：纯映射 ----
    for fb in feedbacks:
        rec = SessionRecord(date=date, activity_id=fb.activity_id,
                            outcome=fb.outcome, quality=quality_for(fb))
        plan.sessions.append(rec)
        plan.rows.append({
            "note": _session_note(fb),
            # ★ 家长**原话**。与 note 并列存着，因为两者用途不同：
            #   note 是给家长看的那句归纳（"家长选「不配合」"），
            #   raw_note 是红旗扫描与回指校验的语料。
            #   以前原话只活在 observations.source 里，而降级时连那里都没有 ——
            #   于是"家长在补充描述里说了自伤"这件事在降级路径上永远看不见。
            "raw_note": fb.note,
            "prompt_level": fb.prompt_level,
            "steps_done": fb.steps_done,
            "source": "parent",
            # ★ 家长点的这一条恒为 0：幂等键的 seq 从 1 起是留给
            #   "从补充描述桥接补记的抗拒"的（那才是第二行、第三行）。
            "seq": 0,
        })

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
        plan.outcomes.append(out)
        plan.degraded = plan.degraded or out.degraded

    if other_out is not None:
        plan.outcomes.append(other_out)
        plan.degraded = plan.degraded or other_out.degraded

    # ---- ④ 抗拒桥接 ----
    # ★ 家长已经点了 refused 时**跳过**：那条 refused session 上面已经写了，
    #   模型再从补充描述里抽出一条 refusal 就是同一件事记两次 ——
    #   而 MAX_REFUSALS_PER_DAY=2，一次练习就够了。
    for fb, out in per_activity:
        if fb.outcome == "refused":
            continue
        # ★ seq 在 `observe.refusal_sessions` 里定好了（当天第几次抗拒），
        #   这里不重新编号 —— 两个地方编号就会有一处漏，
        #   而漏的表现是"两条抗拒撞成一条"，抗拒计数随之少一次。
        plan.refusal_hits.extend(observe.refusal_sessions(out, fb.activity_id))

    # 「其他观察」框：activity_id=None → refusal_sessions 直接返回 []。
    # 这行调用是为了让"它不计入"在代码里是显式的一次调用，
    # 而不是靠"我们没调用它"这种沉默来表达。
    if other_out is not None:
        plan.refusal_hits.extend(observe.refusal_sessions(other_out, None))
    return plan


def write_feedbacks(conn: psycopg.Connection, child_id: str, plan: FeedbackPlan, *,
                    run_id: str | None = None) -> FeedbackWriteResult:
    """把 `FeedbackPlan` 落库。**幂等**（键见 store.log_session 的 docstring）。

    写四样东西，顺序有讲究：
      1. sessions（家长点的那些条）—— 状态机的唯一输入。
      2. observations（从 note 抽出来的事件）—— 检索器的硬过滤输入。
      3. 桥接补记的 refused sessions —— 它们也是状态机的输入。
    """
    res = FeedbackWriteResult()
    if run_id is None:
        run_id = store.day_run_id(child_id, plan.date)

    # ---- ① sessions ----
    for rec, meta in zip(plan.sessions, plan.rows):
        res.session_ids.append(store.log_session(
            conn, child_id, rec, meta["note"], run_id=run_id,
            source=meta["source"], seq=meta["seq"],
            prompt_level=meta["prompt_level"], steps_done=meta["steps_done"]))
    res.written_sessions = len(res.session_ids)

    # ---- ② observations ----
    # ★ 幂等判据是「这段语料今天落过没有」（store.source_published）。
    #   重复提交同一天同一段 note 时它已经在那儿了 → 跳过，
    #   否则 observations 会多出一行，而 `recent_scenes` 与教练诊断
    #   会把同一件事算两遍。
    for out in plan.outcomes:
        if store.source_published(conn, child_id, plan.date, out.source_text):
            continue
        res.observation_ids.extend(observe.persist(conn, child_id, out))
    res.written_observations = len(res.observation_ids)

    # ---- ③ 桥接补记的 refused sessions ----
    # 桥接的 hit 在两种形态下都出现过：对象（刚算出来时）
    # 与 dict（从 state 还原时，checkpoint 里存的是纯 dict）。
    # 两种都要认 —— 而 seq 必须在两种形态下都拿得到，
    # 少了它两条桥接记录会撞成一行（DO UPDATE 覆盖掉一条）。
    for h in plan.refusal_hits:
        if isinstance(h, dict):
            sess, note, seq = h["session"], h["note"], int(h.get("seq", 1))
        else:
            sess, note, seq = h.session, h.note, int(getattr(h, "seq", 1))
        res.bridge_session_ids.append(store.log_session(
            conn, child_id, sess, note, run_id=run_id,
            source="bridge", seq=seq))
    return res


def apply_feedbacks(conn: psycopg.Connection, child_id: str, date: str,
                    feedbacks: list[ActivityFeedback], *,
                    other_notes: str = "", no_llm: bool = False,
                    on_step=None) -> DayFeedbackResult:
    """逐条反馈 → sessions + observations。**每天最多 1 次 LLM 调用每条反馈。**

    ★ Phase 4：这是 `plan_feedbacks` + `write_feedbacks` 的接法，
      **给 CLI（loop.py）保留**。HTTP 那条路走反馈链（图），
      由图的两个节点分别调 plan / write。
    """
    plan = plan_feedbacks(feedbacks, date=date, other_notes=other_notes,
                          no_llm=no_llm, on_step=on_step)
    w = write_feedbacks(conn, child_id, plan)
    res = DayFeedbackResult(date=date)
    res.sessions = list(plan.sessions)
    res.session_ids = list(w.session_ids)
    res.refusal_hits = list(plan.refusal_hits)
    res.outcomes = list(plan.outcomes)
    res.observation_ids = list(w.observation_ids)
    res.degraded = plan.degraded
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


# ================================================================== 幂等守卫
def feedback_digest(feedbacks: list[ActivityFeedback], date: str) -> str:
    """这份输入的指纹（sha256）。**幂等守卫用。**

    ★ 为什么要有它：家长（或前端的重复点击）会把**一模一样**的内容提交两次。
      落库那半已经幂等了（`store.log_session` 的唯一键），
      但图还会**重跑一遍** —— 里面有一次 LLM 调用（日结），
      于是重复提交 = 白白等 20-60 秒。
      → 指纹相同就直接返回上次存下来的结果，一次 LLM 都不付。

    ★ 指纹变了（家长更正了某一条）就该重跑 —— 那时落库那半是幂等的，
      于是更正会**覆盖**当天那一条，而不是多出一行。

    刻意**不含**时间戳之类会变的东西：同样的内容必须得到同样的指纹，
    否则这个守卫形同虚设（而它失效的方式是"静默地多付一次 LLM"）。
    """
    import hashlib
    import json as _json

    payload = [{
        "activity_id": f.activity_id,
        "outcome": f.outcome,
        "prompt_level": f.prompt_level,
        "steps_done": f.steps_done,
        "note": f.note,
    } for f in feedbacks]
    # 排序：提交的**顺序**不该影响指纹 ——
    # 家长把两条反馈调换一下顺序，内容没变，就不该重跑。
    payload.sort(key=lambda x: (x["activity_id"], x["outcome"], x["note"]))
    blob = _json.dumps({"date": date, "feedbacks": payload},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ================================================================== state 还原
def plan_from_state(*, date: str, sessions: list[dict], rows: list[dict],
                     outcomes: list[dict], refusal_hits: list[dict]) -> FeedbackPlan:
    """从 state（**纯 dict**）还原成 `FeedbackPlan`。

    ★ 为什么 state 里存的是 dict 而不是对象：
      checkpointer 用 msgpack 序列化，dataclass / Pydantic 属于未注册类型，
      实测已经打出 "will be blocked in a future version" 的警告，
      而 **checkpoint 的寿命比代码长** —— 今天能反序列化不代表明年能。
      （与观察链存 `model_dump()`、计划链存 `request_to_dict` 同一条理由。）

    还原时**重新过一遍校验**（`model_validate`）：
    老 checkpoint 里缺字段会在这里炸，而不是在写库那一步炸 ——
    那一步炸的话，症状是"落了一半库"，比这里炸难查得多。
    """
    import observe
    from statemachine import SessionRecord

    plan = FeedbackPlan(date=date)
    for s in sessions:
        plan.sessions.append(SessionRecord.model_validate(s))
    plan.rows = [dict(r) for r in rows]
    for o in outcomes:
        plan.outcomes.append(observe.ObservationOutcome.model_validate(o))
    for h in refusal_hits:
        d = dict(h)
        d["session"] = SessionRecord.model_validate(d["session"])
        plan.refusal_hits.append(d)
    plan.degraded = any(bool(getattr(o, "degraded", False)) for o in plan.outcomes)
    return plan


def plan_to_state(plan: FeedbackPlan) -> dict:
    """`FeedbackPlan` → state 里那几项（**纯 dict**）。`plan_from_state` 的逆。"""
    return {
        "sessions": [s.model_dump() for s in plan.sessions],
        "session_rows": [dict(r) for r in plan.rows],
        "outcomes": [o.model_dump(mode="json") for o in plan.outcomes],
        "refusal_hits": [{
            "session": h.session.model_dump(),
            "activity_id": h.activity_id,
            "note": h.note,
            "event_index": h.event_index,
            "behavior": h.behavior,
            "seq": getattr(h, "seq", 1),
        } for h in plan.refusal_hits],
    }


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
    conn = store.reset(db)   # ★ 一行代替删 db/-wal/-shm 三个文件
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
        "SELECT activity_id,outcome,quality,note FROM sessions WHERE child_id=%s "
        "ORDER BY id", (cid,)).fetchall()
    for r in rows:
        print(f"  {r['activity_id']}  {r['outcome']:8s} {r['quality']:.2f}  {r['note']}")
    assert all(r["note"] for r in rows), "每条 session 都该有可读的 note"


if __name__ == "__main__":
    _cli()
