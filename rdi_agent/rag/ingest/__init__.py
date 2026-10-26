"""书籍入库流水线（计划 §9.1，阶段 2A）。

    python -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf

分几步，每步的中间结果都落盘，出问题不用从头再解析：

- ``parse_pdf``  逐页抽文本，页码 / 字符数 / 跳过原因
- ``clean``      把 calibre 的硬换行接回段落，清页眉页脚
- ``sections``   目录 -> 章节树（页区间 + 父子关系）
- ``chunk``      父子块切分
- ``embeddings`` bge-m3 向量化
- ``concepts``   核心术语表（中文别名 -> 书里的英文术语）

``pipeline`` 把它们串起来并写库；``concepts`` 要在入库之后单独跑一次
（术语的出处块依赖于块已经存在）。
"""
