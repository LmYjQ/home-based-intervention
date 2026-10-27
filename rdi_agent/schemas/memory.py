"""长期记忆契约（两层设计）。

记忆只有两层：

1. **拆句** —— 家长的一段话拆成几个**短句**，每句只讲一件事。
2. **分类** —— 每个短句归一个类目（见 ``memory/categories.py``）。

同类合并之后，每个类目一条**小结**，存在 ``children.memory_summaries_json``。
**进 prompt 的是小结，短句只留在库里给家长看**。

**为什么不再分事实 / 观察 / 假设**：那三种类型让"这条记忆怎么用"
取决于它被归成哪一类，而不是取决于它说了什么。实测抽取那一步
模型几乎总是判成"事实"，另外两层永远是空的 —— 复杂度没换来东西。

现在所有短句是同一种东西，区别只有两个开关：

- ``is_preference`` —— 家长希望怎样 / 不希望怎样。**不参与合并**，
  直接进 prompt 当硬约束。
- ``inference`` —— 系统自己推断的。**不参与合并**（合并会把猜测
  写成事实），进 prompt 时标成"未验证的推测"。

**这一份文件里有两套模型，别搞混**：

- ``MemoryCandidateDraft`` / ``MemoryExtractionResult`` 是**给模型看的**，
  字段全部必填、扁平不嵌套。可选嵌套对象会生成 ``anyOf``/``null``，
  各服务端的结构化输出支持得参差不齐；扁平最稳。
- ``MemoryCandidate`` 及以下是**代码内部用的**，带溯源信息、哈希、最终判定。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 候选去向：
#   auto_commit   家长明确说过、有原话引用、无冲突 → 直接写
#   discard       临时情绪、单次无关行为、重复内容 → 不记
#
# 2026-10-27 起没有"等家长点确认"这一档：记忆是自动维护的，
# 家长想推翻某条记忆，在对话里说"不是这样"就行。
DecisionType = Literal["auto_commit", "discard"]

# 这条记忆是从哪来的。``legacy`` 是升级前存进去的裸字符串事实。
SourceKind = Literal["interaction", "feedback", "parent_correction", "legacy"]


class MemoryCandidateDraft(BaseModel):
    """**给模型看的候选短句**。字段全部必填。

    ``extra="forbid"`` + 无默认值 = 每个字段都会进 json_schema 的
    ``required``。没有默认值是有意的：有默认值的字段允许模型整段不写，
    实测模型会直接省略（``FollowupQuestion.options`` 就踩过这个坑）。

    用不上的字段填 ``""`` / ``[]`` / ``false``，不要省略。
    """

    model_config = ConfigDict(extra="forbid")

    # **一句短句，只讲一件事**。
    # 写具体的行为和场景，不要写成"孩子总是……"这种标签。
    content: str
    # 这条属于哪一类（见 ``memory/categories.py``）。
    #
    # **归错类不丢弃这条记忆** —— 类目只影响"怎么合、怎么展示"，
    # 不影响这条记忆本身对不对。认不出来就填 "other"。
    category: str
    # 家长希望怎样 / 不希望怎样（"我不希望他被批评"）。
    # 这类是**硬约束**，不参与合并，直接进 prompt。
    is_preference: bool
    # 这条是**你推断的**，家长没这么说（inference=true）。
    # 推断**照记**，但标出来，不参与合并。
    inference: bool
    # **家长原话，逐字照抄**。抄不出来就不要这条候选 ——
    # 没有原话的记忆，家长没法核对，也没法知道系统为什么记这个。
    #
    # 推断（inference=true）例外：家长本来就没说过这话。
    source_quote: str
    confidence: float
    proposed_decision: DecisionType
    reason: str
    # 明确纠正时填被纠正那条短句的 id（从 prompt 里的 existing_items 取）。
    replaces_memory_id: str
    # 成立条件（"在幼儿园门口""每次交接时"）。
    scope_conditions: list[str]


class MemoryExtractionResult(BaseModel):
    """一次抽取的完整输出。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidateDraft]
    notes: list[str]


# ---- 合并（归纳）阶段的契约 ----
#
# 合并和抽取是两种不同的模型活：
#   抽取 —— 把家长这段话拆成短句，并给每句归类
#   合并 —— 把同一个类目下的多条短句压成**一句话的现状**
#
# 所以给模型的契约也分开：合并的输入是"一批已归类的短句"，
# 输出是"每个类目一条小结"。一批一次调用，不按类目分次。
#
# **为什么不让模型自己挑类目**：类目已经定死在
# ``memory/categories.py`` 里，模型只负责"这几个类目各自的现状是什么"。


class SummaryDraft(BaseModel):
    """**给模型看的**类目小结草稿。字段全部必填。

    同样是有意不给默认值：模型会整段省略没给默认值的字段。
    """

    model_config = ConfigDict(extra="forbid")

    # 必须是 ``memory/categories.py`` 里定义的类目名。
    category: str
    # 一句话说清这个类目下的现状。不超过 80 字。
    content: str
    # 成立条件 —— "在幼儿园门口""连续三天以上"。
    #
    # **必填**：没有条件的归纳会被读成"孩子就是这样"，
    # 而 3 次观察不足以支撑这种断言。
    scope_conditions: list[str]
    # 参与归纳的短句 id，**必须逐字来自输入**。
    # 模型自己编 id 会让溯源链断掉 —— 家长点开小结就看不到原文了。
    derived_from: list[str]
    # 这条小结有多确定：low / medium / high。
    confidence_level: str
    # 这次归纳的把握来自哪几点（模型自己写的理由，给家长看）。
    notes: list[str]


class ConsolidationResult(BaseModel):
    """一次合并的完整输出。"""

    model_config = ConfigDict(extra="forbid")

    summaries: list[SummaryDraft]
    notes: list[str]


class MemoryCandidate(BaseModel):
    """代码内部用的候选：draft + 溯源 + 哈希。

    溯源字段由**代码注入**，不指望模型填 ——
    模型连自己处理的是哪一轮都不知道。
    """

    memory_id: str = ""
    content: str
    # 这条属于哪一类（见 ``memory/categories.py``）。
    category: str = "other"
    # 家长希望怎样 / 不希望怎样。**不参与合并**，直接进 prompt。
    is_preference: bool = False
    # 系统推断的。**不参与合并**，进 prompt 时标成推测。
    inference: bool = False
    # 入库后的状态：active / superseded / deleted。
    status: str = "active"

    conversation_id: str = ""
    source_interaction_id: str = ""
    source_feedback_id: str = ""
    source_turn_id: str = ""
    source_kind: SourceKind = "interaction"
    source_quote: str = ""

    confidence: float = 0.5
    # 2026-10-27：默认从 ``needs_confirmation`` 改成 ``auto_commit``。
    #
    # 记忆改成自动维护之后，"待确认"没有对应的处理路径了 ——
    # 留着当默认值，等于**所有没显式指定这一档的候选都拿不到默认值**。
    proposed_decision: DecisionType = "auto_commit"
    reason: str = ""

    scope_conditions: list[str] = Field(default_factory=list)
    replaces_memory_id: str = ""
    content_hash: str = ""
    # 归纳类的小结：由哪些短句归纳而来（溯源）。
    #
    # **只有 summary 类型才有值**。家长点开小结要能看到"这句话是从
    # 哪几句来的" —— 没有这个列表，归纳就成了黑盒。
    derived_from: list[str] = Field(default_factory=list)

    @property
    def merges(self) -> bool:
        """这条短句会不会参与类目合并。

        偏好和推断都不参与 —— 前者是被合并会丢掉强度的硬约束，
        后者是被合并会变成事实的猜测。
        """
        return not self.is_preference and not self.inference


# 判定之后具体做什么。
#
#   create              新建一条短句
#   supersede           新建一条 + 把 target 标成 superseded
#   none                不写（discard 时用）
#
# **为什么要单独一个字段**：光看 ``decision`` 分不清"新建"和"改写旧记录"，
# commit 层就得靠 reason 字符串猜 —— 那是会让重构悄悄出错的写法。
MemoryAction = Literal["create", "supersede", "none"]


class MemoryDecision(BaseModel):
    """合并阶段对一条候选的最终判定。"""

    candidate: MemoryCandidate
    decision: DecisionType
    action: MemoryAction = "create"
    reason: str = ""
    # 被取代 / 重复命中的那条已有记忆
    target_memory_id: str = ""


class MemoryUpdateResult(BaseModel):
    """一轮记忆写入的结果。

    ``superseded_facts`` 和 ``context_patch`` 是给**当前这一轮**用的：
    纠正发生后，本轮的 ``parse_parent_input`` 必须知道旧值已经作废，
    不能把它接着并进 ``case_summary``。
    """

    created: list[MemoryCandidate] = Field(default_factory=list)
    updated: list[MemoryCandidate] = Field(default_factory=list)
    skipped: list[MemoryCandidate] = Field(default_factory=list)
    decisions: list[MemoryDecision] = Field(default_factory=list)

    # 本轮被作废的旧短句原文。只给代码用，**不进任何 prompt**。
    superseded_facts: list[str] = Field(default_factory=list)
    # 写完之后重新算出来的 child_context 片段
    context_patch: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def merge_notes(self, extra: list[str]) -> "MemoryUpdateResult":
        """把上游的说明并进来（节点之间传递用）。"""
        self.notes = list(dict.fromkeys([*self.notes, *extra]))
        return self
