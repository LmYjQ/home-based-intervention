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
`publish` 是唯一写业务表的地方（Phase 2 起写两张：`observations` 与 `red_flags`），
且它排在**所有** interrupt 之后。
这样"家长提交两次会不会写两遍"在结构上不存在 ——
不是因为我们记得判重，而是因为它没有被放到能被重跑的位置上。

（`publish` 自己仍然按日期做了幂等兜底，那是第二道，
不是第一道 —— 第一道是这个节点的位置。）

★ 为什么 urgent 也要走 publish（Phase 2 改的边）
-------------------------------------------------
Phase 0/1 时这里是 `escalate → END`。于是"孩子今天咬自己"这件事，
**家长的原话从不进 `observations` 表** —— 没有报错，只是最该被记下来的
那一天什么都没留下，而红旗也就无处可落（它在全图没有任何一个写库的地方）。

改成 `escalate → publish → END`，判定一点没变（仍是 `redflag.scan` 的
`highest == "urgent"`），改的只是"判完之后这一天还写不写"。
放进 `publish` 而不是 `escalate` 还有一层：Phase 3 要在 `safety_gate` 之后
插 `⚡ approve_plan`，那时写库放在 `escalate` 就变成"interrupt 之前写库"，
resume 一次写两遍。**节点的位置就是那条约束，不要靠记住。**
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
    """红旗报告 → **纯 dict**。

    ★ Phase 4：格式搬到了 `redflag.dump_report` —— 反馈链也要把扫描结果
      带进 state，两处各写一遍必然漂移。
      （原来这里用 `redflag.dump_hit` 而不是 `h.__dict__.copy()`：
      `RedFlag` 带了一个 `Referral` 对象，dict() 会把它原样塞进 state，
      而 checkpoint 的寿命比代码长 —— 存对象会让老历史在重构后读不出来。）
    """
    return redflag.dump_report(report)


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
        urgent_codes=list(rf.blocking_codes),
    )
    return {
        "redflags": _rf_dump(rf),
        "verdict": verdict.model_dump(),
        # ★ 记下扫的是哪一段：publish 要用同一段算红旗的幂等键。
        #   两处各写一遍 `clarified_text or parent_text` 必然漂移，
        #   而漂移的后果很隐蔽 —— 红旗照样落库，只是幂等键算的是另一段话的哈希。
        "scanned_text": text,
        "trace": mark(state, "safety_gate"),
    }


# ================================================================== 6. 升级
def escalate(state) -> dict:
    """命中 urgent → **暂停计划生成**，并把**逐 code** 的转诊指引写进 state。

    ★ 纯计算，一个字节都不写 —— 落库在 `publish`（见本文件顶部的副作用分布）。
      这个位置不是洁癖：Phase 3 要在 `safety_gate` 之后插 `⚡ approve_plan`，
      在这里写库就变成"interrupt 之前写库"，resume 一次写两遍。

    ★ 转诊指引来自 `redflag.guidance_for`（纯函数），不再是这里硬编码的一句话。
      硬编码那版有个具体后果：命中"异食"和命中"撞头"给家长的是同一句话，
      而这两件事该找的人根本不一样。

    ★ `guidance` 仍然是**一句 str**，因为老 checkpoint 与老前端读的就是这个键
      （`TodayPage.vue` 直接渲染它）。`guidance_items` / `do_not` / `emergency`
      是并排加上去的，不是替换。
    """
    v = state.get("verdict") or {}
    report = redflag.report_from_dump(state.get("redflags") or {})
    g = redflag.guidance_for(report)
    return {
        "escalation": {
            "paused": True,
            "highest": v.get("highest", "urgent"),
            "reasons": v.get("reasons", []),
            "guidance": g["summary"],
            "guidance_items": g["items"],
            "do_not": g["do_not"],
            "emergency": g["emergency"],
        },
        "trace": mark(state, "escalate"),
    }


# ================================================================== 7. 落库
def publish(state) -> dict:
    """★ **全图唯一写业务表的地方**，且排在所有 interrupt 之后。

    写两张表：`observations`（家长今天说的这段话抽出的事件）与
    `red_flags`（这一段话命中的红旗规则）。两者判据都来自纯函数，
    而它们的**幂等是两回事**：observations 按「这段语料」判，
    红旗按「这段语料 × 这一条 code」判 —— 同一天说的两段话里
    各命中一次同一个 code，那是两次独立的报告，不该合并成一行。

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
        # ★ `redflag_triggered` 以 safety_gate 那一次扫描为准，而不是抽取时那次。
        #   两次扫描的语料不一定相同（澄清把回答拼在了后面），而这个布尔是
        #   `store.what_last_week` 数"本周有几天碰到红旗"的唯一依据 ——
        #   两处各判一次，迟早有一天它们对不上，而那是一个没有报错的数据。
        rf_dump = state.get("redflags") or {}
        if rf_dump:
            out = out.model_copy(
                update={"redflags": redflag.report_from_dump(rf_dump)})
        if reextracted:
            # 只删**上一轮那一份**语料的行。原 `_delete_today` 按 child+date 删全部，
            # 会把同一天逐条 note 写的行一起删掉 —— 那是数据丢失，不是覆盖。
            prev = state.get("previous_source") or ""
            if prev and prev != source:
                _delete_source(conn, child_id, state["date"], prev)
        ids = observe.persist(conn, child_id, out)

    return {
        "observation_ids": ids,
        "red_flag_ids": _publish_redflags(state, conn, child_id),
        "trace": mark(state, "publish"),
    }


def _publish_redflags(state, conn, child_id: str) -> list[int]:
    """★ 红旗落库。**刻意在 observations 那条 if 之外。**

    降级（`--no-llm`）时上面那个 `if degraded or not obs` 会让 observations
    一行都不写 —— 那是对的，编造的事件会污染状态机。
    但红旗必须照落：`redflag.py` 存在的第二个理由就是"模型挂了红旗照样能报"，
    把这段写进那个 if 的话，全项目最该有的那条保障就在一次"顺手重构"里没了，
    而且**没有任何东西会报错**。

    幂等靠 `store.append_red_flags` 的唯一索引（第一道是这个节点的位置）。
    """
    report = redflag.report_from_dump(state.get("redflags") or {})
    # ★ `scanned_text` 是 safety_gate 记下的那段。老 checkpoint（Phase 2 之前）没有它，
    #   于是回退到 `observed_text` —— 抽取用的是同一段。
    #   为什么不干脆空字符串：幂等键是 `sha256(这段)`，空文本会让
    #   **同一天同一个 code 的两次不同报告塌成一行**，而那正是这张表存在的意义。
    scanned = state.get("scanned_text") or state.get("observed_text") or ""
    if not report.hits:
        return []
    return store.append_red_flags(
        conn, child_id, state["date"], redflag.rows_for(report),
        source=scanned, source_kind="observation",
    )


def _source_published(conn, child_id: str, date: str, source: str) -> bool:
    """这段语料今天是否已经落过库。

    ★ Phase 4：判据搬到 `store.source_published` 了 ——
      反馈链的写库节点也要问同一句，而两处各写一遍 SQL 必然漂移。
      留这个函数只是为了本文件里读着顺，**它不再有判据**。
    """
    return store.source_published(conn, child_id, date, source)


def _delete_source(conn, child_id: str, date: str, source: str) -> None:
    """只有「澄清后重抽」这一条路才会走到这里。**是覆盖那一段，不是清这一天。**"""
    if not source:
        return
    conn.execute("DELETE FROM observations "
                 "WHERE child_id=%s AND observed_at=%s AND source=%s",
                 (child_id, date, source))
