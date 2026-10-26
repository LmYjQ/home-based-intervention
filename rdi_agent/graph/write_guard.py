"""调试分支的写保护（计划 §12.4）。

Time Travel 生成的分支**不能**写业务表：调试时改一个情境变量再跑一遍，
如果照样写 plans / trials / feedbacks / observations，
现实数据就会被"假设的另一种做法"污染，而且家长下次看到的是调试结果。

**为什么放在节点层而不是每个仓储方法里**：写入口集中在几个 ``persist_*`` /
``capture_*`` 节点和 API 的 ``record_interaction``。给每个仓储方法加
``allow_writes`` 参数会摊薄到十几处调用点，反而更容易漏。

用法：

    if not writes_allowed(state):
        return skip_write(state, "persist_candidate_plans")
"""

from __future__ import annotations

from typing import Any, Mapping


def writes_allowed(state: Mapping[str, Any]) -> bool:
    """这个 state 是不是允许写业务表。"""
    return state.get("debug") is not True


def skip_write(state: Mapping[str, Any], name: str) -> dict[str, Any]:
    """统一的"跳过写入"返回值。

    记进 ``writes_skipped`` 是为了让调试面板和家长能看出
    "这一轮跑了但什么都没落库"，而不是以为数据丢了。
    """
    return {
        "writes_skipped": [*(state.get("writes_skipped") or []), name],
        "trace_metadata": {name: "debug：已跳过业务表写入"},
    }
