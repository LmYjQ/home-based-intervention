"""长期记忆契约。

四类记忆对应四张语义表（见计划 §4.4 / §10.2）：

- ``fact`` / ``preference`` —— L1 稳定档案，落在 ``children`` 的 JSON 列 +
  ``memory_events``；
- ``episode`` —— L2 情境观察，落在 ``observations``；
- ``hypothesis`` —— L3 待验证假设，落在 ``hypotheses``，状态默认
  ``candidate``，**不能自动升级为事实**；
- L4 干预尝试是 ``trials`` + ``feedbacks`` 的事，不在这里。

**这一份文件里有两套模型，别搞混**：

- ``MemoryCandidateDraft`` / ``MemoryExtractionResult`` 是**给模型看的**，
  字段全部必填、扁平不嵌套。可选嵌套对象会生成 ``anyOf``/``null``，
  各服务端的结构化输出支持得参差不齐；扁平最稳。
- ``MemoryCandidate`` 及以下是**代码内部用的**，带溯源信息、哈希、最终判定。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryType = Literal["fact", "preference", "episode", "hypothesis"]

# 候选去向（计划 §10.3）：
#   auto_commit        家长明确说过、有原话引用、无冲突 → 直接写
#   needs_confirmation 模型推断、敏感信息、与旧记忆冲突 → 等家长确认
#   discard            临时情绪、单次无关行为、重复内容 → 不记
DecisionType = Literal["auto_commit", "needs_confirmation", "discard"]

# 这条记忆是从哪来的。``legacy`` 是升级前存进去的裸字符串事实。
SourceKind = Literal["interaction", "feedback", "parent_correction", "legacy"]


class MemoryCandidateDraft(BaseModel):
    """**给模型看的候选记忆**。字段全部必填。

    ``extra="forbid"`` + 无默认值 = 每个字段都会进 json_schema 的
    ``required``。没有默认值是有意的：有默认值的字段允许模型整段不写，
    实测模型会直接省略（``FollowupQuestion.options`` 就踩过这个坑）。

    用不上的字段填 ``""`` / ``[]`` / ``false``，不要省略。
    """

    model_config = ConfigDict(extra="forbid")

    memory_type: MemoryType
    # 一句话说清这条记忆。事实要写具体值和场景，不要抽象成标签。
    content: str
    # **家长原话，逐字照抄**。抄不出来就不要这条候选 ——
    # 没有原话的记忆，家长没法核对，也没法知道系统为什么记这个。
    source_quote: str
    confidence: float
    # 这条是家长说出来的（false），还是你推断的（true）。
    # **推断只能产出 hypothesis**，这是硬规则。
    inference: bool
    proposed_decision: DecisionType
    reason: str
    # 明确纠正时填被纠正那条记忆的 id（从 prompt 里的 existing_* 取）。
    replaces_memory_id: str
    # 这条观察构成某个假设的反例时，填假设 id。
    contradicts_memory_ids: list[str]
    # hypothesis 的适用条件，防止"分离时容易哭"被写成"孩子就是分离焦虑"。
    scope_conditions: list[str]

    # ---- episode 的结构化字段；其它类型填空 ----
    setting: str
    antecedent: str
    observable_behavior: str
    adult_response: str
    child_response: str
    outcome: str
    # 家长说"我们试过 X"时为 true。
    is_attempt: bool


class MemoryExtractionResult(BaseModel):
    """一次抽取的完整输出。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidateDraft]
    notes: list[str]


class MemoryCandidate(BaseModel):
    """代码内部用的候选：draft + 溯源 + 哈希。

    溯源字段由**代码注入**，不指望模型填 ——
    模型连自己处理的是哪一轮都不知道。
    """

    memory_id: str = ""
    memory_type: MemoryType
    content: str
    # 入库后的状态。事实：confirmed / pending_confirmation / superseded /
    # deleted；观察：active / pending_confirmation / superseded / deleted；
    # 假设：candidate / active / weakened / superseded / deleted。
    status: str = "confirmed"

    conversation_id: str = ""
    source_interaction_id: str = ""
    source_feedback_id: str = ""
    source_turn_id: str = ""
    source_kind: SourceKind = "interaction"
    source_quote: str = ""

    confidence: float = 0.5
    inference: bool = False
    proposed_decision: DecisionType = "needs_confirmation"
    reason: str = ""

    scope_conditions: list[str] = Field(default_factory=list)
    replaces_memory_id: str = ""
    contradicts_memory_ids: list[str] = Field(default_factory=list)
    content_hash: str = ""

    # episode 结构化字段
    setting: str = ""
    antecedent: str = ""
    observable_behavior: str = ""
    adult_response: str = ""
    child_response: str = ""
    outcome: str = ""
    is_attempt: bool = False

    def observation_text(self) -> str:
        """把结构化观察拼成一句可读的话（列表展示用）。"""
        parts = [self.observable_behavior, self.antecedent, self.outcome]
        return "；".join(p.strip() for p in parts if p and p.strip()) or self.content


# 判定之后具体做什么。
#
#   create              新建一条记忆
#   supersede           新建一条 + 把 target 标成 superseded
#   link_contradiction  候选本身是重复的观察，但它带来**新的反例**：
#                       不新建，只把已有那条观察挂到假设上
#   none                不写（discard 时用）
#
# **为什么要单独一个字段**：光看 ``decision`` 分不清"新建"和"改写旧记录"，
# commit 层就得靠 reason 字符串猜 —— 那是会让重构悄悄出错的写法。
MemoryAction = Literal["create", "supersede", "link_contradiction", "none"]


class MemoryDecision(BaseModel):
    """合并阶段对一条候选的最终判定。"""

    candidate: MemoryCandidate
    decision: DecisionType
    action: MemoryAction = "create"
    reason: str = ""
    # 被取代 / 被挂反例 / 重复命中的那条已有记忆
    target_memory_id: str = ""
    # 这条观察构成哪几条假设的反例
    contradiction_ids: list[str] = Field(default_factory=list)


class MemoryUpdateResult(BaseModel):
    """一轮记忆写入的结果。

    ``superseded_facts`` 和 ``context_patch`` 是给**当前这一轮**用的：
    纠正发生后，本轮的 ``parse_parent_input`` 必须知道旧值已经作废，
    不能把它接着并进 ``case_summary``。
    """

    created: list[MemoryCandidate] = Field(default_factory=list)
    updated: list[MemoryCandidate] = Field(default_factory=list)
    pending_confirmation: list[MemoryCandidate] = Field(default_factory=list)
    skipped: list[MemoryCandidate] = Field(default_factory=list)
    decisions: list[MemoryDecision] = Field(default_factory=list)

    # 本轮被作废的旧事实原文。只给代码用，**不进任何 prompt**。
    superseded_facts: list[str] = Field(default_factory=list)
    # 写完之后重新算出来的 child_context 片段
    context_patch: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def merge_notes(self, extra: list[str]) -> "MemoryUpdateResult":
        """把上游的说明并进来（节点之间传递用）。"""
        self.notes = list(dict.fromkeys([*self.notes, *extra]))
        return self
