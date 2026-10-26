"""反馈契约。

刻意把"执行了什么"和"结果如何"分开：
家长说"今天没哭"是一条观察，不等于"上一轮方案有效"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChildResponse(BaseModel):
    behavior: str = ""
    affect: str = ""
    words: str = ""


class FeedbackSubmission(BaseModel):
    trial_id: str
    turn_id: str
    attempted: bool
    # "做了但中途停了" 和 "根本没做" 是两件事：前者说明方案能开始、
    # 但执行不下去；后者说明门槛就在第一步。trial 状态要能分开。
    stopped: bool = False
    implementation_notes: str = ""
    child_response: ChildResponse = Field(default_factory=ChildResponse)
    parent_observations: list[str] = Field(default_factory=list)
    difficulties: list[str] = Field(default_factory=list)

    def execution_status(self) -> Literal["attempted", "not_attempted", "stopped"]:
        """这次尝试落到 trials.status 的执行状态。

        优先级：停了 > 做了 > 没做。家长勾了"没做"又勾"中途停"是矛盾的，
        按更具体的那个算 —— 能开始但停下来，比"没开始"更有信息量。
        """
        if self.stopped:
            return "stopped"
        return "attempted" if self.attempted else "not_attempted"


class FeedbackComparison(BaseModel):
    """和过去某一次尝试的对比。

    **一次观察说明不了因果**，但两次放在一起能看出"是不是每次都卡在同一处"。
    这里只描述相似和不同，不判断方案有没有效。
    """

    previous_trial_id: str = ""
    previous_plan_title: str = ""
    previous_trial_status: str = ""
    similarity: str = ""
    difference: str = ""


class FeedbackAnalysis(BaseModel):
    actual_execution: str = ""
    observed_changes: list[str] = Field(default_factory=list)
    execution_difficulties: list[str] = Field(default_factory=list)
    # 时间、环境、孩子拒绝、家长负担 —— 计划 §11.5 要求能识别出来
    execution_barriers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    # 只描述观察到的关系，不下因果结论
    relation_to_plan: str = ""
    confidence_note: str = ""
    history_comparison: list[FeedbackComparison] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
