"""证据片段的中文摘要（计划 §5.3）。

**为什么入库时不生成**：1178 块每块调一次 LLM，成本和延迟都不可接受，
而且大部分块永远不会被检索到。计划 §5.3 的策略是"只为进入证据包的 6–8 块
生成中文摘要，写入 ``knowledge_chunks.summary_zh`` 缓存"。

**版权边界**：摘要是**转述**，不是原文引用，所以可以生成、可以给家长看。
但 ``may_quote_to_user`` 恒为 ``False`` —— 这本书的授权没有核实过，
任何情况下都不能给大段原文。

**缓存失效**：重新入库后块 id 会变、``embedding_version`` 也会变，
所以用 ``embedding_version`` 当版本号，版本对不上就重新生成。

默认关闭（``settings.summary_zh_enabled``）—— 每次开启都要多一次 LLM 调用，
收益只是让证据面板更好看。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from rdi_agent import llm
from rdi_agent.db.engine import session_scope
from rdi_agent.schemas.evidence import EvidenceBundle

# 一次最多处理多少块。一次 LLM 调用处理 6–8 块最划算。
MAX_CHUNKS = 12

# 摘要长度上限（字）。
MAX_SUMMARY_CHARS = 120

_SYSTEM = """\
你把书里的英文段落改写成一句中文摘要，给家长看。

要求：
- 只转述，不加书里没有的内容，不加自己的判断。
- 不引用原文句子（整段照抄是不允许的）。
- 说清楚这段在讲什么，用家长能懂的话，不要术语堆砌。
- 每条摘要不超过 {max_chars} 个字。
- 保留关键信息：概念名、做法、适用场景。

输出 JSON：{{"summaries": [{{"evidence_id": "...", "summary_zh": "..."}}]}}
evidence_id 必须原样抄回去。
"""


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    summary_zh: str


class SummaryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summaries: list[SummaryItem] = Field(default_factory=list)


async def _cached_summaries(chunk_ids: list[str]) -> dict[str, str]:
    """读缓存。只取版本号对得上的。"""
    if not chunk_ids:
        return {}
    from rdi_agent.config import get_settings

    expected = get_settings().embedding_model
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id::text AS chunk_id, c.summary_zh, c.embedding_version
                    FROM knowledge_chunks c
                    WHERE c.id = ANY(CAST(:ids AS uuid[]))
                    """
                ),
                {"ids": list(chunk_ids)},
            )
        ).mappings().all()
    out: dict[str, str] = {}
    for row in rows:
        if not row["summary_zh"]:
            continue
        # embedding_version 是入库时写死的模型名；模型换了缓存就该作废
        version = row["embedding_version"]
        if version and expected and version != expected:
            continue
        out[row["chunk_id"]] = row["summary_zh"]
    return out


async def _write_summaries(summaries: dict[str, str]) -> None:
    if not summaries:
        return
    async with session_scope() as session:
        for chunk_id, summary_zh in summaries.items():
            await session.execute(
                text(
                    """
                    UPDATE knowledge_chunks
                    SET summary_zh = :summary
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": chunk_id, "summary": summary_zh},
            )


async def _generate(items: list[dict[str, Any]]) -> dict[str, str]:
    """一次 LLM 调用生成全部摘要。失败返回空 dict（调用方回退英文片段）。"""
    if not items:
        return {}
    payload = {
        "segments": [
            {
                "evidence_id": item["evidence_id"],
                "章节": item.get("section_title") or "-",
                "英文原文": item.get("source_excerpt") or item.get("summary") or "",
            }
            for item in items
        ]
    }
    try:
        result, _trace = await llm.chat_json(
            system=_SYSTEM.format(max_chars=MAX_SUMMARY_CHARS),
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            model_cls=SummaryBatch,
            name="summary_zh_batch",
            stage="summary_zh",
            temperature=0.2,
            max_tokens=2000,
        )
    except llm.LLMError:
        return {}
    out: dict[str, str] = {}
    for item in result.summaries:
        if item.evidence_id and item.summary_zh:
            out[item.evidence_id] = item.summary_zh[: MAX_SUMMARY_CHARS * 2]
    return out


async def ensure_summaries(bundle: EvidenceBundle) -> None:
    """给证据包里的片段补中文摘要（带缓存）。

    任何一步失败都不抛异常 —— 摘要只是锦上添花，
    拿不到就继续用英文片段。
    """
    try:
        items = [
            {
                "evidence_id": e.evidence_id,
                "section_title": e.section_title,
                "summary": e.summary,
                "source_excerpt": e.source_excerpt,
            }
            for e in bundle.evidence[:MAX_CHUNKS]
        ]
        if not items:
            return
        cached = await _cached_summaries([i["evidence_id"] for i in items])
        missing = [i for i in items if i["evidence_id"] not in cached]
        if missing:
            generated = await _generate(missing)
            await _write_summaries(generated)
            cached.update(generated)
        for item in bundle.evidence:
            summary_zh = cached.get(item.evidence_id)
            if summary_zh:
                item.summary = summary_zh
    except Exception:  # noqa: BLE001 - 摘要失败不该影响主流程
        return
