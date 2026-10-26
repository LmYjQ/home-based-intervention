"""记忆的身份：内容归一化、去重哈希、统一的 id 方案。

**为什么要统一 id**：记忆散在四处 —— ``children`` 的 JSON 数组、
``observations`` 表、``hypotheses`` 表、``trials`` 表。API 只收一个
``memory_id``，必须能从前缀判断该去问谁，否则每个接口都要传
"这条记忆在哪张表"。

    fact_<hash16>       children.confirmed_facts_json 里 type=fact 的那条
    pref_<hash16>       children.confirmed_facts_json 里 type=preference 的那条
    obs_<uuidhex>       observations.id
    hyp_<uuidhex>       hypotheses.id
    attempt_<uuidhex>   trials.id（只读，家长不能改）
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid

# 句末标点。归一化时去掉 —— "孩子 4 岁。" 和 "孩子 4 岁" 是同一条记忆。
_TERMINAL_PUNCT = "。！？；.!?;，,、 "

# 归一化后至少要这么长才算一条记忆。
# 太短的内容（"好" "嗯"）当记忆存下来只会污染 prompt。
MIN_CONTENT_CHARS = 4


def normalize_content(text: str) -> str:
    """归一化：全半角统一、去空白、去句末标点、小写。

    **不做同义词替换**：那需要语义判断，是模型的事。
    这里只保证"同样的字面"得到同样的哈希。
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = re.sub(r"\s+", "", out)
    out = out.strip(_TERMINAL_PUNCT)
    return out.casefold()


def content_hash(memory_type: str, content: str) -> str:
    """去重键：类型 + 归一化内容。

    **带上类型**：同样一句"孩子 4 岁"，作为 fact 和作为 hypothesis
    是两种东西，不能互相去重。
    """
    payload = f"{memory_type}\x00{normalize_content(content)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fact_memory_id(memory_type: str, digest: str, *, generation: int = 1) -> str:
    """事实 id。纠正一次加一代后缀。

    **为什么要代数**：纠正后旧的那条变成 ``superseded``，不再参与去重；
    如果家长后来又把值改回原样，内容哈希会撞上旧 id。
    ``_2`` 后缀让两条记录能同时存在于审计里。
    """
    prefix = "pref" if memory_type == "preference" else "fact"
    suffix = "" if generation <= 1 else f"_{generation}"
    return f"{prefix}_{digest[:16]}{suffix}"


def observation_memory_id(row_id: uuid.UUID | str) -> str:
    return f"obs_{_hex(row_id)}"


def hypothesis_memory_id(row_id: uuid.UUID | str) -> str:
    return f"hyp_{_hex(row_id)}"


def attempt_memory_id(row_id: uuid.UUID | str) -> str:
    return f"attempt_{_hex(row_id)}"


def _hex(value: uuid.UUID | str) -> str:
    if isinstance(value, uuid.UUID):
        return value.hex
    return str(value).replace("-", "")


# 前缀 -> 展示用的 kind。展示口径和 ``memory_type`` 不完全一样：
# ``episode`` 存的是 observations，但给家长看叫"观察"更直白。
_PREFIXES: list[tuple[str, str]] = [
    ("attempt_", "attempt"),
    ("fact_", "fact"),
    ("pref_", "preference"),
    ("obs_", "observation"),
    ("hyp_", "hypothesis"),
]


def parse_memory_id(memory_id: str) -> tuple[str, str]:
    """``memory_id`` -> ``(kind, raw)``；认不出来返回 ``("", memory_id)``。"""
    text = (memory_id or "").strip()
    for prefix, kind in _PREFIXES:
        if text.startswith(prefix):
            return kind, text[len(prefix) :]
    return "", text


def is_fact_kind(kind: str) -> bool:
    return kind in ("fact", "preference")
