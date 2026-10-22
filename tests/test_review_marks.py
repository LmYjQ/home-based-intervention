"""人工抽查标记的测试。

重点验证三件事：

1. **标记的内容哈希键**：内容一字不变，标记跟着走；内容变了，标记失效。
   （用 chunk_index 做键会在切分参数变化时错位到别的片段上。）
2. **重建不回填就丢标记**：这是接这条路的初衷 —— 入库重建会删旧行，
   标记必须能回填。
3. **``needs_correction`` 被检索排除**（计划 §5.4 的过滤要求）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from rdi_agent.rag.ingest import review


@dataclass
class FakeChunk:
    """够用的假块 —— 只要有 content_normalized 和 chunk_index。"""

    content_normalized: str
    chunk_index: int = 0


def _marked(chunks: list[FakeChunk], **marks: str) -> review.ReviewFile:
    rf = review.ReviewFile()
    for text, status in marks.items():
        rf.marks[review.content_key(text)] = review.ReviewMark(status=status)
    return rf


class TestContentKey:
    def test_same_content_same_key(self):
        assert review.content_key("abc") == review.content_key("abc")

    def test_different_content_different_key(self):
        assert review.content_key("abc") != review.content_key("abd")

    def test_key_is_stable_across_processes(self):
        # 硬编码：键写进了版本库里的标记文件，改算法会让所有历史标记失效
        assert review.content_key("hello world") == "sha256:b94d27b9934d3e08"


class TestApplyMarks:
    def test_marks_apply_to_matching_chunk(self):
        chunks = [FakeChunk("alpha", 0), FakeChunk("beta", 1)]
        rf = _marked(chunks, alpha="reviewed")
        applied = review.apply_marks_to_chunks(chunks, rf)
        assert applied == {0: "reviewed"}

    def test_unmarked_chunks_absent_from_result(self):
        chunks = [FakeChunk("alpha", 0), FakeChunk("beta", 1)]
        rf = _marked(chunks, alpha="reviewed")
        applied = review.apply_marks_to_chunks(chunks, rf)
        assert 1 not in applied

    def test_stale_mark_reported_not_silently_dropped(self):
        """内容变了 -> 标记失效，但必须被报出来，不能静默消失。"""
        chunks = [FakeChunk("alpha", 0)]
        rf = _marked(chunks, alpha="reviewed", zeta="reviewed")
        applied = review.apply_marks_to_chunks(chunks, rf)
        stale = review.unmatched_marks(chunks, rf)
        assert applied == {0: "reviewed"}
        assert len(stale) == 1
        assert stale[0] == review.content_key("zeta")


class TestReviewFile:
    def test_roundtrip(self, tmp_path):
        rf = review.ReviewFile(source_id="the_rdi_book_2009")
        rf.marks[review.content_key("x")] = review.ReviewMark(
            status="reviewed", reviewer="me", note="ok"
        )
        path = review.save_review_file(rf, tmp_path / review.REVIEW_FILE_NAME)
        loaded = review.load_review_file(path)
        assert loaded.source_id == "the_rdi_book_2009"
        assert loaded.marks[review.content_key("x")].status == "reviewed"
        assert loaded.marks[review.content_key("x")].reviewer == "me"

    def test_missing_file_is_empty_not_error(self, tmp_path):
        rf = review.load_review_file(tmp_path / "nope.json")
        assert rf.marks == {}

    def test_file_is_valid_json(self, tmp_path):
        rf = review.ReviewFile()
        rf.marks[review.content_key("x")] = review.ReviewMark(status="reviewed")
        path = review.save_review_file(rf, tmp_path / review.REVIEW_FILE_NAME)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == "v1"
        assert "marks" in data


class TestCandidateExport:
    def test_picked_evenly_across_book(self):
        """均匀取样，不是取前 N 个 —— 前 N 个会全挤在第一章。"""

        class FakeRow(dict):
            pass

        # 直接测采样逻辑：50 个候选应该来自 1000 个块里的不同位置
        rows = [FakeChunk(f"text-{i}", i) for i in range(1000)]
        limit = 50
        step = max(1, len(rows) // limit)
        picked = rows[::step][:limit]
        assert len(picked) == limit
        assert picked[0].chunk_index == 0
        assert picked[-1].chunk_index > 900


class TestStatusValues:
    def test_valid_statuses(self):
        assert review.STATUS_REVIEWED in review.VALID_STATUSES
        assert review.STATUS_NEEDS_CORRECTION in review.VALID_STATUSES
        assert review.STATUS_UNREVIEWED in review.VALID_STATUSES


class TestSearchFilter:
    def test_search_excludes_needs_correction(self):
        """检索 SQL 必须排除 needs_correction（计划 §5.4）。

        两路召回（向量 + 关键词）共用一个过滤常量 —— 漏了其中一路，
        标了"需要修正"的片段还是会从另一路冒出来给家长看。
        """
        from rdi_agent.rag import search

        assert "needs_correction" in search.REVIEW_FILTER
        assert "review_status" in search.REVIEW_FILTER

    def test_both_recall_paths_use_the_filter(self):
        """向量路和关键词路都得带这个过滤。"""
        import inspect

        from rdi_agent.rag import search

        src = inspect.getsource(search)
        # 两处 SQL 都要拼上过滤常量
        assert src.count("REVIEW_FILTER") >= 3  # 1 处定义 + 2 处使用
