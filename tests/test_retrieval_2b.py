"""阶段 2B 的检索层测试。

覆盖：查询改写、RRF 合并、重排降级、评测集结构。

**不碰向量 API**：需要 embedding 的用例靠 monkeypatch 挡掉，
或者只测纯函数部分。理由：测试要快、要有网也能跑。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rdi_agent.rag import rerank as rerank_mod
from rdi_agent.rag.ingest import chunk as chunker
from rdi_agent.rag import search as search_mod
from rdi_agent.rag.query_rewrite import RetrievalQueries

CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "retrieval_cases.jsonl"


def _title_of(section_code: str) -> str:
    """从 ``007_针对挑战性行为的干预`` 里取出标题部分。"""
    return section_code.split("_", 1)[1] if "_" in section_code else section_code


def _norm_title(title: str) -> str:
    """标题归一：只比字面，分隔符全去掉。

    章节编号里的分隔符和 ``title_zh`` 不总一致 ——
    ``006_针对非适应性_非功能性的…`` 对应的 ``title_zh`` 是
    ``针对非适应性、非功能性的…``（一个下划线一个顿号）。
    """
    return re.sub(r"[\s_、，,。·\-—－/（）()]+", "", title or "")


def _candidate(chunk_id, vector_rank=None, fts_rank=None):
    return search_mod._Candidate(
        chunk_id=chunk_id,
        chunk_index=0,
        section_code="000_test",
        section_title="T",
        pdf_page_start=1,
        pdf_page_end=1,
        snippet="snip",
        is_parent=False,
        knowledge_type="narrative",
        review_status="unreviewed",
        vector_rank=vector_rank,
        fts_rank=fts_rank,
    )


class TestRetrievalQueries:
    def test_dataclass_shape(self):
        q = RetrievalQueries(
            original_query="x",
            semantic_query="x",
            keyword_query="x OR y",
        )
        assert q.concept_ids == []
        assert q.filters == {}
        assert q.bridged_terms == []

    def test_to_dict_is_serialisable(self):
        q = RetrievalQueries(
            original_query="x",
            semantic_query="x",
            keyword_query="x OR y",
            concept_ids=["a"],
        )
        data = q.to_dict()
        assert json.dumps(data, ensure_ascii=False)
        assert data["concept_ids"] == ["a"]


class TestRrfMerge:
    def test_both_ranks_kept(self):
        merged = search_mod._rrf_merge(
            [_candidate("a", vector_rank=1)], [_candidate("a", fts_rank=3)]
        )
        assert len(merged) == 1
        assert merged[0].vector_rank == 1
        assert merged[0].fts_rank == 3

    def test_score_is_sum_of_both_paths(self):
        merged = search_mod._rrf_merge(
            [_candidate("a", vector_rank=1)], [_candidate("a", fts_rank=3)]
        )
        expected = 1.0 / (search_mod.RRF_K + 1) + 1.0 / (search_mod.RRF_K + 3)
        assert merged[0].score == pytest.approx(expected)

    def test_dedup_by_chunk_id(self):
        merged = search_mod._rrf_merge(
            [_candidate("a", vector_rank=1)], [_candidate("a", fts_rank=1)]
        )
        assert len(merged) == 1

    def test_single_path_keeps_own_rank(self):
        merged = search_mod._rrf_merge(
            [_candidate("a", vector_rank=2)], [_candidate("b", fts_rank=1)]
        )
        by_id = {c.chunk_id: c for c in merged}
        assert by_id["a"].vector_rank == 2
        assert by_id["a"].fts_rank is None
        assert by_id["b"].fts_rank == 1

    def test_sorted_by_score_desc(self):
        merged = search_mod._rrf_merge(
            [_candidate("a", vector_rank=1), _candidate("b", vector_rank=20)],
            [_candidate("c", fts_rank=1)],
        )
        scores = [c.score for c in merged]
        assert scores == sorted(scores, reverse=True)


class TestRerankDegradation:
    """降级必须可见 —— 静默降级比不降级更糟。"""

    def test_empty_documents(self):
        import asyncio

        result = asyncio.run(rerank_mod.rerank("q", []))
        assert result.order == []
        assert result.degraded is False

    def test_disabled_flag_degrades(self, monkeypatch):
        import asyncio

        from rdi_agent.config import get_settings

        monkeypatch.setattr(get_settings(), "rerank_enabled", False)
        result = asyncio.run(rerank_mod.rerank("q", ["a", "b"]))
        assert result.degraded is True
        assert "rerank_enabled" in result.reason
        # 降级时保持原顺序，不能丢候选
        assert result.order == [0, 1]

    def test_missing_api_key_degrades(self, monkeypatch):
        import asyncio

        from rdi_agent.config import get_settings

        monkeypatch.setattr(get_settings(), "llm_api_key", "")
        result = asyncio.run(rerank_mod.rerank("q", ["a", "b"]))
        assert result.degraded is True
        assert "API_KEY" in result.reason
        assert result.order == [0, 1]

    def test_http_error_degrades(self, monkeypatch):
        import asyncio

        import httpx

        from rdi_agent.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "rerank_enabled", True)
        monkeypatch.setattr(settings, "llm_api_key", "fake-key")

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        result = asyncio.run(rerank_mod.rerank("q", ["a", "b"]))
        assert result.degraded is True
        assert result.reason
        assert result.order == [0, 1]


class TestEvalSet:
    """评测集本身的结构约束 —— 评测集坏了，指标就没意义。"""

    def _cases(self):
        cases = []
        with CASES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    cases.append(json.loads(line))
        return cases

    def test_has_at_least_25_cases(self):
        assert len(self._cases()) >= 25

    def test_case_ids_unique(self):
        ids = [c["case_id"] for c in self._cases()]
        assert len(ids) == len(set(ids))

    def test_required_fields_present(self):
        for case in self._cases():
            assert case.get("case_id")
            assert case.get("query")
            assert "expected_sections" in case
            assert "must_include_terms" in case
            assert case.get("category")

    def test_has_irrelevant_cases(self):
        """必须有关���问题用例 —— 否则"不硬凑"这项没法验收。"""
        irrelevant = [
            c for c in self._cases() if not c.get("expected_sections")
        ]
        assert len(irrelevant) >= 2

    def test_section_codes_exist_in_book(self):
        """标注的章节必须真实存在 —— 编出来的章节会让指标失真。

        **按标题认，不按编号**（2026-10-26 改）。

        编号只是入库时排的序号，重新入库就可能整体挪位 ——
        实测新版入库多了一个封面章节，后面全部 +1，
        标注里的编号就全成了"不存在的章节"。

        章节的身份是**标题**（``title_zh``），编号只是索引。
        拿标题当判据，"这个章节还在不在"就不会被重新编号误伤。
        编号本身对不对，交给下面那条用例盯。
        """
        import asyncio

        from sqlalchemy import text

        from rdi_agent.db.engine import session_scope

        codes = {
            c
            for case in self._cases()
            for c in case.get("expected_sections", [])
        }

        async def check():
            async with session_scope() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT b.section_code, b.title_zh
                            FROM book_sections b
                            JOIN knowledge_sources k ON k.id = b.source_id
                            WHERE k.source_id = 'guideline2026'
                            """
                        )
                    )
                ).all()
            return rows

        known_titles = {_norm_title(title) for _, title in asyncio.run(check())}
        unknown = [c for c in codes if _norm_title(_title_of(c)) not in known_titles]
        assert not unknown, f"标注里有不存在的章节：{unknown}"

    def test_section_codes_are_in_sync_with_book(self):
        """编号必须和库里对得上 —— 否则检索评测的 recall 会静默变成 0。

        ``evals/run_retrieval.py`` 是**按 section_code 精确匹配**的
        （``if item["section_code"] in expected``）。
        编号一漂移，所有用例的 recall 全归零，**而且不报错** ——
        指标看着"很差"，真因是标注过期了。

        重新入库改了编号，这条会挡住，并指出该改哪。
        """
        import asyncio

        from sqlalchemy import text

        from rdi_agent.db.engine import session_scope

        codes = {
            c
            for case in self._cases()
            for c in case.get("expected_sections", [])
        }

        async def check():
            async with session_scope() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT b.section_code
                            FROM book_sections b
                            JOIN knowledge_sources k ON k.id = b.source_id
                            WHERE k.source_id = 'guideline2026'
                            """
                        )
                    )
                ).scalars().all()
            return set(rows)

        known = asyncio.run(check())
        unknown = codes - known
        assert not unknown, (
            f"标注里的章节编号和库里对不上：{unknown}。"
            f"重新入库改了编号，要同步更新 {CASES_PATH.name}（按标题对应，"
            "不是简单 +1）"
        )


class TestEvaluationMetrics:
    """指标计算本身。"""

    def test_summarize_computes_recall_and_mrr(self):
        from evals.run_retrieval import CaseResult, summarize

        good = CaseResult(
            case_id="a",
            query="q",
            category="c",
            expected_sections=["001_x"],
            must_include_terms=[],
            notes="",
            hit_at_5=True,
            first_hit_rank=1,
            reciprocal_rank=1.0,
        )
        bad = CaseResult(
            case_id="b",
            query="q",
            category="c",
            expected_sections=["001_x"],
            must_include_terms=[],
            notes="",
            hit_at_5=False,
            first_hit_rank=None,
            reciprocal_rank=0.0,
        )
        summary = summarize([good, bad])
        assert summary["recall_at_5"] == 0.5
        assert summary["mrr"] == 0.5
        assert summary["relevant"] == 2


class TestEmbedInputSwitch:
    """向量化输入形态的开关。

    **为什么要有这个开关**：中文路径的 ``content_normalized`` 是 jieba
    分词后的文本，而 bge-m3 是在自然中文上训的。到底该喂哪一份，
    是**要测出来的**，不是拍脑袋定的（见 ``tools/ab_embed_input.py``）。
    留成开关是为了这个选择**可复现、可回退** —— 评测集和报告里的
    数字要能对上用的是哪一种。
    """

    def test_two_forms_exist(self):
        from rdi_agent.rag.ingest import pipeline

        assert pipeline.EMBED_INPUT_NORMALIZED == "normalized"
        assert pipeline.EMBED_INPUT_ORIGINAL == "original"

    def test_normalized_uses_segmented_text(self):
        from rdi_agent.rag.ingest import pipeline

        chunk = chunker.Chunk(
            content_original="早期干预\n\n越早越好",
            content_normalized="早期 干预 越早 越好",
            pdf_page_start=1,
            pdf_page_end=1,
            chunk_index=0,
            parent_index=0,
            is_parent=True,
            knowledge_type="paragraph",
            section_code="001_x",
        )
        out = pipeline._embed_text(chunk, pipeline.EMBED_INPUT_NORMALIZED)
        assert out == "早期 干预 越早 越好"

    def test_original_uses_natural_text_without_newlines(self):
        from rdi_agent.rag.ingest import pipeline

        chunk = chunker.Chunk(
            content_original="早期干预\n\n越早越好",
            content_normalized="早期 干预 越早 越好",
            pdf_page_start=1,
            pdf_page_end=1,
            chunk_index=0,
            parent_index=0,
            is_parent=True,
            knowledge_type="paragraph",
            section_code="001_x",
        )
        out = pipeline._embed_text(chunk, pipeline.EMBED_INPUT_ORIGINAL)
        # 换行压成空格 —— 表格行之间的 \n\n 对嵌入只是噪声
        assert out == "早期干预 越早越好"
        assert "\n" not in out

    def test_original_is_not_segmented(self):
        """自然文本**不能**是分词形态 —— 这是这个开关存在的全部意义。"""
        from rdi_agent.rag.ingest import pipeline

        chunk = chunker.Chunk(
            content_original="（一）早期干预。早期干预可以显著改善预后。",
            content_normalized="（ 一 ） 早期干预 。 早期干预 可以 显著 改善 预后 。",
            pdf_page_start=1,
            pdf_page_end=1,
            chunk_index=0,
            parent_index=0,
            is_parent=True,
            knowledge_type="paragraph",
            section_code="001_x",
        )
        out = pipeline._embed_text(chunk, pipeline.EMBED_INPUT_ORIGINAL)
        assert out == chunk.content_original
        assert out != chunk.content_normalized
