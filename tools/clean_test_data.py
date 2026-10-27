"""清空开发过程产生的测试数据（业务表 + Checkpointer）。

**为什么要有这个脚本**：手工开发时随手建账号、跑反馈闭环、试调试分叉，
几天下来库里就是一堆 `conv_xxx` / `trial_xxx` 的垃圾数据。
手工 `TRUNCATE` 容易漏表、漏 schema（Checkpointer 在 `langgraph` schema 里，
不在 `public`），而且忘加 `RESTART IDENTITY` 会让主键继续往上跳。

**清什么**：
- 业务表：孩子、会话、证据包、方案、尝试、反馈、记忆（观察/假设/审计事件）
- Checkpointer：`langgraph` schema 下的 checkpoints / blobs / writes
  （不清的话，重启后旧会话的线程还在，调试面板里全是历史分支）

**不清什么**：
- 知识库（`knowledge_sources` / `book_sections` / `knowledge_chunks` / `concept_aliases`）
  —— 入库一次要跑 embedding，贵，而且清了就测不了检索。
  真要清知识库用 `tools/drop_rdi_book.py`。
- `alembic_version` / 表结构 —— 脚本只删数据，不动 schema。

**用法**：

    python -m tools.clean_test_data --dry-run    # 只看会删多少
    python -m tools.clean_test_data              # 真删
    python -m tools.clean_test_data --yes        # 真删，不交互确认
    python -m tools.clean_test_data --keep-checkpoints   # 只清业务表，保留会话线程

**真删要确认**：默认会先打印清单并等一次回车，
免得手滑在有真实数据的库上敲了回车。
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from rdi_agent.db.engine import dispose_engine, get_engine

# 业务表：清空顺序无所谓，TRUNCATE ... CASCADE 一次搞定外键。
BUSINESS_TABLES = [
    "children",
    "interactions",
    "evidence_bundles",
    "plans",
    "trials",
    "feedbacks",
    "memory_items",
    "memory_events",
]

# Checkpointer 的表在独立 schema 里，名字和业务表不冲突。
CHECKPOINT_SCHEMA = "langgraph"
CHECKPOINT_TABLES = [
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
]

# 知识库相关：明确列出来是为了"脚本里永远碰不到它们"。
KEEP_TABLES = [
    "knowledge_sources",
    "book_sections",
    "knowledge_chunks",
    "concept_aliases",
]


async def _table_exists(conn, schema: str, table: str) -> bool:
    row = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    )
    return row.scalar() is not None


async def _count(conn, schema: str, table: str) -> int:
    row = await conn.execute(
        text(f'SELECT count(*) FROM "{schema}"."{table}"')
    )
    return int(row.scalar() or 0)


async def survey(include_checkpoints: bool) -> dict[str, dict[str, int]]:
    """先数一遍每张表有多少行。只读，不改。"""
    plan: dict[str, dict[str, int]] = {}
    async with get_engine().connect() as conn:
        for table in BUSINESS_TABLES:
            if await _table_exists(conn, "public", table):
                plan[table] = {"rows": await _count(conn, "public", table)}
        if include_checkpoints:
            for table in CHECKPOINT_TABLES:
                if await _table_exists(conn, CHECKPOINT_SCHEMA, table):
                    plan[f"{CHECKPOINT_SCHEMA}.{table}"] = {
                        "rows": await _count(conn, CHECKPOINT_SCHEMA, table)
                    }
    return plan


async def clean(include_checkpoints: bool) -> dict[str, int]:
    """真删。返回每张表被清掉的行数。"""
    deleted: dict[str, int] = {}
    async with get_engine().begin() as conn:
        # 业务表：一次 TRUNCATE，CASCADE 解决外键，RESTART IDENTITY 让主键从头来。
        present = [
            t for t in BUSINESS_TABLES if await _table_exists(conn, "public", t)
        ]
        if present:
            # 先数再删：报告里要的是"清掉了多少"，不是"现在还剩多少"。
            for t in present:
                deleted[t] = await _count(conn, "public", t)
            names = ", ".join(f'"{t}"' for t in present)
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

        if include_checkpoints:
            present = [
                t
                for t in CHECKPOINT_TABLES
                if await _table_exists(conn, CHECKPOINT_SCHEMA, t)
            ]
            if present:
                for t in present:
                    deleted[f"{CHECKPOINT_SCHEMA}.{t}"] = await _count(
                        conn, CHECKPOINT_SCHEMA, t
                    )
                names = ", ".join(f'"{CHECKPOINT_SCHEMA}"."{t}"' for t in present)
                await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return deleted


def _print_plan(plan: dict[str, dict[str, int]]) -> None:
    if not plan:
        print("库里没有可清的业务表（表还没建？先跑 make migrate）")
        return
    print("将要清空的表：")
    for name, info in plan.items():
        print(f"  {name:32s} {info['rows']:>8d} 行")
    print(f"  {'（知识库不动）':32s}")


async def _run(args: argparse.Namespace) -> None:
    plan = await survey(not args.keep_checkpoints)
    _print_plan(plan)
    total = sum(info["rows"] for info in plan.values())
    if total == 0:
        print("\n本来就是空的，不用清。")
        return

    if args.dry_run:
        print(f"\n[dry-run] 合计 {total} 行，未执行任何删除。")
        return

    if not args.yes:
        print("\n确认清空？输入 yes 继续：", end="", flush=True)
        answer = input().strip().lower()
        if answer != "yes":
            print("已取消。")
            return

    deleted = await clean(not args.keep_checkpoints)
    print(f"\n已清空 {len(deleted)} 张表，合计 {sum(deleted.values())} 行。")
    for name, rows in deleted.items():
        print(f"  {name:32s} 清掉 {rows} 行")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="清空开发期产生的测试数据（业务表 + Checkpointer）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印会删多少，不真删"
    )
    parser.add_argument(
        "--yes", action="store_true", help="跳过交互确认（脚本里调用时用）"
    )
    parser.add_argument(
        "--keep-checkpoints",
        action="store_true",
        help="保留 langgraph schema 里的会话线程，只清业务表",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args))
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    main()
