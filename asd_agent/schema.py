# -*- coding: utf-8 -*-
"""
抽取 Schema
===========

设计要点
--------
1. **枚举来自 vocab.py，单一事实来源**。改词表只需改一处，schema / 校验 / 统计同步更新。
2. **枚举用 Literal 而不是 str + 事后校验**。区别很关键：事后校验只能"发现"错误并重试，
   而 Literal 会直接进入 JSON Schema 的 enum 约束，由服务端约束解码强制执行。
   这就是把"标签漂移"消灭在生成阶段而不是事后拦截。
3. **必填 vs 可空要区分**。必填字段保证下游统计不缺列；
   拿不准的字段用 `| None` + 默认值，让模型显式说"我不知道"，
   而不是被迫编一个值 —— 编造的前因比缺失的前因危害大得多。
4. **spans 回指**：每个关键判断都要能指回原句，供家长核对与纠错。
5. **红旗不进主 schema**。见 redflag 独立说明。
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

from vocab import VOCABS

Scene       = Literal[tuple(VOCABS["scene"])]           # type: ignore[valid-type]
Behavior    = Literal[tuple(VOCABS["behavior"])]        # type: ignore[valid-type]
Antecedent  = Literal[tuple(VOCABS["antecedent"])]      # type: ignore[valid-type]
Consequence = Literal[tuple(VOCABS["consequence"])]     # type: ignore[valid-type]
Person      = Literal[tuple(VOCABS["people_present"])]  # type: ignore[valid-type]

# 参与回指的字段（避免 model 自指，直接枚举）
SPANNED = ("scene", "behavior", "antecedent", "consequence", "raw_quote")
SpannedField = Literal["scene", "behavior", "antecedent", "consequence", "raw_quote", "intensity"]


class Grounding(BaseModel):
    """一个判断在原文中的依据。quote 必须是 raw_text 的连续子串。"""
    model_config = ConfigDict(extra="forbid")
    field: SpannedField = Field(description="这条依据支持的是哪个字段的判断")
    quote: str = Field(description="原文中的连续片段，必须能在原文中逐字找到")


class Observation(BaseModel):
    """一条行为观察记录。"""

    model_config = ConfigDict(extra="forbid")  # 多余字段直接判失败，防止模型夹带私货

    # ---- 核心分类（必填，缺列会让下游统计失真）----
    scene: Scene = Field(description=f"场景，取值必须是词表 code 之一")
    behavior: Behavior = Field(description="主要行为，取值必须是词表 code 之一")
    antecedent: Antecedent = Field(
        description="前因。只能记录可观察到的线索；确实看不出时填 unclear，不要推测动机"
    )
    consequence: Consequence = Field(
        description="后果。按『这个行为替他解决了什么』来判，而不是表面现象"
    )

    # ---- 量化 ----
    intensity: int = Field(ge=1, le=5, description="强度 1(轻微) 到 5(严重失控)")
    duration_sec: int | None = Field(
        default=None, ge=0, le=36000, description="持续秒数，原文没提就填 null，不要估算"
    )

    # ---- 情境 ----
    people_present: list[Person] = Field(
        default_factory=list, description="在场者 code 列表；确定只有自己一个人时填 ['alone']"
    )

    # ---- 可信度与回指 ----
    raw_quote: str = Field(description="原文中最能代表这次事件的一段话，逐字复制")
    spans: list[Grounding] = Field(
        default_factory=list, description="关键判断在原文中的依据，用于让家长核对"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="你对自己这组判断的把握程度")

    # ---- 四层之「事实层」：哪些字段是原话里直接写着的 ----
    # ★ 为什么逐事件而另外三层不逐事件（技术方案 §4 场景 A 要的是四层）：
    #   「这条事件的哪些字段有原话依据」是一个**逐事件**的问题 ——
    #   同一次输入里的两条事件，一条的 behavior 是家长亲口说的、
    #   另一条的前因是模型补的，这两件事只能挂在各自的事件上。
    #   而「家长的解读」「待验证假设」「缺失信息」是**跨事件**的
    #   （方案 §6 的 hypotheses 表有 related_observation_ids 复数），
    #   挂在每条事件上会重复三份，所以那三层在 observe.ObservationBatch 上。
    #
    # **可选 + 默认空，且绝不能改成 required** —— 两个真实消费者：
    #   · server.py 的教练诊断用裸 `except: continue` 重校验历史 payload
    #   · graph/nodes.py 的 publish 同样重校验旧 checkpoint
    #   required 字段会让**每一条旧行静默消失**、旧 checkpoint 直接 raise。
    #   同理，降级路径（observe._fallback_batch）产出空列表，
    #   遵守「降级时一条都不编」。
    fact_fields: list[SpannedField] = Field(
        default_factory=list, max_length=6,
        description="这条事件里哪些字段是**原话里直接写着的**（能指给家长核对），"
                    "哪些是你推断出来的。只列有原话依据的字段，推断出来的不要列。",
    )

    # ---- 主动声明不确定 ----
    needs_clarification: bool = Field(
        default=False, description="证据不足以做出可靠判断时置 true"
    )
    clarifying_question: str | None = Field(
        default=None, description="needs_clarification 为 true 时，向家长提一个最关键的追问"
    )
    note: str | None = Field(
        default=None, description="需要提醒家长注意但不属于上述任何一类的情况"
    )


def json_schema() -> dict:
    """生成要发给模型的 JSON Schema（OpenAI 兼容格式）。"""
    return Observation.model_json_schema()


def dump() -> None:
    import json
    s = json_schema()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print("\n--- 规模 ---")
    for k, v in s["properties"].items():
        if "enum" in v:
            print(f"  {k:22s} enum={len(v['enum'])}")
    print(f"  required={s.get('required')}")


if __name__ == "__main__":
    dump()
