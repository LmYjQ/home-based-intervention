# -*- coding: utf-8 -*-
"""
案例层的数据形状
================

★ 三类东西，分得很清
--------------------
1. **模型给的**（`EvidenceBatch` / `EvidenceClaim`）—— 有 `extra="forbid"`，
   schema 直接当 `response_format` 的 `json_schema` 用。
2. **Python 判定的**（`EvidenceSupport` 的资格规则、置信度）—— 枚举 + 纯函数。
3. **给人看的**（`labels.py`）—— 不在这里，见那个文件。

★ 为什么 `support` 与 `status` 是两个字段
----------------------------------------
`status` 说的是「**这条信息是谁说的**」（家长说的 / 观察到的 / 我们推的），
`support` 说的是「**这段原文够不够撑起这条结论**」。

它们经常不一致：家长说了「他今天不太配合」（status=parent_reported），
但模型把这句话撑成「他今天有社交回避倾向」（status=inferred, support=partial）——
前者是事实，后者是推断。合成一个字段就分不清"家长说的"和"我们推的"，
而家长恰恰需要这个区分（2.3.8 的变量字典第一原则：
**家长看到的每一句话，都必须能回答「这是谁说的」**）。

★ `extra="forbid"` 不是洁癖
---------------------------
多输出的字段说明模型在自由发挥。schema 约束解码在**服务端**执行，
这里 `extra="forbid"` 是第二道 —— 两道都要有（`llm.chat_json` 的说明）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ================================================================== 枚举
class EvidenceSource(str, Enum):
    """这条信息**从哪来**。"""

    parent_message = "parent_message"      # 家长这一轮说的话
    parent_report = "parent_report"        # 家长贴的报告
    clinical_report = "clinical_report"    # 医院/机构的报告
    observation = "observation"            # 系统的观察记录
    model_inference = "model_inference"    # 模型自己的推论


class EvidenceStatus(str, Enum):
    """这条信息**是谁说的**。"""

    parent_reported = "parent_reported"    # 家长亲口说的
    observed = "observed"                  # 现场观察到的
    inferred = "inferred"                  # 我们推的
    confirmed = "confirmed"                # 已确认


class EvidenceSupport(str, Enum):
    """这段原文**够不够**撑起这条结论。"""

    direct = "direct"      # 原文直接说了
    partial = "partial"    # 原文只说了一半，结论是推出来的
    none = "none"          # 原文根本没提


# ================================================================== 模型输出
class Contradiction(BaseModel):
    """两条说法对不上。**并列保留，不自动删任何一条**（原始陈述不可被覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    a: str = Field(min_length=1, max_length=200, description="说法一，原样")
    b: str = Field(min_length=1, max_length=200, description="说法二，原样")


class EvidenceClaim(BaseModel):
    """一条证据。**这是模型输出的形状，不是内部存储的形状** ——
    内部存储另有 `case_evidence` 表（带 claim_hash 做幂等键）。"""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(
        min_length=1, max_length=200,
        description="**一条**事实陈述，一句话。主语是孩子，不要写「家长说」。"
                    "例：孩子在幼儿园不愿意靠近其他小朋友")
    quote: str = Field(
        max_length=300,
        description="家长原话里的**连续片段**，逐字复制，不要改写、不要拼接。"
                    "如果这条不是从原话直接来的（比如是我们的推论），"
                    "就填空字符串并把 status 写成 inferred")
    source: EvidenceSource = Field(description="这条信息从哪来")
    status: EvidenceStatus = Field(
        description="**是谁说的**：parent_reported=家长亲口说的；"
                    "observed=现场观察到的；inferred=我们推的；confirmed=已确认")
    support: EvidenceSupport = Field(
        description="原文够不够撑起这条结论：direct=直接说了；"
                    "partial=只说了一半；none=根本没提")
    occurred_at: str = Field(
        default="", max_length=40,
        description="什么时候发生的，原话里有就照抄（「今天早上」「上周」），"
                    "没有就填空字符串")
    scene: str = Field(
        default="", max_length=40,
        description="在哪儿发生的，原话里有就照抄（「幼儿园」「家里」）")


class ClarifyOption(BaseModel):
    """一个候选答案。**家长点一下就能答**，不必自己组织语言。

    ★ 为什么给选项而不是只给一个空框
    ------------------------------
    家长对着一个空输入框会不知道该说什么 —— 尤其当问题问的是
    「他哭的时候一般是谁在旁边」这种他从没想过要总结的事。
    给几个常见情况让他点，是**把"要回忆并组织语言"降级成"认出一个"**。

    `label` 是**会被记进语料的那句话**（家长选了它，等于他说了这句），
    所以它要写成家长会说的话，不是分类名。
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1, max_length=80,
        description="候选答案，**写成家长会说的那句话**"
                    "（如「我抱着他，他一会儿就好」），不要写成分类名"
                    "（如「家长安抚」）")
    hint: str = Field(
        default="", max_length=120,
        description="一句解释，帮家长区分相近的选项；没有就留空")


class ClarifyQuestion(BaseModel):
    """一个要确认的问题。

    ★ `unknown` 是**回指**：它把这条问题挂回评估里那条「还不清楚的」。
      家长答完，这一项就从「还不清楚的」里销掉 —— 没有这个字段，
      销账就只能靠"新事实碰巧覆盖了它"那种模糊匹配（`assessment.fold` 里的旧做法）。

    ★ `why` 是**给家长看的理由**，不是给我们自己看的日志
      ----------------------------------------------------
      同一个问题，被追问和被审问，家长心里的感受完全不同。
      家长看到"他刚才只说了不愿意靠近别人，但没说是在什么情况下"，
      就知道我们听懂了；他看到的是一个光秃秃的问句，
      得到的印象是"这系统在盘问我"。
      这一句还顺手挡掉了另一种失败形态：模型把同一个问题换个说法再问一遍 ——
      家长一眼就能看出"这不是刚才那个问题吗"。

    ★ 一轮给**全部**问题，不是一个
    -------------------------------
    家长要是一个一个被问、被答、再被问、再被答，
    中间每次都要重新加载页面（而刷新之后那个"问题还在"只有服务端知道）。
    一次把要问的都摆出来，家长自己按顺序答完再一次性提交 ——
    交互上更接近"填一张表"，而不是"被审问"。
    """

    model_config = ConfigDict(extra="forbid")

    unknown: str = Field(
        min_length=1, max_length=120,
        description="这一条要确认的**未知项**是什么，一句话。"
                    "家长答完它就该从「还不清楚的」里消失")
    question: str = Field(
        min_length=1, max_length=200,
        description="问句，一句就够，**能用下面的选项回答**的那种"
                    "（如「他哭的时候，一般是谁在旁边？」）")
    why: str = Field(
        min_length=1, max_length=200,
        description="**为什么现在要问这一条**。说清楚两件事："
                    "他刚才那番话里缺了什么、"
                    "问清楚之后哪一步判断会不一样。"
                    "写给家长看，所以要说人话，不要写「缺少关键特征」这种套话")
    options: list[ClarifyOption] = Field(
        min_length=2, max_length=4,
        description="2-4 个候选答案，**要覆盖常见情况**。"
                    "家长可以选一个，也可以不选、自己打字")


class EvidenceBatch(BaseModel):
    """一轮抽取的完整产物。

    ★ 为什么 `questions` / `contradictions` / `quote` 都是**必填**（没有 default）
      --------------------------------------------------
      带 `default` 的字段**不在 schema 的 `required` 里** ——
      模型就可以整个键不输出（实测：给 `unknowns` 设了 default 之后，
      模型一轮都没填过它，而它在 prompt 里被要求填）。

      必填 ≠ 一定要有内容：`questions` 可以是 `[]`。
      **要的是"这个键必须在"，不是"这个键必须有值"。**
    """

    model_config = ConfigDict(extra="forbid")

    thinking: str = Field(
        default="", max_length=300,
        description="**先写这一段**：读完他这番话，你现在确定的是什么、"
                    "还差什么。写给家长看的人话，1-3 句。"
                    "（这是让你先想再答，输出 claims 之前把这一轮理一遍）")
    claims: list[EvidenceClaim] = Field(
        min_length=0, max_length=8,
        description="这一轮里**能核对上**的事实，每条一条。"
                    "已经在「已经知道的」里出现过的不要再重复输出。"
                    "这一段话里没有可核对的事实就输出空数组 []")
    questions: list[ClarifyQuestion] = Field(
        max_length=4,
        description="**这一轮要请家长确认的每一个问题**，一次性全给出来"
                    "（最多 4 个）。没有问题就输出 []。"
                    "已经问过并且家长答过的，不要再列进来")
    contradictions: list[Contradiction] = Field(
        max_length=3,
        description="这段话里**两处说法对不上**的地方（家长前后说的不一样）。"
                    "没有就输出 []")


# ================================================================== 内部形状
class Assessment(BaseModel):
    """一版评估。**这是给人看的那一层**（label/hint 由 `labels.py` 补）。"""

    model_config = ConfigDict(extra="forbid")

    known_facts: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    risk_flags: list[dict[str, Any]] = Field(default_factory=list)
