# -*- coding: utf-8 -*-
"""
唯一的 LLM 出口
===============
为什么单独抽一层：现在 verify_schema.py 第 419 行自己建 client、自己带超时和计时。
三个 Agent 各写一遍就是三份重试逻辑 + 三份超时配置，而且 demo 现场出问题时
你不知道该去哪个文件里调。所以这里收敛成唯一入口。

三件事在这里统一处理，各 Agent 一律不要自己碰 OpenAI client：

1. **受限解码** —— 永远用 response_format={"type":"json_schema"}。
   verify_schema.py 的 A/B 实测（12/12 vs 0/12）已经证明 json_object 不可用。
2. **trace 冒泡** —— 每次调用返回一条 LLMTrace，一路传到 FastAPI 响应里，
   前端把它显示成"第 2 次重试 / 7.3s"。**把延迟摆在 UI 上比说"我们做了缓存"强得多。**
3. **重试策略** —— 只对 timeout / 5xx 重试，不对 schema 校验失败重试
   （后者说明 prompt 有问题，重发同样的输入是浪费）。截断和多余字段各有专门的重试。

另外内建 **live / replay / capture** 三种模式。面试现场的唯一保险：
replay 模式下所有响应走本地文件，全流程 <1 秒、零网络依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ValidationError

load_dotenv()

DEFAULT_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")

# 面试现场的超时预算：20s。留 5s 余量给重试和 SSE 传输。
# verify_schema 用的是 60s，那是离线批量脚本，可以等；demo 不能等。
TIMEOUT_S = min(float(os.getenv("LLM_TIMEOUT_S", "60")), 20.0)

_CLIENT: OpenAI | None = None
_CLIENTS: dict[float, OpenAI] = {}


def client(timeout_s: float | None = None) -> OpenAI:
    """按超时取 client。同一超时复用同一个连接池。

    timeout_s 存在的理由：**不同 stage 的合理耗时差一个数量级**。
    每日链路（教练卡片、行为观察）必须 20s 内返回，超时要卡死；
    但报告抽取这种**一次性的建档操作**实测要 45-50s（7B 出 ~1000 token），
    用同一个 20s 上限去卡它，结果不是"慢一点"而是直接失败进降级路径。
    把上限做成全局常量，等于用每日链路的 SLA 误伤建档链路。
    阶段 4 里建档走后台 job + SSE，45s 有进度条可看；每日链路仍是 20s。
    """
    global _CLIENT
    t = TIMEOUT_S if timeout_s is None else float(timeout_s)
    if t == TIMEOUT_S:
        if _CLIENT is None:
            _CLIENT = _make(t)
        return _CLIENT
    if t not in _CLIENTS:
        _CLIENTS[t] = _make(t)
    return _CLIENTS[t]


def _make(timeout_s: float) -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("SILICONFLOW_API_KEY", ""),
        base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        timeout=timeout_s,
        max_retries=0,  # 重试逻辑在这里显式控制，不要让 SDK 偷偷重试
    )


# ------------------------------------------------------------------ trace
@dataclass
class LLMTrace:
    """一次 LLM 调用的完整轨迹。会一路冒泡到 API 响应。"""

    stage: str = ""
    model: str = ""
    latency_ms: int = 0
    attempt: int = 1
    retries: list[str] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = "live"          # live / replay
    raw_content: str | None = None
    degraded: bool = False        # True 表示走了 fallback，输出不是模型给的

    def brief(self) -> str:
        """给前端 Agent Trace 面板用的一行摘要。"""
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


# ------------------------------------------------------------------ 回放
# replay 目录结构：<replay_dir>/<stage>.json = {"key": ..., "content": ..., "trace": {...}}
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


def configure(*, mode: str | None = None, replay_dir: str | Path | None = None) -> str:
    """设置运行模式。返回生效的模式。

    mode: live / replay / capture
      live    —— 真调模型
      replay  —— 只读本地文件；文件不存在时**直接失败**（不静默回落到 live，
                 否则"回放模式"就名存实亡，面试时你根本不知道自己在看假数据）
      capture —— 真调模型，并把原始响应逐字落盘，供之后转成 replay

    不传 mode 时读环境变量 DEMO_MODE，默认 live。
    """
    global _REPLAY_DIR, _CAPTURE
    mode = mode or os.getenv("DEMO_MODE", "live")
    if replay_dir:
        _REPLAY_DIR = Path(replay_dir)
    elif _REPLAY_DIR is None:
        _REPLAY_DIR = Path(__file__).parent / "fixtures" / "responses"
    if mode == "capture":
        _CAPTURE = True
        _REPLAY_DIR.mkdir(parents=True, exist_ok=True)
        return mode
    _CAPTURE = False
    return mode


def current_mode() -> str:
    return os.getenv("DEMO_MODE", "live")


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
        json.dumps({"stage": stage, "key": key, "content": content},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ 公共工具
def check_quote(source: str, quote: str) -> bool:
    """回指校验：quote 必须是 source 的连续子串。

    verify_schema.py 里的 quote_grounded 逻辑抽到这里，让 A/B 脚本和 demo 共用
    同一份判定标准 —— 否则两边标准漂移，结论就不可信了。
    """
    if not quote:
        return True  # 空串 = 声明为"推断而非直述"，不算回指失败
    return quote in source


# ------------------------------------------------------------------ 主入口
def chat_json(
    *,
    system: str,
    user: str,
    model_cls: type[BaseModel],
    name: str,
    stage: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    temperature: float = 0.0,
    max_attempts: int = 3,
    timeout_s: float | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[BaseModel, LLMTrace]:
    """一次受限解码调用，返回 (校验后的模型实例, 轨迹)。

    参数
    ----
    model_cls : 要校验的 Pydantic 模型。其 model_json_schema() 直接作为
                json_schema 传给服务端 —— 约束解码在**服务端**执行，不是这里
                校验完才算数。事后 model_validate_json 这一步是代码保证，
                两者都要有。
    stage     : 用于回放文件名和前端 trace 展示，必须稳定（如 "coach_analysis"）。
    timeout_s : 本次调用的超时上限，默认 20s。**建档类长任务（报告抽取）要显式调大**，
                否则它会在 20s 处超时并落进降级路径 —— 那不是"慢一点"，
                是整份能力画像拿不到。见 client() 的说明。
    on_step   : 回调，每一步开始时调用。前端用它实时打点（"正在做…，已 8s"），
                这是防止面试现场看起来像卡死的主要手段。

    重试策略
    --------
    - timeout / 5xx / 连接错误 → 退避重试，最多 max_attempts 次
    - finish_reason == "length"  → 立刻把 max_tokens 提到 1.5 倍重试一次
      （这是最常见的翻车原因：CoachCard 塞了 5 个 step + 提示阶梯 + 4 段抗拒脚本，
        7B 极易写超导致 JSON 不完整）
    - ValidationError          → 把精确错误路径回灌，做**一次**修复重试
      （"你多输出了 X，请只输出 schema 要求的字段"）
    """
    stage = stage or name
    key = _replay_key(name, system, user)
    trace = LLMTrace(stage=stage, model=model)

    # ---- replay 短路 ----
    if current_mode() == "replay":
        content = _replay_load(stage, key)
        if content is None:
            raise LLMError(
                f"回放缺失：{stage}.{key}。DEMO_MODE=replay 下不回落 live，"
                f"因为那样你无法分辨自己在看的是不是假数据。"
                f"先用 DEMO_MODE=capture 跑一遍把真实响应录下来。"
            )
        trace.source = "replay"
        trace.latency_ms = 0
        trace.raw_content = content
        return _parse(model_cls, content), trace

    schema = model_cls.model_json_schema()
    rf = {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}

    messages: list[dict] = [
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
            resp = client(timeout_s).chat.completions.create(
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
            time.sleep(min(2 ** attempt, 8))
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

        # ---- 截断：最常见的翻车点，立刻加 token 重试 ----
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
                # 把错误路径回灌，让模型自己修 —— 比在这里手写补丁划算
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": ("上一次的输出不符合要求：\n"
                                + _brief_errors(e, verbose=True)
                                + "\n请重新输出，只包含 schema 要求的字段。"),
                })
                continue
            raise LLMError(f"{stage} 连续 {attempt} 次 schema 不合规：{_brief_errors(e)}") from e

        if _CAPTURE:
            _replay_save(stage, key, content)
        return parsed, trace

    raise LLMError(f"{stage} 重试 {max_attempts} 次仍失败")


def _parse(model_cls: type[BaseModel], content: str) -> BaseModel:
    return model_cls.model_validate_json(content)


def _brief_errors(e: ValidationError, verbose: bool = False) -> str:
    parts = []
    for err in e.errors()[:6]:
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}({err['type']})")
    head = "; ".join(parts)
    if verbose:
        for err in e.errors()[:4]:
            loc = ".".join(str(x) for x in err["loc"])
            parts2 = f"  - {loc}: {err.get('msg')}"
            head += "\n" + parts2
    return head


# ------------------------------------------------------------------ 健康检查
def health() -> dict[str, Any]:
    """启动自检。demo 现场最怕的就是"以为在用真模型，其实 key 是空的"。"""
    mode = current_mode()
    info = {
        "mode": mode,
        "model": DEFAULT_MODEL,
        "timeout_s": TIMEOUT_S,
        "has_key": bool(os.environ.get("SILICONFLOW_API_KEY")),
        "llm_ok": False,
        "error": "",
    }
    if mode == "replay":
        info["llm_ok"] = True
        info["note"] = "replay 模式，全部走本地文件，不依赖网络"
        return info
    if not info["has_key"]:
        info["error"] = "SILICONFLOW_API_KEY 未配置"
        return info
    try:
        client().chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        info["llm_ok"] = True
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def warmup() -> None:
    """启动时打一发极短的请求预热连接，把 TLS 握手的延迟挡在第一次业务调用之前。"""
    try:
        client().chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
    except Exception:
        pass  # 连不上就交给 health() 去报，不在这里抛


if __name__ == "__main__":
    import sys
    print(json.dumps(health(), ensure_ascii=False, indent=2))
    sys.exit(0 if health()["llm_ok"] else 1)
