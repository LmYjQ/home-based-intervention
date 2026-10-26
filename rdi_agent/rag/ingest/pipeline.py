"""入库流水线：Markdown -> 可追溯的向量 + 关键词索引。

    python -m rdi_agent.rag.ingest.pipeline --md-dir data/ocr/guideline2026

**幂等**：整个 source 的块先删后建。重建后条目数必须一致 ——
这是计划 §9.1 的验收项之一，所以重跑一次不会多出半套数据。

**分步可查**：每一步的中间结果都落盘（``pages.jsonl`` / ``section_map.json``），
出问题时不用从头再解析一遍。

**版权**：这份指南的授权未核实，``rights_status`` 固定 ``internal_review_only``。
库里存的是**供检索的片段**，不是给用户看的原文；面向用户的引用走
"摘要 + 页码"，不给大段原文。

**为什么源是 Markdown 而不是 PDF**：指南是扫描件，PDF 里没有文字层
（``fitz`` 抽出来是空串）。所以入库源是 OCR 转出来的分页 Markdown ——
一个文件对应 PDF 的一页，**页码因此可以保留**，引用能翻回扫描件。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from rdi_agent.config import get_settings
from rdi_agent.db.engine import session_scope
from rdi_agent.db.models import BookSection, KnowledgeChunk, KnowledgeSource
from rdi_agent.rag.ingest import chunk as chunker
from rdi_agent.rag import search as search_mod
from rdi_agent.rag.ingest import embeddings, parse_md, review, sections_zh
from rdi_agent.rag.ingest.sections import Section, SectionMap

SOURCE_ID = "guideline2026"
SOURCE_TITLE = "孤独症谱系障碍儿童干预康复指南（2026 年版）"
SOURCE_AUTHORS = ["国家卫生健康委员会"]
PUBLICATION_YEAR = 2026
# 授权未核实：只用于本地研发和内部演示
RIGHTS_STATUS = "internal_review_only"
INDEX_VERSION = "v2"
SOURCE_LANGUAGE = "zh"

# 向量化的输入取哪一份文本。
#
# **两���内容各有用途，不能混**：
#
# - ``content_normalized``：jieba 分词后的文本。**FTS 需要它** ——
#   ``content_tsv`` 是 ``to_tsvector('simple', content_normalized)`` 的
#   生成列，``simple`` 按空格分词，不分词的话整段中文是一个 lexeme。
# - ``content_original``：没分词的原文。
#
# **问题在于给 bge-m3 喂哪一份**：bge-m3 是在**自然中文**上训的，
# 喂它"（ 一 ） 早期干预 。 早期干预 可以 显著 ..."这种分好词的文本，
# 拿到的不是它训练时见过的分布。
#
# 实测（``tools/ab_embed_input.py``，34 条中文用例，只走向量路）：
# 两种输入的 recall@5 **完全相同**（0.7941），MRR 自然文本略高
# （0.5983 → 0.6361）。所以这不是个"修 bug"，是**在两者间选一个更优的**。
#
# 留成常量而不是写死，是为了让这个选择**可复现、可回退** —— 评测集、
# 报告里的数字都要能对上用的是哪一种。
EMBED_INPUT_NORMALIZED = "normalized"  # 分词文本（当前默认）
EMBED_INPUT_ORIGINAL = "original"  # 自然文本


def _embed_text(chunk: chunker.Chunk, embed_input: str) -> str:
    """挑一份文本送去向量化。

    ``original`` 走 ``content_original``，但要把换行压成空格 ——
    表格行之间是 ``\\n\\n``，那些换行对嵌入没有意义，只是噪声。
    """
    if embed_input == EMBED_INPUT_ORIGINAL:
        return " ".join(chunk.content_original.split())
    return chunk.content_normalized


@dataclass
class IngestResult:
    pages_total: int
    pages_parsed: int
    pages_skipped: int
    skipped_detail: dict[str, list[int]]
    sections: int
    parent_chunks: int
    child_chunks: int
    embedded: int
    content_hash: str
    report_path: str


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _dir_hash(directory: Path) -> str:
    """目录内容哈希。

    **按文件名排序** —— 页码顺序必须稳定，否则同样的内容每次入库
    得到不同的 hash，"内容变了没变"就判断不了了。

    只哈希 ``page_*.md`` —— 目录里可能还有别的文件（合并版），
    它们不参与入库，不该影响 hash。
    """
    h = hashlib.sha256()
    for path in sorted(directory.glob("page_*.md")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def _section_pages(
    section_map: SectionMap,
    blocks: list[parse_md.Block],
) -> list[tuple[Section, list[tuple[int, str]]]]:
    """把正文块按章节归位。

    **按块归，不按页归**：指南的一页里可能有多个标题（第 3 页同时有
    "三、干预康复内容"和两个"（一）（二）"），按页归会把一页里
    不同章节的内容混在一起 —— 章节名和正文对不上，引用会指错地方。

    **表格行各自成段**：附件 1–4 全是宽表格，**一行就是一条完整的
    干预要点**（"年龄 → 干预目标 → 干预内容 → 干预重点"）。
    如果把整张表当成连续段落，子块会把 7 条策略（正强化 / 负强化 /
    消退 / 负惩罚 / 辅助 / 反应中断 / 差别强化）挤进同一个块 ——
    问"孩子做对了怎么奖励"时，这个大块和讲挑战性行为的块竞争，
    正确的答案挤不进前 5 名。

    所以这里把表格行用 ``\\n\\n`` 分隔 —— 切块器按空行分段，
    表格行就成了独立的段落。
    """
    grouped = sections_zh.assign_blocks(blocks, section_map)
    result: list[tuple[Section, list[tuple[int, str]]]] = []
    for section, items in grouped:
        parts: list[tuple[int, str]] = []
        for page, text in items:
            # 表格行已经在 parse_blocks 里是独立的 Block，
            # 这里只要把同一页的多个块用空行连起来，让切块器能分开它们。
            parts.append((page, text))
        result.append((section, parts))
    return result


async def write_to_db(
    *,
    source: KnowledgeSource,
    section_map: sections.SectionMap,
    chunks: list[chunker.Chunk],
    vectors: list[list[float]] | None,
    marks: dict[int, str] | None = None,
) -> dict[str, int]:
    """把章节和块写进库。父块和子块一起写，子块用 ``parent_chunk_id`` 指回去。

    ``marks`` 是人工抽查标记，{chunk_index: status}（见
    :mod:`rdi_agent.rag.ingest.review`）。重建会删掉旧行，所以标记必须在
    重建时**回填** —— 否则人工核对的结果会被下一次入库冲掉。
    没命中的块一律 ``unreviewed``。
    """
    marks = marks or {}
    async with session_scope() as session:
        # 先删干净：重建必须得到同样的条目数
        old_source = (
            await session.execute(
                select(KnowledgeSource).where(KnowledgeSource.source_id == source.source_id)
            )
        ).scalar_one_or_none()
        if old_source is not None:
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.source_id == old_source.id)
            )
            await session.execute(
                delete(BookSection).where(BookSection.source_id == old_source.id)
            )
            await session.delete(old_source)
            await session.flush()

        session.add(source)
        await session.flush()

        # 章节：先全部插进去拿到 id，再回填父子关系
        rows: dict[str, BookSection] = {}
        for section in section_map.sections:
            row = BookSection(
                source_id=source.id,
                section_code=section.section_code,
                title_original=section.title,
                title_zh=section.title_zh,
                section_order=section.order,
                pdf_page_start=section.pdf_page_start,
                pdf_page_end=section.pdf_page_end,
                print_page_start=None,  # 印刷页码在重排后的 PDF 里丢了
                print_page_end=None,
                parse_status="parsed",
            )
            session.add(row)
            rows[section.section_code] = row
        await session.flush()

        for section in section_map.sections:
            if not section.parent_code:
                continue
            parent = rows.get(section.parent_code)
            child = rows.get(section.section_code)
            if parent is not None and child is not None:
                child.parent_section_id = parent.id
        await session.flush()

        # 向量按块在列表里的位置对齐（chunks 的顺序就是 chunk_index 的顺序）
        vectors_by_index: dict[int, list[float]] = {
            i: v for i, v in enumerate(vectors or [])
        }

        # 块：父块先写，子块才能拿到父块的 UUID
        chunk_ids: dict[int, uuid.UUID] = {}
        for position, item in enumerate(chunks):
            if not item.is_parent:
                continue
            row = KnowledgeChunk(
                source_id=source.id,
                section_id=rows[item.section_code].id if item.section_code in rows else None,
                parent_chunk_id=None,
                chunk_index=item.chunk_index,
                content_original=item.content_original,
                content_normalized=item.content_normalized,
                embedding=vectors_by_index.get(position),
                knowledge_type=item.knowledge_type,
                concept_ids_json=item.concept_ids,
                pdf_page_start=item.pdf_page_start,
                pdf_page_end=item.pdf_page_end,
                parse_confidence=1.0,
                review_status=marks.get(item.chunk_index, review.STATUS_UNREVIEWED),
                embedding_model=embeddings_model(),
                embedding_version=INDEX_VERSION,
            )
            session.add(row)
            await session.flush()
            chunk_ids[item.chunk_index] = row.id

        for position, item in enumerate(chunks):
            if item.is_parent:
                continue
            row = KnowledgeChunk(
                source_id=source.id,
                section_id=rows[item.section_code].id if item.section_code in rows else None,
                # 注意：不能用 `item.parent_chunk_index or -1`。
                # `or` 把 0 当假值 —— 第一章的子块（父块序号就是 0）会被
                # 算成 -1，父块 id 找不到，parent_chunk_id 变成 NULL。
                # 那样这些子块在检索里会被当成父块（is_parent 靠
                # parent_chunk_id IS NULL 判定），父块扩展也找不到它们的父块。
                parent_chunk_id=chunk_ids.get(
                    item.parent_chunk_index
                    if item.parent_chunk_index is not None
                    else -1
                ),
                chunk_index=item.chunk_index,
                content_original=item.content_original,
                content_normalized=item.content_normalized,
                embedding=vectors_by_index.get(position),
                knowledge_type=item.knowledge_type,
                concept_ids_json=item.concept_ids,
                pdf_page_start=item.pdf_page_start,
                pdf_page_end=item.pdf_page_end,
                parse_confidence=1.0,
                review_status=marks.get(item.chunk_index, review.STATUS_UNREVIEWED),
                embedding_model=embeddings_model(),
                embedding_version=INDEX_VERSION,
            )
            session.add(row)
        await session.flush()

        return {
            "sections": len(rows),
            "chunks": len(chunks),
        }


def embeddings_model() -> str:
    return get_settings().embedding_model


async def run(
    *,
    md_dir: str | Path,
    source_id: str = SOURCE_ID,
    rebuild_sections: bool = False,
    do_embed: bool = True,
    embed_input: str = EMBED_INPUT_NORMALIZED,
) -> IngestResult:
    """跑一遍完整入库。"""
    settings = get_settings()
    md_dir = Path(md_dir)
    index_dir = Path(settings.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. 逐页解析 ----
    page_records = parse_md.extract_pages(md_dir)
    parse_md.write_pages(page_records, index_dir / "pages.jsonl")

    # ---- 2. 拆块（标题 / 段落 / 表格行）----
    #
    # 用**原始** Markdown 拆块 —— ``parse_blocks`` 靠 ``#`` 识别标题，
    # 剥掉标记就认不出来了。
    blocks: list[parse_md.Block] = []
    for page_no, raw in parse_md.read_raw_pages(md_dir):
        blocks.extend(parse_md.parse_blocks(raw, page_no))

    # ---- 3. 章节树（人工改过就沿用）----
    section_map_path = index_dir / "section_map.json"
    section_map = None if rebuild_sections else sections_zh.load_section_map(
        section_map_path
    )
    if section_map is None:
        section_map = sections_zh.build_sections_zh(
            blocks,
            source_id=source_id,
            total_pages=len(page_records),
            pdf=str(md_dir),
        )
        sections_zh.write_section_map(section_map, section_map_path)

    # ---- 4. 切块 ----
    chunks: list[chunker.Chunk] = []
    index = 0
    for section, pages in _section_pages(section_map, blocks):
        # 附件：指南的"附件 1–4"都是宽表格（不同年龄的干预要点、
        # 行为干预策略、挑战性行为要点、共存问题要点）——
        # 这部分是家长最用得上的实操内容。
        #
        # **表格行各自成块**（``atomic_items=True``）：一行就是一条完整的
        # 干预要点。按字数攒子块的话，7 条策略会挤进同一个 543 字的块，
        # 问"孩子做对了要怎么奖励"时正确��案挤不进前 5 名。
        in_appendix = section.title.startswith("附件")
        produced = chunker.chunk_section(
            section_code=section.section_code,
            section_title=section.title,
            pages=pages,
            in_appendix=in_appendix,
            start_index=index,
            language=SOURCE_LANGUAGE,
            atomic_items=in_appendix,
        )
        chunks.extend(produced)
        index += len(produced)

    # ---- 5. 向量化 ----
    vectors: list[list[float]] | None = None
    embedded = 0
    if do_embed:
        texts = [_embed_text(c, embed_input) for c in chunks]
        vectors = await embeddings.embed_texts(texts)
        dim = embeddings.expected_dim()
        bad = [i for i, v in enumerate(vectors) if len(v) != dim]
        if bad:
            raise embeddings.EmbeddingError(
                f"{len(bad)} 个向量维度不对（期望 {dim}），入库中止"
            )
        embedded = len(vectors)

    # ---- 6. 写库 ----
    # 人工抽查标记：重建会删旧行，所以标记在这里回填
    review_file = review.load_review_file(index_dir / review.REVIEW_FILE_NAME)
    marks_applied = review.apply_marks_to_chunks(chunks, review_file)
    stale_marks = review.unmatched_marks(chunks, review_file)
    source = KnowledgeSource(
        source_id=source_id,
        source_type="guideline",
        title=SOURCE_TITLE,
        authors_json=SOURCE_AUTHORS,
        publication_year=PUBLICATION_YEAR,
        language=SOURCE_LANGUAGE,
        rights_status=RIGHTS_STATUS,
        source_uri=str(md_dir),
        content_hash=_dir_hash(md_dir),
        version=INDEX_VERSION,
    )
    counts = await write_to_db(
        source=source,
        section_map=section_map,
        chunks=chunks,
        vectors=vectors,
        marks=marks_applied,
    )

    # 高频词表跟着内容变 —— 入库后强制重算，
    # 否则检索侧还拿着上一版的词频（模块级缓存）。
    await search_mod.ensure_high_freq_terms(force=True)

    # ---- 7. 入库报告 ----
    stats = parse_md.summarize(page_records)
    parents = sum(1 for c in chunks if c.is_parent)
    children = len(chunks) - parents
    report = {
        "source_id": source_id,
        "title": SOURCE_TITLE,
        "language": SOURCE_LANGUAGE,
        "md_dir": str(md_dir),
        "content_hash": _dir_hash(md_dir),
        "index_version": INDEX_VERSION,
        "embedding_model": embeddings_model(),
        "embedding_dim": embeddings.expected_dim(),
        # 向量化用的是哪份文本 —— 评测报告要靠它区分"结果变了是因为
        # 换了输入形态，还是因为重新入库了"。
        "embed_input": embed_input,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pages": stats,
        "sections": {
            "count": counts["sections"],
            "notes": section_map.notes,
            "tree": [
                {
                    "section_code": s.section_code,
                    "title": s.title,
                    "parent": s.parent_code,
                    "pages": [s.pdf_page_start, s.pdf_page_end],
                }
                for s in section_map.sections
            ],
        },
        "chunks": {
            "total": len(chunks),
            "parents": parents,
            "children": children,
            "embedded": embedded,
        },
        "review": {
            "file": review.REVIEW_FILE_NAME,
            "marked_total": len(review_file.marks),
            "applied": len(marks_applied),
            "stale": stale_marks,
        },
        "chunker": {
            "parent_target_chars": chunker.PARENT_TARGET_CHARS,
            "child_target_chars": chunker.CHILD_TARGET_CHARS,
            "child_overlap_chars": chunker.CHILD_OVERLAP_CHARS,
            "language": SOURCE_LANGUAGE,
        },
        "rights_status": RIGHTS_STATUS,
    }
    report_path = index_dir / "ingest_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return IngestResult(
        pages_total=stats["total_pages"],
        pages_parsed=stats["parsed_pages"],
        pages_skipped=stats["skipped_pages"],
        skipped_detail=stats["skipped_detail"],
        sections=counts["sections"],
        parent_chunks=parents,
        child_chunks=children,
        embedded=embedded,
        content_hash=report["content_hash"],
        report_path=str(report_path),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="指南入库流水线")
    parser.add_argument(
        "--md-dir",
        default=str(get_settings().guideline_md_dir),
        help="分页 Markdown 目录（page_001.md …）",
    )
    parser.add_argument("--source-id", default=SOURCE_ID)
    parser.add_argument(
        "--rebuild-sections",
        action="store_true",
        help="重新生成 section_map.json（会丢掉人工改过的边界）",
    )
    parser.add_argument(
        "--no-embed", action="store_true", help="只切块不向量化（调试用）"
    )
    parser.add_argument(
        "--embed-input",
        choices=[EMBED_INPUT_NORMALIZED, EMBED_INPUT_ORIGINAL],
        default=EMBED_INPUT_NORMALIZED,
        help=(
            "向量化用哪份文本：normalized=jieba 分词文本（默认），"
            "original=自然原文。FTS 两种都一样（生成列读的是 normalized）"
        ),
    )
    args = parser.parse_args()

    result = asyncio.run(
        run(
            md_dir=args.md_dir,
            source_id=args.source_id,
            rebuild_sections=args.rebuild_sections,
            do_embed=not args.no_embed,
            embed_input=args.embed_input,
        )
    )
    print(
        f"页 {result.pages_total}（解析 {result.pages_parsed} / 跳过 {result.pages_skipped}）"
        f" | 章节 {result.sections} | 父块 {result.parent_chunks} | 子块 {result.child_chunks}"
        f" | 向量 {result.embedded}（输入 {args.embed_input}）"
    )
    print(f"报告：{result.report_path}")


if __name__ == "__main__":
    main()
