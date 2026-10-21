"""唯一的 LLM 出口。

从旧 ``asd_agent/llm.py`` 迁移的能力（计划 §2.2），旧 prompt 和旧 Agent 调用全部丢弃：

1. **受限解码** —— 永远用 ``response_format={"type": "json_schema"}``。
   旧项目的 A/B 实测是 12/12 vs 0/12，``json_object`` 不可用。
2. **trace 冒泡** —— 每次调用返回一条 :class:`LLMTrace`，一路传到 API 响应里。
   把延迟和重试摆在 UI 上，比说"我们做了缓存"强得多。
3. **重试策略** —— 只对 timeout / 5xx 重试；schema 校验失败做一次"把错误路径回灌"的修复重试。

另有 **live / replay / capture** 三种模式。replay 模式下所有响应走本地文件，
全流程零网络依赖；文件缺失时**直接失败**，不静默回落到 live。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from rdi_agent.config import get_settings

__all__ = [
    "LLMError",
    "LLMTrace",
    "LLMUnavailable",
    "chat_json",
    "configure",
    "current_mode",
    "health",
    "warmup",
]


# ------------------------------------------------------------------ trace
@dataclass
class LLMTrace:
    """一次 LLM 调用的完整轨迹，会一路冒泡到 API 响应。"""

    stage: str = ""
    model: str = ""
    latency_ms: int = 0
    attempt: int = 1
    retries: list[str] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = "live"  # live / replay / capture
    raw_content: str | None = None
    degraded: bool = False  # True 表示走了 fallback，输出不是模型给的

    def brief(self) -> str:
        head = f"{self.latency_ms}ms"
        if self.attempt > 1:
            head += f" · 第{self.attempt}次"
        if self.degraded:
            head += " · 已降级"
        if self.retries:
            head += f" · 重试原因: {'; '.join(self.retries)}"
        return head


class LLMError(RuntimeError):
    """LLM 调用失败（超时、5xx、截断重试后仍失败、schema 始终不合规）。"""


class LLMUnavailable(LLMError):
    """根本没连上（没配 key、网络不通）。上层据此切换到 fallback。"""


# ------------------------------------------------------------------ client
# 全链路是 async（async SQLAlchemy / async FastAPI / async LangGraph），
# 所以 LLM 出口也必须是 async：同步客户端会把事件循环堵住 5–20 秒，
# 整个服务在那段时间里连健康检查都答不了。
_CLIENT: AsyncOpenAI | None = None
_CLIENTS: dict[float, AsyncOpenAI] = {}


def client(timeout_s: float | None = None) -> AsyncOpenAI:
    """按超时取 client，同一超时复用同一个连接池。

    不同 stage 的合理耗时差一个数量级：日常链路必须 20s 内返回，
    建档类长任务要显式调大。做成参数而不是全局常量，避免用日常链路的
    SLA 误伤建档链路。
    """
    global _CLIENT
    settings = get_settings()
    t = settings.llm_timeout_s if timeout_s is None else float(timeout_s)
    if t == settings.llm_timeout_s:
        if _CLIENT is None:
            _CLIENT = _make(t)
        return _CLIENT
    if t not in _CLIENTS:
        _CLIENTS[t] = _make(t)
    return _CLIENTS[t]


def _make(timeout_s: float) -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=timeout_s,
        max_retries=0,  # 重试逻辑在这里显式控制，不要让 SDK 偷偷重试
    )


def reset_client() -> None:
    """丢掉缓存的 client。改过 key 或 base_url 之后必须调用，否则还会用旧连接。"""
    global _CLIENT
    _CLIENT = None
    _CLIENTS.clear()


# ------------------------------------------------------------------ 回放
_REPLAY_DIR: Path | None = None
_CAPTURE = False


def _replay_key(name: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    h.update(name.encode())
    h.update(b"\0")
    h.update(system.encode())
    h.update(b"\0")
    h.update(user.encode())
    return h.hexdigest()[:16]


def configure(
    *,
    mode: str | None = None,
    replay_dir: str | Path | None = None,
) -> str:
    """设置运行模式，返回生效的模式。

    mode: live / replay / capture
      live    —— 真调模型
      replay  —— 只读本地文件；文件不存在时直接失败
      capture —— 真调模型，并把原始响应逐字落盘，供之后转成 replay
    """
    global _REPLAY_DIR, _CAPTURE
    settings = get_settings()
    mode = mode or settings.demo_mode
    if replay_dir:
        _REPLAY_DIR = Path(replay_dir)
    elif _REPLAY_DIR is None:
        _REPLAY_DIR = settings.replay_dir
    if mode == "capture":
        _CAPTURE = True
        _REPLAY_DIR.mkdir(parents=True, exist_ok=True)
        return mode
    _CAPTURE = False
    return mode


def current_mode() -> str:
    return get_settings().demo_mode


def _replay_path(stage: str, key: str) -> Path:
    assert _REPLAY_DIR is not None
    return _REPLAY_DIR / f"{stage}.{key}.json"


def _replay_load(stage: str, key: str) -> str | None:
    p = _replay_path(stage, key)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["content"]
    return None


def _replay_save(stage: str, key: str, content: str) -> None:
    p = _replay_path(stage, key)
    p.write_text(
        json.dumps(
            {"stage": stage, "key": key, "content": content},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ 主入口
async def chat_json(
    *,
    system: str,
    user: str,
    model_cls: type[BaseModel],
    name: str,
    stage: str = "",
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
    max_attempts: int | None = None,
    timeout_s: float | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[BaseModel, LLMTrace]:
    """一次受限解码调用，返回 (校验后的模型实例, 轨迹)。

    参数
    ----
    model_cls : 要校验的 Pydantic 模型。其 model_json_schema() 直接作为
                json_schema 传给服务端 —— 约束解码在**服务端**执行，
                事后 model_validate_json 这一步是代码保证，两者都要有。
    stage     : 用于回放文件名和前端 trace 展示，必须稳定（如 "intake_parse"）。
    on_step   : 回调，每一步开始时调用，前端用它实时打点。

    重试策略
    --------
    - timeout / 5xx / 连接错误 → 退避重试
    - finish_reason == "length" → 把 max_tokens 提到 1.5 倍重试
    - ValidationError → 把精确错误路径回灌，做一次修复重试
    """
    settings = get_settings()
    stage = stage or name
    model = model or settings.llm_model
    max_attempts = max_attempts or settings.llm_max_attempts
    key = _replay_key(name, system, user)
    trace = LLMTrace(stage=stage, model=model)

    # ---- replay 短路 ----
    if current_mode() == "replay":
        content = _replay_load(stage, key)
        if content is None:
            raise LLMError(
                f"回放缺失：{stage}.{key}。DEMO_MODE=replay 下不回落 live，"
                "因为那样你无法分辨自己在看的是不是假数据。"
                "先用 DEMO_MODE=capture 跑一遍把真实响应录下来。"
            )
        trace.source = "replay"
        trace.latency_ms = 0
        trace.raw_content = content
        return _parse(model_cls, content), trace

    schema = model_cls.model_json_schema()
    rf = {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        trace.attempt = attempt
        if on_step:
            on_step(f"{stage} · 第{attempt}次尝试")
        t0 = time.time()
        try:
            resp = await client(timeout_s).chat.completions.create(
                model=model,
                messages=messages,
                response_format=rf,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            trace.latency_ms = int((time.time() - t0) * 1000)
            trace.retries.append(msg[:120])
            if "api_key" in msg.lower() or "connection" in msg.lower() or attempt >= max_attempts:
                raise LLMUnavailable(f"{stage} 连不上模型：{msg}") from e
            await asyncio.sleep(min(2**attempt, 8))
            continue

        ch = resp.choices[0]
        content = ch.message.content
        trace.latency_ms = int((time.time() - t0) * 1000)
        trace.finish_reason = ch.finish_reason or ""
        trace.raw_content = content
        if getattr(resp, "usage", None):
            trace.prompt_tokens = resp.usage.prompt_tokens or 0
            trace.completion_tokens = resp.usage.completion_tokens or 0

        if not content:
            trace.retries.append("空响应")
            if attempt >= max_attempts:
                raise LLMError(f"{stage} 连续 {attempt} 次空响应")
            continue

        # ---- 截断：立刻加 token 重试 ----
        if trace.finish_reason == "length":
            trace.retries.append("输出被截断(max_tokens 不够)")
            max_tokens = int(max_tokens * 1.5)
            if attempt < max_attempts:
                continue
            raise LLMError(f"{stage} 加到 {max_tokens} tokens 仍被截断")

        # ---- 校验 ----
        try:
            parsed = _parse(model_cls, content)
        except ValidationError as e:
            trace.retries.append("schema 不合规: " + _brief_errors(e))
            if attempt < max_attempts:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一次的输出不符合要求：\n"
                            + _brief_errors(e, verbose=True)
                            + "\n请重新输出，只包含 schema 要求的字段。"
                        ),
                    }
                )
                continue
            raise LLMError(f"{stage} 连续 {attempt} 次 schema 不合规：{_brief_errors(e)}") from e

        if _CAPTURE:
            _replay_save(stage, key, content)
        return parsed, trace

    raise LLMError(f"{stage} 重试 {max_attempts} 次仍失败")


def _parse(model_cls: type[BaseModel], content: str) -> BaseModel:
    return model_cls.model_validate_json(content)


def _brief_errors(e: ValidationError, verbose: bool = False) -> str:
    parts: list[str] = []
    for err in e.errors()[:6]:
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}({err['type']})")
    head = "; ".join(parts)
    if verbose:
        for err in e.errors()[:4]:
            loc = ".".join(str(x) for x in err["loc"])
            head += f"\n  - {loc}: {err.get('msg')}"
    return head


# ------------------------------------------------------------------ 健康检查
async def health() -> dict[str, Any]:
    """启动自检。demo 现场最怕的就是"以为在用真模型，其实 key 是空的"。"""
    settings = get_settings()
    mode = settings.demo_mode
    info: dict[str, Any] = {
        "mode": mode,
        "model": settings.llm_model,
        "timeout_s": settings.llm_timeout_s,
        "has_key": settings.has_llm_key,
        "llm_ok": False,
        "error": "",
    }
    if mode == "replay":
        info["llm_ok"] = True
        info["note"] = "replay 模式，全部走本地文件，不依赖网络"
        return info
    if not info["has_key"]:
        info["error"] = "LLM_API_KEY 未配置"
        return info
    try:
        await client().chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        info["llm_ok"] = True
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


async def warmup() -> None:
    """启动时打一发极短的请求预热连接，把 TLS 握手的延迟挡在第一次业务调用之前。"""
    try:
        await client().chat.completions.create(
            model=get_settings().llm_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
    except Exception:
        # 连不上就交给 health() 去报，不在这里抛
        pass


if __name__ == "__main__":
    import asyncio
    import sys

    info = asyncio.run(health())
    print(json.dumps(info, ensure_ascii=False, indent=2))
    sys.exit(0 if info["llm_ok"] else 1)
