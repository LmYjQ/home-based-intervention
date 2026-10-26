"""章节树：PDF 目录 -> 可追溯的章节区间。

**页区间和父子关系分开定**：

- **页区间**按物理页序：结束页 = 后面第一个条目（不论层级）的起始页 - 1。
  Part 因此只覆盖自己那页分隔页 —— 它本来就没有正文。
- **父子关系**按目录嵌套：calibre 的 nav 把 ``Introduction``(第 17 页)
  挂在 ``PART ONE``(第 31 页) 下面，页码比父级还小。目录的结构是书自己的，
  予以尊重；页码对不上就在 ``note`` 里写明，留给人工在 ``section_map.json``
  里确认。

**印刷页码置空**：calibre 重排时把原书的页码丢了 —— 全书 392 页里只有 1 行是
纯数字（315 页的 "287"），不足以反推全书页码。``print_page_*`` 一律留空，
检索结果只承诺"能按 PDF 页码翻回去"，不承诺印刷页码。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import fitz

# 目录里页码 <= 0 的条目视为坏数据
_MIN_TOC_PAGE = 1


@dataclass
class Section:
    """一个章节。"""

    section_code: str  # 稳定标识，进 URL 和外键
    title: str  # 目录里的原标题
    toc_level: int  # 目录层级（1 = Part/前言/附录，2 = 章）
    pdf_page_start: int
    pdf_page_end: int
    order: int
    parent_code: str | None = None
    note: str | None = None
    title_zh: str | None = None

    def covers(self, page: int) -> bool:
        return self.pdf_page_start <= page <= self.pdf_page_end

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Section":
        return cls(**data)


@dataclass
class SectionMap:
    """章节树文件。人工改过就以文件为准，不重新生成。"""

    source_id: str
    pdf: str
    total_pages: int
    sections: list[Section] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "pdf": self.pdf,
            "total_pages": self.total_pages,
            "notes": self.notes,
            "sections": [s.to_dict() for s in self.sections],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SectionMap":
        return cls(
            source_id=data["source_id"],
            pdf=data.get("pdf", ""),
            total_pages=int(data.get("total_pages", 0)),
            sections=[Section.from_dict(s) for s in data.get("sections", [])],
            notes=list(data.get("notes", [])),
        )


def _slug(title: str, max_len: int = 40) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", title).strip("_").lower()
    return s[:max_len] or "section"


def read_toc(pdf_path: str | Path) -> list[tuple[int, str, int]]:
    """读 PDF 目录，返回 [(level, title, page)]，去掉坏条目。"""
    with fitz.open(pdf_path) as doc:
        raw = doc.get_toc()
    out: list[tuple[int, str, int]] = []
    for level, title, page in raw:
        if page < _MIN_TOC_PAGE or not title.strip():
            continue
        out.append((int(level), title.strip(), int(page)))
    return out


def build_sections(
    pdf_path: str | Path,
    *,
    source_id: str = "the_rdi_book_2009",
    total_pages: int | None = None,
) -> SectionMap:
    """从 PDF 目录生成章节树。

    两套规则各管一件事：

    - **页序**定页区间。结束页 = 后面第一个条目（不论层级）的起始页 - 1。
      Part 因此只覆盖自己那页分隔页（它本来就没有正文），章与章不互相吞页。
    - **目录嵌套**定父子。calibre 的 nav 明确把 ``Introduction`` 挂在
      ``PART ONE`` 下面，这个结构是书自己的，予以尊重。
    """
    path = Path(pdf_path)
    if total_pages is None:
        with fitz.open(path) as doc:
            total_pages = doc.page_count

    toc = read_toc(path)
    # 页序为主、层级为辅：同一页上先 Part 后章
    page_order = sorted(toc, key=lambda e: (e[2], e[0]))

    notes: list[str] = []
    sections: list[Section] = []
    for order, (level, title, start) in enumerate(page_order):
        # 结束页 = 下一个条目的起始页 - 1（不论层级）
        if order + 1 < len(page_order):
            end = page_order[order + 1][2] - 1
        else:
            end = total_pages
        if end < start:
            end = start
            notes.append(f"{title}(p{start}) 与下一条同页，区间按 1 页计")
        sections.append(
            Section(
                section_code=f"{order:03d}_{_slug(title)}",
                title=title,
                toc_level=level,
                pdf_page_start=start,
                pdf_page_end=end,
                order=order,
            )
        )

    _assign_parents(sections, toc, notes)
    return SectionMap(
        source_id=source_id,
        pdf=str(path),
        total_pages=total_pages,
        sections=sections,
        notes=notes,
    )


def _assign_parents(
    sections: list[Section], toc: list[tuple[int, str, int]], notes: list[str]
) -> None:
    """按目录嵌套挂父级。

    目录的**列表顺序**表达了嵌套（Part 后面紧跟它的各章），
    与页码无关 —— 所以用列表顺序取父级，用页序算区间。
    两者对不上时（比如某章的页码在父 Part 之前）记进 note，
    留给人工在 ``section_map.json`` 里确认。
    """
    by_page_level = {(s.pdf_page_start, s.toc_level, s.title): s for s in sections}
    stack: list[tuple[int, Section]] = []
    for level, title, page in toc:
        section = by_page_level.get((page, level, title))
        if section is None:
            continue
        parent: Section | None = None
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            parent = stack[-1][1]
        section.parent_code = parent.section_code if parent else None
        if parent is not None and section.pdf_page_start < parent.pdf_page_start:
            section.note = (
                f"起始页(p{section.pdf_page_start})在父级 "
                f"{parent.title}(p{parent.pdf_page_start}) 之前 —— 目录页码与嵌套不一致，"
                "父级按目录保留，待人工确认"
            )
            notes.append(section.note)
        stack.append((level, section))


def write_section_map(section_map: SectionMap, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(section_map.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_section_map(in_path: str | Path) -> SectionMap | None:
    """读回人工可能改过的章节树。文件不存在返回 None。"""
    path = Path(in_path)
    if not path.exists():
        return None
    return SectionMap.from_dict(json.loads(path.read_text(encoding="utf-8")))


def section_starts(section_map: SectionMap) -> set[int]:
    """所有章节的起始页 —— 解析阶段用来识别"章节标签页"。"""
    return {s.pdf_page_start for s in section_map.sections}
