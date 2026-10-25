# -*- coding: utf-8 -*-
"""
家长措辞的唯一出处
==================

★ 原则
------
1. **state 字段名是给代码和模型看的，家长看到的是措辞。**
   `known_facts` 这种名字出现在家长眼前是失败的 —— 它暴露了内部结构，
   还会让家长误以为这是一份"结论清单"。
2. **措辞只在这里写。** 前端不硬编码、不翻译、不猜。
   （与项目既有约定同源：前端不算业务数、`api.js` 是唯一出口。）
3. **内部字段一律不发给家长。** 把"置信度 0.35""support=none"甩给家长
   既没有意义，又会误导 —— 家长会以为"0.35"是孩子的某种指标。

★ 明确不展示的字段清单（防止实现时"顺手就带上了"）
--------------------------------------------------
`support` / `evidence[].status` / `confidence` / `claim_hash` /
`assessment_version` / `thread_id` / 内部错误码。

    —— 但有一条例外：`evidence[].status` 决定这条进不进"事实栏"，
       而家长需要知道"这条是我们推的还是你说的"。
       所以**不展示这个字段名**，改成在条目上给一个「谁说的」角标
       （见 `CLAIM_ORIGIN`）—— 同样的信息，换成家长能懂的说法。
"""

from __future__ import annotations

#: 八个生命周期状态的家长措辞。
STATUS_LABELS: dict[str, dict[str, str]] = {
    "assessing": {
        "label": "我们在了解情况",
        "hint": "你说的我们正在整理",
    },
    "awaiting_parent_input": {
        "label": "想请你补充一点",
        "hint": "有一处还不太清楚",
    },
    "ready_to_execute": {
        "label": "方案准备好了",
        "hint": "等你选一个",
    },
    "awaiting_feedback": {
        "label": "等你练完告诉我一声",
        "hint": "练完回来说一句就行",
    },
    "reviewing_outcome": {
        "label": "我们在看看这次的效果",
        "hint": "",
    },
    "revising_plan": {
        "label": "我们在改方案",
        "hint": "上次的做法要调一下",
    },
    "paused": {
        "label": "先停一下",
        "hint": "出现需要先处理的情况",
    },
    "closed": {
        "label": "这个案子结束了",
        "hint": "",
    },
}

#: 四栏的标题与解释（家长看到的就是这两行）。
COLUMN_LABELS: dict[str, dict[str, str]] = {
    "known_facts": {
        "label": "我们已经知道的",
        "hint": "这些是你亲口说过、而且原话能对得上的事",
    },
    "hypotheses": {
        "label": "我们的猜测（还没确认）",
        "hint": "这些是我们根据已知的事推出来的，可能不对",
    },
    "unknowns": {
        "label": "还不清楚的",
        "hint": "弄清楚这些，才知道该怎么做",
    },
    "contradictions": {
        "label": "两处说法对不上",
        "hint": "你前后说的不一样，我们两个都留着",
    },
}

#: 一条证据的「谁说的」角标。
#:
#: ★ 为什么不直接展示 `evidence[].status`
#: ----------------------------------------
#: `status` 是个内部字段名，家长看不懂；而"这条是我们推的"这个信息
#: **必须给** —— 否则家长会把我们的猜测当成他说过的话。
#: 所以换个说法给同一个信息，而不是把字段名换掉。
CLAIM_ORIGIN: dict[str, dict[str, str]] = {
    "parent_reported": {"label": "你说的", "hint": ""},
    "observed": {"label": "看到的", "hint": ""},
    "inferred": {"label": "我们推的", "hint": "可能不对"},
    "confirmed": {"label": "已确认", "hint": ""},
}

#: 追问卡的标题。
CLARIFY_LABELS: dict[str, str] = {
    "title": "想问你一句",
    "hint": "只需要回答这一句就行",
}


def status_label(status: str) -> dict[str, str]:
    """生命周期状态的措辞。**未知状态也要有话说** ——
    空 dict 会让前端渲染出一个没有标题的徽标，而那看着像 bug。"""
    return STATUS_LABELS.get(status, {"label": "进行中", "hint": ""})


def column_labels() -> dict[str, dict[str, str]]:
    """四栏的措辞。**每次返回一份新的 dict** ——
    调用方拿到的是响应体，不是模块常量；模块常量被就地改了会很难查。"""
    return {k: dict(v) for k, v in COLUMN_LABELS.items()}


def claim_origin(status: str) -> dict[str, str]:
    """证据条目的「谁说的」角标。"""
    return CLAIM_ORIGIN.get(status, {"label": "我们记录的", "hint": ""})
