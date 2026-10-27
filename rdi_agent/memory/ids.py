"""记忆的身份：内容归一化、去重哈希、统一的 id 方案。

**为什么要统一 id**：记忆散在三处 —— ``memory_items`` 表、
``children`` 的小结 JSON 数组、``trials`` 表。API 只收一个
``memory_id``，必须能从前缀判断该去问谁，否则每个接口都要传
"这条记忆在哪张表"。

    mi_<uuidhex>       memory_items.id（一句短句）
    summ_<hash16>      children.memory_summaries_json 里的类目小结
    attempt_<uuidhex>  trials.id（只读，家长不能改）
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

# 短句的三个"用途"，去重哈希要带上 ——
# 同样一句"不希望他被批评"，作为偏好和作为普通短句是两种东西。
KIND_PLAIN = "note"
KIND_PREFERENCE = "pref"
KIND_INFERENCE = "inf"


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
    """去重键：用途 + 归一化内容。

    **带上用途**：同样一句"孩子 4 岁"，作为普通短句和作为类目小结
    是两种东西，不能互相去重。
    """
    payload = f"{memory_type}\x00{normalize_content(content)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def note_hash_kind(*, is_preference: bool, inference: bool) -> str:
    """短句的哈希用途前缀。"""
    if is_preference:
        return KIND_PREFERENCE
    if inference:
        return KIND_INFERENCE
    return KIND_PLAIN


def note_content_hash(
    content: str, *, is_preference: bool = False, inference: bool = False
) -> str:
    """短句的去重键。"""
    return content_hash(
        note_hash_kind(is_preference=is_preference, inference=inference), content
    )


def summary_memory_id(category: str, digest: str, *, generation: int = 1) -> str:
    """类目小结 id。

    **带上类目**：同一个类目只留一条活的小结，新一轮归纳出来的
    supersede 旧的 —— 所以 id 里带类目才能一眼看出"这俩是同一个类目"。
    """
    suffix = "" if generation <= 1 else f"_{generation}"
    return f"summ_{category}_{digest[:12]}{suffix}"


def note_memory_id(row_id: uuid.UUID | str) -> str:
    return f"mi_{_hex(row_id)}"


def attempt_memory_id(row_id: uuid.UUID | str) -> str:
    return f"attempt_{_hex(row_id)}"


def _hex(value: uuid.UUID | str) -> str:
    if isinstance(value, uuid.UUID):
        return value.hex
    return str(value).replace("-", "")


# 前缀 -> 展示用的 kind。
_PREFIXES: list[tuple[str, str]] = [
    ("attempt_", "attempt"),
    ("summ_", "summary"),
    ("mi_", "note"),
]


def parse_memory_id(memory_id: str) -> tuple[str, str]:
    """``memory_id`` -> ``(kind, raw)``；认不出来返回 ``("", memory_id)``。"""
    text = (memory_id or "").strip()
    for prefix, kind in _PREFIXES:
        if text.startswith(prefix):
            return kind, text[len(prefix) :]
    return "", text
