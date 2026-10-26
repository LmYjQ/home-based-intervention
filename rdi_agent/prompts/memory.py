"""记忆抽取 prompt（计划 §10.3）。

**这是整层记忆的入口**：抽错一条事实，之后每一轮都会拿它当背景。
所以 prompt 的第一原则是"宁可不记，也不要记错"：

- 没有**原话**的不记 —— 家长没法核对；
- 推断出来的**只能当假设**，且永远是"候选"状态；
- 概括（"孩子总是……"）不记成观察；
- 诊断标签不记。

**为什么单独一个文件**：和 :mod:`rdi_agent.prompts.chat` 一样 ——
提示词的改动和流程的改动要在 diff 里分得开。
"""

from __future__ import annotations

import json
from typing import Any

from rdi_agent.prompts.chat import _COMMON_RULES


def extract_memories(
    *,
    source_kind: str,
    parent_text: str,
    followup_answers: list[dict[str, str]] | None,
    child_profile: dict[str, Any] | None,
    existing_facts: list[dict[str, Any]],
    existing_hypotheses: list[dict[str, Any]],
    recent_observations: list[dict[str, Any]],
    today: str,
    feedback: dict[str, Any] | None = None,
    feedback_analysis: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """返回 ``(system, user)``。"""
    system = (
        _COMMON_RULES
        + """
现在你的任务不是回答家长，而是**从家长说的话里挑出值得长期记住的东西**。

四类记忆：

- `fact`：家长明确说过的稳定信息（年龄、家里有谁、幼儿园情况、说话能力、
  已知的场景）。要具体，带上场景，不要写成标签。
- `preference`：家长希望怎样 / 不希望怎样、能接受的取舍、沟通偏好。
- `episode`：**一个具体事件**——在哪、当时发生了什么、孩子做了什么、
  大人怎么回应、孩子什么反应、结果如何。不是"孩子容易哭"这种概括。
- `hypothesis`：对重复出现模式的一个**暂定解释**。措辞必须是"可能……"，
  必须写适用条件（scope_conditions），不能写成结论。

硬规则：

1. **source_quote 必须逐字照抄家长的原话**。不能改写、不能概括、
   不能是你自己组织的话。抄不出来，这条就不要提。
2. **你推断的东西（inference=true）只能是 hypothesis**。
   不要把推断写成 fact —— 模型猜的东西不能变成"家长说过的事实"。
3. 家长说"我们试过 X / 我让他 Y" → 用 episode 记，is_attempt=true。
   **不要**把系统以前的建议写成家长做过的事 —— 那是两回事。
4. 家长明确纠正旧信息（"不是……是……"、"之前说错了"、"其实他五岁"）→
   填 `replaces_memory_id`，值必须从下面 existing_facts 里的 id 原样抄。
5. 这条观察跟 existing_hypotheses 里某个假设对不上 → 把那个假设的 id
   填进 `contradicts_memory_ids`。id 必须从 existing_hypotheses 里原样抄。
6. 带诊断标签的内容（自闭症/多动症/谱系/发育迟缓等）**不要提**。
   系统不做诊断，也不替家长存标签。
7. 涉及自伤、伤人、诊断、用药、家庭重大变故这类敏感内容：
   `proposed_decision` 写 `needs_confirmation`，不要写 `auto_commit`。
8. 寒暄、临时情绪（"我今天好累"）、跟孩子无关的事、这次没说的新信息 →
   `proposed_decision` 写 `discard`，或者干脆不提出候选。

`proposed_decision` 怎么选：

- `auto_commit`：家长明确说过、原话能逐字引用、不是推断、不敏感、不和旧记忆冲突；
- `needs_confirmation`：推断、敏感、和旧记忆冲突、或者你不太确定；
- `discard`：临时内容、重复内容、没有长期价值。

输出要求：

- 用不上的字段填 `""`、`[]`、`false`，**不要省略字段**。
- `content` 一句话说清这条记忆，不要超过 60 个字。
- `confidence` 是 0–1 的小数，表示你对"这条值得长期记住"的把握。
- 一次最多提 6 条。宁可少提，不要凑数。
"""
    )

    payload: dict[str, Any] = {
        "today": today,
        "source_kind": source_kind,
        "parent_text": parent_text,
        "followup_answers": followup_answers or [],
        "child_profile": child_profile or {},
        "existing_facts": existing_facts,
        "existing_hypotheses": existing_hypotheses,
        "recent_observations": recent_observations,
    }
    if feedback is not None:
        payload["feedback"] = feedback
    if feedback_analysis is not None:
        payload["feedback_analysis"] = feedback_analysis

    return system, json.dumps(payload, ensure_ascii=False, indent=2)
