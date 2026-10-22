"""人工抽查标记：``reviewed`` / ``unreviewed`` / ``needs_correction``。

计划 §9.1 第 11 步：优先核对评测集和演示会用到的约 50 个片段，通过后标记
``reviewed``，其余保持 ``unreviewed``。§5.4 的检索过滤要求
``review_status != needs_correction`` —— 也就是"核对发现有问题"的片段
要能被排除出召回，而不只是标个"看过"。

**为什么不直接改数据库**：``pipeline.write_to_db`` 是"先按 source_id 全删、
再重建"的流程（计划 §5.2 第 12 步要求一键可重建）。直接 UPDATE 的标记
下一次重建就没了。所以标记存在 ``data/index/chunk_reviews.json``，
入库时回填，标记跟着内容走。

**为什么键是内容哈希而不是 chunk_index**：改了切分参数，``chunk_index``
整体位移，标记会错位到别的片段上 —— 这比丢标记更危险。内容哈希跟着文字走：
文字改了，标记自动失效，这正是想要的（被改过的片段必须重新核对）。

所以这里的规则是：**标记只在内容一字未变时有效**。入库报告会写明
"多少条标记命中、多少条因为内容变了失效"，失效不是静默的。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import text

from rdi_agent.db.engine import session_scope

# 标记文件放在索引目录里，和 section_map.json / ingest_report.json 同级
REVIEW_FILE_NAME = "chunk_reviews.json"
CANDIDATE_FILE_NAME = "review_candidates.jsonl"

# 允许的状态。检索默认排除 needs_correction（计划 §5.4）
STATUS_UNREVIEWED = "unreviewed"
STATUS_REVIEWED = "reviewed"
STATUS_NEEDS_CORRECTION = "needs_correction"
VALID_STATUSES = (STATUS_UNREVIEWED, STATUS_REVIEWED, STATUS_NEEDS_CORRECTION)

# 计划 §9.1：约 50 个片段
DEFAULT_CANDIDATE_COUNT = 50


def content_key(content_normalized: str) -> str:
    """内容哈希。标记的唯一键。

    只取 16 位十六进制（64 bit）：1000 来个片段的规模下碰撞概率可以忽略，
    而短键能在文件里一眼看懂。
    """
    digest = hashlib.sha256(content_normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


@dataclass
class ReviewMark:
    """一条标记。"""

    status: str = STATUS_UNREVIEWED
    reviewer: str = ""
    note: str = ""
    reviewed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewMark":
        return cls(
            status=data.get("status", STATUS_UNREVIEWED),
            reviewer=data.get("reviewer", ""),
            note=data.get("note", ""),
            reviewed_at=data.get("reviewed_at", ""),
        )


@dataclass
class ReviewFile:
    """标记文件整体。"""

    version: str = "v1"
    source_id: str = ""
    marks: dict[str, ReviewMark] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_id": self.source_id,
            "marks": {k: v.to_dict() for k, v in sorted(self.marks.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewFile":
        return cls(
            version=data.get("version", "v1"),
            source_id=data.get("source_id", ""),
            marks={
                k: ReviewMark.from_dict(v)
                for k, v in (data.get("marks") or {}).items()
            },
        )


# ------------------------------------------------------------------ 文件读写


def default_review_path(index_dir: Path | str) -> Path:
    return Path(index_dir) / REVIEW_FILE_NAME


def load_review_file(path: Path | str) -> ReviewFile:
    """读标记文件。不存在返回空文件（不是错误）。"""
    p = Path(path)
    if not p.exists():
        return ReviewFile()
    return ReviewFile.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_review_file(review_file: ReviewFile, path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(review_file.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def apply_marks_to_chunks(
    chunks: Sequence[Any], review_file: ReviewFile
) -> dict[int, str]:
    """把标记映射到块。返回 {chunk_index: status}，只含命中的。

    块要能算出 ``content_normalized`` —— 切块阶段的 ``Chunk`` 有。
    """
    out: dict[int, str] = {}
    for chunk in chunks:
        key = content_key(chunk.content_normalized)
        mark = review_file.marks.get(key)
        if mark is not None:
            out[chunk.chunk_index] = mark.status
    return out


def unmatched_marks(
    chunks: Sequence[Any], review_file: ReviewFile
) -> list[str]:
    """哪些标记这次没命中（内容变了，或者块没了）。

    失效的标记**不删** —— 留着能看出"这段被人核对过，现在对不上了"，
    顺手删掉就等于把问题藏起来了。
    """
    live = {content_key(c.content_normalized) for c in chunks}
    return sorted(k for k in review_file.marks if k not in live)


# ------------------------------------------------------------------ 候选导出


async def export_candidates(
    *,
    source_id: str,
    limit: int = DEFAULT_CANDIDATE_COUNT,
    kind: str = "child",
    section_code: str | None = None,
) -> list[dict[str, Any]]:
    """从库里挑一批片段导出，供人工核对。

    挑选策略：按 ``chunk_index`` 均匀取样（不是取前 N 个）。前 N 个会全挤在
    第一章，覆盖不到全书的章节分布；均匀取样能覆盖到各个 Part。
    """
    sql = """
        SELECT c.id, c.chunk_index, c.content_normalized, c.content_original,
               c.pdf_page_start, c.pdf_page_end, c.knowledge_type,
               c.review_status, s.section_code, s.title_original
        FROM knowledge_chunks c
        JOIN knowledge_sources src ON src.id = c.source_id
        JOIN book_sections s ON s.id = c.section_id
        WHERE src.source_id = :source_id
    """
    params: dict[str, Any] = {"source_id": source_id}
    if kind == "child":
        sql += " AND c.parent_chunk_id IS NOT NULL"
    elif kind == "parent":
        sql += " AND c.parent_chunk_id IS NULL"
    if section_code:
        sql += " AND s.section_code = :section_code"
        params["section_code"] = section_code
    sql += " ORDER BY c.chunk_index"

    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()

    if not rows:
        return []
    step = max(1, len(rows) // limit)
    picked = rows[::step][:limit]

    out: list[dict[str, Any]] = []
    for row in picked:
        normalized = row["content_normalized"] or ""
        out.append(
            {
                "content_key": content_key(normalized),
                "chunk_index": row["chunk_index"],
                "chunk_id": str(row["id"]),
                "section_code": row["section_code"],
                "section_title": row["title_original"],
                "pdf_page_start": row["pdf_page_start"],
                "pdf_page_end": row["pdf_page_end"],
                "knowledge_type": row["knowledge_type"],
                "current_status": row["review_status"],
                "chars": len(normalized),
                "text": normalized,
            }
        )
    return out


def write_candidates(candidates: list[dict[str, Any]], path: Path | str) -> Path:
    """候选清单写成 JSONL —— 人工读起来方便，也方便逐行贴回标记。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for item in candidates:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return p


def read_candidates(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ------------------------------------------------------------------ 写回数据库


async def apply_to_db(review_file: ReviewFile, *, source_id: str) -> dict[str, int]:
    """把标记写到数据库里对应的块上。

    **不需要重新入库**：这里只 UPDATE ``review_status``，不动内容也不动向量。
    重新入库会重算 embedding（要外发正文），没必要。

    **标记文件是唯一真相**：标记文件里没有、但库里标了 ``reviewed`` /
    ``needs_correction`` 的块，会被重置回 ``unreviewed``。所以删掉标记文件
    再跑一次 ``apply``，就等于"撤回全部标记" —— 不需要另外的撤销命令。
    （代价是：谁要是绕过标记文件直接 UPDATE 库，改动会被 apply 冲掉。
    这是有意的：标记只有一个入口，才不会出现两处状态打架。）

    匹配靠内容哈希：Python 侧算 sha256，和标记文件里的键对齐。
    （PG 里的 ``md5()`` 和 Python 的 sha256 不是一回事，不能直接比。）
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id, c.content_normalized, c.review_status
                    FROM knowledge_chunks c
                    JOIN knowledge_sources src ON src.id = c.source_id
                    WHERE src.source_id = :source_id
                    """
                ),
                {"source_id": source_id},
            )
        ).mappings().all()

        by_key: dict[str, list[Any]] = {}
        for row in rows:
            by_key.setdefault(content_key(row["content_normalized"] or ""), []).append(
                row
            )

        updated = 0
        matched = 0
        not_found = 0
        reset = 0
        for key, mark in review_file.marks.items():
            hit = by_key.get(key)
            if not hit:
                not_found += 1
                continue
            matched += 1
            for row in hit:
                if row["review_status"] == mark.status:
                    continue
                await session.execute(
                    text("UPDATE knowledge_chunks SET review_status = :status WHERE id = :id"),
                    {"status": mark.status, "id": row["id"]},
                )
                updated += 1

        # 标记文件里没有的，把库里非 unreviewed 的状态收回来
        for key, hit in by_key.items():
            if key in review_file.marks:
                continue
            for row in hit:
                if row["review_status"] == STATUS_UNREVIEWED:
                    continue
                await session.execute(
                    text(
                        "UPDATE knowledge_chunks SET review_status = :status WHERE id = :id"
                    ),
                    {"status": STATUS_UNREVIEWED, "id": row["id"]},
                )
                reset += 1

    return {
        "matched": matched,
        "updated": updated,
        "not_found": not_found,
        "reset": reset,
    }


# ------------------------------------------------------------------ CLI


def _mark_status(
    review_file: ReviewFile,
    *,
    key: str,
    status: str,
    reviewer: str,
    note: str,
) -> None:
    if status not in VALID_STATUSES:
        raise SystemExit(f"状态只能是 {'/'.join(VALID_STATUSES)}")
    existing = review_file.marks.get(key) or ReviewMark()
    existing.status = status
    if reviewer:
        existing.reviewer = reviewer
    if note:
        existing.note = note
    existing.reviewed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    review_file.marks[key] = existing


async def _cmd_export(args: argparse.Namespace) -> None:
    candidates = await export_candidates(
        source_id=args.source_id,
        limit=args.limit,
        kind=args.kind,
        section_code=args.section,
    )
    path = write_candidates(candidates, args.out)
    print(f"导出 {len(candidates)} 个候选片段 -> {path}")
    print("人工核对后用 `review mark` 标记，或直接编辑 chunk_reviews.json")


async def _cmd_mark(args: argparse.Namespace) -> None:
    review_file = load_review_file(args.file)
    review_file.source_id = review_file.source_id or args.source_id

    keys: list[str] = []
    if args.key:
        keys.extend(args.key)
    if args.candidate_file:
        candidates = read_candidates(args.candidate_file)
        wanted = set(args.candidate_index or [])
        for item in candidates:
            if not wanted or item["chunk_index"] in wanted:
                keys.append(item["content_key"])

    if not keys:
        raise SystemExit("没指定要标记的片段（--key 或 --candidate-file）")

    for key in keys:
        _mark_status(
            review_file,
            key=key,
            status=args.status,
            reviewer=args.reviewer,
            note=args.note,
        )
    save_review_file(review_file, args.file)
    print(f"标记了 {len(keys)} 个片段 -> {args.file}")


async def _cmd_apply(args: argparse.Namespace) -> None:
    review_file = load_review_file(args.file)
    stats = await apply_to_db(review_file, source_id=args.source_id)
    print(
        f"命中 {stats['matched']} 条，更新 {stats['updated']} 行，"
        f"重置 {stats['reset']} 行（标记文件里没有的），"
        f"在库里找不到 {stats['not_found']} 条（内容可能变了）"
    )


async def _cmd_list(args: argparse.Namespace) -> None:
    review_file = load_review_file(args.file)
    counts: dict[str, int] = {}
    for mark in review_file.marks.values():
        counts[mark.status] = counts.get(mark.status, 0) + 1
    print(f"标记文件 {args.file}：{len(review_file.marks)} 条")
    for status in VALID_STATUSES:
        print(f"  {status}: {counts.get(status, 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="人工抽查标记")
    parser.add_argument("--file", default=None, help="标记文件路径")
    parser.add_argument("--source-id", default="the_rdi_book_2009")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="导出待核对的候选片段")
    p_export.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_COUNT)
    p_export.add_argument("--kind", choices=("child", "parent", "any"), default="child")
    p_export.add_argument("--section", default=None)
    p_export.add_argument("--out", default=None)

    p_mark = sub.add_parser("mark", help="标记片段")
    p_mark.add_argument("--key", action="append", default=[], help="内容哈希键")
    p_mark.add_argument("--candidate-file", default=None, help="候选清单")
    p_mark.add_argument(
        "--candidate-index", type=int, action="append", default=[], help="候选里的 chunk_index"
    )
    p_mark.add_argument("--status", default=STATUS_REVIEWED, choices=VALID_STATUSES)
    p_mark.add_argument("--reviewer", default="")
    p_mark.add_argument("--note", default="")

    p_apply = sub.add_parser("apply", help="把标记写回数据库")
    p_apply.add_argument("--dry-run", action="store_true")

    sub.add_parser("list", help="看标记统计")

    args = parser.parse_args()

    settings_index = Path("data/index")
    if args.file is None:
        args.file = str(default_review_path(settings_index))
    if getattr(args, "out", None) is None:
        args.out = str(settings_index / CANDIDATE_FILE_NAME)

    if args.command == "export":
        asyncio.run(_cmd_export(args))
    elif args.command == "mark":
        asyncio.run(_cmd_mark(args))
    elif args.command == "apply":
        asyncio.run(_cmd_apply(args))
    elif args.command == "list":
        asyncio.run(_cmd_list(args))


if __name__ == "__main__":
    main()
