"""向量化：BAAI/bge-m3（SiliconFlow，1024 维）。

**为什么单独一个模块**：向量化是唯一会花钱、又会失败的外部调用。把它跟切块
逻辑分开，才能在重建索引时只重跑 embedding、复用已有的块。

**批量**：一次请求塞一批文本，比一条一条快一个数量级。批次开太大又容易被
服务端限流，所以默认 16，超时就退成 8、4 重试。
"""

from __future__ import annotations

import asyncio
from typing import Sequence

import httpx

from rdi_agent.config import get_settings

# 单批文本数
DEFAULT_BATCH_SIZE = 16
# 失败重试次数与退避
MAX_ATTEMPTS = 3


class EmbeddingError(RuntimeError):
    """向量化失败。"""


async def _post_batch(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    batch: Sequence[str],
) -> list[list[float]]:
    response = await client.post(
        f"{base_url}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "input": list(batch),
            "encoding_format": "float",
        },
    )
    if response.status_code != 200:
        raise EmbeddingError(
            f"embedding 失败 HTTP {response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    data = payload.get("data") or []
    if len(data) != len(batch):
        raise EmbeddingError(
            f"embedding 返回条数不符：请求 {len(batch)}，返回 {len(data)}"
        )
    # 服务端不保证顺序，按 index 排回来
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    return [item["embedding"] for item in ordered]


async def embed_texts(
    texts: Sequence[str],
    *,
    model: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[list[float]]:
    """把一批文本变成向量。返回顺序和输入顺序一致。

    失败就抛 :class:`EmbeddingError` —— 入库是"要么全成要么不成"的事，
    半套向量比没有向量更危险。
    """
    if not texts:
        return []
    settings = get_settings()
    model = model or settings.embedding_model
    base_url = settings.llm_base_url
    api_key = settings.llm_api_key
    if not api_key:
        raise EmbeddingError("没有配置 LLM_API_KEY，无法向量化")

    size = max(1, min(batch_size, len(texts)))
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for start in range(0, len(texts), size):
            batch = list(texts[start: start + size])
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    out.extend(
                        await _post_batch(client, base_url, api_key, model, batch)
                    )
                    last_error = None
                    break
                except Exception as e:  # noqa: BLE001 - 统一按可重试处理
                    last_error = e
                    if attempt >= max_attempts:
                        break
                    await asyncio.sleep(min(2**attempt, 8))
            if last_error is not None:
                raise EmbeddingError(
                    f"第 {start // size + 1} 批向量化失败：{last_error}"
                ) from last_error
    return out


async def embed_query(text: str, *, model: str | None = None) -> list[float]:
    """单条查询向量化。"""
    vectors = await embed_texts([text], model=model)
    return vectors[0]


def expected_dim() -> int:
    """配置里声明的向量维度。入库前用它校验长度对不对。"""
    return get_settings().embedding_dim
