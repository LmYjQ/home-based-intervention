"""删除旧的英文书来源（``the_rdi_book_2009``）。

**为什么单独一个脚本**：知识库从英文书换成中文指南是"替换"，
不是"新增"。留着旧书的话：

- 检索会**混检** —— 问"挑战性行为"会把英文书的段落一起捞上来，
  两套来源的内容风格、术语体系完全不同，混在一起家长分不清；
- 阈值没法标定 —— 距离阈值是对着**一个**知识库标出来的，
  两个库混在一起，标出来的数没有意义；
- 评测指标会失真 —— "无关问题"（红烧肉）可能在英文书里命中
  某些段落，硬凑率的数字就不代表真实水平。

**为什么不在迁移里删**：迁移是"改结构"，删数据是"改内容"。
混在一起的话，迁移一旦失败回滚，内容也回滚不了。
分成两步更安全：迁移先跑通（可回滚），确认无误再删内容。

    python -m tools.drop_rdi_book --dry-run    # 只看会删什么
    python -m tools.drop_rdi_book              # 真删
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete, select

from rdi_agent.db.engine import session_scope
from rdi_agent.db.models import BookSection, KnowledgeChunk, KnowledgeSource

OLD_SOURCE_ID = "the_rdi_book_2009"


async def drop_source(source_id: str, *, dry_run: bool = False) -> dict[str, int]:
    """删掉一个来源的全部内容。

    **删序**：块 -> 章节 -> 来源。块和章节都有外键指向来源，
    反过来删会撞约束。

    **块里有自引用**（``parent_chunk_id`` 指向同表的父块），
    所以用一条 ``DELETE ... WHERE source_id = ...`` 一次删完 ——
    按行删的话，第一行删掉之后指向它的子块外键就悬空了。
    Postgres 的外键是 ``ON DELETE`` 默认行为（NO ACTION），
    但同一条语句里删全部行时约束在语句结束时检查，所以没问题。
    """
    async with session_scope() as session:
        source = (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.source_id == source_id
                )
            )
        ).scalar_one_or_none()
        if source is None:
            return {"source": 0, "chunks": 0, "sections": 0}

        chunk_count = (
            await session.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.source_id == source.id
                )
            )
        ).scalars().all()
        section_count = (
            await session.execute(
                select(BookSection.id).where(
                    BookSection.source_id == source.id
                )
            )
        ).scalars().all()

        stats = {
            "source": 1,
            "chunks": len(chunk_count),
            "sections": len(section_count),
        }
        if dry_run:
            return stats

        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
        )
        await session.execute(
            delete(BookSection).where(BookSection.source_id == source.id)
        )
        await session.delete(source)
        return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="删除旧的英文书来源")
    parser.add_argument("--source-id", default=OLD_SOURCE_ID)
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印会删多少，不真删"
    )
    args = parser.parse_args()

    stats = asyncio.run(drop_source(args.source_id, dry_run=args.dry_run))
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}已删除来源 {args.source_id}："
        f"来源 {stats['source']} 个，块 {stats['chunks']} 个，"
        f"章节 {stats['sections']} 个"
    )


if __name__ == "__main__":
    main()
