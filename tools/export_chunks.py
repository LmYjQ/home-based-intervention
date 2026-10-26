"""把切分结果导出到本地目录，给 ``tools/chunk_browser.html`` 看。

    python -m tools.export_chunks --md-dir data/ocr/guideline2026 \
        --out data/index/chunk_preview

**不连库、不调 embedding** —— 纯离线跑一遍切分逻辑，所以改切分规则时
可以秒级看到结果。入库跑的是同一份代码（``rdi_agent.rag.ingest.chunk``），
看到的和入库的一致。

输出目录：

- ``manifest.json`` —— 网页读的就是它（父块清单 + 全部块的元数据和正文）
- ``chunks/p0000_xxx.md`` / ``chunks/c0001_xxx.md`` —— 每块一个文件，
  正文是原文，方便 grep / diff

**为什么落在 data/index/ 下**：指南正文不能进版本库（仓库是公开的，
而 ``knowledge_sources.rights_status`` 是 ``internal_review_only``）。
``.gitignore`` 已经把 ``data/index/*`` 忽略了。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from rdi_agent.config import get_settings
from rdi_agent.rag.ingest import chunk as chunker
from rdi_agent.rag.ingest import parse_md

SOURCE_ID = "guideline2026"
SOURCE_TITLE = "孤独症谱系障碍儿童干预康复指南（2026 年版）"

# 文件名里不能有的字符
_UNSAFE = re.compile(r"[^\w一-鿿.-]+")


def _slug(text: str) -> str:
    """标题 -> 文件名片段。"""
    return _UNSAFE.sub("_", text).strip("_")[:40] or "chunk"


def build(
    md_dir: str | Path,
    *,
    source_id: str = SOURCE_ID,
    language: str = "zh",
) -> tuple[list[dict], list[dict], dict]:
    """跑一遍切分，返回（父块清单, 全部块, 统计）。"""
    md_path = Path(md_dir)
    blocks: list[parse_md.Block] = []
    for page_no, raw in parse_md.read_raw_pages(md_path):
        blocks.extend(parse_md.parse_blocks(raw, page_no))
    blocks, merged = parse_md.merge_page_breaks(blocks)

    section_map, chunks = chunker.chunk_document(
        blocks,
        source_id=source_id,
        pdf=str(md_path),
        total_pages=len({b.page for b in blocks}),
        language=language,
    )

    titles = {s.section_code: s for s in section_map.sections}

    parents: list[dict] = []
    child_count: dict[int, int] = {}
    for chunk in chunks:
        if chunk.is_parent:
            continue
        if chunk.parent_chunk_index is not None:
            child_count[chunk.parent_chunk_index] = (
                child_count.get(chunk.parent_chunk_index, 0) + 1
            )

    for chunk in chunks:
        if not chunk.is_parent:
            continue
        section = titles.get(chunk.section_code)
        parents.append(
            {
                "chunk_index": chunk.chunk_index,
                "section_code": chunk.section_code,
                "title": section.title if section else chunk.section_code,
                "title_zh": section.title_zh if section else None,
                "level": section.toc_level if section else 1,
                "parent_code": section.parent_code if section else None,
                "order": section.order if section else 0,
                "pdf_page_start": chunk.pdf_page_start,
                "pdf_page_end": chunk.pdf_page_end,
                "child_count": child_count.get(chunk.chunk_index, 0),
            }
        )

    chunk_rows: list[dict] = []
    for chunk in chunks:
        section = titles.get(chunk.section_code)
        chunk_rows.append(
            {
                "chunk_index": chunk.chunk_index,
                "is_parent": chunk.is_parent,
                "parent_chunk_index": chunk.parent_chunk_index,
                "section_code": chunk.section_code,
                "title": section.title if section else "",
                "level": section.toc_level if section else 1,
                "pdf_page_start": chunk.pdf_page_start,
                "pdf_page_end": chunk.pdf_page_end,
                "knowledge_type": chunk.knowledge_type,
                "chars": len(chunk.content_original),
                "content": chunk.content_original,
            }
        )

    stats = {
        "pages": len({b.page for b in blocks}),
        "parents": len(parents),
        "children": len(chunk_rows) - len(parents),
        "merged_page_breaks": merged,
        "max_child_chars": max(
            (row["chars"] for row in chunk_rows if not row["is_parent"]),
            default=0,
        ),
        "max_parent_chars": max(
            (row["chars"] for row in chunk_rows if row["is_parent"]),
            default=0,
        ),
    }
    return parents, chunk_rows, stats


def write(
    out_dir: str | Path,
    parents: list[dict],
    chunks: list[dict],
    stats: dict,
    *,
    source_id: str = SOURCE_ID,
    pdf: str = "",
) -> Path:
    """写到目录里。"""
    out = Path(out_dir)
    chunk_dir = out / "chunks"
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    titles = {row["section_code"]: row["title"] for row in parents}
    for row in chunks:
        prefix = "p" if row["is_parent"] else "c"
        title = titles.get(row["section_code"], row["section_code"])
        name = f"{prefix}{row['chunk_index']:04d}_{_slug(title)}.md"
        (chunk_dir / name).write_text(row["content"], encoding="utf-8")

    manifest = {
        "source_id": source_id,
        "source_title": SOURCE_TITLE,
        "pdf": pdf,
        "total_pages": stats["pages"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": stats,
        "parents": parents,
        "chunks": chunks,
    }
    path = out / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="导出切分结果到本地目录")
    parser.add_argument("--md-dir", default=str(settings.guideline_md_dir))
    parser.add_argument("--out", default="data/index/chunk_preview")
    parser.add_argument("--source-id", default=SOURCE_ID)
    args = parser.parse_args()

    parents, chunks, stats = build(args.md_dir, source_id=args.source_id)
    path = write(
        args.out,
        parents,
        chunks,
        stats,
        source_id=args.source_id,
        pdf=args.md_dir,
    )
    print(
        f"父块 {stats['parents']} | 子块 {stats['children']}"
        f" | 跨页合并 {stats['merged_page_breaks']} 处"
        f" | 最长子块 {stats['max_child_chars']} 字"
        f" | 最长父块 {stats['max_parent_chars']} 字"
    )
    print(f"清单：{path}")


if __name__ == "__main__":
    main()
