"""关键词检索后端的切换接口（计划 §9.2 最后一条任务）。

**这里测的是"切换机制"本身，不是"哪个后端更好"** ——
后者是评测脚本（``evals/run_retrieval.py``）的事。

**不碰数据库**：后端只负责"造一条查询"，造完了交给调用方去跑。
所以这些用例不连 Postgres 也能跑。
"""

from __future__ import annotations

import pytest

from rdi_agent.rag import keyword_backends as kb
from rdi_agent.rag import search as search_mod
from rdi_agent.rag import terms as terms_mod


class TestBackendRegistry:
    def test_default_backend_is_postgres_fts(self):
        assert kb.get_backend().name == "postgres_fts"

    def test_unknown_name_falls_back_to_default(self):
        """配错名字不能把检索链搞挂。

        配置错了就让整条检索链挂掉、家长拿不到任何建议 ——
        这比"用默认后端跑"糟糕得多。退化必须可见：``/health`` 同时报
        实际生效的和配置里写的。
        """
        assert kb.get_backend("不存在的后端").name == "postgres_fts"

    def test_none_falls_back_to_default(self):
        assert kb.get_backend(None).name == "postgres_fts"

    def test_available_backends_only_lists_implemented(self):
        """**没实现的后端不算"可用"** —— 配上去会让检索链直接挂掉。"""
        names = kb.available_backends()
        assert "postgres_fts" in names
        assert "paradedb_bm25" not in names

    def test_registered_backends_lists_skeleton(self):
        """注册表里还留着骨架（方便看"有哪些后端计划过"）。"""
        assert "paradedb_bm25" in kb.registered_backends()

    def test_unimplemented_backend_falls_back(self):
        """配了 ``paradedb_bm25`` **不能**让每次检索都抛异常。

        这是真的坑：后端只留了骨架，``build_query`` 抛
        ``NotImplementedError``。要是直接返回它，配上这个名字
        **每一次**检索都会炸，整条检索链挂掉 ——
        而"检索链挂掉"比"用默认后端跑"糟糕得多。
        """
        assert kb.get_backend("paradedb_bm25").name == "postgres_fts"


class TestPostgresFtsBackend:
    async def test_empty_query_returns_none(self):
        """没有可用的词就交回 None —— 调用方会退化成只用向量路。"""
        backend = kb.PostgresFtsBackend()
        built = await backend.build_query("的 了 和", top_k=5, section_code=None)
        assert built is None

    async def test_builds_or_tsquery(self):
        """关键词路要的是"命中其中任一个精确术语"。

        ``websearch_to_tsquery`` 对多个词是 AND 语义，一段话要同时提到
        三个词才被捞出来，召回几乎为零。所以显式构造 OR。
        """
        backend = kb.PostgresFtsBackend()
        built = await backend.build_query(
            "挑战性行为 消退", top_k=5, section_code=None
        )
        assert built is not None
        sql, params = built
        assert params["tsquery"] == "'挑战性行为' | '消退'"
        assert "to_tsquery('simple', :tsquery)" in sql
        assert "|" in params["tsquery"]

    async def test_applies_shared_filters(self):
        """两路召回的过滤必须一致 —— 漏掉一路，坏片段就从另一路冒出来。"""
        backend = kb.PostgresFtsBackend()
        sql, _ = await backend.build_query("干预", top_k=5, section_code=None)
        assert "needs_correction" in sql
        assert "reference" in sql

    async def test_section_filter(self):
        backend = kb.PostgresFtsBackend()
        sql, params = await backend.build_query(
            "干预", top_k=5, section_code="006_x"
        )
        assert "s.section_code = :section_code" in sql
        assert params["section_code"] == "006_x"

    async def test_top_k_becomes_limit(self):
        backend = kb.PostgresFtsBackend()
        sql, params = await backend.build_query("干预", top_k=7, section_code=None)
        assert "LIMIT :k" in sql
        assert params["k"] == 7

    async def test_sql_is_executable_shape(self):
        """查一下表名的拼写 —— 拼错了要到运行时才发现。"""
        backend = kb.PostgresFtsBackend()
        sql, _ = await backend.build_query("挑战性行为", top_k=5, section_code=None)
        assert "FROM knowledge_chunks c" in sql
        assert "JOIN book_sections s" in sql


class TestParadeDbBm25Backend:
    async def test_raises_not_implemented_with_reason(self):
        """未实现的后端要**说清楚为什么**，不是静默退化。

        直接调它会抛 —— 但 :func:`get_backend` 不会把它交出去。
        留着这个异常是有意的：谁要是绕过注册表直接用它，
        会立刻知道"这个后端没实现"，而不是拿到一个假结果。
        """
        backend = kb.ParadeDbBm25Backend()
        assert backend.implemented is False
        with pytest.raises(NotImplementedError) as exc:
            await backend.build_query("干预", top_k=5, section_code=None)
        message = str(exc.value)
        assert "pg_search" in message
        assert "同义表述" in message


class TestSearchWiring:
    def test_search_reads_configured_backend(self):
        """配置里的 ``keyword_backend`` 真的要接上。

        之前这个字段存在但没人读 —— 配了也没用。
        """
        from rdi_agent.config import get_settings

        assert get_settings().keyword_backend == "postgres_fts"

    def test_search_module_reexports_filters(self):
        """``search.REVIEW_FILTER`` 这种写法要继续能用。

        过滤常量搬到 ``filters.py`` 之后，历史引用（测试、文档）不能断。
        """
        assert "needs_correction" in search_mod.REVIEW_FILTER
        assert search_mod.REVIEW_FILTER is kb.REVIEW_FILTER

    def test_high_freq_helper_reexported(self):
        """``search_mod.ensure_high_freq_terms()`` 是调用方一贯的写法。

        改了调用点就多一处"评测和线上走的不是同一套"的风险 ——
        2B 评测脚本的 0.88 假值就是这么来的。
        """
        assert search_mod.ensure_high_freq_terms is terms_mod.ensure_high_freq_terms


class TestTermsMoved:
    """词表逻辑搬到 ``terms.py`` 之后，行为不能变。"""

    def test_keyword_terms_drops_single_char_and_stops(self):
        """单字虚词（的/了/和）命中等于没命中，剔掉。"""
        assert terms_mod.keyword_terms("的 了 和 挑战性行为") == ["挑战性行为"]

    def test_keyword_terms_strips_tsquery_operators(self):
        """tsquery 的运算符出现在词里会报语法错。"""
        terms = terms_mod.keyword_terms("挑战性行为: (消退) 辅助")
        assert terms == ["挑战性行为", "消退", "辅助"]

    def test_keyword_terms_dedupes(self):
        assert terms_mod.keyword_terms("干预 干预 行为") == ["干预", "行为"]

    def test_discriminative_terms_without_high_freq_returns_all(self):
        """高频词表没加载时，退化成"不剔任何词"。"""
        assert terms_mod.discriminative_terms(["干预", "行为"]) == ["干预", "行为"]
