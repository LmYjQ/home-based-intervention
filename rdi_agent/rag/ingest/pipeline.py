"""入库流水线：PDF -> 可追溯的向量 + 关键词索引。

    python -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf

**幂等**：整个 source 的块先删后建。重建后条目数必须一致 ——
这是计划 §9.1 的验收项之一，所以重跑一次不会多出半套数据。

**分步可查**：每一步的中间结果都落盘（``pages.jsonl`` / ``section_map.json``），
出问题时不用从头再解析一遍 PDF。

**版权**：这本书的授权未核实，``rights_status`` 固定 ``internal_review_only``。
库里存的是**供检索的片段**，不是给用户看的原文；面向用户的引用走
"摘要 + 页码"，不给大段原文。
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
from rdi_agent.rag.ingest import clean, embeddings, parse_pdf, sections

SOURCE_ID = "the_rdi_book_2009"
SOURCE_TITLE = "The RDI Book: Forging New Pathways for Autism, Asperger's and PDD"
SOURCE_AUTHORS = ["Steven Gutstein", "Stacy Rosenzweig Pirhala"]
PUBLICATION_YEAR = 2009
# 授权未核实：只用于本地研发和内部演示
RIGHTS_STATUS = "internal_review_only"
INDEX_VERSION = "v1"


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


def _section_pages(
    section_map: sections.SectionMap,
    cleaned: dict[int, str],
) -> list[tuple[sections.Section, list[tuple[int, str]]]]:
    """把清洗后的正文按章节归位。"""
    result = []
    for section in section_map.sections:
        pages = [
            (page, cleaned[page])
            for page in range(section.pdf_page_start, section.pdf_page_end + 1)
            if page in cleaned and cleaned[page].strip()
        ]
        if pages:
            result.append((section, pages))
    return result


async def write_to_db(
    *,
    source: KnowledgeSource,
    section_map: sections.SectionMap,
    chunks: list[chunker.Chunk],
    vectors: list[list[float]] | None,
) -> dict[str, int]:
    """把章节和块写进库。父块和子块一起写，子块用 ``parent_chunk_id`` 指回去。"""
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
                review_status="unreviewed",
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
                parent_chunk_id=chunk_ids.get(item.parent_chunk_index or -1),
                chunk_index=item.chunk_index,
                content_original=item.content_original,
                content_normalized=item.content_normalized,
                embedding=vectors_by_index.get(position),
                knowledge_type=item.knowledge_type,
                concept_ids_json=item.concept_ids,
                pdf_page_start=item.pdf_page_start,
                pdf_page_end=item.pdf_page_end,
                parse_confidence=1.0,
                review_status="unreviewed",
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
    pdf_path: str | Path,
    source_id: str = SOURCE_ID,
    rebuild_sections: bool = False,
    do_embed: bool = True,
) -> IngestResult:
    """跑一遍完整入库。"""
    settings = get_settings()
    pdf = Path(pdf_path)
    index_dir = Path(settings.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. 章节树（人工改过就沿用）----
    section_map_path = index_dir / "section_map.json"
    section_map = None if rebuild_sections else sections.load_section_map(section_map_path)
    if section_map is None:
        section_map = sections.build_sections(pdf, source_id=source_id)
        sections.write_section_map(section_map, section_map_path)

    # ---- 2. 逐页抽取 ----
    page_records = parse_pdf.extract_pages(
        pdf, label_pages=sections.section_starts(section_map)
    )
    parse_pdf.write_pages(page_records, index_dir / "pages.jsonl")

    # ---- 3. 清洗 ----
    non_empty = [r for r in page_records if not r.skipped]
    headers = clean.find_repeated_lines([r.text for r in non_empty])
    drop_lines = set(headers)
    cleaned: dict[int, str] = {}
    for record in non_empty:
        result = clean.clean_page(record.text, drop_lines=drop_lines)
        if result.normalized:
            cleaned[record.pdf_page] = result.original

    # ---- 4. 切块 ----
    chunks: list[chunker.Chunk] = []
    index = 0
    for section, pages in _section_pages(section_map, cleaned):
        in_appendix = section.toc_level == 1 and "APPEND" in section.title.upper()
        produced = chunker.chunk_section(
            section_code=section.section_code,
            section_title=section.title,
            pages=pages,
            in_appendix=in_appendix,
            start_index=index,
        )
        chunks.extend(produced)
        index += len(produced)

    # ---- 5. 向量化 ----
    vectors: list[list[float]] | None = None
    embedded = 0
    if do_embed:
        texts = [c.content_normalized for c in chunks]
        vectors = await embeddings.embed_texts(texts)
        dim = embeddings.expected_dim()
        bad = [i for i, v in enumerate(vectors) if len(v) != dim]
        if bad:
            raise embeddings.EmbeddingError(
                f"{len(bad)} 个向量维度不对（期望 {dim}），入库中止"
            )
        embedded = len(vectors)

    # ---- 6. 写库 ----
    source = KnowledgeSource(
        source_id=source_id,
        source_type="book",
        title=SOURCE_TITLE,
        authors_json=SOURCE_AUTHORS,
        publication_year=PUBLICATION_YEAR,
        language="en",
        rights_status=RIGHTS_STATUS,
        source_uri=str(pdf),
        content_hash=_file_hash(pdf),
        version=INDEX_VERSION,
    )
    counts = await write_to_db(
        source=source, section_map=section_map, chunks=chunks, vectors=vectors
    )

    # ---- 7. 入库报告 ----
    stats = parse_pdf.summarize(page_records)
    parents = sum(1 for c in chunks if c.is_parent)
    children = len(chunks) - parents
    report = {
        "source_id": source_id,
        "pdf": str(pdf),
        "content_hash": _file_hash(pdf),
        "index_version": INDEX_VERSION,
        "embedding_model": embeddings_model(),
        "embedding_dim": embeddings.expected_dim(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pages": stats,
        "sections": {
            "count": counts["sections"],
            "notes": section_map.notes,
        },
        "chunks": {
            "total": len(chunks),
            "parents": parents,
            "children": children,
            "embedded": embedded,
        },
        "cleaning": {
            "header_footer_lines_dropped": sorted(drop_lines),
        },
        "chunker": {
            "parent_target_chars": chunker.PARENT_TARGET_CHARS,
            "child_target_chars": chunker.CHILD_TARGET_CHARS,
            "child_overlap_chars": chunker.CHILD_OVERLAP_CHARS,
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
    parser = argparse.ArgumentParser(description="RDI Book 入库流水线")
    parser.add_argument(
        "--pdf",
        default=str(get_settings().documents_dir / "the_rdi_book.pdf"),
        help="PDF 路径",
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
    args = parser.parse_args()

    result = asyncio.run(
        run(
            pdf_path=args.pdf,
            source_id=args.source_id,
            rebuild_sections=args.rebuild_sections,
            do_embed=not args.no_embed,
        )
    )
    print(
        f"页 {result.pages_total}（解析 {result.pages_parsed} / 跳过 {result.pages_skipped}）"
        f" | 章节 {result.sections} | 父块 {result.parent_chunks} | 子块 {result.child_chunks}"
        f" | 向量 {result.embedded}"
    )
    print(f"报告：{result.report_path}")


if __name__ == "__main__":
    main()
