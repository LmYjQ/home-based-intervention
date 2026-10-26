"""证据清单的渲染：门控必须看到原文，不只是子块摘要（2026-10-26）。

**这个文件测的是一条真实的线上事故**：

会话 ``399cee38-…1a8540f25359`` 里，检索其实**召回得很好** ——
8 条证据、8 条可用、``evidence_gaps`` 是空的，其中一条正是
「（四）针对挑战性行为的干预」（PDF 7-9 页），里面明明白白写着
"前因干预""替代行为训练""后果管理"。

但门控判了 ``indirect``，家长只拿到一段有限回答，没有方案卡。

**原因不在判据，在输入**：检索的召回单元是**子块**，
``summary`` 只是子块开头 220 字 —— 这一节的子块恰好是
"访谈家长时可通过哪些问题分析行为的功能"，也就是**评估环节**。
真正能落成建议的干预方法在**父块**里（``source_excerpt``），
而门控的 prompt 当时**只渲染 summary**。

模型看到的是"一份家长访谈问卷 + 几段孤独症章节"，
判 indirect 完全合理 —— **门控没判错，是证据没给它看全**。

所以修的是**渲染层**，不是判据：门控这一路要把父块原文节选一起渲染。
"""

from __future__ import annotations

from rdi_agent.prompts.chat import _evidence_lines

EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"

# 这一节的**子块**：家长访谈问题清单（评估环节）。
CHILD_SNIPPET = (
    "访谈儿童家长时，可通过以下问题分析挑战性行为的功能："
    "这种行为通常发生在一天中的什么时间？通常发生在什么情况下？"
)

# 同一节的**父块**：干预方法在这一段里。
PARENT_EXCERPT = (
    "（四）针对挑战性行为的干预\n"
    "挑战性行为通常具有以下功能：一是获得他人的关注……\n"
    "1. 功能分析。应分析行为前因、行为本身和行为后果……\n"
    "（1）前因干预。通过调整环境或降低任务难度以减少挑战性行为的触发因素……\n"
    "（2）替代行为训练。通过教授功能性替代行为以减少挑战性行为的发生……\n"
    "（3）后果管理。通过强化适当行为、减少对问题行为的强化来减少挑战性行为……"
)


def _evidence() -> list[dict]:
    return [
        {
            "evidence_id": EVIDENCE_ID,
            "section_title": "（四）针对挑战性行为的干预",
            "pdf_page_start": 7,
            "pdf_page_end": 9,
            "summary": CHILD_SNIPPET,
            "source_excerpt": PARENT_EXCERPT,
            "source_type": "guideline",
            "review_status": "unreviewed",
        }
    ]


# ------------------------------------------------------------------ 默认不带原文
def test_default_rendering_has_no_excerpt():
    """默认不带原文 —— 别的调用方（有限回答等）不该被撑大。"""
    text = _evidence_lines(_evidence())
    assert "原文节选" not in text
    assert "前因干预" not in text


# ------------------------------------------------------------------ 门控这一路带原文
def test_include_excerpt_shows_parent_text():
    """带上父块后，模型才看得见干预方法。"""
    text = _evidence_lines(_evidence(), include_excerpt=True)
    assert "原文节选" in text
    assert "前因干预" in text
    assert "替代行为训练" in text
    assert "后果管理" in text


def test_include_excerpt_keeps_the_metadata():
    """加了原文不能把章节、页码、审核状态弄丢 —— 引用校验靠这些。"""
    text = _evidence_lines(_evidence(), include_excerpt=True)
    assert EVIDENCE_ID in text
    assert "（四）针对挑战性行为的干预" in text
    assert "PDF 第 7-9 页" in text
    assert "unreviewed" in text
    assert "guideline" in text


# ------------------------------------------------------------------ 超长截断
def test_long_excerpt_is_truncated():
    """父块可能很长 —— 不能把整个 prompt 撑爆。"""
    item = _evidence()[0]
    item["source_excerpt"] = "甲" * 5000
    text = _evidence_lines([item], include_excerpt=True)
    assert "甲" * 1200 in text
    assert "甲" * 1201 not in text
    assert "…" in text


def test_missing_excerpt_does_not_break_rendering():
    """有的证据没有父块 —— 不能渲染出一个空的"原文节选："。"""
    item = _evidence()[0]
    item["source_excerpt"] = None
    text = _evidence_lines([item], include_excerpt=True)
    assert "原文节选" not in text
    assert EVIDENCE_ID in text
