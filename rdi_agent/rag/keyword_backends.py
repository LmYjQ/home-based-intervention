"""关键词检索后端（计划 §9.2 最后一条任务）。

**为什么抽这一层**：关键词路的 SQL 原本写死在 ``search.py::_fts_candidates``
里，换一个关键词引擎就要把那个函数整个重写。现在有了一个协议，
换后端 = 加一个类 + 在 :func:`get_backend` 里注册一行。

**但别指望它能解决召回问题。** 2B 的评测（``evals/REPORT_v3.md`` §4）
测出 5 条未命中全是**中文同义表述对不上**：

    查询用词  指南里实际用词  命中
    一周      每周            0 块
    哪里      场地            0 块

分词是精确匹配，"一周"和"每周"在 ``content_tsv`` 里是两个不同的 lexeme。
**换 BM25 也召不回** —— 压根没召回到，谈不上排序。

所以 BM25 能改善的只有"词命中了但排得不对"，而那不是当前的瓶颈。
这也是为什么 :class:`ParadeDbBm25Backend` 只留了骨架没实现
（见那里的说明）。

**排序为什么不能直接用引擎的打分**：``ts_rank_cd`` 按**词频**排，
而中文查询里最高频的词恰恰是最没信息量的那些 —— "儿童"出现在几乎
每一块里。所以本项目的排序是「命中了几个**有区分度**的词」降序，
这个口径写在 :meth:`PostgresFtsBackend.build_query` 里，与引擎无关。
换后端时**这个口径要一起带过去**，否则排序会退化。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rdi_agent.rag import terms as terms_mod
from rdi_agent.rag.filters import CONTENT_FILTER, REVIEW_FILTER, select_columns


@runtime_checkable
class KeywordSearchBackend(Protocol):
    """关键词召回后端的协议。

    实现者只需要给出**候选查询**（SQL + 参数），
    排序口径和高频词过滤是**共用的**（见 :mod:`rdi_agent.rag.terms`），
    因为那套口径是针对中文的，与具体引擎无关。
    """

    #: 后端名，跟 ``settings.keyword_backend`` 对应。
    name: str

    #: 这个后端**真的能用**吗？
    #:
    #: 留了骨架但没实现的后端（如 ``paradedb_bm25``）把它设成 ``False`` ——
    #: :func:`get_backend` 会**跳过**它们退回默认后端。
    #: 少了这个标志，配置里写上 ``paradedb_bm25`` 就会让**每一次**检索
    #: 抛 ``NotImplementedError``，整条检索链挂掉 ——
    #: 而"检索链挂掉"比"用默认后端跑"糟糕得多。
    implemented: bool

    async def build_query(
        self,
        query: str,
        *,
        top_k: int,
        section_code: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        """构造这一路的 SQL 和参数。

        返回 ``None`` 表示"这个查询没法用这个后端处理"，
        调用方会退化成只用向量路。

        ``query`` 是**已经改写好的**关键词查询（``query_rewrite`` 负责
        桥接和分词），空格分隔。
        """
        ...


class PostgresFtsBackend:
    """Postgres 内置全文检索（默认后端）。

    ``content_tsv`` 是 ``to_tsvector('simple', content_normalized)``
    生成的。``simple`` 配置按**空格**分词，所以入库时必须先把内容
    jieba 分词（见 ``clean.normalize_zh``）—— 否则整段中文会变成
    一个 lexeme。查询这边同样要分词：查询串先 jieba 切成词，
    再拼成 OR 的 tsquery。

    **为什么用 OR 而不是 websearch_to_tsquery**：``websearch_to_tsquery``
    对多个词是 **AND** 语义（实测 ``websearch_to_tsquery('simple',
    '挑战性行为 消退 辅助')`` → ``'挑战性行为' & '消退' & '辅助'``）。
    关键词路要的是"命中其中任一个精确术语" —— AND 的话，
    一段话要同时提到三个词才被捞出来，召回几乎为零。

    所以这里**显式构造 OR 的 tsquery**：``to_tsquery('simple', 'a | b | c')``。

    **词必须转义**：tsquery 里 ``:`` 是权重、``&`` ``|`` ``!`` 是运算符，
    词里带这些字符会报语法错。所以用 ``quote_literal`` 之外的办法 ——
    在 Python 侧把非字母数字汉字的字符剔掉，只留安全的词
    （见 :func:`rdi_agent.rag.terms.keyword_terms`）。
    """

    name = "postgres_fts"
    implemented = True

    async def build_query(
        self,
        query: str,
        *,
        top_k: int,
        section_code: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        terms = terms_mod.keyword_terms(query)
        if not terms:
            return None

        tsquery = " | ".join(f"'{t}'" for t in terms)
        sql = (
            select_columns()
            + """
        WHERE c.content_tsv @@ to_tsquery('simple', :tsquery)
          AND """
            + REVIEW_FILTER
            + CONTENT_FILTER
        )
        params: dict[str, Any] = {"tsquery": tsquery}
        if section_code:
            sql += " AND s.section_code = :section_code"
            params["section_code"] = section_code

        # ---- 排序的第一关键字：命中了几个**有区分度**的词 ----
        #
        # **为什么不用 ts_rank_cd 排第一**：它按**词频**排序，而中文查询里
        # 最高频的词恰恰是最没信息量的那些 —— "儿童"出现在 295 块里、
        # "行为" 288 块、"干预" 286 块（全文共 73 块）。
        #
        # 实测（"孩子做对了要怎么奖励"，命中"正强化"的那一块）：
        # ts_rank_cd 排序下它排**第 10** —— 因为含"孩子"的块词频高，
        # 排名虚高，把真正的答案压下去了。
        #
        # 改成"命中**区分度词**的数量降序"之后：命中词多 = 覆盖了查询里
        # 那些**有信息量**的说法 = 更可能是答案。命中数相同时再按 ts_rank 排。
        #
        # **区分度词 = 剔掉高频词之后的词**（见 ``terms.discriminative_terms``）。
        # 高频词仍然参与**召回**（命中就是命中），只是不参与**排序** ——
        # 彻底删掉的话，"孩子摔东西"这种查询会因为没有区分度词而召不回东西。
        #
        # **全被高频词占满时**：区分度词为空，排序退化成纯 ts_rank。
        # 这不是错误 —— 查询本来就是"孩子""干预"这种词时，没什么可分的。
        disc = terms_mod.discriminative_terms(terms)
        if disc:
            hit_count_sql = """
                SELECT count(*) FROM unnest(ARRAY[
                    {terms}
                ]) AS t(word)
                WHERE c.content_tsv @@ to_tsquery('simple', word)
            """.format(
                terms=", ".join(f"'{t}'" for t in disc)
            )
            # **命中词的"信息量"之和** —— 长的词比短的词有信息量。
            #
            # 为什么需要这一层：命中词数相同时（都是 1 个），ts_rank 会按
            # 词频排，而词频高的恰恰是虚词。实测（"能不能用手机APP或者线上
            # 课程来干预"，期望章节 010 数智干预康复）：
            #
            #   区分度词 = [不能, 手机, APP, 或者, 线上, 课程, 数智干预康复]
            #   只命中"数智干预康复"的块（信息量 6 字）
            #   vs 只命中"课程"的块（信息量 2 字）
            #
            # 按 ts_rank 排，"课程"的块在前；按信息量排，"数智干预康复"在前 ——
            # 后者才是答案。
            weight_sql = """
                SELECT COALESCE(sum(length(t.word)), 0)
                FROM unnest(ARRAY[
                    {terms}
                ]) AS t(word)
                WHERE c.content_tsv @@ to_tsquery('simple', t.word)
            """.format(
                terms=", ".join(f"'{t}'" for t in disc)
            )
            sql += f" AND ({hit_count_sql}) > 0"
            order_by = (
                f" ORDER BY ({hit_count_sql}) DESC,"
                f" ({weight_sql}) DESC,"
                " ts_rank_cd(c.content_tsv, to_tsquery('simple', :tsquery)) DESC"
            )
        else:
            order_by = (
                " ORDER BY ts_rank_cd(c.content_tsv, to_tsquery('simple', :tsquery)) DESC"
            )
        sql += order_by + " LIMIT :k"
        params["k"] = top_k
        return sql, params


class ParadeDbBm25Backend:
    """ParadeDB 的 BM25 索引（**未实现**）。

    BM25 是搜索引擎那套经典的相关性打分，比 ``ts_rank_cd`` 成熟 ——
    ``ts_rank_cd`` 主要按词频排，而中文里最高频的词恰恰最没信息量。

    **为什么不实现**：

    1. **本地 PG 装不上** —— 需要 ``pg_search`` 扩展，本项目的
       ``deploy/docker-compose.yml`` 用的是标准 ``pgvector/pgvector:pg16``，
       装不了。写了也跑不了，等于写了个不能验证的分支。
    2. **不解决当前瓶颈** —— 见模块 docstring：5 条未命中全是
       同义表述召不回（关键词路 0 块命中），BM25 改善的是排序，
       而排序不是瓶颈。

    **要实现的话怎么做**：

    - 在 Postgres 上建 BM25 索引（``CREATE INDEX ... USING bm25
      (content, content_tsv)``），查询走 ``content @@ '关键词'``，
      分数用 ``paradedb.score(...)``。
    - **但要把本模块的排序口径一起带过去**：命中区分度词数 → 信息量 →
      引擎分。否则中文排序会退化成按词频排。
    - 入库侧还要考虑：BM25 索引的字段是原文本还是分词文本？
      现在的 ``content_tsv`` 是分词文本，BM25 对分词文本的词频统计
      会失真（虚词被切开）。

    所以这是个**已知的后续项**，不是遗漏。
    """

    name = "paradedb_bm25"
    implemented = False

    async def build_query(
        self,
        query: str,
        *,
        top_k: int,
        section_code: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        raise NotImplementedError(
            "paradedb_bm25 后端尚未实现。原因：(1) 本地 Postgres 装不上 "
            "pg_search 扩展，写了也验证不了；(2) 2B 评测测出当前瓶颈是"
            "中文同义表述召不回（关键词路 0 块命中），换 BM25 改善不了 —— "
            "BM25 改善的是排序，而排序不是瓶颈。"
            "详见 evals/REPORT_v3.md §4。"
        )


# 后端注册表。配置 ``settings.keyword_backend`` 按这里取。
_BACKENDS: dict[str, KeywordSearchBackend] = {
    PostgresFtsBackend.name: PostgresFtsBackend(),
    ParadeDbBm25Backend.name: ParadeDbBm25Backend(),
}

# 会被当成"没配"处理的默认值 —— 未实现的后端在它下面静默退回默认后端。
_FALLBACK = PostgresFtsBackend.name


def get_backend(name: str | None = None) -> KeywordSearchBackend:
    """按名字取后端。未知名字**或未实现的后端**都退回默认后端。

    **为什么退回而不是报错**：配置错了就让整条检索链挂掉，
    家长拿不到任何建议 —— 这比"用默认后端跑"糟糕得多。
    未实现的后端（``paradedb_bm25``）同理 —— 它会抛
    ``NotImplementedError``，那等于把整条链搞挂。

    **但退化必须是可见的**：``/health`` 同时报
    ``keyword_backend``（实际生效的）和 ``keyword_backend_configured``
    （配置里写的），两者不一致就是配错了。
    """
    if not name:
        name = _FALLBACK
    backend = _BACKENDS.get(name)
    if backend is None or not backend.implemented:
        return _BACKENDS[_FALLBACK]
    return backend


def available_backends() -> list[str]:
    """注册表里的**可用**后端名（不含未实现的）。"""
    return sorted(n for n, b in _BACKENDS.items() if b.implemented)


def registered_backends() -> list[str]:
    """注册表里的**全部**后端名（含未实现的骨架）。"""
    return sorted(_BACKENDS)
