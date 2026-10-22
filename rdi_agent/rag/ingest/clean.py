"""文本清洗：把 calibre 的排版痕迹还原成能读、能检索的正文。

**要解决什么**：这个 PDF 是从 mobi 重排的，正文每 70~80 字符硬换行 —— 一句话
会被拦腰截成两行。不接回去的话：

- 检索侧：一个词被劈成两半，FTS 和向量都命中不了；
- 展示侧：家长看到的引用是碎的。

**规则**（每条都对应 PDF 里的一个具体现象）：

1. **空行分段**。calibre 用空行分隔段落，空行是真实的段落边界。
2. **行内换行接回**。同一段里的换行是排版产物，接回时补一个空格。
3. **行尾连字符不拆**。全书只有 96 处行尾连字符，逐条看过，全是
   ``hyper-`` / ``seven-year-olds`` / ``goal-directed`` 这种真连字符，
   所以**保留连字符、去掉空格**，而不是把词接成 ``hypertalker``。
4. **页眉页脚按频次剔除**。同一行在超过 1/4 的非空页上原样出现，
   且长度不超过 60 字符，才算页眉页脚 —— 两个条件缺一不可，
   否则会误删正文里反复出现的短句。
5. **原文与规范化文本分开存**。``content_original`` 只做段落还原，
   ``content_normalized`` 额外做空白折叠和 Unicode 标点归一，
   后者才是送去 embedding 和 FTS 的那份。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# 判定页眉页脚的门槛
HEADER_MIN_PAGES_RATIO = 0.25
HEADER_MAX_LEN = 60

# Unicode 标点 -> ASCII。FTS 用 'english' 配置，弯引号会把词切开。
_PUNCT_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...",
    " ": " ", "": "",
}


def _norm_punct(text: str) -> str:
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    return text


def find_repeated_lines(
    pages: list[str], min_ratio: float = HEADER_MIN_PAGES_RATIO, max_len: int = HEADER_MAX_LEN
) -> dict[str, int]:
    """找出疑似页眉页脚：短行 + 在很多页上原样重复。

    返回 {行文本: 出现次数}。空结果说明这本书没有页眉页脚（calibre 重排时
    通常已经丢掉了），不是没检查。
    """
    counter: Counter[str] = Counter()
    non_empty = 0
    for text in pages:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        non_empty += 1
        # 一页里同一行只算一次
        counter.update(set(lines))
    if not non_empty:
        return {}
    threshold = max(3, int(non_empty * min_ratio))
    return {
        line: count
        for line, count in counter.items()
        if count >= threshold and len(line) <= max_len
    }


def _join_block(lines: list[str]) -> str:
    """把一个段落块里的多行接回一行。"""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        if out.endswith("-"):
            # 真连字符：保留它，不补空格
            out += line
        else:
            out += " " + line
    return out


def unwrap(text: str, drop_lines: set[str] | None = None) -> str:
    """把硬换行接回段落。空行是段落边界。

    ``drop_lines`` 是要整行丢掉的集合（页眉页脚）。
    """
    drop_lines = drop_lines or set()
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        if line in drop_lines:
            continue
        current.append(line)
    if current:
        blocks.append(current)

    paragraphs = [_join_block(block) for block in blocks]
    return "\n\n".join(p for p in paragraphs if p)


def normalize(text: str) -> str:
    """规范化：折行折叠 + 标点归一 + 空白收敛。"""
    text = _norm_punct(text)
    text = unicodedata.normalize("NFKC", text)
    # 换行变空格，剩下的空白一律收成一个
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass(slots=True)
class CleanResult:
    original: str  # 段落还原后的原文
    normalized: str  # 送 embedding / FTS 的那份
    dropped_headers: list[str]


def clean_page(text: str, drop_lines: set[str] | None = None) -> CleanResult:
    """清洗一页。"""
    original = unwrap(text, drop_lines=drop_lines)
    normalized = normalize(original)
    return CleanResult(
        original=original,
        normalized=normalized,
        dropped_headers=sorted(drop_lines or set()),
    )


def clean_pages(
    pages: list[str], drop_lines: set[str] | None = None
) -> list[CleanResult]:
    return [clean_page(text, drop_lines=drop_lines) for text in pages]
