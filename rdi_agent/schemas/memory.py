"""长期记忆契约。

四类记忆对应四张语义表（见计划 §4.4）：
fact / preference 落在 children 的 JSON 列 + memory_events；
episode 落在 observations + trials + feedbacks；
hypothesis 落在 hypotheses 表，状态默认 candidate，不能自动升级为事实。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MemoryType = Literal["fact", "preference", "episode", "hypothesis"]


class MemoryCandidate(BaseModel):
    """LLM 抽取出的候选记忆，还没写库。"""

    memory_type: MemoryType
    content: str
    source_interaction_id: str
    source_quote: str = ""
    confidence: float = 0.5
    # hypothesis 的作用条件，防止把"分离时容易哭"写成"孩子就是分离焦虑"
    scope_conditions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class MemoryUpdateResult(BaseModel):
    created: list[MemoryCandidate] = Field(default_factory=list)
    updated: list[MemoryCandidate] = Field(default_factory=list)
    pending_confirmation: list[MemoryCandidate] = Field(default_factory=list)
    skipped: list[MemoryCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
