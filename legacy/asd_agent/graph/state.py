# -*- coding: utf-8 -*-
"""
图的 state
==========

**为什么字段这么少。** 技术方案 §5.3 列了二十多个字段，那是"照抄方案"的结果，
不是这张图的形状。这里按**现有实体**收敛：一个字段如果今天没有对应的
Pydantic 模型 / 表 / 纯函数能填它，它就不该出现在 state 里 ——
state 里躺着一个永远为空的字段，比没有这个字段更糟：
读代码的人会以为那条路已经通了。

★ 三条硬约束（写进这个文件是因为它们要在**定义层**就被看见，
  而不是等到某个节点写库时才发现）：

  1. `interrupt()` 恢复时会**重跑整个节点**。所以节点在 `interrupt()` 之前
     不得有任何不可重复的副作用 —— 写库、发消息、通知都不行。
     图的结构因此被排成：`… 所有纯计算 … → interrupt → … 落库 …`。
     `trace` 字段刻意用 `operator.add` 累加，就是为了把"重跑"显式记下来
     （见下面 trace 的注释），而不是假装它没发生。

  2. **Checkpoint 不替代业务表。** state 里存的是"这次编排走到哪、
     人给了什么回答"，而 `sessions` / `observations` / `skill_state` /
     `daily_summary` 仍然 append-only。`publish` 节点是**唯一**写业务表的地方，
     它排在所有 interrupt 之后 —— 于是"重复提交会不会写两遍"这个问题
     在结构上就不存在。
     Phase 2 让它写**两张**表（`observations` + `red_flags`），但仍是同一个节点：
     将来 Phase 3 在 `safety_gate` 之后插 `⚡ approve_plan`，
     分成两个写库节点就意味着其中一个会落在 interrupt 之前。

  3. **模型给证据，Python 做判定。** state 里没有任何字段的取值
     依赖"模型觉得应该是什么"。`needs_clarification` 这类判断一律由
     Python 规则给出（`validate_observation` 节点），模型只提供事件与追问文案。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


# ================================================================== interrupt 载荷
class ClarifyAnswer(BaseModel):
    """★ 家长对澄清 interrupt 的回答。

    **用 Pydantic 而不是裸 dict**（LangGraph 1.x 的
    `interrupt(value, *, response_schema=...)` 直接吃这个类）：
    前端提交的操作必须经过**服务端** schema 校验，
    「approve / edit / reject」这三个词一旦多一个少一个，
    应该在恢复那一刻就炸，而不是顺着流程流到落库那一步才炸。
    """

    model_config = {"extra": "forbid"}

    answer: str = Field(
        default="", max_length=500,
        description="家长对追问的回答原话。允许为空 —— "
                    "「我想不起来」本身也是有效信息，"
                    "它让流程带着「确实不知道」继续，而不是被编造填满。")
    skip: bool = Field(
        default=False,
        description="家长不想答、想跳过。跳过不等于澄清成功，"
                    "下游必须能区分「答了」与「没答」。")


class EscalationVerdict(BaseModel):
    """`safety_gate` 的判定结果。**纯 Python 从 `redflag.scan` 来的，不是模型说的。**"""

    escalate: bool = Field(description="是否需要暂停计划生成、转人工/转诊")
    reasons: list[str] = Field(
        default_factory=list, description="触发升级的 code + 引文，给人看的中文")
    highest: str = Field(default="", description="最高严重度 urgent/high/watch/空")
    #: 触发了升级的那些 code（= `redflag.scan` 的 blocking_codes，**纯 Python 来的**）。
    #: 单列一份是因为 `reasons` 是给人看的中文整句，
    #: 前端要按 code 取转诊指引时不该去解析中文。
    urgent_codes: list[str] = Field(default_factory=list)


class PlanReview(BaseModel):
    """审批 interrupt 的载荷。

    ★ `Literal` 而不是 str：前端传了第四种动作就在**恢复那一刻**被拒
    （`interrupt(..., response_schema=PlanReview)`），而不是顺着流程
    流到落库才炸。这正是技术方案 §5.4「前端提交的操作必须经过服务端
    Schema 校验」的落点。

    ★ `note` 允许为空但 `action` 必填 —— 家长点"批准"时不该被要求写理由。
      `reject` 时理由有意义，但**不强制**：强制了就有一批家长卡在按钮上，
      而这批人恰恰是最需要被听到的（拒绝而不说原因，正是
      "这个安排我们做不到"）。理由记不记得住由 `freeze_plan` 决定。
    """

    model_config = {"extra": "forbid"}

    action: Literal["approve", "edit", "reject"]
    note: str = Field(default="", max_length=500)


# ================================================================== 计划链 state
class PlanState(TypedDict, total=False):
    """**独立的** state，与 `InterventionState` 分开。

    ★ 为什么不用同一个 TypedDict：
      两条链的字段几乎不重叠，而共用一个会让"这个字段是谁写的"
      变成一个每次都要查的问题。更实际的后果是 checkpoint 体积 ——
      观察链的 observation / redflags 会被原样存进每一次计划 run 的
      checkpoint，而计划链一次 run 的历史没人会翻。
      两张 state 的代价是 `mark()` 之类的助手要写两遍（真的只多了一行）。

    `total=False` 的理由同 `InterventionState`：节点只写自己产出的那几项。
    """

    # ---- 身份与坐标 ----
    child_id: str
    date: str
    profile_id: int | None

    #: ★ 由图入口调 `store.plan_paused(conn, child_id)` 算好后传进来。
    #: 计划链里**只有这一个暂停口径**（LANGGRAPH.md 第四节之三），
    #: 节点体不自己查库、也不自己判断，见 plan_nodes.generate_plan 的注释。
    paused: bool

    #: `loop.build_request(...)` 的产物，以**纯 dict** 存
    #: （`activities.request_to_dict`）。**唯一来源是那个函数** ——
    #: server.py 的 `_build_request` 也是它的薄封装，
    #: 全项目只有一处"画像 + 状态机 + 记忆库 → 检索请求"的装配。
    #: ★ 存 dict 而不是对象：checkpointer 用 msgpack，
    #:   dataclass / Pydantic 属于未注册类型，实测已打出
    #:   "will be blocked in a future version" 的警告。
    #:   节点用 `plan_nodes._req()` 现还原。
    plan_request: dict[str, Any]

    # ---- 检索与草稿 ----
    candidate_ids: list[str]
    excluded: list[dict[str, Any]]
    plan_activity_ids: list[str]
    #: ★ 今天排的那几项，**每项一份完整的 `Activity` dict**（含 `kind` / `origin`）。
    #:
    #:   为什么不能只存 `plan_activity_ids`：
    #:   生成项（`gen:` 前缀）**不在活动库里** —— 只存 id 的话，
    #:   下游（卡片 / 反馈 / 状态机归属）拿不到它的步骤、材料、目标技能，
    #:   而"从活动库按 id 反查"这条路对生成项**查不到**。
    #:
    #:   为什么存 dict 而不是对象：checkpoint 用 msgpack 序列化，
    #:   Pydantic 实例属于未注册类型（见 `plan_item.to_item` 的说明）。
    plan_items: list[dict[str, Any]]
    plan_total_minutes: int
    draft_fingerprint: dict[str, Any]
    #: ★ **当前生效的那一版**的指纹，由图入口从 `plans` 表读回来。
    #: 比"从活动库重算"可靠：活动库改了一条活动的难度，
    #: 会让一份半年前的计划凭空变成"有变更"，而那次变更没人提过。
    current_fingerprint: dict[str, Any] | None

    # ---- 审批 ----
    needs_review: bool
    changes: list[str]
    review: dict[str, Any]          # PlanReview.model_dump()
    #: "改一下"已经用了几轮。循环出口守卫，见 plan_nodes.approve_plan。
    edit_loop: int
    #: 家长已经在这个 thread 上批过一次（哪怕那次批的是"改一下"）。
    #: 之后重新生成的那一版不再打断 —— 只由 approve_plan 写。
    already_reviewed: bool

    # ---- 冻结与落库 ----
    plan_status: str
    plan_action: str
    plan_note: str
    plan_paused: bool
    plan_id: int
    plan_version: int

    trace: list[str]


# ================================================================== 反馈链 state
class RunState(TypedDict, total=False):
    """**第三张**图（反馈链）的 state。与观察链、计划链各自独立。

    ★ 为什么又是独立的一张：
      触发时机不同（观察链=家长写了一段话；计划链=打开今天/重新生成；
      反馈链=家长填完今天的逐条反馈），而 thread_id 会打架 ——
      同 thread 的 `invoke` 会**接着上一次跑**（LANGGRAPH.md ③ 的实测）。
      所以三张图各有各的前缀：`{child}:{date}` / `plan:` / `run:`。

    `total=False` 的理由同前两张：节点只写自己产出的那几项。
    """

    # ---- 身份与坐标 ----
    child_id: str
    date: str
    no_llm: bool
    #: ★ 「一天的执行」的身份 = thread_id = `store.day_run_id(child, date)`。
    #:   它同时是落库三张表时幂等键的一半（sessions / skill_state /
    #:   daily_summary），也是"重跑一次同一天"能被认出来的凭据。
    run_id: str

    # ---- 输入 ----
    #: 家长点的那些条（每条一个 dict：activity_id / outcome /
    #: prompt_level / steps_done / note）。
    feedbacks: list[dict[str, Any]]
    #: ★ 这份输入的指纹（sha256）。**幂等守卫用**：
    #:   同一天提交**一模一样**的内容 → 指纹相同 → 直接返回已存的那次结果，
    #:   不重跑图、不重付 LLM、不重写库。
    #:   而内容改了（家长更正了）→ 指纹不同 → 重跑，落库那半是幂等的，
    #:   于是更正会**覆盖**当天那一条，而不是多出一行。
    input_digest: str

    #: ★ 由图入口调 `store.plan_paused(conn, child_id)` 算好后传进来。
    #:   反馈链里**只有这一个暂停口径**（LANGGRAPH.md 第四节之三），
    #:   节点体不自己查库、也不自己判断 —— 见 run_nodes.next_action。
    paused: bool

    # ---- 计划关联 ----
    #: 当天生效的那一版计划（`plans` 表）。练习记录挂在它下面 ——
    #: "孩子做的是哪一版安排下的"这句话要有落点。
    plan_id: int | None
    plan_version: int

    # ---- record_execution 的产物（纯计算，一行都没写）----
    #: 家长点的那些条（SessionRecord 的 dump）。
    sessions: list[dict[str, Any]]
    #: 与 sessions 一一对应的元信息：note（落库用的那句）/
    #: raw_note（家长原话，红旗扫描用）/ prompt_level / steps_done /
    #: source / seq。
    session_rows: list[dict[str, Any]]
    #: 从每条 note 里抽出来的事件（ObservationOutcome 的 dump）。
    outcomes: list[dict[str, Any]]
    #: 从 note 里桥接出来的抗拒（RefusalHit 的 dump）。
    refusal_hits: list[dict[str, Any]]
    #: 逐条扫描家长原话得到的红旗报告：每项 {"source": 原话, "report": dump}。
    redflag_reports: list[dict[str, Any]]

    # ---- analyze_outcome 的产物 ----
    #: 当天的结果统计（纯 Python 算的，不调模型）。
    outcome_stats: dict[str, Any]
    #: 日结（`summary.DailySummary` 的 dump）。降级时是兜底那份。
    daily_summary: dict[str, Any]

    # ---- next_action 的产物 ----
    #: 状态转移（`statemachine.StateTransition` 的 dump）。
    transitions: list[dict[str, Any]]
    #: continue / adjust / pause / escalate —— **纯 Python 判的**。
    next_action: str
    #: 给家长看的那句人话。
    action_reason: str

    # ---- 落库结果（apply_transitions 节点产出）----
    session_ids: list[int]
    observation_ids: list[int]
    red_flag_ids: list[int]
    state_ids: list[int]
    summary_id: int

    trace: list[str]


# ================================================================== state
class InterventionState(TypedDict, total=False):
    """一天的干预流程。

    `total=False`：所有字段可选。图从 checkpoint 恢复时，
    之前那个节点写过的字段由 LangGraph 自己回填，节点体只写自己产出的那几项。
    """

    # ---- 身份与坐标（图的入口就有，之后每个节点都靠它定位）----
    child_id: str
    date: str
    #: 一条 graph 调用是否允许调模型。`--no-llm` 与 ASD_NO_LLM 走同一个开关。
    no_llm: bool

    # ---- 输入：家长今天说的话 ----
    #: 「其他观察」框，或逐条反馈之外的整段输入
    parent_text: str

    # ---- 观察链产物 ----
    #: `observe.extract_observations` 的 `ObservationOutcome`，
    #: 以 `model_dump()` 存 —— **不是对象本身**。
    #: 存对象会让 checkpoint 反序列化时要求那个类在当前 import 图里可解析，
    #: 而 checkpoint 的生命周期比代码长；存 dict 则历史永远读得出来。
    observation: dict[str, Any]
    #: 抽取时**用的那段文本**。抽取的幂等判据是「语料变没变」，
    #: 而不是「observation 字段在不在」—— 澄清之后 observation 还在，
    #: 但语料确实变了，那一次必须重抽。
    observed_text: str
    #: 澄清重抽**之前**那段语料（= 上一轮已落库的 source）。
    #: `publish` 澄清后要删旧行，删的是「上一轮那一份」而不是「这一天全部」——
    #: 同一天 `feedback.apply_feedbacks` 还会从逐条活动的 note 写别的行，
    #: 按日期删会把那些一起删掉（Phase 1 修掉的两个潜伏 bug 之一）。
    previous_source: str
    #: 事件条数（方便路由与展示，不必每次解 dict）
    n_events: int
    #: 模型给出的追问文案（**文案是证据**；该不该问由 Python 判）
    clarifying_question: str

    # ---- 澄清 interrupt 的往返 ----
    needs_clarification: bool
    clarification_answer: str
    #: 澄清**已经问过一次**。这是 `clarify → extract → validate` 这个环的
    #: 唯一出口守卫 —— 少了它，判据重跑一遍仍为真就会无限追问同一个问题。
    clarify_asked: bool
    #: 家长答完之后回去重跑的那段文本。**这是"生活片段一等输入"的落点** ——
    #: 回答不是覆盖原话，而是追加进去，回指校验的基准始终是完整语料。
    clarified_text: str

    # ---- 安全链 ----
    #: `redflag.scan` 的报告，以 dict 存（同上，不存对象）
    redflags: dict[str, Any]
    verdict: dict[str, Any]        # EscalationVerdict.model_dump()
    #: escalate 节点的产物：暂停 + 转诊指引（逐 code 一条，不揉成一段）。
    #: 里面 `guidance` 仍然是**一句 str** —— 老 checkpoint 与老前端读的就是这个键，
    #: 换成结构会打断它们；`guidance_items` / `do_not` / `emergency` 是并排加的。
    escalation: dict[str, Any]
    #: ★ `safety_gate` **实际扫描的那段文本**。
    #: 单列一个字段是因为 publish 要用它算红旗的幂等键（见 store.append_red_flags），
    #: 而两处各写一遍 `clarified_text or parent_text` 必然漂移 ——
    #: 漂移的后果很隐蔽：红旗照样落库，只是幂等键算的是另一段话的哈希。
    scanned_text: str

    # ---- 落库结果（publish 节点产出）----
    #: 写进 observations 表的行 id。**空列表是合法的**（今天没有新记录）。
    observation_ids: list[int]
    #: 写进 red_flags 表的行 id，**只含本次新增**。重跑一次是 `[]`，
    #: 前端要显示「今天记下了 N 条红旗」时，那必须是真的新增而不是库里总数。
    red_flag_ids: list[int]

    # ---- 计划链（Phase 3 接）----
    #: 今天的候选活动 id，按分数降序
    candidate_ids: list[str]
    draft_plan: dict[str, Any]
    review: dict[str, Any]         # PlanReview.model_dump()

    # ---- 反馈链（Phase 4 接）----
    feedbacks: list[dict[str, Any]]
    transitions: list[dict[str, Any]]

    # ---- 编排自身的轨迹 ----
    #: 每个节点**完成**时把自己名字追加进完整的新列表（见 `nodes.mark`）。
    #: 用途是调试与自检时看清"这次到底走了哪几个节点"。
    #: 它**不能**用来观察 `interrupt()` 的重跑 —— 见 `selfcheck._probe_rerun`：
    #: 重跑的证据是"节点在 interrupt 之前做过的副作用会被做两遍"，
    #: 而生产节点在 interrupt 之前不做任何事，所以 state 里本来就看不到。
    trace: list[str]


# ================================================================== config 助手
def thread_id_for(child_id: str, date: str) -> str:
    """`f"{child_id}:{date}"` —— 每天一个 thread，图活一周。

    ★ 天然唯一，而 `thread_id` **不可复用**：同名 run 的 checkpoint 会累积
    （实测 4 → 8 → 10，LANGGRAPH.md ④）。用日期做后缀正是为了让每一天
    天然是一个新 thread，而不是第二次写同一天。
    """
    return f"{child_id}:{date}"


def thread_config(child_id: str, date: str) -> dict:
    return {"configurable": {"thread_id": thread_id_for(child_id, date)}}


def fork_config(child_id: str, date: str, checkpoint_id: str) -> dict:
    """★ 分叉**必须换 thread_id** —— 这是「分叉不影响真实历史」的技术前提。

    实测（LANGGRAPH.md ③）：
      · 同 thread `invoke(..., past.config)` → 状态不变，但原 thread 的
        checkpoint 被追加污染（4 → 6）
      · 同 thread `update_state(past.config, ...)` → **原 thread 的状态被改写**
      · 换 thread_id + 历史 state 作输入 → 完全隔离

    所以 fork 的 thread_id 带上被分叉的那个 checkpoint 的 id。
    """
    return {"configurable": {
        "thread_id": f"{child_id}:{date}#fork@{checkpoint_id}",
    }}


#: 计划链的 thread 前缀。与 `draft.DRAFT_PREFIX` 同一个理由：
#: **靠命名空间保证隔离，不是在代码里靠自觉 if。**
PLAN_PREFIX = "plan:"


def plan_thread_id(child_id: str, date: str) -> str:
    """`plan:{child_id}:{date}`。

    ★ 为什么不能与观察链共用 `{child_id}:{date}`：
      实测（LANGGRAPH.md ③）同 thread 的 `invoke` 会**接着上一次跑**，
      于是家长写一段观察会把计划链的 run 续下去（或者反过来）。
      前缀是这个问题的结构性答案，不是命名偏好。
    """
    return f"{PLAN_PREFIX}{child_id}:{date}"


def plan_config(child_id: str, date: str) -> dict:
    return {"configurable": {"thread_id": plan_thread_id(child_id, date)}}


#: 反馈链的 thread 前缀。**它就是 `store.day_run_id`** ——
#: 「一天的执行」既住在 checkpoint 里（thread），也写在落库的行上（run_id），
#: 让它们是同一个字符串，"重跑一次同一天"在两处就是同一个身份，
#: 幂等键于是自然成立（见 store.day_run_id 的注释）。
def run_thread_id(child_id: str, date: str) -> str:
    import store
    return store.day_run_id(child_id, date)


def run_config(child_id: str, date: str) -> dict:
    return {"configurable": {"thread_id": run_thread_id(child_id, date)}}


class CaseClarifyAnswer(BaseModel):
    """★ 家长对**一个问题**的回答。

    `unknown` 是回指 —— 它把这条回答挂回评估里那条「还不清楚的」，
    家长答完那一项就该销掉（`assessment.fold` 用它精确销账）。

    `answer` 是**家长说的话**（选中的选项文字，或者他自己打的那句）。
    它会被**追加进语料** —— 也就是说，选了一个选项，
    等于家长亲口说了那句话。
    """

    model_config = ConfigDict(extra="forbid")

    unknown: str = Field(default="", max_length=200,
                         description="这条回答对应哪个未知项")
    answer: str = Field(default="", max_length=1000,
                        description="家长的回答原话。允许为空 —— "
                                    "「我想不起来」本身也是有效信息")
    skipped: bool = Field(default=False,
                          description="家长明确跳过这一条（说不上来）")


class CaseClarifyAnswers(BaseModel):
    """★ 一次提交**全部**问题的回答。

    ★ 为什么是"一次提交全部"
    ----------------------
    家长面对的是**一屏问题**，不是一个一个被问。
    逐个问、逐个答的代价是：每答一个就要重新加载页面，
    而"还有个问题没答"这件事只有服务端知道 —— 于是家长很容易卡在半路。
    一次提交全部，交互上就是"填完一张表再交"。

    ★ `answers` 可以是空的
    -------------------
    家长可能一个问题都不想答（或者页面刷新了想跳过）。
    空列表是合法的，图会照常往下走。
    """

    model_config = ConfigDict(extra="forbid")

    answers: list[CaseClarifyAnswer] = Field(
        default_factory=list, max_length=8,
        description="家长对每一个问题的回答，**顺序与问题一致**")


# ================================================================== 案例（阶段 2）
class CaseState(TypedDict, total=False):
    """一个案例的评估流程。

    ★ 与 `InterventionState` 的区别只有一处，但很要紧：
      观察链的 state 是「**一天**的」，案例的 state 是「**长期**的」。
      所以这里多了一个 `corpus`（累积语料）——
      观察链每天从零开始，案例不能。

    ★ 字段少是有意的（沿用本文件开头的约定）：
      一个字段如果今天没有对应的模型 / 表 / 纯函数能填它，它就不该出现在 state 里。
    """

    # ---- 身份 ----
    case_id: str
    child_id: str
    thread_id: str
    title: str
    no_llm: bool

    # ---- 输入与语料 ----
    #: 家长**这一轮**说的话。
    parent_text: str
    #: 累积语料（这个案例到目前为止家长说的全部话）。
    #: ★ 它也落在 `cases.corpus` 里 —— checkpoint 是编排位置，不是业务事实（铁律 #1），
    #:   而"这个案例家长说了什么"要能跨进程重启读回来。
    corpus: str

    # ---- 评估（`Assessment` 的 dict，**纯 dict**）----
    known_facts: list[str]
    hypotheses: list[str]
    unknowns: list[str]
    contradictions: list[dict[str, Any]]
    risk_flags: list[dict[str, Any]]

    # ---- 本轮抽出的证据 ----
    #: `EvidenceOutcome` 的 dict（**纯 dict**）。checkpoint 只存纯 dict（铁律 #9）。
    #: 整个 outcome 一起存，是因为"证据 / 未知 / 矛盾 / 追问"是**一次抽取的产物**，
    #: 拆成四个字段存就得保证它们永远同步 —— 而同步是靠人记的。
    outcome: dict[str, Any]
    #: ★ **累积**的证据（这个案例到目前为止的每一条）。
    #:
    #:   为什么不能只留最后一轮
    #:   ----------------------
    #:   追问的往返是在**同一张图**里绕圈的：`clarify → extract → … → publish`。
    #:   绕完两圈再 publish 时，`outcome` 里只剩**最后一轮**抽出来的东西，
    #:   而前几轮抽出的事实已经进了 `known_facts`（字符串）。
    #:   于是落库的证据表只有最后那几条，而评估版本里有全部 ——
    #:   **证据表与评估对不上**，而证据表正是"这句话从哪来"的出处。
    #:
    #:   所以证据在这里**累积**：每轮的新证据都往里追加，
    #:   publish 落的是**全部**（唯一索引按 claim_hash 去重）。
    all_claims: list[dict[str, Any]]
    #: ★ 模型这一轮的**分析**（`EvidenceBatch.thinking`）。
    #:
    #:   为什么它是一个字段而不是日志
    #:   ------------------------------
    #:   追问一旦发生，家长面前就凭空多出几个问题 ——
    #:   而"我明明已经说了这么多"是这时候最典型的反应。
    #:   把模型自己那句「你现在确定的是什么、还差什么」摆出来，
    #:   问题就从"凭什么问我"变成"哦，确实没提"。
    #:   它也随评估版本一起落库，所以几个月后回看这一版为什么长这样，仍然说得清。
    thinking: str
    #: 这一轮**新**出现的证据条数。
    #: ★ 多轮追问的判据（`detect_unknowns`）：家长回答了却没带来新信息 → 不再问。
    new_claims: int
    #: ★ 这一轮要问家长的**全部**问题（`ClarifyQuestion` 的 dict）。
    #: 一次给全，不是给一个 —— 理由见 `case/models.py::ClarifyQuestion`。
    questions: list[dict[str, Any]]
    #: ★ 已经**问过并且家长答了**的未知项。
    #: 销账用（`assessment.fold`），也是给模型的"别再问这个"清单。
    answered_unknowns: list[str]
    #: 已经发生过的追问往来
    #: （`{unknown, question, why, answer, skipped}`），
    #: 落进评估版本的历史里 —— "我们问过什么、为什么问、家长怎么答的"要能查。
    clarifications: list[dict[str, Any]]
    #: ★ 本轮回答的「问题 + 答案」对照（`{question, answer}`）。
    #: 家长选的选项文字是没有主语的（"换了新环境才会这样"），
    #: 抽取时要**连着问题一起**给它，它才能把答案还原成一句完整的话。
    #: ★ 只进 prompt，**不进语料** —— 语料里只有家长说过的话。
    qa_context: list[dict[str, Any]]

    # ---- 追问的往返 ----
    needs_clarification: bool
    clarify_rounds: int
    #: 家长对追问的回答原话（展示用）。
    clarification_answer: str

    # ---- 安全 ----
    verdict: dict[str, Any]
    escalation: dict[str, Any]

    # ---- 产出 ----
    assessment_version: int
    evidence_ids: list[int]
    status: str
    degraded: bool
    note: str

    trace: list[str]


#: 案例链的 thread 前缀。**它就是 `case_id` 前面加一个前缀** ——
#: 案例的 thread 是**长期**的（不像观察链按天开新的），
#: 所以它必须与 `{child}:{date}` 那个命名空间分开，否则会互相顶掉。
CASE_PREFIX = "case:"


def case_thread_id(case_id: str) -> str:
    """`case:{case_id}`。

    ★ 注意 `case_id` 本身已经是 `case:{child}:{seq}`，
      所以最终长这样：`case:case:demo-001:1`。
      看起来重复，但它是**故意的**：
      `cases.thread_id` 这一列存的就是它，而"thread_id 以 case: 开头"
      是路由层判断"这是个案例 thread"的唯一依据 ——
      与 `plan:` / `run:` / `draft:` 同一套命名空间隔离的办法。
    """
    return f"{CASE_PREFIX}{case_id}"


def case_config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_thread_id(case_id)}}
