# -*- coding: utf-8 -*-
"""
节点体
======

**全部是现有纯函数的薄封装。** 每个节点上面都注明它调的是哪个函数 ——
这不是注释洁癖，是为了让"这个判定从哪来"在代码里可查：
LangGraph 让"加个节点就交给模型"变容易了，而那恰好会回退现有设计最好的那条原则
（模型给证据，Python 做判定）。所以这里每个节点都必须能回答
「你凭什么这么判」，答案只能是「我调了 `statemachine.propose_transition`」，
不能是「我让模型判的」。

★ 副作用的分布是这张图结构的原因，不是顺手排的
----------------------------------------------------
`interrupt()` 恢复时会**重跑整个节点**（实测 `before_interrupt` 出现 2 次）。
于是：

    record_intake → extract_observation → validate_observation → clarify(⚡)
                 → safety_gate → escalate? → publish

`publish` 之前的所有节点都是**纯读或纯计算**，一个字节都不写业务表；
`publish` 是唯一写 `observations` 表的地方，且它排在**所有** interrupt 之后。
这样"家长提交两次会不会写两遍"在结构上不存在 ——
不是因为我们记得判重，而是因为它没有被放到能被重跑的位置上。

（`publish` 自己仍然按日期做了幂等兜底，那是第二道，
不是第一道 —— 第一道是这个节点的位置。）
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import observe                                      # noqa: E402
import redflag                                      # noqa: E402
import store                                        # noqa: E402

from langgraph.types import interrupt              # noqa: E402

from .state import ClarifyAnswer, EscalationVerdict   # noqa: E402


def mark(state, name: str) -> list[str]:
    """把节点名追加进 `trace`，返回**完整的**新列表。

    ★ 这里记的是**节点完成**的顺序，不是"节点进入"了几次。
      有人会指望靠它看出 `interrupt()` 的重跑 —— 那是想错了：
      重跑的证据来自"节点在 interrupt 之前做过什么"，
      而 `mark` 不在 interrupt 之前做（那正是它要遵守的约束）。
      要验重跑，用 `selfcheck.py` 里那个专门的小探针图，
      别在这里塞一个"为了记录而故意写副作用"的反模式。
    """
    return list(state.get("trace") or []) + [name]


# ================================================================== 1. 收话
def record_intake(state) -> dict:
    """今天的入口。只做归一化，**不抽取、不判定、不写库**。

    为什么单独一个节点：它给后面所有节点一个稳定的起点坐标
    （`child_id` / `date` / `parent_text`），而这三个字段的缺失应该是
    一眼可辨的入参错误，而不是散落在后面每个节点里各判一次。
    """
    text = (state.get("parent_text") or "").strip()
    if not state.get("child_id"):
        raise ValueError("record_intake: 缺 child_id")
    if not state.get("date"):
        raise ValueError("record_intake: 缺 date")
    return {
        "child_id": state["child_id"],
        "date": state["date"],
        "parent_text": text,
        "no_llm": bool(state.get("no_llm", False)),
        "trace": mark(state, "record_intake"),
    }


# ================================================================== 2. 抽取
def extract_observation(state) -> dict:
    """调 `observe.extract_observations`（一次 LLM 调用，或降级）。

    ★ 纯函数：只算不写。`observe.persist` 不在这里 —— 见本文件的副作用分布说明。

    `no_llm` 从 state 取而不是读环境变量：离线回归要能**逐个节点**地跑降级，
    而环境变量是进程级的，一个进程里混不到两种模式。

    ★ 幂等：语料没变就复用上一次的抽取结果。
      两个理由，一个是省钱，一个是**这是"抽取已完成"这件事唯一的表达** ——
      分叉出来的 thread 带着上一轮的 observation 直接继续，
      不该为此再付一次 LLM；而"语料变了才重抽"这条判据用 `observed_text`
      表达，不用"有没有 observation 字段"（后者在澄清那轮会误判，
      澄清后语料确实变了、observation 字段也还在）。
    """
    text = state.get("parent_text", "")
    if state.get("observation") and state.get("observed_text") == text:
        return {"trace": mark(state, "extract_observation(reused)")}

    out = observe.extract_observations(
        text, date=state["date"], activity=None,
        no_llm=bool(state.get("no_llm", False)))

    return {
        # 存 dict 而不是 Pydantic 对象：checkpoint 的寿命比代码长，
        # 存对象会让老历史在重构后读不出来。
        "observation": out.model_dump(mode="json"),
        "observed_text": text,
        "redflags": _rf_dump(out.redflags),
        "n_events": len(out.events),
        # 澄清文案由模型给（它看得懂家长的话），**该不该问由 Python 判**（下一个节点）
        "clarifying_question": _first_question(out),
        "trace": mark(state, "extract_observation"),
    }


def _rf_dump(report) -> dict:
    return {
        "triggered": report.triggered,
        "highest": report.highest,
        "blocking_codes": list(report.blocking_codes),
        "hits": [h.__dict__.copy() for h in report.hits],
    }


def _first_question(out) -> str:
    """取第一条事件上的追问。**取文案，不取要不要问的决定权。**"""
    for e in out.events:
        if e.needs_clarification and e.clarifying_question:
            return e.clarifying_question
    return ""


# ================================================================== 3. 判定要不要问
def validate_observation(state) -> dict:
    """★ 纯 Python 判定：这条输入是否缺到不能往下走。

    **判据就一条：有没有一条事件的 `needs_clarification` 为真。**
    没有第二条 —— 「事件数少于 3」「没提到场景」这类启发式看着合理，
    但它们会让系统在家长说得少的时候反复打断，而家长说得少往往正是
    「今天没什么事」，那恰恰该安静地走完。

    降级路径（`--no-llm`）抽不出事件，于是这里恒判 false：
    降级时打断家长只会得到"我不知道"，而那个答案不含任何新信息。

    ★ `clarify_asked` 是这张图唯一的**循环出口守卫**，缺了它会死循环：
      澄清 → 重抽 → 若仍判"缺信息" → 又澄清 → …
      而重复追问同一个问题对家长不只是烦，是**系统把自己没听懂的锅推给人**。
      一天只问一次，问完不管答没答清楚都往下走 ——
      真需要更细的信息，`missing_information`（Phase 1 的四层之一）
      才是该记它的地方，而不是再问一遍。
    """
    out = state.get("observation") or {}
    degraded = bool(out.get("degraded"))
    asked = bool(state.get("clarifying_question"))
    already = bool(state.get("clarify_asked"))

    need = bool(asked) and not degraded and not already and _any_event_needs(out)
    return {
        "needs_clarification": need,
        "trace": mark(state, "validate_observation"),
    }


def _any_event_needs(obs_dump: dict) -> bool:
    return any(bool(e.get("needs_clarification"))
               for e in (obs_dump.get("events") or []))


# ================================================================== 4. ⚡ 澄清 interrupt
def clarify(state) -> dict:
    """⚡ 第一个真 HITL。**这里的答案会拼回原话，让抽取重跑一次。**

    ★ 为什么 interrupt 之前这个节点没有副作用：
      它什么都没做 —— 抽取在**上一个**节点已经算完了，
      这个节点只负责"停一下等人"。等 `Command(resume=...)` 回来，
      整个节点重跑，但它重跑出来的结果和第一次一样（纯读 state）。

    ★ 为什么回答要**追加**而不是替换：
      回指校验（`observe.verify`）比的是**整段原话**。
      家长先说了"今天不太配合"、被追问后补一句"是因为他困了"，
      语料应该是两句加起来 —— 只留后半句，回指校验会拿一个
      它从没见过的基准去核，结果是把模型**正确**的引用判成编造
      （PROGRESS.md 阶段 4 的同款坑：`to_coach_inputs` 的 raw 必须是整段原话）。
    """
    q = state.get("clarifying_question") or "这段话里还有些地方没说清楚，能补充一点吗？"
    ans: ClarifyAnswer = interrupt(
        {"type": "clarify", "question": q,
         "child_id": state["child_id"], "date": state["date"]},
        response_schema=ClarifyAnswer,
    )
    # response_schema 校验后是 ClarifyAnswer 实例；
    # 万一上层传了裸 dict（自定义 resume 路径），这里收一下
    if isinstance(ans, dict):
        ans = ClarifyAnswer.model_validate(ans)

    extra = (ans.answer or "").strip()
    base = state.get("parent_text", "")
    merged = f"{base}\n{extra}" if extra and not ans.skip else base

    return {
        "clarification_answer": ans.answer,
        "clarified_text": merged,
        "clarify_asked": True,
        # 重跑抽取：拿到完整语料再抽一次，这次 events 会更准。
        # 仍然纯计算 —— persist 在 publish。
        **({} if merged == base else _reextract(state, merged)),
        "trace": mark(state, "clarify"),
    }


def _reextract(state, text: str) -> dict:
    out = observe.extract_observations(
        text, date=state["date"], activity=None,
        no_llm=bool(state.get("no_llm", False)))
    # ★ 先记下**旧**语料再覆盖 —— publish 澄清后要按它删掉上一轮落的那几行。
    #   取的是旧 observation 的 source_text，不是 state.observed_text：
    #   后者存的是家长原话，而落库的 source 列是 extract 时传进去的那段
    #   （两者今天相等，但语义不同，别混用）。
    prev = (state.get("observation") or {}).get("source_text") or ""
    return {
        "observation": out.model_dump(mode="json"),
        "observed_text": text,
        "previous_source": prev,
        "redflags": _rf_dump(out.redflags),
        "n_events": len(out.events),
        "parent_text": text,
    }


# ================================================================== 5. 安全闸门
def safety_gate(state) -> dict:
    """调 `redflag.scan`（11 条正则 × 三档严重度，零 token）。

    ★ 这里不做任何判断，判断在 `redflag.py` 里已经做完了 ——
      本节点只把结果搬进 state。**它不是"再判一次"**：
      重复判定会让两处规则将来漂移，而安全规则的漂移是最不能容忍的一类。
    """
    # 重抽之后语料变长了（澄清回答拼在后面），要重扫一遍 ——
    # "他咬自己"可能恰恰是在被追问后才说出来的。
    text = state.get("clarified_text") or state.get("parent_text", "")
    rf = redflag.scan(text)
    verdict = EscalationVerdict(
        escalate=rf.highest == "urgent",
        reasons=[f"{h.group}（{h.code}）：{h.quote}" for h in rf.hits],
        highest=rf.highest,
    )
    return {
        "redflags": _rf_dump(rf),
        "verdict": verdict.model_dump(),
        "trace": mark(state, "safety_gate"),
    }


# ================================================================== 6. 升级
def escalate(state) -> dict:
    """命中 urgent → **暂停计划生成**，并把转诊指引写进 state。

    ★ Phase 0 只做「暂停 + 给指引」。**红旗落库**（把红旗从"响应里飘一次"
      变成一条长期记录）是 Phase 2 的活 —— `RedFlagList.vue` 现在
      渲染一次就永久消失，本质上是这张表还没有列。
      这里刻意先不落库：落库就意味着 Phase 0 要动 schema，而计划文件
      明确要求「不动 asd_agent/ 任何现有模块」。

    ★ 为什么它不是 END：它产出的 state 要被前端读到并置顶展示，
      也要被 audit 记一笔。让它直接 END 就丢了"为什么停"的上下文。
    """
    v = state.get("verdict") or {}
    return {
        "escalation": {
            "paused": True,
            "highest": v.get("highest", "urgent"),
            "reasons": v.get("reasons", []),
            "guidance": "建议当天联系儿科/发育行为专科医生，并记录发生时间与当时情境。"
                        "在专业评估之前，不要自行加大干预强度。",
        },
        "trace": mark(state, "escalate"),
    }


# ================================================================== 7. 落库
def publish(state) -> dict:
    """★ **全图唯一写业务表的地方**，且排在所有 interrupt 之后。

    幂等兜底（第二道保险）：**这段语料**今天已经落过库时，
    只在家长**又答了一次澄清**的情况下重写 —— 因为那次的语料真的变了。
    没重抽过就不重写。

    幂等键是「本次语料」而不是「这一天」（Phase 1 改的，原因见下面 `already` 那行）。

    为什么不干脆做成"永远重写"：重复写入会让 `refusal_counts`
    与红旗按天计数（`COUNT(DISTINCT observed_at)`）的语义变模糊，
    而这两处都是状态机的输入。
    """
    conn = store.connect()
    child_id = state["child_id"]

    # 澄清过 → 语料变了，重抽的产物才是今天该落的那一份
    obs = state.get("observation") or {}
    degraded = bool(obs.get("degraded"))
    # ★ 幂等键是**本次语料**，不是「这一天」。
    #   原判据是 `_today_has_observations(child, date)`（这天有没有任何一行），
    #   它在 Phase 0 成立是因为当时只有图一个写入口。图一接进 HTTP，
    #   同一天 `feedback.apply_feedbacks` 也会从逐条活动的 note 写 observations 行
    #   —— 那个粗判据会把「其他观察」误判成「今天已落库」而**静默跳过**，
    #   家长写的那段话永远不落库（没有报错，只有数据不见）。
    #   改成按 source 判之后，「今天还写过别的东西」不再吞掉「今天这段话」。
    #   这也让写侧与读侧一致：server.py 早就按 (observed_at, source) 分组了。
    source = obs.get("source_text") or ""
    already = _source_published(conn, child_id, state["date"], source)
    # ★ 用 clarify_asked 判，不能用 `clarified_text != parent_text` ——
    #   _reextract 会把 parent_text 直接改成 merged，两者恒相等，
    #   那个比较永远为假，于是澄清后的重抽结果永远不落库（静默丢数据）。
    reextracted = bool(state.get("clarify_asked"))

    if degraded or not obs:
        # 降级时**一条都不编** —— 与 observe._fallback_batch 同一条原则。
        # 编造的事件会污染 refusal_counts / recent_scenes / 教练诊断，
        # 而那三条都会改变明天的计划。
        ids: list[int] = []
    elif already and not reextracted:
        ids = []
    else:
        out = observe.ObservationOutcome.model_validate(obs)
        # ★ 把 date 钉成 state["date"]，不要用 observation 自带的那个。
        #   `observe.persist` 写的是 `observed_at=outcome.date`，而上面的幂等判据
        #   查的是 `state["date"]` —— 两个键必须一致，否则「查的那天」和
        #   「写的那天」不是同一天，幂等判据永远匹配不上，于是每次跑都重写一遍。
        #   正常路径上两者相等（extract 收的就是 state["date"]），但这三条路上不等：
        #   带 observation 进来的重跑、分叉带过来的历史 state、澄清后的重抽。
        out = out.model_copy(update={"date": state["date"]})
        if reextracted:
            # 只删**上一轮那一份**语料的行。原 `_delete_today` 按 child+date 删全部，
            # 会把同一天逐条 note 写的行一起删掉 —— 那是数据丢失，不是覆盖。
            prev = state.get("previous_source") or ""
            if prev and prev != source:
                _delete_source(conn, child_id, state["date"], prev)
        ids = observe.persist(conn, child_id, out)

    return {
        "observation_ids": ids,
        "trace": mark(state, "publish"),
    }


def _source_published(conn, child_id: str, date: str, source: str) -> bool:
    """这段语料今天是否已经落过库。`source` 就是 `observations.source` 那一列。

    刻意不做 `sha(source)`：PG 上 `text` 比较走的是等值索引路径，
    而这张表按 child_id 过滤后一天也就几行，实测不构成瓶颈。
    """
    if not source:
        # 没有语料可比 —— 空 source 的行是老数据（它们只到 observed_at 为止）。
        # 按「已落库」处理，与旧行为一致：宁可不多写，不凭空造。
        return bool(conn.execute(
            "SELECT 1 FROM observations "
            "WHERE child_id=%s AND observed_at=%s AND (source IS NULL OR source='') LIMIT 1",
            (child_id, date)).fetchone())
    return bool(conn.execute(
        "SELECT 1 FROM observations "
        "WHERE child_id=%s AND observed_at=%s AND source=%s LIMIT 1",
        (child_id, date, source)).fetchone())


def _delete_source(conn, child_id: str, date: str, source: str) -> None:
    """只有「澄清后重抽」这一条路才会走到这里。**是覆盖那一段，不是清这一天。**"""
    if not source:
        return
    conn.execute("DELETE FROM observations "
                 "WHERE child_id=%s AND observed_at=%s AND source=%s",
                 (child_id, date, source))
