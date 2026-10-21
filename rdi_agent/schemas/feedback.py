"""反馈契约。

刻意把"执行了什么"和"结果如何"分开：
家长说"今天没哭"是一条观察，不等于"上一轮方案有效"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChildResponse(BaseModel):
    behavior: str = ""
    affect: str = ""
    words: str = ""


class FeedbackSubmission(BaseModel):
    trial_id: str
    turn_id: str
    attempted: bool
    implementation_notes: str = ""
    child_response: ChildResponse = Field(default_factory=ChildResponse)
    parent_observations: list[str] = Field(default_factory=list)
    difficulties: list[str] = Field(default_factory=list)


class FeedbackAnalysis(BaseModel):
    actual_execution: str = ""
    observed_changes: list[str] = Field(default_factory=list)
    execution_difficulties: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    # 只描述观察到的关系，不下因果结论
    relation_to_plan: str = ""
    confidence_note: str = ""
