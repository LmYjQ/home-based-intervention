"""人工确认结果（改文本 / 删块 / 标记）的 sidecar。

**为什么要存文件而不是直接改数据库**：``pipeline.write_to_db`` 是
"按 source_id 全删、再重建"的流程。直接 UPDATE 的东西下一次入库就没了。
标记已经用 ``chunk_reviews.json`` 解决过一遍（见 :mod:`review`），
这里沿用同一个思路：确认结果落在 ``data/index/confirmed_chunks.json``，
入库时回填。

**匹配键跟着内容走**，不是 ``chunk_index`` —— 切分逻辑改一改序号就全变了。
键的组成：

- 父块：``p:{section_code}``（章节 code 由标题推导，比序号稳）
- 子块：``c:{页码}:{正文哈希}:{同页同文第几次出现}``

最后那一段是给重复正文用的 —— 附件 2 里有两行一模一样的
"只强化他行为"，只靠哈希分不出是哪一行。

**父块正文是拼出来的**（标题 + 各个子块），所以在网页里父块正文只读。
改了子块，父块跟着重拼，父子不会打架。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from rdi_agent.rag.ingest.clean import normalize_zh
from rdi_agent.rag.ingest.chunk import Chunk
from rdi_agent.rag.ingest.sections import Section, SectionMap

__all__ = [
    "CONFIRMED_FILE_NAME",
    "ConfirmedFile",
    "ConfirmedEntry",
    "apply_confirmed",
    "build_from_confirmed",
    "chunk_keys",
    "content_hash",
    "default_confirmed_path",
    "load_confirmed",
    "save_confirmed",
]

CONFIRMED_FILE_NAME = "confirmed_chunks.json"

STATUS_UNREVIEWED = "unreviewed"
STATUS_REVIEWED = "reviewed"
STATUS_NEEDS_CORRECTION = "needs_correction"

# 只有这两种状态算"人工标过"。
#
# ``unreviewed`` 是数据库的默认值 —— 网页上没动过的块也存这个值。
# 如果把它当成"人工标过"，就会把 review.py 里更早标的 reviewed 冲掉。
_MEANINGFUL_STATUS = {STATUS_REVIEWED, STATUS_NEEDS_CORRECTION}


def _status_of(entry: "ConfirmedEntry") -> str | None:
    """确认结果里的状态；没标过返回 ``None``。"""
    if entry.review_status in _MEANINGFUL_STATUS:
        return entry.review_status
    return None


def content_hash(text: str) -> str:
    """正文哈希（和 :func:`review.content_key` 一个思路）。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def chunk_keys(
    entries: Iterable[tuple[bool, str, int, str]],
) -> list[str]:
    """给一批块算匹配键。

    每个元素是 ``(is_parent, section_code, pdf_page_start, content_original)``，
    **顺序必须和导出时一致** —— 同页同文的出现序号是按顺序数的。
    """
    counts: dict[tuple[int, str], int] = {}
    keys: list[str] = []
    for is_parent, section_code, page_start, content in entries:
        if is_parent:
            keys.append(f"p:{section_code}")
            continue
        base = (page_start, content_hash(content))
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        keys.append(f"c:{page_start}:{base[1]}:{seen}")
    return keys


@dataclass
class ConfirmedEntry:
    """一条确认结果。"""

    chunk_index: int
    is_parent: bool
    parent_chunk_index: int | None
    section_code: str
    title: str
    level: int
    pdf_page_start: int
    pdf_page_end: int
    knowledge_type: str
    original_content: str
    content: str
    review_status: str = STATUS_UNREVIEWED
    deleted: bool = False
    key: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_index": self.chunk_index,
            "is_parent": self.is_parent,
            "parent_chunk_index": self.parent_chunk_index,
            "section_code": self.section_code,
            "title": self.title,
            "level": self.level,
            "pdf_page_start": self.pdf_page_start,
            "pdf_page_end": self.pdf_page_end,
            "knowledge_type": self.knowledge_type,
            "original_content": self.original_content,
            "content": self.content,
            "review_status": self.review_status,
            "deleted": self.deleted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConfirmedEntry":
        return cls(
            chunk_index=int(data.get("chunk_index", 0)),
            is_parent=bool(data.get("is_parent", False)),
            parent_chunk_index=data.get("parent_chunk_index"),
            section_code=data.get("section_code", ""),
            title=data.get("title", ""),
            level=int(data.get("level", 0)),
            pdf_page_start=int(data.get("pdf_page_start", 0)),
            pdf_page_end=int(data.get("pdf_page_end", 0)),
            knowledge_type=data.get("knowledge_type", "narrative"),
            original_content=data.get("original_content", ""),
            content=data.get("content", ""),
            review_status=data.get("review_status", STATUS_UNREVIEWED),
            deleted=bool(data.get("deleted", False)),
        )


@dataclass
class ConfirmedFile:
    """确认结果文件。"""

    source_id: str = ""
    pdf: str = ""
    total_pages: int = 0
    generated_at: str = ""
    parents: list[dict] = field(default_factory=list)
    chunks: list[ConfirmedEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "pdf": self.pdf,
            "total_pages": self.total_pages,
            "generated_at": self.generated_at,
            "parents": self.parents,
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConfirmedFile":
        file = cls(
            source_id=data.get("source_id", ""),
            pdf=data.get("pdf", ""),
            total_pages=int(data.get("total_pages", 0)),
            generated_at=data.get("generated_at", ""),
            parents=list(data.get("parents", [])),
            chunks=[ConfirmedEntry.from_dict(c) for c in data.get("chunks", [])],
        )
        file.assign_keys()
        return file

    def assign_keys(self) -> None:
        """按文件里的顺序给每条算匹配键。"""
        keys = chunk_keys(
            (
                c.is_parent,
                c.section_code,
                c.pdf_page_start,
                c.original_content,
            )
            for c in self.chunks
        )
        for entry, key in zip(self.chunks, keys):
            entry.key = key


def default_confirmed_path(index_dir: Path | str) -> Path:
    """确认结果文件在索引目录里的位置。"""
    return Path(index_dir) / CONFIRMED_FILE_NAME


def load_confirmed(path: Path | str) -> ConfirmedFile | None:
    """读确认结果文件。文件不存在返回 ``None``。"""
    file_path = Path(path)
    if not file_path.exists():
        return None
    data = json.loads(file_path.read_text(encoding="utf-8"))
    return ConfirmedFile.from_dict(data)


def save_confirmed(path: Path | str, confirmed: ConfirmedFile) -> Path:
    """写确认结果文件。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(confirmed.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return file_path


def _rebuild_parents(chunks: list[Chunk]) -> None:
    """父块正文 = 标题 + 各个子块正文。

    子块被改过或删过之后，父块要跟着重拼 —— 父块正文不是独立数据，
    它就是子块的汇总。
    """
    children: dict[int, list[Chunk]] = {}
    for chunk in chunks:
        if not chunk.is_parent and chunk.parent_chunk_index is not None:
            children.setdefault(chunk.parent_chunk_index, []).append(chunk)
    for chunk in chunks:
        if not chunk.is_parent:
            continue
        kids = children.get(chunk.chunk_index, [])
        if not kids:
            continue
        title = chunk.content_original.split("\n")[0]
        text = title + "\n" + "\n".join(k.content_original for k in kids)
        chunk.content_original = text
        chunk.content_normalized = normalize_zh(text)


def _renumber(chunks: list[Chunk], marks: dict[int, str]) -> dict[int, str]:
    """删过块之后重排 ``chunk_index``，父子指向也跟着改。"""
    remap = {chunk.chunk_index: i for i, chunk in enumerate(chunks)}
    new_marks: dict[int, str] = {}
    for chunk in chunks:
        old = chunk.chunk_index
        chunk.chunk_index = remap[old]
        if chunk.parent_chunk_index is not None:
            chunk.parent_chunk_index = remap.get(chunk.parent_chunk_index)
        if old in marks:
            new_marks[chunk.chunk_index] = marks[old]
    return new_marks


def apply_confirmed(
    chunks: list[Chunk],
    confirmed: ConfirmedFile | None,
) -> tuple[list[Chunk], dict[int, str], dict]:
    """把确认结果回填到新切分结果上。

    返回 ``(块, {chunk_index: review_status}, 统计)``。

    **回填三件事**：改文本、删块、标状态。

    **匹配不上的条目保留并报出来**（跟 ``review.unmatched_marks`` 一个路数）——
    切分逻辑改了导致对不上时，要让人看见，而不是静默丢掉。
    """
    stats: dict = {"applied": 0, "edited": 0, "deleted": 0, "unmatched": []}
    if confirmed is None or not confirmed.chunks:
        return chunks, {}, stats

    by_key = {entry.key: entry for entry in confirmed.chunks}
    keys = chunk_keys(
        (c.is_parent, c.section_code, c.pdf_page_start, c.content_original)
        for c in chunks
    )

    kept: list[Chunk] = []
    marks: dict[int, str] = {}
    dropped_parents: set[int] = set()
    for chunk, key in zip(chunks, keys):
        entry = by_key.get(key)
        if entry is None:
            kept.append(chunk)
            continue
        if entry.deleted:
            stats["deleted"] += 1
            if chunk.is_parent:
                dropped_parents.add(chunk.chunk_index)
            continue
        if entry.content and entry.content != chunk.content_original:
            chunk.content_original = entry.content
            chunk.content_normalized = normalize_zh(entry.content)
            stats["edited"] += 1
        status = _status_of(entry)
        if status:
            marks[chunk.chunk_index] = status
        stats["applied"] += 1
        kept.append(chunk)

    if dropped_parents:
        kept = [
            c
            for c in kept
            if not (not c.is_parent and c.parent_chunk_index in dropped_parents)
        ]

    _rebuild_parents(kept)
    # 子块全被删掉的父块也不留（空父块在检索里没用）
    parents_with_children = {c.parent_chunk_index for c in kept if not c.is_parent}
    kept = [
        c
        for c in kept
        if not c.is_parent or c.chunk_index in parents_with_children
    ]

    marks = _renumber(kept, marks)
    stats["unmatched"] = [e.key for e in confirmed.chunks if e.key not in set(keys)]
    return kept, marks, stats


def build_from_confirmed(
    confirmed: ConfirmedFile,
) -> tuple[SectionMap, list[Chunk], dict[int, str]]:
    """按确认结果直接拼出（章节树, 块, 状态标记）。

    给"一键导入"用 —— 不重新切分，直接拿确认结果入库。
    """
    sections = [
        Section(
            section_code=str(p.get("section_code", "")),
            title=str(p.get("title", "")),
            toc_level=int(p.get("level", 1)),
            pdf_page_start=int(p.get("pdf_page_start", 0)),
            pdf_page_end=int(p.get("pdf_page_end", 0)),
            order=int(p.get("order", 0)),
            parent_code=p.get("parent_code"),
            title_zh=p.get("title_zh"),
        )
        for p in confirmed.parents
    ]

    alive = [e for e in confirmed.chunks if not e.deleted]
    chunks: list[Chunk] = [
        Chunk(
            content_original=entry.content or entry.original_content,
            content_normalized=normalize_zh(entry.content or entry.original_content),
            pdf_page_start=entry.pdf_page_start,
            pdf_page_end=entry.pdf_page_end,
            chunk_index=entry.chunk_index,
            parent_index=0,
            is_parent=entry.is_parent,
            knowledge_type=entry.knowledge_type,
            section_code=entry.section_code,
            parent_chunk_index=entry.parent_chunk_index,
        )
        for entry in alive
    ]

    marks = {
        entry.chunk_index: status
        for entry in alive
        if (status := _status_of(entry))
    }

    _rebuild_parents(chunks)
    # 子块全被删掉的父块也不留（空父块在检索里没用）
    parents_with_children = {c.parent_chunk_index for c in chunks if not c.is_parent}
    chunks = [
        c
        for c in chunks
        if not c.is_parent or c.chunk_index in parents_with_children
    ]
    marks = _renumber(chunks, marks)

    section_map = SectionMap(
        source_id=confirmed.source_id,
        pdf=confirmed.pdf,
        total_pages=confirmed.total_pages,
        sections=sections,
    )
    return section_map, chunks, marks
