"""重排：把混合检索的候选按"跟原问题的相关性"重新排一遍。

**为什么需要**：RRF 只看名次，不看内容。像"孩子不敢跟人打招呼"这种查询，
向量路会捞回一堆"讲孤儿院里孩子的故事"的段落 —— 语义上沾边，
但**没有一条在讲怎么应对退缩**。重排是把候选交给一个交叉编码器
（query + 文档一起过模型）逐条打分，它能看出"这段到底在讲什么"。

**必须能降级**：重排是一次外部 API 调用，会超时、会限流、会没钱。
计划 §9.2 的验收明确要求"关闭 reranker 时仍有可接受的召回，
系统能明确显示当前是否降级"。所以：

- 重排失败 **不抛异常**，回退到 RRF 的原顺序；
- 降级状态记在 :class:`RerankResult.degraded` 和 ``.reason`` 里，
  调用方必须能把它显示出来 —— **静默降级比不降级更糟**，
  因为用户会以为看到的是重排后的结果。

    python -m rdi_agent.rag.rerank "孩子不敢跟人打招呼" --text "..."
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from rdi_agent.config import get_settings


@dataclass
class RerankResult:
    """重排结果 + 降级状态。"""

    # 重排后的下标顺序（对应传入的 candidates 下标）
    order: list[int]
    # 每个候选的重排分（未重排时为 None）
    scores: list[float | None] = field(default_factory=list)
    degraded: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "scores": self.scores,
            "degraded": self.degraded,
            "reason": self.reason,
        }


async def rerank(
    query: str,
    documents: Sequence[str],
    *,
    top_n: int | None = None,
    model: str | None = None,
    timeout_s: float = 30.0,
) -> RerankResult:
    """重排候选。**任何失败都降级，不抛异常。**

    降级时返回原顺序（``list(range(len(documents)))``），
    ``degraded=True`` 且 ``reason`` 说明原因。
    """
    original_order = list(range(len(documents)))
    if not documents:
        return RerankResult(order=[], scores=[], degraded=False)

    settings = get_settings()
    if not settings.rerank_enabled:
        return RerankResult(
            order=original_order,
            scores=[None] * len(documents),
            degraded=True,
            reason="rerank_enabled=false（配置里关掉了重排）",
        )
    if not settings.llm_api_key:
        return RerankResult(
            order=original_order,
            scores=[None] * len(documents),
            degraded=True,
            reason="没有配置 LLM_API_KEY，无法重排",
        )

    model = model or settings.rerank_model
    payload: dict[str, Any] = {
        "model": model,
        "query": query,
        "documents": list(documents),
        "top_n": top_n or len(documents),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                f"{settings.llm_base_url}/rerank",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=payload,
            )
            if response.status_code != 200:
                return RerankResult(
                    order=original_order,
                    scores=[None] * len(documents),
                    degraded=True,
                    reason=f"重排接口 HTTP {response.status_code}: {response.text[:200]}",
                )
            data = response.json()
    except Exception as e:  # noqa: BLE001 - 降级是这里的第一职责
        return RerankResult(
            order=original_order,
            scores=[None] * len(documents),
            degraded=True,
            reason=f"重排调用失败：{type(e).__name__}: {e}",
        )

    results = data.get("results") or []
    if not results:
        return RerankResult(
            order=original_order,
            scores=[None] * len(documents),
            degraded=True,
            reason="重排接口返回空结果",
        )

    # 服务端可能不按 relevance 排序，自己排一遍保证顺序确定
    results = sorted(results, key=lambda r: r.get("relevance_score", 0.0), reverse=True)

    order: list[int] = []
    scores: list[float | None] = [None] * len(documents)
    for item in results:
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(documents)):
            continue
        order.append(idx)
        scores[idx] = float(item.get("relevance_score", 0.0))

    if not order:
        return RerankResult(
            order=original_order,
            scores=[None] * len(documents),
            degraded=True,
            reason="重排返回的下标都越界了",
        )
    # 没被重排返回的候选排到最后，别直接丢
    for idx in original_order:
        if idx not in order:
            order.append(idx)

    return RerankResult(order=order, scores=scores, degraded=False)


async def main() -> None:
    parser = argparse.ArgumentParser(description="重排")
    parser.add_argument("query")
    parser.add_argument("--text", action="append", required=True, help="候选文本，可重复")
    args = parser.parse_args()

    result = await rerank(args.query, args.text)
    print(f"降级: {result.degraded}  原因: {result.reason or '-'}")
    for rank, idx in enumerate(result.order, start=1):
        score = result.scores[idx]
        score_text = f"{score:.4f}" if score is not None else "-"
        print(f"[{rank}] score={score_text}  {args.text[idx][:100]}")


if __name__ == "__main__":
    asyncio.run(main())
