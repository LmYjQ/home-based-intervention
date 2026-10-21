# -*- coding: utf-8 -*-
"""
案例图的节点体
==============

    START → load_case_context → extract_new_evidence(LLM) → update_assessment
          → safety_check → urgent? ──是──→ escalate ──┐
                            │                        │
                            ↓                        │
                        detect_unknowns               │
                            │ 需要追问? ──是──→ ⚡clarify_with_parent
                            │                        │  （回抽取）
                            ↓                        ↓
                       publish_case_events ←─────────┘（唯一写库节点）

★ 副作用的分布（与现有三张图同一条纪律）
----------------------------------------
`publish_case_events` 之前的所有节点都是**纯读或纯计算**，一个字节都不写业务表；
它是**唯一**写业务表的地方，且排在**所有** interrupt 之后。

理由是实测出来的：`interrupt()` 恢复时会**重跑整个节点**
（`LANGGRAPH.md` 第二节⑤，PostgresSaver 下才验得出来）。
于是"家长答一次追问会不会写两遍"这个问题，
在结构上就不存在 —— 不是因为我们记得判重，而是因为它没有被放到能被重跑的位置上。

★ 为什么这个图**不**是现有观察链的改造
------------------------------------
观察链落 `observations`（今天发生了什么），本图落 `cases` / `case_evidence` /
`assessment_versions`（这个案例到底怎么回事）。
两张图各写各的表 → 现有观察链**一行都不用改**（REFACTOR.md §2.3.3.3）。

★ 唯一的 LLM 节点是 `extract_new_evidence`
-------------------------------------------
其余全是纯函数。这不是"少用模型"的意思，是"**判定不该由模型做**"：
能不能进 known_facts、引文对不对得上、该不该追问 —— 三条判据都是死的，
写在代码里比写在 prompt 里可靠。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import redflag                                      # noqa: E402
import store                                        # noqa: E402

from langgraph.types import interrupt              # noqa: E402

from case import assessment as case_assessment      # noqa: E402
from case import evidence as case_evidence          # noqa: E402
from case import lifecycle                          # noqa: E402

from .state import CaseClarifyAnswers, EscalationVerdict  # noqa: E402

#: 最多追问几轮。**全局上限**，不是"每天一次"。
#:
#: ★ 为什么观察链那个"只许问一次"的守卫在这里要放开
#: ---------------------------------------------------
#: 观察链的 `clarify_asked` 守卫是为了防"系统把自己没听懂的锅推给人"，
#: 代价是**一天只能问一次** —— 而一个案例是要看好几轮的。
#: 放开多轮就得换一个判据：**"这一轮有没有新证据"**
#: （见 `detect_unknowns`）—— 家长答了却没带来任何新信息，就不再问第二轮。
MAX_CLARIFY_ROUNDS = 3


def mark(state, name: str) -> list[str]:
    """把节点名追加进 trace（同 `nodes.mark`）。"""
    return list(state.get("trace") or []) + [name]


# ================================================================== 1. 读上下文
def load_case_context(state) -> dict:
    """读案例与它最新的一版评估。**纯读，一个字节都不写。**

    ★ 为什么语料在这里拼，而不是让 HTTP 层传进来
    ----------------------------------------------
    语料是**业务事实**（家长到目前为止说了什么），它住在 `cases.corpus` 里。
    HTTP 层只传「这一轮新说的话」，语料由图自己拼 ——
    这样"语料"就只有一个口径，跨进程重启也读得回来
    （checkpoint 是编排位置，不是业务事实，见铁律 #1）。

    ★ 幂等：同一段话提交两次，语料**不会**变成两遍
    ---------------------------------------------
    判据是「语料的尾巴已经就是这段话」。这比"记一个指纹"更省，
    而且它天然覆盖"家长把同一句话又发了一遍"这种最常见的重复。

    ★ 库里可能**还没有**这个案例 —— 这不是异常
    ----------------------------------------------
    阶段 2 之后，家家的回答**全部提交之前，案例一个字节都不落库**
    （见 `publish_case_events` 的说明）。所以第一轮跑到这里时，
    `cases` 表里查不到这一行是**正常路径**。
    此时 child_id / title 从 state 拿（建案例时 HTTP 层一起传进来的），
    其余一律按"空"起算 —— 这与"这个案例刚建"本来就是同一件事。
    """
    case_id = state.get("case_id")
    if not case_id:
        raise ValueError("load_case_context: 缺 case_id")

    conn = store.connect()
    row = store.get_case(conn, case_id) or {}

    latest = (store.latest_assessment(conn, case_id) if row else None) or {}
    known = list(latest.get("known_facts") or [])
    hypos = list(latest.get("hypotheses") or [])
    unknowns = list(latest.get("unknowns") or [])
    contradictions = list(latest.get("contradictions") or [])
    flags = list(latest.get("risk_flags") or [])
    # ★ state 里已有的**优先**：追问的往返在 checkpoint 里绕圈，
    #   第二轮开始时上一轮的事实还只在 state 上（还没落库）。
    known = list(state.get("known_facts") or []) or known
    hypos = list(state.get("hypotheses") or []) or hypos
    unknowns = list(state.get("unknowns") or []) or unknowns
    contradictions = list(state.get("contradictions") or []) or contradictions
    flags = list(state.get("risk_flags") or []) or flags

    text = (state.get("parent_text") or "").strip()
    corpus = row.get("corpus") or state.get("corpus") or ""
    if text and not corpus.endswith(text):
        corpus = f"{corpus}\n{text}" if corpus else text

    return {
        "case_id": case_id,
        "child_id": row.get("child_id") or state.get("child_id") or "",
        "thread_id": row.get("thread_id") or f"case:{case_id}",
        "title": row.get("title") or state.get("title") or "",
        "corpus": corpus,
        "known_facts": known,
        "hypotheses": hypos,
        "unknowns": unknowns,
        "contradictions": contradictions,
        "risk_flags": flags,
        "assessment_version": int(row.get("current_assessment_version") or 0),
        "status": row.get("status") or "assessing",
        "trace": mark(state, "load_case_context"),
    }


# ================================================================== 2. 抽取（唯一的 LLM 节点）
def extract_new_evidence(state) -> dict:
    """调 `case.evidence.extract_evidence`（一次 LLM 调用，或降级）。

    ★ 抽的是**这一轮**的话（`parent_text`），不是整段语料
    ---------------------------------------------------
    整段语料会让模型把前几轮的事实再吐一遍 ——
    而"这一轮新增了什么"正是多轮追问的判据（`new_claims`），
    混在一起就分不清"新学的"和"早就知道的"了。
    已有事实作为**上下文**进 prompt，让它别重复。

    ★ 降级时**一条都不编**（理由见 `case/evidence.py` 的模块说明）。
    """
    text = (state.get("parent_text") or "").strip()
    out = case_evidence.extract_evidence(
        text,
        known_facts=list(state.get("known_facts") or []),
        answered=list(state.get("answered_unknowns") or []),
        qa=list(state.get("qa_context") or []),
        no_llm=bool(state.get("no_llm", False)))
    return {
        "outcome": out.model_dump(mode="json"),
        "questions": list(out.questions),
        # ★ 模型这一轮的分析原话。**每轮覆盖**（最新的一次判断），
        #   但澄清记录里还留着当时那一版的 `why`（见 clarify_with_parent），
        #   所以"当初为什么问"不会被后一轮的分析冲掉。
        "thinking": out.thinking,
        "degraded": out.degraded,
        "note": out.note,
        "trace": mark(state, "extract_new_evidence"),
    }


# ================================================================== 3. 折成评估
def update_assessment(state) -> dict:
    """★ **纯 Python 判定**：这一轮的证据能不能进「我们已经知道的」。

    判据在 `case/assessment.py::fold` 里，四条硬不变量在
    `case/evidence.py::verify` 里。这个节点只是把它们串起来。
    """
    outcome = case_evidence.EvidenceOutcome.model_validate(
        state.get("outcome") or {"source_text": ""})

    current = {
        "known_facts": list(state.get("known_facts") or []),
        "hypotheses": list(state.get("hypotheses") or []),
        "unknowns": list(state.get("unknowns") or []),
        "contradictions": list(state.get("contradictions") or []),
        "risk_flags": list(state.get("risk_flags") or []),
    }
    new_assessment, fresh, n_new = case_assessment.fold(
        current, outcome, answered_unknowns=list(state.get("answered_unknowns") or []))

    # ★ 过滤掉**已经问过并且答了**的问题
    # --------------------------------
    # prompt 里已经明说了「这些不要再问」，但 7B 仍会换个说法再问一遍
    # （实测：家长刚选了「换了新老师或新环境才会这样」，
    #   下一轮又被问「是一直这样，还是换了环境会好一些？」，
    #   连选项都是刚选过的那一条）。
    #
    # 家长刚答完又被问同一个问题，比不问糟得多 ——
    # 它让"我们认真听了你说话"这件事在家长眼里当场失效。
    # 所以这条判据**不能靠模型自觉**（铁律 #2：模型给证据，Python 做判定）。
    answered = list(state.get("answered_unknowns") or [])
    questions = list(state.get("questions") or [])
    fresh_q = [
        q for q in questions
        if not any(case_assessment._same_claim(q.get("unknown", ""), a)
                   for a in answered)
    ]
    dropped = len(questions) - len(fresh_q)
    note = state.get("note") or ""
    if dropped:
        note = ((note + "  ") if note else "") + \
            f"{dropped} 个问题家长已经答过了，没有再问。"

    # ★ 证据**累积**：追问的往返在同一张图里绕圈，
    #   绕完再 publish 时 outcome 里只剩最后一轮 ——
    #   而落库的证据表必须是**全部**（见 CaseState.all_claims 的说明）。
    all_claims = list(state.get("all_claims") or [])
    seen = {c.get("claim") for c in all_claims}
    for c in fresh:
        if c.claim in seen:
            continue
        seen.add(c.claim)
        all_claims.append(c.model_dump(mode="json"))

    return {
        "known_facts": new_assessment["known_facts"],
        "hypotheses": new_assessment["hypotheses"],
        "unknowns": new_assessment["unknowns"],
        "contradictions": new_assessment["contradictions"],
        "risk_flags": new_assessment["risk_flags"],
        "questions": fresh_q,
        "all_claims": all_claims,
        "new_claims": n_new,
        "note": note,
        "trace": mark(state, "update_assessment"),
    }


# ================================================================== 4. 安全闸门
def safety_check(state) -> dict:
    """调 `redflag.scan`（11 条正则，**零 token**）。**判定一字不改。**

    ★ 与观察链的 `safety_gate` 同款：节点只把结果搬进 state，
      判定已经在 `redflag.py` 里做完了。**它不是"再判一次"** ——
      重复判定会让两处安全规则将来漂移，而安全规则的漂移是最不能容忍的一类。

    ★ 扫的是**这一轮**的话，不是整段语料
    -----------------------------------
    整段语料会让第一轮命中的红旗在第二轮再报一次 ——
    而红旗在案例层是**这个案例的持续状态**，不是每轮的事件。
    所以这里扫本轮，命中结果与已有的合并（按 code + quote 去重）。
    """
    text = state.get("parent_text") or ""
    rf = redflag.scan(text)
    verdict = EscalationVerdict(
        escalate=rf.highest == "urgent",
        reasons=[f"{h.group}（{h.code}）：{h.quote}" for h in rf.hits],
        highest=rf.highest,
        urgent_codes=list(rf.blocking_codes),
    )

    # 与已有的风险信号合并（按 code+quote 去重）——
    # 红旗是**持续状态**，第一轮命中的不能因为第二轮没提就消失。
    merged = list(state.get("risk_flags") or [])
    seen = {(f.get("code"), f.get("quote")) for f in merged}
    for row in redflag.rows_for(rf):
        key = (row.get("code"), row.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)

    return {
        "verdict": verdict.model_dump(),
        "risk_flags": merged,
        "trace": mark(state, "safety_check"),
    }


# ================================================================== 5. 要不要追问
def detect_unknowns(state) -> dict:
    """★ **纯 Python 判定**：这一轮要不要打断家长问问题。

    三条判据，**同时**成立才问：

    1. **这一轮有要问的问题**（`questions` 非空 —— 每个问题自带
       它对应的那条「还不清楚的」和几个候选答案）
    2. **这一轮有新证据**（`new_claims > 0`）
    3. 还没问满三轮（`clarify_rounds < MAX_CLARIFY_ROUNDS`）

    ★ 第 2 条是多轮追问的**命门**
    ------------------------------
    观察链那个"只许问一次"的守卫（`clarify_asked`）是为了防
    "系统把自己没听懂的锅推给人"。本图要放开多轮，就得换一个判据：
    **家长答了却没带来任何新信息 → 就不再问**。

    没有这一条，模型可以在"未知项"上原地打转：
    问 → 答（还是那些话）→ 再问同一句 → 再答 → ……
    家长会觉得系统在耍他，而这比不问糟得多。

    ★ 降级时不问
    ------------
    降级时抽不出东西，也就说不上"缺什么" ——
    打断家长只会得到"我不知道"，而那个答案不含任何新信息。
    """
    questions = list(state.get("questions") or [])
    n_new = int(state.get("new_claims") or 0)
    rounds = int(state.get("clarify_rounds") or 0)
    degraded = bool(state.get("degraded"))

    need = bool(questions) and n_new > 0 \
        and rounds < MAX_CLARIFY_ROUNDS and not degraded

    return {
        "needs_clarification": need,
        "trace": mark(state, "detect_unknowns"),
    }


# ================================================================== 6. ⚡ 追问 interrupt
def clarify_with_parent(state) -> dict:
    """⚡ 打断家长问**这一批**问题。**interrupt 之前这个节点没有任何副作用。**

    ★ 一次给全，不是一个一个问
    --------------------------
    家长面对的是**一屏问题**（每个问题带几个候选答案，可以点，也可以自己打字），
    答完**一次性提交**。
    逐个问逐个答的代价：每答一个就要重新加载页面，
    而"还有个问题没答"只有服务端知道 —— 家长很容易卡在半路。

    ★ 回答要**追加**而不是替换
    --------------------------
    与观察链的 `clarify` 同一理由：回指校验比的是「这一轮的话」，
    家长先说"不太配合"、被追问后补一句"是因为他困了"，
    语料应该是两句加起来 —— 只留后半句，回指校验会拿一个
    它从没见过的基准去核，结果是把模型**正确**的引用判成编造。

    ★ 答完回**抽取**（不是回判定）
    -------------------------------
    回答已经拼进语料了，不重抽一遍，补的信息就只是躺在 state 里没人用。
    """
    questions = list(state.get("questions") or [])
    ans: CaseClarifyAnswers = interrupt(
        {"type": "case_clarify",
         "case_id": state.get("case_id", ""),
         "questions": questions},
        response_schema=CaseClarifyAnswers,
    )
    if isinstance(ans, dict):
        ans = CaseClarifyAnswers.model_validate(ans)

    base = state.get("parent_text") or ""
    corpus = state.get("corpus") or ""

    # ★ 把**每一条**回答分别并进语料
    # ------------------------------
    # 每条回答都是家长**亲口说的一句话**（选中的选项文字，或他自己打的那句），
    # 所以它进语料、它进下一轮的抽取。
    # 分开并而不是拼成一段：下一轮抽的是「这一轮的词」，
    # 拼成一段的话 7B 会把它当一整块（见下面那段注释）。
    answered = list(state.get("answered_unknowns") or [])
    clarifications = list(state.get("clarifications") or [])
    pieces: list[str] = []
    qa: list[dict] = []

    for a in ans.answers:
        text = (a.answer or "").strip()
        if not text and a.skipped:
            text = "（这一条家长说不上来）"
        if not text:
            continue
        if not corpus.endswith(text):
            corpus = f"{corpus}\n{text}" if corpus else text
        pieces.append(text)
        if a.unknown:
            answered.append(a.unknown)
        # ★ 连着 `question` / `why` 一起记
        #   ------------------------------
        #   只存答案不存问题，就看不出这个答案是被哪句话逼出来的；
        #   只存问题不存 why，回头看就是一堆没有理由的追问 ——
        #   而家长记得最清楚的就是"你凭什么问我这个"。
        #   两者一起进评估版本，"我们是怎么一步步搞明白的"才是完整的一条线。
        q = next((x for x in questions
                  if (x.get("unknown") or "") == (a.unknown or "")), None)
        clarifications.append({
            "unknown": a.unknown,
            "question": (q or {}).get("question", ""),
            "why": (q or {}).get("why", ""),
            "answer": text,
            "skipped": bool(a.skipped),
        })
        # ★ 问题 + 答案的对照，进下一轮的 prompt（不进语料）。
        #   没有它，选中的选项文字（"换了新环境才会这样"）就是一句没有主语的话，
        #   模型只能猜它在说什么 —— 实测会漏掉一半。
        qa.append({"question": (q or {}).get("question", ""), "answer": text})

    # ★ 下一轮抽取的是**这一批回答**，不是「原话 + 回答」
    # -------------------------------------------------
    # 合并后再抽，实测 7B 会把长文本当一整块：
    # 它重新吐了一遍前面已经抽过的句子，而**回答里的新信息一个字都没提**。
    # 于是"这一轮新增了什么"这个判据（多轮追问的命门）拿到的是假的。
    #
    # 每轮抽**这一轮的词**，已有事实作为上下文进 prompt ——
    # 这正是 REFACTOR.md §2.3.3.2 写的「增量」：
    # 「输入：家长这一轮的原话 + 已有 known_facts / unknowns」。
    return {
        "parent_text": "\n".join(pieces) if pieces else base,
        "corpus": corpus,
        "answered_unknowns": answered,
        "clarifications": clarifications,
        "qa_context": qa,
        "clarify_rounds": int(state.get("clarify_rounds") or 0) + 1,
        "trace": mark(state, "clarify_with_parent"),
    }


# ================================================================== 7. 升级
def escalate(state) -> dict:
    """命中 urgent → 给转诊指引。**纯计算，一个字节都不写**（落库在 publish）。

    指引来自 `redflag.guidance_for`（纯函数），与观察链同款：
    命中"异食"和命中"撞头"该找的人根本不一样，不能给同一句话。
    """
    v = state.get("verdict") or {}
    if not v.get("escalate"):
        return {"trace": mark(state, "escalate(skipped)")}
    g = redflag.guidance_for(redflag.scan(state.get("parent_text") or ""))
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


# ================================================================== 8. 落库（唯一写库节点）
def publish_case_events(state) -> dict:
    """★ **全图唯一写业务表的地方**，且排在所有 interrupt 之后。

    ★★ 案例这一行是**这一刻才建的**
    --------------------------------
    家家的回答**全部提交之前**，`cases` 表里没有这个案例 ——
    语料、证据、评估版本、一切都只躺在 checkpoint 里。
    理由：一份写到一半的案例比没有更糟。
    它会让"我们已经知道 X"看起来像既成事实，而 X 是从一段还没说完的话里来的；
    家长中途不答了，库里就永远剩着那些半截的结论，
    下次进来看到的是"我们早就这么判断了"，而他从没确认过。

    于是落库只有一个时刻：**这一轮不再需要问家长了**。
    这一刻写进去的，是一段完整的、有出处的、能对得上原话的评估。

    ★ 幂等
    ------
    第一层：**唯一索引 + ON CONFLICT**（`uq_assess_version` / `uq_evidence_once`）。
    第二层：checkpoint 里的输入指纹（`case_nodes` 的调用方负责传）。

    ★ 为什么版本号是「当前版本 + 1」而不是别的
    -------------------------------------------
    评估版本是**这个案例的历史**："我们是怎么一步步搞明白的"。
    每跑完一轮就多一版 —— 于是"两次补充信息 → 两个版本"这件事
    在数据上直接看得出来，不用另外记一张"跑了几次"的表。

    ★ 状态由 `case.lifecycle.next_status` 判
    -----------------------------------------
    节点不自己判"该在哪个状态" —— 那是生命周期表的职责（唯一口径）。
    """
    conn = store.connect()
    case_id = state["case_id"]
    child_id = state.get("child_id") or ""
    if not child_id:
        raise ValueError("publish_case_events: 缺 child_id（案例还没落过库）")
    version = int(state.get("assessment_version") or 0) + 1

    escalated = bool((state.get("verdict") or {}).get("escalate"))
    status = lifecycle.next_status(
        has_unknowns=bool(state.get("unknowns")),
        awaiting_clarify=bool(state.get("needs_clarification")),
        escalated=escalated,
    )

    # ★ 这一行到这里才建。`ensure_case` 幂等：
    #   第二次跑（家长又补了一段）它什么也不做，
    #   而 case_id / child_id / thread_id / seq 这些**行的身份**一次也不改。
    store.ensure_case(conn, case_id, child_id,
                      title=state.get("title") or "", status=status,
                      corpus=state.get("corpus") or "")

    # ★ 落的是**累积**的证据（`all_claims`），不是最后一轮抽出来的那几条
    #   —— 追问的往返在同一张图里绕圈，绕完时 outcome 里只剩最后一轮。
    rows = [case_evidence.to_row(c) for c in (state.get("all_claims") or [])]
    ids = store.append_evidence(conn, case_id, version, rows)

    payload = {
        "known_facts": list(state.get("known_facts") or []),
        "hypotheses": list(state.get("hypotheses") or []),
        "unknowns": list(state.get("unknowns") or []),
        "contradictions": list(state.get("contradictions") or []),
        "risk_flags": list(state.get("risk_flags") or []),
        # ★ 这一版的分析原话：模型读完最后这轮话之后，
        #   自己说的「现在确定的是什么、还差什么」。
        #   少了它，这一版为什么长这样就只剩结果没有理由了。
        "thinking": state.get("thinking") or "",
        # ★ 追问往来也进这一版：问过什么、为什么问、家长怎么答的。
        #   "我们是怎么一步步搞明白的"里，"问了什么"和"怎么答的"是同一件事的两半 ——
        #   只存答案不存问题，就看不出这个答案是被哪个问题逼出来的。
        "clarifications": list(state.get("clarifications") or []),
    }
    store.append_assessment_version(conn, case_id, version, payload)

    store.update_case(conn, case_id, status=status, version=version)
    # ★ 语料是**并**进去的，不是覆盖
    #   （第二次运行时，家长在图跑的这一轮里又发了一段话 ——
    #     覆盖就把它抹掉了，而抹掉的是家长刚说的话）。
    store.merge_corpus(conn, case_id, state.get("corpus") or "")

    return {
        "assessment_version": version,
        "evidence_ids": ids,
        "status": status,
        "trace": mark(state, "publish_case_events"),
    }
