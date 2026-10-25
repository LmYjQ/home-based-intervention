"""两路召回共用的 SQL 片段。

单独成文件是因为**过滤条件必须两路一致** ——
向量路和关键词路漏掉一条过滤，被排除的坏片段就会从另一路冒出来。
上次加 ``review_status`` 过滤时就差点只加到一路。

这个文件不 import 别的模块，所以 :mod:`rdi_agent.rag.search` 和
:mod:`rdi_agent.rag.keyword_backends` 都能引它，不会绕成环。
"""

from __future__ import annotations

# 召回过滤（计划 §5.4）：标了 needs_correction 的片段不进召回。
REVIEW_FILTER = "c.review_status <> 'needs_correction'"

# 参考文献 / 脚注 / 推荐阅读不进召回。
#
# **为什么要按标题再判一次**：``knowledge_type='reference'`` 是入库时
# ``ingest/chunk.py:classify()`` 打的标记，而它只认标题含 "references" 的章节。
# "FOOTNOTES"（section_code=028_footnotes）因此没被标成 reference，
# 会混进证据里 —— 给家长看一段脚注列表没有任何意义。
# 所以这里除了看 knowledge_type，还按标题兜一层。
#
# 用标题规则而不是硬编码 section_code：书一换、版本一改，
# 章节编号就变了，标题规则不用动。
CONTENT_FILTER = """
          AND c.knowledge_type <> 'reference'
          AND s.title_original NOT ILIKE '%reference%'
          AND s.title_original NOT ILIKE '%footnote%'
          AND s.title_original NOT ILIKE '%recommended reading%'
"""

# 两路召回共用的列。写两遍就容易改漏一处。
SELECT_COLUMNS = """
        SELECT c.id, c.chunk_index, s.section_code, s.title_original,
               c.pdf_page_start, c.pdf_page_end, c.content_original,
               (c.parent_chunk_id IS NULL) AS is_parent,
               c.knowledge_type, c.review_status
        FROM knowledge_chunks c
        JOIN book_sections s ON s.id = c.section_id
"""


def select_columns(extra: str = "") -> str:
    """两路召回共用的列。``extra`` 是这一路独有的列。

    向量路要多取一个 ``similarity``（余弦相似度）。
    """
    if not extra:
        return SELECT_COLUMNS
    return SELECT_COLUMNS.replace(
        "c.knowledge_type, c.review_status\n",
        f"c.knowledge_type, c.review_status,\n{extra}\n",
    )
