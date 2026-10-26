"""把人工确认过的切分结果写回库。

    python -m tools.import_chunks --file data/index/confirmed_chunks.json

**做什么**（"标记 + 改文本 + 删块"）：

- 删掉的块不写进去
- 改过文本的块用新文本（并重算分词后的 ``content_normalized``）
- 标过状态的块写上 ``review_status``
- 没动过的块原样入库

**向量复用**：文本没变的块直接沿用库里旧向量，只有真改过/新加的块才调
embedding 接口。

**写库方式是"先删后建"**（复用 ``pipeline.write_to_db``）—— 和入库流水线
一个语义，重跑一次不会多出半套数据。

**确认结果同时存成 sidecar**（``data/index/confirmed_chunks.json``）。
入库流水线重建时会自动回填它，所以 ``make ingest`` 不会把人工确认冲掉。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from rdi_agent.config import get_settings
from rdi_agent.db.engine import session_scope
from rdi_agent.db.models import KnowledgeSource
from rdi_agent.rag import search as search_mod
from rdi_agent.rag.ingest import concepts, confirmed, embeddings, pipeline

DOWNLOADS = Path.home() / "Downloads"


def find_confirmed(explicit: str | None, index_dir: Path) -> Path:
    """找确认结果文件。

    顺序：命令行给的 -> 索引目录里的 -> ``~/Downloads`` 里最新的。
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"文件不存在：{path}")
        return path

    local = confirmed.default_confirmed_path(index_dir)
    if local.exists():
        return local

    candidates = sorted(
        DOWNLOADS.glob("confirmed_chunks*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise SystemExit(
        "找不到确认结果文件。\n"
        "  用 --file 指定，或者先在 tools/chunk_browser.html 里点"
        "「导出确认结果」。"
    )


def _parse_vector(text_value: str) -> list[float]:
    """pgvector 的文本形态 ``[0.1,0.2]`` -> list[float]。"""
    return [float(x) for x in text_value.strip().strip("[]").split(",") if x]


async def old_vectors(source_id: str) -> dict[str, list[float]]:
    """库里现有的向量，按 ``content_normalized`` 索引。

    **必须在写库之前取** —— ``write_to_db`` 是先删后建，删完就没了。
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.content_normalized, c.embedding::text AS emb
                    FROM knowledge_chunks c
                    JOIN knowledge_sources s ON s.id = c.source_id
                    WHERE s.source_id = :sid AND c.embedding IS NOT NULL
                    """
                ),
                {"sid": source_id},
            )
        ).all()
    out: dict[str, list[float]] = {}
    for normalized, emb in rows:
        out[normalized] = _parse_vector(emb)
    return out


async def run(file_path: Path, *, index_dir: Path) -> None:
    data = json.loads(file_path.read_text(encoding="utf-8"))
    file = confirmed.ConfirmedFile.from_dict(data)
    if not file.chunks:
        raise SystemExit(f"{file_path} 里没有块")

    section_map, chunks, marks = confirmed.build_from_confirmed(file)
    parents = sum(1 for c in chunks if c.is_parent)
    children = len(chunks) - parents
    print(
        f"确认结果：父块 {parents} | 子块 {children}"
        f" | 标记 {len(marks)} 条"
        f" | 删掉 {sum(1 for e in file.chunks if e.deleted)} 块"
    )

    # ---- 向量：没改过的沿用旧的 ----
    source_id = file.source_id or pipeline.SOURCE_ID
    cache = await old_vectors(source_id)
    vectors: list[list[float] | None] = [None] * len(chunks)
    todo: list[int] = []
    for i, chunk in enumerate(chunks):
        cached = cache.get(chunk.content_normalized)
        if cached is not None:
            vectors[i] = cached
        else:
            todo.append(i)
    if todo:
        texts = [
            pipeline._embed_text(chunks[i], pipeline.EMBED_INPUT_NORMALIZED)
            for i in todo
        ]
        print(f"重新向量化 {len(todo)} 个块（其余 {len(chunks) - len(todo)} 个沿用旧向量）")
        fresh = await embeddings.embed_texts(texts)
        for i, vector in zip(todo, fresh):
            vectors[i] = vector

    # ---- 写库 ----
    md_dir = Path(file.pdf) if file.pdf else Path(pipeline.SOURCE_ID)
    content_hash = (
        pipeline._dir_hash(md_dir) if md_dir.is_dir() else file.pdf or ""
    )
    source = KnowledgeSource(
        source_id=source_id,
        source_type="guideline",
        title=pipeline.SOURCE_TITLE,
        authors_json=pipeline.SOURCE_AUTHORS,
        publication_year=pipeline.PUBLICATION_YEAR,
        language=pipeline.SOURCE_LANGUAGE,
        rights_status=pipeline.RIGHTS_STATUS,
        source_uri=str(md_dir),
        content_hash=content_hash,
        version=pipeline.INDEX_VERSION,
    )
    counts = await pipeline.write_to_db(
        source=source,
        section_map=section_map,
        chunks=chunks,
        vectors=vectors,  # type: ignore[arg-type]
        marks=marks,
    )
    print(f"已写入：章节 {counts['sections']} | 块 {counts['chunks']}")

    # ---- 存 sidecar：之后 make ingest 会自动回填这份确认结果 ----
    sidecar = confirmed.save_confirmed(
        confirmed.default_confirmed_path(index_dir), file
    )
    print(f"确认结果已存：{sidecar}")

    # ---- 跟着内容变的东西 ----
    # 块 UUID 全变了，术语表里记的出处块 ID 会失效，重算一遍。
    concept_stats = await concepts.load()
    print(f"术语 {concept_stats['total']} 条已重算")
    await search_mod.ensure_high_freq_terms(force=True)
    print("高频词表已重算")


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="导入人工确认过的切分结果")
    parser.add_argument(
        "--file",
        default=None,
        help="confirmed_chunks.json 的路径（默认在索引目录或 ~/Downloads 里找）",
    )
    args = parser.parse_args()

    index_dir = Path(settings.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    file_path = find_confirmed(args.file, index_dir)
    print(f"读入：{file_path}")
    asyncio.run(run(file_path, index_dir=index_dir))


if __name__ == "__main__":
    main()
