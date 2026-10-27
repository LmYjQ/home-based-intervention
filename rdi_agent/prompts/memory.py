"""记忆抽取 prompt（两层设计）。

**这是整层记忆的入口**：抽错一条，之后每一轮都会拿它当背景。

任务只有两件事：

1. **拆句** —— 家长这段话拆成几个短句，每句只讲一件事。
2. **分类** —— 每个短句归一个类目（见 ``memory/categories.py``）。

同类合并是**另一层**（``consolidate_summaries``），输入是已经归好类的短句。

prompt 的第一原则是"宁可不记，也不要记错"：

- 没有**原话**的不记 —— 家长没法核对；
- 诊断标签不记；
- 抽象成标签的（"孩子总是……"）不记成短句。

**为什么单独一个文件**：和 :mod:`rdi_agent.prompts.chat` 一样 ——
提示词的改动和流程的改动要在 diff 里分得开。
"""

from __future__ import annotations

import json
from typing import Any

from rdi_agent.memory.categories import CATEGORY_AFFECTS, CATEGORY_HINT, CATEGORY_LABEL
from rdi_agent.prompts.chat import _COMMON_RULES


def _category_rules() -> str:
    """类目枚举和判据。给抽取 prompt 和归纳 prompt 共用一份。

    **单一来源**：枚举写死在 ``memory/categories.py``，
    这里只是把它渲染成人能读的规则。写成两份必然对不上，
    而对不上的后果是"模型归的类在代码里不存在"——
    那种 bug 只在合并时才炸，排查起来很贵。
    """
    lines = []
    for category, label in CATEGORY_LABEL.items():
        hint = CATEGORY_HINT.get(category, "")
        affects = CATEGORY_AFFECTS.get(category, "")
        lines.append(f"- `{category}`（{label}）：{hint}")
        lines.append(f"  它决定：{affects}")
    return "\n".join(lines)


def extract_memories(
    *,
    source_kind: str,
    parent_text: str,
    followup_answers: list[dict[str, str]] | None,
    child_profile: dict[str, Any] | None,
    existing_items: list[dict[str, Any]],
    today: str,
    feedback: dict[str, Any] | None = None,
    feedback_analysis: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """返回 ``(system, user)``。"""
    system = (
        _COMMON_RULES
        + """
现在你的任务不是回答家长，而是**把家长说的话拆成短句，并给每句归类**。

两步：

1. **拆句**：把这段话拆成几个短句，**每一句只讲一件事**。
   一句话里不要同时出现两件事（"在幼儿园哭而且不肯穿鞋"要拆成两句）。
2. **分类**：给每个短句归一个类目（`category`）。

硬规则：

1. **source_quote 必须逐字照抄家长的原话**。不能改写、不能概括、
   不能是你自己组织的话。抄不出来，这条就不要提。
   （唯一的例外：你推断出来的句子 inference=true，本来就没有原话。）
2. **一句只讲一件事**。写具体的行为和场景。
   不要写"孩子总是……""这种孩子……"这样的标签 —— 那是判断，不是他说的。
   家长说"他每天早上都哭着说不想去"，那就写这一句，
   不要再补一句"他情绪不好"。
3. **家长的偏好单独标**（`is_preference=true`）：
   家长希望怎样、不希望怎样、能接受的取舍、跟大人沟通的偏好。
   例子："我不希望他被批评""老师说话慢一点""不要当着别人面说他不行"。
   偏好是**硬约束**，它跟"孩子是什么样"是两回事，别混在一起。
4. **你推断的标 `inference=true`**，措辞必须是"可能……""看起来像……"，
   不能写成结论。推断照提，系统会标出来。
5. 家长明确纠正旧信息（"不是……是……"、"之前说错了"、"其实他五岁"）→
   填 `replaces_memory_id`，值必须从下面 existing_items 里的 id 原样抄。
6. 带诊断标签的内容（自闭症/多动症/谱系/发育迟缓等）**不要提**。
   系统不做诊断，也不替家长存标签。
7. 涉及自伤、伤人、诊断、用药、家庭重大变故这类敏感内容：
   家长**明确说过**的照常提出来（这是他孩子的真实情况，
   藏着反而可能藏掉该找专业人士的风险信号）；
   但如果是**你推断**的，按第 4 条标 inference=true。
8. 寒暄、临时情绪（"我今天好累"）、跟孩子无关的事、这次没说的新信息 →
   `proposed_decision` 写 `discard`，或者干脆不提出候选。
9. 家长说"我们试过 X"照样提出来 —— 那是家长真做过的事。
   **不要**把系统以前给过的建议写成家长做过的事。

给每句归一个类目（`category`）：

"""
        + _category_rules()
        + """

**归类目的不是"这件事属于什么话题"，而是"它改变了建议的哪一部分"。**
同样一件事，落在不同类目会让建议写法不同 —— 这才是归类的标准。

例子：
- "在幼儿园门口大哭" → `separation`（它决定交接怎么安排），
  **不是** `emotion`（虽然它跟情绪有关）。
- "在家也会无缘无故哭" → `emotion`（它决定所有场景的节奏和防崩预案）。
- "不会用手指指东西，要东西会拉大人的手" → `communication`。
- "5 岁" → `other`（它不改变建议怎么写）。

**归错类不会丢掉这条记忆** —— 只是它会被归到别的类目去。
但请认真归：归类错了，这条信息在方案里就用不上。

`proposed_decision` 怎么选：

- `auto_commit`：家长明确说过、原话能逐字引用、值得长期记住；
- `discard`：临时内容、重复内容、没有长期价值。

输出要求：

- 用不上的字段填 `""`、`[]`、`false`，**不要省略字段**。
- `content` 一句短句，不超过 40 个字。
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
        "existing_items": existing_items,
    }
    if feedback is not None:
        payload["feedback"] = feedback
    if feedback_analysis is not None:
        payload["feedback_analysis"] = feedback_analysis

    return system, json.dumps(payload, ensure_ascii=False, indent=2)


def consolidate_summaries(
    *,
    buckets: list[dict[str, Any]],
    today: str,
) -> tuple[str, str]:
    """把同一个类目下的多条短句，归纳成一条"现状"。

    ``buckets`` 每项：

        {"category": "separation", "label": "分离与过渡",
         "items": [{"memory_id": "mi_xxx", "text": "...", "turn_id": "..."}]}

    **一次调用处理所有类目** —— 按类目分次调会把一次合并变成 N 次，
    延迟和失败率都跟着涨 N 倍。
    """
    system = (
        _COMMON_RULES
        + """
现在你的任务不是回答家长，而是**把同一个类目下的多条短句，归纳成一句话的现状**。

每个类目给你若干条短句（都是家长描述的具体情况）。
请为每个类目写一条小结。

硬规则：

1. **只归纳，不能新增**。小结里的每一句都要能在给你的短句里找到依据。
   短句里没有的、你自己想出来的，一律不许写。
2. **必须带成立条件**（`scope_conditions`）。写清楚"在什么情况下"。
   没有条件的小结会被读成"孩子就是这样" ——
   而几条记录不足以支撑这种断言。
3. **措辞是"在……情况下……"，不是"孩子是……"**。
   "在幼儿园门口会哭" 可以；"孩子情绪有问题" 不可以。
4. **矛盾就不归纳**。如果同一类目下的短句互相冲突（比如一条说会哭、
   一条说从不哭），**不要**硬凑一个自洽的说法。
   在 `notes` 里写清哪里冲突了，这个类目就不出小结。
5. `derived_from` 要填参与这条小结的短句的 `memory_id`，**原样照抄**，
   一个都不能编，也不要少填。
6. `confidence_level` 给这条小结的把握：high / medium / low。
   短句多、指向一致才给 high。只有 2-3 条就 medium 或 low。
7. `content` 一句话，不超过 80 字。
8. **每个类目都要出小结** —— 包括你只看出一点点的情况。
   家长需要知道系统看到了什么，哪怕只是一条。
   实在没内容就写最朴素的复述，不要省略这个类目。

类目含义（帮你判断该写什么）：

"""
        + _category_rules()
        + """

输出要求：

- 用不上的字段填 `""`、`[]`，**不要省略字段**。
- `summaries` 的顺序和输入的类目顺序一致。
"""
    )

    payload: dict[str, Any] = {
        "today": today,
        "buckets": buckets,
    }
    return system, json.dumps(payload, ensure_ascii=False, indent=2)
