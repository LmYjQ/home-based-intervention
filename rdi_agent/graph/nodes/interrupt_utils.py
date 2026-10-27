"""中断恢复值的解析。

聊天追问和反馈追问用的是同一种恢复形状：

    {"text": "给模型看的整段话",
     "answers": [{"question": "...", "answer": "..."}, ...]}

**为什么要结构化**：恢复值光是一段文本的话，"哪句回答的是哪道题"
就只能靠模型猜。实测模型会把整段问答丢掉 —— 家长明确答了
"没有不愉快的经历"，抽出来的 observed_facts 里还是没有。
问答对走结构化通道，由代码确定性地并进事实，不指望模型抽。
"""

from __future__ import annotations

from typing import Any


def unpack_resume(value: Any) -> tuple[str, list[dict[str, str]]]:
    """把恢复值拆成 (给模型看的文本, 结构化的问答对)。"""
    if isinstance(value, dict):
        text = str(value.get("text") or "")
        raw_answers = value.get("answers") or []
    elif isinstance(value, list):  # 老形状：直接发数组回来
        return "\n".join(str(x) for x in value), []
    else:
        return str(value), []

    answers: list[dict[str, str]] = []
    for item in raw_answers:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not answer:
            continue
        answers.append({"question": question, "answer": answer})
    return text, answers


def build_asked_qa(
    questions: list[dict[str, Any]],
    answers: list[dict[str, str]],
) -> list[dict[str, str]]:
    """把"这一轮问了什么"和"家长答了什么"配成对。

    **问过但没答的也要记进去**（``answer`` 留空）。

    **为什么**：模型只有同时看到"问过"和"答没答"，才能分清
    两件本质不同的事：

    - 家长**答了** → 这件事问完了，基于答案往下走，别再问；
    - 家长**没答上来** → 换个角度问，问得更具体。

    只给问题列表时这两种在模型眼里长得一样，于是它换个说法把原问题
    重问一遍（实测"在园和在家表现有没有不同"被问了两遍）。

    **空答案的来源**：``unpack_resume`` 会把空回答丢掉（空回答没有信息量），
    所以"问了但家长没答"这件事只能靠问题列表本身还原 ——
    这正是这里要把问题全部带上的原因。
    """
    answer_by_question = {
        str(item.get("question") or "").strip(): str(item.get("answer") or "").strip()
        for item in answers
        if str(item.get("question") or "").strip()
    }
    # 这一轮只问了一道题，而家长是在**同一个输入框**里回了一段话
    # （没带原问题）—— 那段话就是这道题的答案。
    #
    # **不归的话**：模型看到的是"问了但没答"，会想再问一遍
    # —— 而家长面前只有一次追问的机会（默认 1 轮，见 Settings.followup_max_rounds）。
    if len(questions) == 1:
        only = str(questions[0].get("question") or "").strip()
        orphan = next(
            (
                item
                for item in answers
                if not str(item.get("question") or "").strip()
                and str(item.get("answer") or "").strip()
            ),
            None,
        )
        if only and orphan is not None and not answer_by_question.get(only):
            answer_by_question[only] = str(orphan.get("answer") or "").strip()
    pairs: list[dict[str, str]] = []
    seen_questions: set[str] = set()
    for item in questions:
        question = str(item.get("question") or "").strip()
        if not question or question in seen_questions:
            continue
        seen_questions.add(question)
        pairs.append(
            {"question": question, "answer": answer_by_question.get(question, "")}
        )
    # 家长自己另外补的话（没带原问题），也记下来 ——
    # 这是这一轮唯一的新信息。
    for item in answers:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question or not answer:
            continue
        pairs.append({"question": "", "answer": answer})
    return pairs
