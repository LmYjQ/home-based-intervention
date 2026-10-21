"""个案理解相关契约。

**结构上禁止诊断**：CaseSummary 里没有 diagnostic_conclusion 之类的字段，
模型想输出诊断也没有地方可放。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CaseSummary(BaseModel):
    """从家长输入里抽出来的事实、目标、情境和缺口。"""

    # extra="forbid"：模型多输出一个 diagnostic_conclusion 之类的字段要直接报错。
    # Pydantic 默认是"忽略多余字段"，那样模型就能悄悄塞进诊断而没人拦得住。
    model_config = ConfigDict(extra="forbid")

    observed_facts: list[str] = Field(default_factory=list)
    parent_goals: list[str] = Field(default_factory=list)
    context: dict[str, str] = Field(default_factory=dict)
    already_tried: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    retrieval_concepts: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
