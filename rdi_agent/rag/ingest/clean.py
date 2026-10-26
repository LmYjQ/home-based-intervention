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

import jieba

# 连续 ASCII 词（英文缩写、数字）。分词前先保护起来。
_ASCII_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'\-_.]*|\d+(?:\.\d+)*")

# 分词时的占位符。用 \x00 包住下标，jieba 会把它当成一个整体。
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

# 一个汉字（以及扩展区的汉字）。
_CJK_CHAR = re.compile(r"[一-鿿]")

# 按"含汉字 / 不含汉字"把文本切成段。含汉字的段交给 jieba，其余原样保留。
_CJK_SPLIT = re.compile(r"([一-鿿]+)")

# 指南的核心术语 —— 必须作为**整词**进 jieba 词典。
#
# **为什么**：jieba 按统计切词，"挑战性行为"这种复合术语会被拆成
# "挑战" "性" "行为"，"前因干预"会被拆成 "前因" "干预"。
# 拆碎之后关键词路命中的是碎片 —— 问"挑战性行为"命中的是
# 一堆提到"行为"的块，精度直接垮掉。
#
# 这些词全部是指南正文里实际用到的（见 ``concepts.py`` 的术语表），
# 不是凭空造的。
GUIDELINE_TERMS = (
    # 行为类型
    "挑战性行为",
    "缺陷行为",
    "局限兴趣",
    "重复刻板行为",
    "适应性行为",
    "替代行为",
    "问题行为",
    "自我刺激行为",
    "自我管理行为",
    "正面行为",
    # 干预策略
    "正强化",
    "负强化",
    "消退",
    "负惩罚",
    "差别强化",
    "前因干预",
    "后果管理",
    "功能分析",
    "高级辅助",
    "低级辅助",
    "自然情境教学",
    "回合试验教学",
    "结构化教学",
    "回合训练",
    "共同注意",
    "泛化",
    "模仿",
    "强化",
    "惩罚",
    "示范",
    "塑造",
    "感觉统合",
    "无错误教学",
    "自然发展行为干预",
    "关键反应训练",
    "图片交换沟通系统",
    "同伴介导干预",
    "社交故事",
    "地板时光",
    # 原则与评估
    "个体化干预",
    "早期干预",
    "长程高强度",
    "综合干预",
    "科学循证",
    "家庭参与",
    "社会融合",
    "个体化教育干预计划",
    "季度目标",
    "里程碑式行为",
    "强化物",
    "消退策略",
    "回应扩散",
    "共同调控",
    "发育水平评估",
    "社会适应性行为",
    # 章节名
    "健康教育",
    "家长心理支持",
    "家庭干预",
    "心理治疗",
    "数智干预康复",
    "随访",
    "质量控制",
    "干预康复场地",
    "干预康复人员",
    "共存问题",
    "共患病",
    "干预康复目的",
    "干预康复基本原则",
    "干预康复内容",
    # 共存问题
    "睡眠问题",
    "饮食问题",
    "如厕问题",
    "性相关问题",
    "感觉过敏",
    "癫痫",
    "注意力缺陷多动障碍",
    "焦虑障碍",
    "强迫症",
)


def _register_terms() -> None:
    """把指南术语注册进 jieba 词典（幂等）。"""
    for term in GUIDELINE_TERMS:
        jieba.add_word(term, freq=10000)


_register_terms()

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
    """规范化：折行折叠 + 标点归一 + 空白收敛。

    **中文用 NFC 而不是 NFKC**。NFKC 是"兼容性分解"，会把全角标点折成
    半角：``（一）`` → ``(一)``、``，`` → ``,``、``！`` → ``!``。
    对英文书没影响（它的原文本来就是 ASCII），对中文却是破坏 ——
    全角标点是中文的**语义边界**：

    - ``。！？`` 是句末标点，切块器要靠它断句，折成 ``.!?`` 之后
      中文句子的边界判断就依赖后面的空格了；
    - ``（）`` 是条目编号的括号，折掉之后 ``（一）早期干预`` 变成
      ``(一)早期干预``，读起来像英文。

    NFC 只做"等价字符的统一"（比如全角字母 Ａ → A），不动标点宽度 ——
    这正是中文正文需要的。

    **英文书那边为什么用 NFKC**：那边原文是英文 + 弯引号，
    NFKC 顺带把弯引号折成直引号，恰好是 FTS 想要的。两边需求不同，
    所以用 ``normalize_zh`` 分开。
    """
    text = _norm_punct(text)
    text = unicodedata.normalize("NFKC", text)
    # 换行变空格，剩下的空白一律收成一个
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_zh(text: str) -> str:
    """中文规范化：NFC + 标点归一 + 空白收敛 + jieba 分词。

    **为什么要分词**：``content_tsv`` 是
    ``to_tsvector('simple', content_normalized)`` 生成的生成列。
    ``simple`` 配置按**非字母数字**切词，中文连写的话整段会变成
    **一个** lexeme —— 关键词路对中文等于没有。

    而 ``simple`` 认空格。所以这里用 jieba 把中文切成空格分隔的词，
    每个词就成了独立的 lexeme，``to_tsvector('simple', ...)`` 也就
    能按词命中了。

    **分词后原文去哪了**：``content_original`` 保留没分词的原文，
    分词只发生在 ``content_normalized`` 这份"送去检索"��副本里。

    **jieba 词典里补了指南术语**：``挑战性行为`` 默认会被切成
    "挑战/性/行为"，导致关键词路命中的是碎片。加进自定义词典后
    它是一个整词 —— 术语命中精度直接受影响。
    """
    text = _norm_punct(text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return segment_zh(text)


def segment_zh(text: str) -> str:
    """jieba 分词，输出空格分隔的词串。

    **英文和数字整体保留**：jieba 会把 ``ABA`` 切成 ``A`` ``B`` ``A``，
    对混合文本不合适。

    **做法**：把文本按"中文段 / 非中文段"切开，只对中文段分词，
    非中文段（英文词、数字、标点）原样保留。

    **为什么不用占位符**：早先试过用 ``\\x00N\\x00`` 把 ASCII 词替换成
    占位符再还原 —— 但 jieba 会把占位符本身拆开（``\\x00`` ``0`` ``\\x00``），
    还原就失配了。切成段来走没有这个问题。
    """
    if not text:
        return ""
    result: list[str] = []
    for part in _CJK_SPLIT.split(text):
        if not part:
            continue
        if _CJK_CHAR.search(part):
            result.extend(t for t in jieba.lcut(part, cut_all=False) if t.strip())
        else:
            result.append(part)
    return " ".join(result).strip()




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
