"""确认结果 sidecar 的单元测试。

确认结果 = 人工在网页上做的三件事：**改文本、删块、标状态**。
这些结果存在 ``data/index/confirmed_chunks.json``，入库时回填 ——
因为 ``pipeline.write_to_db`` 是"先删后建"，直接改库的内容会被下一次入库冲掉。

**不碰 PDF、不碰数据库、不调 API** —— 纯函数，快。
"""

from __future__ import annotations

from rdi_agent.rag.ingest import chunk as chunker
from rdi_agent.rag.ingest import confirmed, parse_md


def _chunks(text: str) -> list[chunker.Chunk]:
    blocks = parse_md.parse_blocks(text, 1)
    blocks, _ = parse_md.merge_page_breaks(blocks)
    _, chunks = chunker.chunk_document(blocks, source_id="test")
    return chunks


def _file(chunks: list[chunker.Chunk], **overrides) -> confirmed.ConfirmedFile:
    """把块拼成一份确认结果文件。"""
    entries = []
    for chunk in chunks:
        entries.append(
            confirmed.ConfirmedEntry(
                chunk_index=chunk.chunk_index,
                is_parent=chunk.is_parent,
                parent_chunk_index=chunk.parent_chunk_index,
                section_code=chunk.section_code,
                title="测试",
                level=1,
                pdf_page_start=chunk.pdf_page_start,
                pdf_page_end=chunk.pdf_page_end,
                knowledge_type=chunk.knowledge_type,
                original_content=chunk.content_original,
                content=chunk.content_original,
            )
        )
    file = confirmed.ConfirmedFile(source_id="test", chunks=entries)
    file.assign_keys()
    for key, value in overrides.items():
        setattr(file, key, value)
    return file


class TestKeys:
    def test_parents_keyed_by_section(self):
        chunks = _chunks("## 甲\n\n一。\n\n## 乙\n\n二。\n")
        file = _file(chunks)
        parents = [e for e in file.chunks if e.is_parent]
        assert all(e.key.startswith("p:") for e in parents)

    def test_identical_text_on_same_page_gets_different_keys(self):
        """附件 2 里有两行一模一样的"只强化他行为" —— 只靠哈希分不出是哪一行。"""
        chunks = _chunks("## 甲\n\n重复\n\n重复\n")
        file = _file(chunks)
        children = [e for e in file.chunks if not e.is_parent]
        assert len(children) == 2
        assert children[0].key != children[1].key

    def test_same_text_on_different_pages_gets_different_keys(self):
        blocks = []
        for page in (1, 2):
            blocks.extend(parse_md.parse_blocks("## 甲\n\n重复。\n", page))
        blocks, _ = parse_md.merge_page_breaks(blocks)
        _, chunks = chunker.chunk_document(blocks, source_id="test")
        file = _file(chunks)
        children = [e for e in file.chunks if not e.is_parent]
        assert len({e.key for e in children}) == len(children)


class TestApplyConfirmed:
    def test_edit_is_applied(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.content = "改过的文本"
        file.assign_keys()

        new_chunks, marks, stats = confirmed.apply_confirmed(chunks, file)
        assert stats["edited"] == 1
        assert any("改过的文本" in c.content_original for c in new_chunks)

    def test_delete_drops_the_chunk(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.deleted = True
        file.assign_keys()

        new_chunks, _, stats = confirmed.apply_confirmed(chunks, file)
        assert stats["deleted"] == 1
        assert len([c for c in new_chunks if not c.is_parent]) == 1
        assert target.original_content not in [c.content_original for c in new_chunks]

    def test_deleting_a_parent_drops_its_children(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        parent = [e for e in file.chunks if e.is_parent][0]
        parent.deleted = True
        file.assign_keys()

        new_chunks, _, _ = confirmed.apply_confirmed(chunks, file)
        assert not new_chunks

    def test_parent_without_children_is_dropped(self):
        chunks = _chunks("## 甲\n\n一。\n")
        file = _file(chunks)
        for entry in file.chunks:
            if not entry.is_parent:
                entry.deleted = True
        file.assign_keys()

        new_chunks, _, _ = confirmed.apply_confirmed(chunks, file)
        assert not new_chunks

    def test_status_is_applied(self):
        chunks = _chunks("## 甲\n\n一。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.review_status = "reviewed"
        file.assign_keys()

        _, marks, _ = confirmed.apply_confirmed(chunks, file)
        assert marks == {target.chunk_index: "reviewed"}

    def test_unreviewed_does_not_override_older_marks(self):
        """网页上没动过的块也是 "unreviewed" —— 不能把 review.py 里更早的标记冲掉。"""
        chunks = _chunks("## 甲\n\n一。\n")
        file = _file(chunks)
        _, marks, _ = confirmed.apply_confirmed(chunks, file)
        assert marks == {}

    def test_parent_text_is_rebuilt_from_children(self):
        chunks = _chunks("## 甲\n\n一。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.content = "改过的文本"
        file.assign_keys()

        new_chunks, _, _ = confirmed.apply_confirmed(chunks, file)
        parent = [c for c in new_chunks if c.is_parent][0]
        assert parent.content_original == "甲\n改过的文本"

    def test_unmatched_entries_are_reported(self):
        """切分逻辑改了导致对不上时，要让人看见，不能静默丢掉。"""
        chunks = _chunks("## 甲\n\n一。\n")
        file = _file(chunks)
        file.chunks.append(
            confirmed.ConfirmedEntry(
                chunk_index=999,
                is_parent=False,
                parent_chunk_index=None,
                section_code="x",
                title="x",
                level=1,
                pdf_page_start=1,
                pdf_page_end=1,
                knowledge_type="narrative",
                original_content="对不上的块",
                content="对不上的块",
                key="c:1:deadbeefdeadbeef:0",
            )
        )
        _, _, stats = confirmed.apply_confirmed(chunks, file)
        assert stats["unmatched"] == ["c:1:deadbeefdeadbeef:0"]


class TestBuildFromConfirmed:
    def test_deleted_entries_are_not_written(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.deleted = True
        file.assign_keys()

        _, built, _ = confirmed.build_from_confirmed(file)
        assert len([c for c in built if not c.is_parent]) == 1

    def test_chunk_index_is_continuous_after_renumber(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        target = [e for e in file.chunks if not e.is_parent][0]
        target.deleted = True
        file.assign_keys()

        _, built, _ = confirmed.build_from_confirmed(file)
        assert [c.chunk_index for c in built] == list(range(len(built)))

    def test_children_still_point_to_their_parent(self):
        chunks = _chunks("## 甲\n\n一。\n\n二。\n")
        file = _file(chunks)
        _, built, _ = confirmed.build_from_confirmed(file)
        parents = {c.chunk_index for c in built if c.is_parent}
        for c in built:
            if not c.is_parent:
                assert c.parent_chunk_index in parents
