"""Markdown 源解析：把指南的 Markdown 转成可追溯的"页"。

**为什么要有这条路径**：这份指南是**扫描件**，PDF 里没有文字层
（``fitz`` 抽出来是空字符串）。``parse_pdf`` 对它完全无效 ——
它按页抽文字，扫��� PDF 抽出来是空的。

所以入库源改成 Markdown：``data/ocr/guideline2026/page_001.md`` …
``page_025.md``，一个文件对应 PDF 的一页。这样：

- **页码能翻回去**：块上的 ``pdf_page_start`` 就是原 PDF 的页号，
  家长/审核者拿着页码能翻到扫描件上的对应页。
- **不需要 OCR 就能入库**：内容已经在 Markdown 里了。

**页码从哪来**：分页文件本身带页号（文件名），合并版不带。
所以**必须用分页文件当源**，不能用合并版 —— 合并版会丢页码。
（合并版和分页版内容一致：块对块比对 125 块，只有附件 1 里一处措辞差异。）

**和 ``parse_pdf`` 的接口对齐**：都产出 :class:`PageRecord`，
下游（清洗、切块）不用改。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# 分页文件名：page_001.md
_PAGE_FILE = re.compile(r"^page_(\d+)\.md$")

# Markdown 标题：一级到六级
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# 表格分隔行：| --- | --- |
_TABLE_SEPARATOR = re.compile(r"^\|[\s:\-|]+\|$")


@dataclass
class PageRecord:
    """一页的内容。字段和 ``parse_pdf.PageRecord`` 一致，下游不用改。"""

    pdf_page: int
    char_count: int
    text: str
    skipped_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)


@dataclass
class Block:
    """Markdown 里的一段。

    **为什么要单独一层**：Markdown 的行不等于语义段 —— 标题行、
    表格行、正文行要分开处理（表格要转成行式文本，标题要变成章节边界）。
    """

    page: int
    kind: str  # heading / paragraph / table / list_item
    text: str
    level: int = 0  # 标题层级（1-6），非标题为 0


def _iter_pages(md_dir: Path) -> list[tuple[int, Path]]:
    """按页号排序返回 (页号, 文件路径)。"""
    found: list[tuple[int, Path]] = []
    for path in sorted(md_dir.iterdir()):
        if not path.is_file():
            continue
        match = _PAGE_FILE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def parse_blocks(text: str, page: int) -> list[Block]:
    """把一页的 Markdown 拆成块。

    表格的每一行是**一个块** —— 指南的附件 1–4 全是宽表格，
    一行是一整条干预要点（"年龄 → 干预目标 → 干预内容 → 干预重点"），
    按行切才能让召回粒度对上"针对 X 阶段的干预要点"这种问题。
    """
    blocks: list[Block] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        if heading:
            blocks.append(
                Block(
                    page=page,
                    kind="heading",
                    text=heading.group(2).strip(),
                    level=len(heading.group(1)),
                )
            )
            continue
        if _TABLE_SEPARATOR.match(line):
            # 表格分隔行本身没有内容，跳过
            continue
        if _is_table_row(line):
            # 表格行转成可读文本：``| 正强化 | 以物质性... |``
            # -> ``正强化 以物质性...``。
            # 管道符留着会让向量和分词都读到一堆无意义的符号。
            blocks.append(
                Block(page=page, kind="table", text=_table_row_to_text(line))
            )
            continue
        blocks.append(Block(page=page, kind="paragraph", text=line))
    return blocks


def _table_row_to_text(line: str) -> str:
    """表格行 -> 纯文本。

    **保留单元格的语义单元**：``| 0~18 月龄 | 促进感知觉... |`` 变成
    ``0~18 月龄 促进感知觉...`` —— 单元格之间用一个空格，不切断
    单元内部的语义。

    **``<br>`` 换成句号**：Markdown 里 ``<br>`` 表示单元格内的换行。
    留着它，向量模型会把它当成一串无意义的字符。换成句号之后，
    "1. xxx。<br>2. yyy。" 变成 "1. xxx。 2. yyy。" —— 句子边界清楚了。
    """
    cells = [c.strip() for c in line.strip("|").split("|")]
    cleaned = [c.replace("<br>", "。 ").replace("**", "") for c in cells]
    return " ".join(c for c in cleaned if c)


def read_raw_pages(md_dir: str | Path) -> list[tuple[int, str]]:
    """读分页 Markdown 目录，返回 [(页号, **原始** Markdown 文本)]。

    **为什么要原始文本**：``parse_blocks`` 要靠 ``#`` 识别标题。
    如果这里先把 Markdown 标记剥掉，标题就变成了普通文字，
    章节树就建不出来。

    所以：**原始文本给切块用，剥掉标记的文本给人看**（``pages.jsonl``）。
    """
    directory = Path(md_dir)
    return [
        (page_no, path.read_text(encoding="utf-8"))
        for page_no, path in _iter_pages(directory)
    ]


def extract_pages(md_dir: str | Path) -> list[PageRecord]:
    """读分页 Markdown 目录，返回页记录。

    ``text`` 字段存的是**去掉 Markdown 标记**的纯文本 —— 这是给人看的
    （``pages.jsonl``、人工核对）。要拿原始 Markdown 做章节切分，
    用 :func:`read_raw_pages`。

    空页会被标成 ``skipped``（原因 ``too_short``），和 ``parse_pdf`` 的
    语义一致 —— 下游按 ``skipped`` 过滤。
    """
    directory = Path(md_dir)
    records: list[PageRecord] = []
    for page_no, path in _iter_pages(directory):
        text = path.read_text(encoding="utf-8")
        # 去掉 Markdown 标记，得到"这一页说了什么"的纯文本
        stripped = strip_markdown(text)
        if len(stripped.strip()) < 20:
            records.append(
                PageRecord(
                    pdf_page=page_no,
                    char_count=len(stripped.strip()),
                    text=stripped,
                    skipped_reason="too_short",
                )
            )
            continue
        records.append(
            PageRecord(
                pdf_page=page_no,
                char_count=len(stripped.strip()),
                text=stripped,
            )
        )
    return records


def strip_markdown(text: str) -> str:
    """去掉 Markdown 语法标记，保留文字。

    **只去标记，不动文字本身** —— 尤其是表格行：
    ``| 正强化 | 以物质性... |`` 变成 ``正强化 以物质性...``，
    管道符和首尾空格去掉，单元格之间用一个空格连起来。
    语义单元（"正强化"）不会被拆散，因为一行就是一条要点。
    """
    out_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        if heading:
            out_lines.append(heading.group(2).strip())
            continue
        if _TABLE_SEPARATOR.match(line):
            continue
        if _is_table_row(line):
            out_lines.append(_table_row_to_text(line))
            continue
        # 去掉行内的加粗/斜体标记
        out_lines.append(line.replace("**", "").replace("__", ""))
    return "\n".join(out_lines)


def write_pages(records: list[PageRecord], out_path: str | Path) -> Path:
    """把页记录落盘（``pages.jsonl``），和 ``parse_pdf.write_pages`` 一致。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(
                json.dumps(
                    {
                        "pdf_page": record.pdf_page,
                        "char_count": record.char_count,
                        "skipped_reason": record.skipped_reason,
                        "text": record.text,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def read_pages(in_path: str | Path) -> list[PageRecord]:
    """读回 ``pages.jsonl``。"""
    path = Path(in_path)
    if not path.exists():
        return []
    records: list[PageRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            records.append(
                PageRecord(
                    pdf_page=int(data["pdf_page"]),
                    char_count=int(data.get("char_count", 0)),
                    text=data.get("text", ""),
                    skipped_reason=data.get("skipped_reason", ""),
                )
            )
    return records


def summarize(records: list[PageRecord]) -> dict:
    """入库报告里的页统计。和 ``parse_pdf.summarize`` 同一个形状。"""
    skipped: dict[str, list[int]] = {}
    for record in records:
        if record.skipped:
            skipped.setdefault(record.skipped_reason, []).append(record.pdf_page)
    return {
        "total_pages": len(records),
        "parsed_pages": sum(1 for r in records if not r.skipped),
        "skipped_pages": sum(1 for r in records if r.skipped),
        "skipped_detail": skipped,
    }
