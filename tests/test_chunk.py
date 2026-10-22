"""切块器的单元测试。

切块器之前没有测试，所以一个"重叠搬了整段"的 bug 活到了入库之后才被
发现（见 ``test_adjacent_children_do_not_duplicate_whole_group``）。
这里把几条容易复发的性质钉住。

**这些测试不碰 PDF、不碰数据库、不调 API** —— 纯函数，快。
"""

from __future__ import annotations

import pytest

from rdi_agent.rag.ingest import chunk as chunker


def _long_paragraph(n_chars: int, seed: str = "alpha") -> str:
    """造一段 n_chars 左右的文本，句子之间用空格。"""
    sentence = f"This is a {seed} sentence about guided participation and RDI. "
    repeats = max(1, n_chars // len(sentence) + 1)
    return (sentence * repeats)[:n_chars]


def _chunk(text: str, **kwargs) -> list[chunker.Chunk]:
    return chunker.chunk_section(
        section_code="000_test",
        section_title="Test Chapter",
        pages=[(1, text)],
        **kwargs,
    )


class TestOverlap:
    def test_overlap_is_bounded_by_chars_not_items(self):
        """重叠长度必须按字符封顶，不能超过预算太多。

        回归测试：早期实现把末尾的 **整个 unit** 搬过来当重叠，
        而这本书的 unit 最长 1100 字符，于是"重叠 150 字符"实际变成
        "重叠一整个段落"。
        """
        group = [chunker._Item(page=1, text=_long_paragraph(1000))]
        overlap = chunker._overlap_of(group, chunker.CHILD_OVERLAP_CHARS)
        total = sum(len(item.text) for item in overlap)
        # 允许一点误差（切在句子边界上），但不能是整段
        assert total <= chunker.CHILD_OVERLAP_CHARS * 1.5

    def test_overlap_of_empty_group_is_empty(self):
        assert chunker._overlap_of([], chunker.CHILD_OVERLAP_CHARS) == []

    def test_overlap_of_zero_budget_is_empty(self):
        group = [chunker._Item(page=1, text=_long_paragraph(500))]
        assert chunker._overlap_of(group, 0) == []

    def test_overlap_starts_at_sentence_boundary(self):
        """开头不能是半句话 —— 半句开头既不好读，向量也偏。"""
        text = "First sentence here. Second sentence here. Third sentence here."
        group = [chunker._Item(page=1, text=text)]
        overlap = chunker._overlap_of(group, 30)
        if overlap:
            joined = " ".join(item.text for item in overlap)
            # 开头要么是句号之后，要么是原句开头
            assert joined[0].isupper() or joined.startswith(".")


class TestChildGroups:
    def test_final_chunk_respects_max_chars(self):
        """最终块长（含前置重叠）不能超过 CHILD_MAX_CHARS。"""
        text = "\n\n".join(_long_paragraph(900, f"s{i}") for i in range(12))
        chunks = [c for c in _chunk(text) if not c.is_parent]
        assert chunks
        for c in chunks:
            assert len(c.content_original) <= chunker.CHILD_MAX_CHARS, (
                f"块 {c.chunk_index} 长 {len(c.content_original)}，"
                f"超过上限 {chunker.CHILD_MAX_CHARS}"
            )

    def test_children_cover_all_content(self):
        """切块不能丢内容 —— 父块拼起来必须覆盖全部原文。"""
        text = "\n\n".join(_long_paragraph(700, f"p{i}") for i in range(10))
        chunks = _chunk(text)
        parents = [c for c in chunks if c.is_parent]
        joined = " ".join(p.content_original for p in parents)
        for i in range(10):
            assert f"p{i}" in joined


class TestNoDuplication:
    def test_adjacent_children_do_not_duplicate_whole_group(self):
        """相邻子块不能出现"后者完整包含前者"。

        回归测试：旧实现把上一个组的**整段**搬到下一个组开头。
        实测在真实的 RDI Book 上，24% 的相邻子块对是这种包含关系，
        重复字符占子块总量 12% —— 向量召回时同一段占掉两个名次。
        """
        text = "\n\n".join(_long_paragraph(800, f"u{i}") for i in range(20))
        chunks = [c for c in _chunk(text) if not c.is_parent]
        assert len(chunks) > 2

        offenders = []
        for a, b in zip(chunks, chunks[1:]):
            if a.parent_chunk_index != b.parent_chunk_index:
                continue  # 跨父块不算
            if len(a.content_original) < 200:
                continue
            # b 的开头 200 字符 == a 的开头 200 字符 => b 包含 a
            if b.content_original[:200] == a.content_original[:200]:
                offenders.append((a.chunk_index, b.chunk_index))
        assert not offenders, f"这些相邻子块对是包含关系：{offenders}"

    def test_duplicate_ratio_is_small(self):
        """重复字符占比要小（重叠是有限度的）。"""
        text = "\n\n".join(_long_paragraph(800, f"d{i}") for i in range(20))
        chunks = [c for c in _chunk(text) if not c.is_parent]
        total = sum(len(c.content_original) for c in chunks)
        # 统计相邻子块共享的前缀长度
        shared = 0
        for a, b in zip(chunks, chunks[1:]):
            if a.parent_chunk_index != b.parent_chunk_index:
                continue
            n = 0
            for x, y in zip(a.content_original, b.content_original):
                if x != y:
                    break
                n += 1
            shared += n
        ratio = shared / total if total else 0
        assert ratio < 0.25, f"相邻子块共享字符占比 {ratio:.1%} 过高"


class TestStructure:
    def test_children_point_to_their_parent(self):
        chunks = _chunk("\n\n".join(_long_paragraph(600, f"x{i}") for i in range(8)))
        parents = {c.chunk_index for c in chunks if c.is_parent}
        children = [c for c in chunks if not c.is_parent]
        assert children
        for c in children:
            assert c.parent_chunk_index is not None
            assert c.parent_chunk_index in parents

    def test_first_parent_children_keep_parent_index_zero(self):
        """第一章子块的父块序号就是 0，不能被当成"没有父块"。

        回归测试：``chunk_ids.get(item.parent_chunk_index or -1)`` ——
        ``0 or -1`` 在 Python 里是 -1，第一章的子块就丢了父块。
        """
        chunks = _chunk("\n\n".join(_long_paragraph(600, f"y{i}") for i in range(8)))
        first_parent = min(c.chunk_index for c in chunks if c.is_parent)
        assert first_parent == 0
        first_children = [
            c
            for c in chunks
            if not c.is_parent and c.parent_chunk_index == first_parent
        ]
        assert first_children

    def test_chunk_index_is_continuous(self):
        chunks = _chunk("\n\n".join(_long_paragraph(600, f"z{i}") for i in range(8)))
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_empty_input_returns_no_chunks(self):
        assert _chunk("") == []


class TestClassify:
    def test_appendix_flag(self):
        chunks = chunker.chunk_section(
            section_code="001_app",
            section_title="APPENDIX A",
            pages=[(1, _long_paragraph(3000))],
            in_appendix=True,
        )
        assert all(c.knowledge_type == "appendix" for c in chunks)

    def test_reference_section(self):
        chunks = chunker.chunk_section(
            section_code="002_ref",
            section_title="References by Gutstein",
            pages=[(1, _long_paragraph(3000))],
        )
        assert all(c.knowledge_type == "reference" for c in chunks)


class TestSplitParagraphs:
    def test_splits_on_blank_lines(self):
        parts = chunker.split_paragraphs("a\n\nb\n\nc")
        assert parts == ["a", "b", "c"]

    def test_drops_empty_parts(self):
        assert chunker.split_paragraphs("a\n\n\n\nb") == ["a", "b"]

    def test_single_paragraph(self):
        assert chunker.split_paragraphs("only one") == ["only one"]


class TestSplitLongParagraph:
    def test_short_paragraph_untouched(self):
        assert chunker._split_long_paragraph("short", 100) == ["short"]

    def test_splits_on_sentence_boundary(self):
        text = "One two three. Four five six. Seven eight nine."
        pieces = chunker._split_long_paragraph(text, 20)
        assert len(pieces) > 1
        # 合起来不能丢内容（除空白）
        assert "".join(pieces).replace(" ", "") == text.replace(" ", "")

    def test_hard_split_when_sentence_too_long(self):
        text = "x" * 250
        pieces = chunker._split_long_paragraph(text, 100)
        assert all(len(p) <= 100 for p in pieces)
        assert "".join(pieces) == text
