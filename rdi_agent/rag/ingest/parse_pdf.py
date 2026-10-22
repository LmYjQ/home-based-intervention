"""逐页抽取 PDF 文本。

这个 PDF 是 calibre 从 mobi 重排出来的，有两个坑：

1. **有些页没有文字层**（封面、Part 分隔页、章节扉页）。这些页不能猜内容，
   只能如实记成"跳过 + 原因"——比编一句占位文字强。
2. **正文每 70~80 字符硬换行**。那是排版产物，不是段落边界：一句没说完的话
   会被拦腰截断。这里只管"抽"，把换行接回段落是 :mod:`.clean` 的活。

输出 ``data/index/pages.jsonl``，一行一页。中间产物，不进版本库。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz

# 一页少于这个字符数就当"没有正文"。
# 低于这个数的页要么是整页图，要么是只有一行标题 —— 两种都单独标注原因。
MIN_CHARS = 20

# 跳过原因（写进 pages.jsonl，ingest_report 会统计）
REASON_NO_TEXT = "no_text_layer"  # 一个字符都没有
REASON_TOO_SHORT = "too_short"  # 有字，但太少，判为扉页/分隔页
REASON_LABEL = "section_label_page"  # 只写了 "Chapter One" 这种章节标签

# 章节标签页的字数上限。实测这本书的标签页是 "Chapter One"(11) / "Appendix A"(10)
LABEL_MAX_CHARS = 40


@dataclass(slots=True)
class PageRecord:
    """一页的抽取结果。"""

    pdf_page: int  # 1-based，和 PDF 阅读器显示的页码一致
    char_count: int
    text: str
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def extract_pages(
    pdf_path: str | Path,
    min_chars: int = MIN_CHARS,
    label_pages: set[int] | None = None,
) -> list[PageRecord]:
    """逐页抽文本。页码从 1 开始。

    ``label_pages`` 是章节起始页集合（来自目录）。落在这些页上、且只有
    "Chapter One" 这种标签的页，单独记 ``section_label_page`` ——
    它们不是坏页，只是没有正文，混进 "too_short" 里说不清。
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF 不存在：{path}")

    label_pages = label_pages or set()
    records: list[PageRecord] = []
    with fitz.open(path) as doc:
        for index in range(doc.page_count):
            text = doc[index].get_text()
            stripped = text.strip()
            page_no = index + 1
            reason: str | None = None
            if not stripped:
                reason = REASON_NO_TEXT
            elif page_no in label_pages and len(stripped) <= LABEL_MAX_CHARS:
                reason = REASON_LABEL
            elif len(stripped) < min_chars:
                reason = REASON_TOO_SHORT
            records.append(
                PageRecord(
                    pdf_page=index + 1,
                    char_count=len(stripped),
                    text=text,
                    skipped_reason=reason,
                )
            )
    return records


def write_pages(records: list[PageRecord], out_path: str | Path) -> Path:
    """落盘成 JSONL。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    return path


def read_pages(in_path: str | Path) -> list[PageRecord]:
    """读回 JSONL。"""
    path = Path(in_path)
    records: list[PageRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            records.append(
                PageRecord(
                    pdf_page=data["pdf_page"],
                    char_count=data["char_count"],
                    text=data.get("text", ""),
                    skipped_reason=data.get("skipped_reason"),
                )
            )
    return records


def summarize(records: list[PageRecord]) -> dict[str, object]:
    """给 ingest_report 用的统计。"""
    skipped: dict[str, list[int]] = {}
    for record in records:
        if record.skipped_reason:
            skipped.setdefault(record.skipped_reason, []).append(record.pdf_page)
    return {
        "total_pages": len(records),
        "parsed_pages": sum(1 for r in records if not r.skipped),
        "skipped_pages": sum(len(v) for v in skipped.values()),
        "skipped_detail": {k: v for k, v in sorted(skipped.items())},
        "total_chars": sum(r.char_count for r in records),
    }
