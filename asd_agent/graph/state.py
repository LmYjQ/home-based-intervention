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

  3. **模型给证据，Python 做判定。** state 里没有任何字段的取值
     依赖"模型觉得应该是什么"。`needs_clarification` 这类判断一律由
     Python 规则给出（`validate_observation` 节点），模型只提供事件与追问文案。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


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


class PlanReview(BaseModel):
    """审批 interrupt 的载荷（Phase 3 接；此处先定义形状，Phase 0 不画这个节点）。

    ★ `Literal` 而不是 str：模型或前端传了第四种动作就在恢复那一刻被拒。
    """

    model_config = {"extra": "forbid"}

    action: Literal["approve", "edit", "reject"]
    note: str = Field(default="", max_length=500)


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
    #: escalate 节点的产物：暂停 + 转诊指引。红旗**落库**是 Phase 2 的活。
    escalation: dict[str, Any]

    # ---- 落库结果（publish 节点产出）----
    #: 写进 observations 表的行 id。**空列表是合法的**（今天没有新记录）。
    observation_ids: list[int]

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
