"""切块器的单元测试。

切分规则就四条（人工定的），这里把它们钉住：

1. 父块 = 一个标题到下一个标题之间的全部内容
2. 子块 = 一行一段
3. ``<br>`` 也分段
4. 每页最后一段不以句末标点收尾的，跟下一页第一段合并

**这些测试不碰 PDF、不碰数据库、不调 API** —— 纯函数，快。
"""

from __future__ import annotations

from rdi_agent.rag.ingest import chunk as chunker
from rdi_agent.rag.ingest import parse_md


def _chunk_pages(pages: list[tuple[int, str]]) -> list[chunker.Chunk]:
    """按页切分（走完整的那条路：拆块 -> 跨页合并 -> 切块）。"""
    blocks: list[parse_md.Block] = []
    for page, text in pages:
        blocks.extend(parse_md.parse_blocks(text, page))
    blocks, _ = parse_md.merge_page_breaks(blocks)
    _, chunks = chunker.chunk_document(blocks, source_id="test")
    return chunks


def _chunk(text: str) -> list[chunker.Chunk]:
    return _chunk_pages([(1, text)])


def _children(chunks: list[chunker.Chunk]) -> list[chunker.Chunk]:
    return [c for c in chunks if not c.is_parent]


def _parents(chunks: list[chunker.Chunk]) -> list[chunker.Chunk]:
    return [c for c in chunks if c.is_parent]


class TestParentIsHeadingSection:
    def test_heading_starts_a_new_parent(self):
        chunks = _chunk(
            "# 书名\n\n开头的段落。\n\n"
            "## 第一章\n\n甲段落。\n\n"
            "## 第二章\n\n乙段落。\n"
        )
        assert len(_parents(chunks)) == 3
        assert [p.content_original.split("\n")[0] for p in _parents(chunks)] == [
            "书名",
            "第一章",
            "第二章",
        ]

    def test_heading_without_body_makes_no_parent_chunk(self):
        """``## 三、干预康复内容`` 后面直接是 ``###`` —— 不产空父块。"""
        chunks = _chunk("## 三、干预康复内容\n\n### （一）健康教育\n\n甲段落。\n")
        parents = _parents(chunks)
        assert len(parents) == 1
        assert parents[0].content_original.startswith("（一）健康教育")

    def test_parent_text_starts_with_heading(self):
        chunks = _chunk("## 第一章\n\n甲段落。\n")
        assert _parents(chunks)[0].content_original == "第一章\n甲段落。"

    def test_parent_differs_from_its_only_child(self):
        """父块和唯一子块不能一字不差 —— 检索只回子块，重复的父块白占向量。"""
        chunks = _chunk("## 第一章\n\n只有一段。\n")
        parent = _parents(chunks)[0]
        child = _children(chunks)[0]
        assert parent.content_original != child.content_original

    def test_level_one_heading_is_a_parent_too(self):
        """书名也是父块 —— 它下面那段总论以前根本没进库。"""
        chunks = _chunk("# 书名\n\n总论。\n")
        assert len(_parents(chunks)) == 1
        assert _children(chunks)[0].content_original == "总论。"


class TestChildIsOneLine:
    def test_each_line_is_a_child(self):
        chunks = _chunk("## 第一章\n\n甲。\n\n乙。\n\n丙。\n")
        children = _children(chunks)
        assert [c.content_original for c in children] == ["甲。", "乙。", "丙。"]

    def test_content_is_not_lost(self):
        text = "## 第一章\n\n" + "\n".join(f"第{i}条。" for i in range(20)) + "\n"
        chunks = _chunk(text)
        joined = "\n".join(c.content_original for c in _children(chunks))
        for i in range(20):
            assert f"第{i}条。" in joined


class TestBrSplits:
    def test_br_in_paragraph_splits(self):
        chunks = _chunk("## 第一章\n\n前半段<br>后半段\n")
        children = _children(chunks)
        assert [c.content_original for c in children] == ["前半段", "后半段"]

    def test_br_in_table_row_does_not_split_the_cell(self):
        """表格行例外：格子里的内容不拆，一行表格还是一块。"""
        chunks = _chunk("## 附件\n\n| 策略 | 说明 |\n| --- | --- |\n| 甲 | 甲一<br>甲二 |\n")
        children = _children(chunks)
        # 表头行也是一个子块（规则如此，网页里人工删）
        assert children[0].content_original == "策略 说明"
        # 整行是一块，格子里的 <br> 换成句号
        assert children[1].content_original == "甲 甲一。 甲二"

    def test_br_in_table_cell_keeps_single_period(self):
        """前一句本来就带句号时不能再加一个（"。。"）。"""
        chunks = _chunk("## 附件\n\n| 甲 | 甲一。<br>甲二 |\n")
        assert _children(chunks)[0].content_original == "甲 甲一。 甲二"

    def test_table_row_becomes_plain_text(self):
        chunks = _chunk("## 附件\n\n| 正强化 | 以物质性… |\n")
        assert _children(chunks)[0].content_original == "正强化 以物质性…"


class TestMergePageBreaks:
    def test_tail_without_period_merges_with_next_page(self):
        chunks = _chunk_pages(
            [
                (1, "## 第一章\n\n前半句没说完"),
                (2, "接上后半句。\n"),
            ]
        )
        children = _children(chunks)
        assert len(children) == 1
        assert children[0].content_original == "前半句没说完接上后半句。"
        assert children[0].pdf_page_start == 1
        assert children[0].pdf_page_end == 2

    def test_tail_with_period_does_not_merge(self):
        chunks = _chunk_pages(
            [
                (1, "## 第一章\n\n说完了。"),
                (2, "新的一段。\n"),
            ]
        )
        children = _children(chunks)
        assert len(children) == 2
        assert children[0].pdf_page_end == 1

    def test_question_mark_counts_as_end(self):
        chunks = _chunk_pages(
            [
                (1, "## 第一章\n\n这样可以吗？"),
                (2, "新的一段。\n"),
            ]
        )
        assert len(_children(chunks)) == 2

    def test_table_row_does_not_merge_with_next_page(self):
        chunks = _chunk_pages(
            [
                (1, "## 附件\n\n| 甲 | 说明 |"),
                (2, "新的一段。\n"),
            ]
        )
        children = _children(chunks)
        assert len(children) == 2
        assert children[0].content_original == "甲 说明"

    def test_heading_does_not_merge(self):
        """下一页开头是标题 —— 标题是父块边界，不能把上一段拼到标题后面。"""
        chunks = _chunk_pages(
            [
                (1, "## 第一章\n\n前半句没说完"),
                (2, "## 第二章\n\n新的一段。\n"),
            ]
        )
        children = _children(chunks)
        assert len(children) == 2
        assert children[0].content_original == "前半句没说完"
        assert children[1].content_original == "新的一段。"

    def test_merge_across_three_pages(self):
        chunks = _chunk_pages(
            [
                (1, "## 第一章\n\n甲没说完"),
                (2, "乙没说完"),
                (3, "丙说完了。\n"),
            ]
        )
        children = _children(chunks)
        assert len(children) == 1
        assert children[0].content_original == "甲没说完乙没说完丙说完了。"
        assert (children[0].pdf_page_start, children[0].pdf_page_end) == (1, 3)


class TestStructure:
    def test_children_point_to_their_parent(self):
        chunks = _chunk("## 第一章\n\n甲。\n\n乙。\n")
        parents = {c.chunk_index for c in _parents(chunks)}
        children = _children(chunks)
        assert children
        for c in children:
            assert c.parent_chunk_index is not None
            assert c.parent_chunk_index in parents

    def test_first_parent_children_keep_parent_index_zero(self):
        """第一章子块的父块序号就是 0，不能被当成"没有父块"。"""
        chunks = _chunk("## 第一章\n\n甲。\n")
        first_parent = min(c.chunk_index for c in _parents(chunks))
        assert first_parent == 0
        first_children = [
            c
            for c in _children(chunks)
            if c.parent_chunk_index == first_parent
        ]
        assert first_children

    def test_chunk_index_is_continuous(self):
        chunks = _chunk("## 第一章\n\n甲。\n\n乙。\n\n## 第二章\n\n丙。\n")
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_parents_come_before_their_children(self):
        chunks = _chunk("## 第一章\n\n甲。\n\n## 第二章\n\n乙。\n")
        order = [c.chunk_index for c in chunks]
        for c in _children(chunks):
            assert order.index(c.parent_chunk_index) < order.index(c.chunk_index)

    def test_empty_input_returns_no_chunks(self):
        assert _chunk("") == []

    def test_body_before_any_heading_is_dropped(self):
        """没有父块可挂的正文不单独成块。"""
        chunks = _chunk("孤儿正文。\n\n## 第一章\n\n甲。\n")
        assert [c.content_original for c in _children(chunks)] == ["甲。"]


class TestClassify:
    def test_appendix_flag(self):
        chunks = _chunk("## 附件 1 干预要点\n\n| 甲 | 说明 |\n")
        assert all(c.knowledge_type == "appendix" for c in chunks)

    def test_definition_section(self):
        chunks = _chunk("## 六、名词解释\n\n挑战性行为：…\n")
        assert all(c.knowledge_type == "definition" for c in chunks)

    def test_narrative_by_default(self):
        chunks = _chunk("## 一、目的\n\n甲。\n")
        assert all(c.knowledge_type == "narrative" for c in chunks)
