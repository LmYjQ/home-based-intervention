"""父子块切分。

**为什么分父子**：召回靠子块（短、精准、embeddings 有区分度），可读性靠父块
（完整语义、能当引用原文给家长看）。如果只切小块，命中之后拿到的是半句话；
如果只切大块，向量被平均掉，"哪一段讲的引导式参与"就分不出来了。

- 父块：约 2400 字符，承载完整语义
- 子块：约 500 字符，带约 150 字符重叠，``parent_chunk_id`` 指回父块

**切分单位是段落**，不是句子也不是字符。段落是作者自己划的语义边界，
在段落中间下刀会切出"前半句接后半句"这种读不通的块。

**重叠按字符截，不按整段搬**。这本书的 unit 最长 1100 字符（作者的长句
风格），早期实现"把末尾的 unit 整个搬过来"，实际重叠高达一整个段落：
实测 24% 的相邻子块对是"后者完整包含前者"，重复字符占子块总量 12%。
后果是向量召回里同一段占掉两个名次，白白挤掉别的段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rdi_agent.rag.ingest.clean import _CJK_CHAR, normalize, normalize_zh

# 父块目标长度（字符）
PARENT_TARGET_CHARS = 2400
PARENT_MAX_CHARS = 3600
# 子块目标长度与重叠
#
# 这本书的句子本身就长（实测一句 1000 字符），段落更是整页级别。
# 所以子块的实际粒度由"拆句后的单元"决定，目标值设成比单元小，
# 让一个单元自己成为一个子块：实测子块平均约 1000 字符（约 200 token），
# 对密集散文是合适的召回粒度。
CHILD_TARGET_CHARS = 500
CHILD_MAX_CHARS = 1100
CHILD_OVERLAP_CHARS = 150
# unit 的拆分上限要比块上限小一个重叠 —— 否则"一个 unit 装一个块"时，
# 块长 = unit + 重叠 会冲出 CHILD_MAX_CHARS（实测最大冲到 1250）。
CHILD_UNIT_MAX_CHARS = CHILD_MAX_CHARS - CHILD_OVERLAP_CHARS

# 段落分隔（clean.unwrap 用 \n\n 分段）
_PARAGRAPH_SEP = "\n\n"

# 句子边界：句号后跟空格、问号、叹号
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# 中文句末标点。中文**没有**"句号后跟空格"这个模式 ——
# 句号紧跟着下一个句子，所以英文那套 ``(?<=[.!?])\s+`` 对中文一句都切不开。
#
# 中文句末标点：。！？；以及右括号（条目编号结束）。
# 分号也算句末 —— 指南里"前因干预。通过…；替代行为训练。通过…"这种
# 分号分句很常见，不切的话超长段落只能按硬长度切，切点落在句中间。
_CJK_SENTENCE_END = re.compile(r"(?<=[。！？；])(?![”』」）)])")


@dataclass
class Chunk:
    """一个块。父块和子块用同一个结构，``is_parent`` 区分。"""

    content_original: str
    content_normalized: str
    pdf_page_start: int
    pdf_page_end: int
    chunk_index: int  # 全书连续编号
    parent_index: int  # 所属父块在本节内的序号
    is_parent: bool
    knowledge_type: str = "narrative"
    section_code: str = ""
    parent_chunk_index: int | None = None  # 子块 -> 父块
    concept_ids: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.content_normalized)


@dataclass
class _Item:
    """段落 + 它所在的 PDF 页。"""

    page: int
    text: str


def split_paragraphs(text: str) -> list[str]:
    """按空行切段落。"""
    parts = [p.strip() for p in text.split(_PARAGRAPH_SEP)]
    return [p for p in parts if p]


def _split_sentences(text: str) -> list[str]:
    """按句子边界切。

    **中英文分别处理**：英文靠"句号后跟空格"，中文靠句末标点。
    两套正则不能合并成一个 —— 中文句号后面**没有空格**，
    英文句号后面**有空格**，混在一起会切出一堆空串。

    句末标点本身留在句子的**末尾**（不切掉），所以拼回去的时候
    标点不会丢。
    """
    if not text:
        return []
    sentences = _SENTENCE_END.split(text)
    out: list[str] = []
    for part in sentences:
        if not part:
            continue
        out.extend(s for s in _CJK_SENTENCE_END.split(part) if s)
    return out


def _split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """超长段落按句子边界拆开。拆不动就按硬长度切。"""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    buffer = ""
    for sentence in _split_sentences(text):
        if not sentence:
            continue
        # 中文句子之间**不能补空格** —— 补了会在正文中插进无意义的空格，
        # 而且和分词后的内容对不上（分词结果里也没有这个空格）。
        joiner = "" if _is_cjk(sentence) else " "
        candidate = f"{buffer}{joiner}{sentence}" if buffer else sentence
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        while len(sentence) > max_chars:
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _is_cjk(text: str) -> bool:
    """文本里有没有汉字。用来决定句子之间要不要补空格。"""
    return bool(_CJK_CHAR.search(text))



def _group(items: list[_Item], target: int, max_chars: int) -> list[list[_Item]]:
    """把段落攒成组：到 target 就收口，超过 max_chars 强制断。"""
    groups: list[list[_Item]] = []
    current: list[_Item] = []
    size = 0
    for item in items:
        if len(item.text) > max_chars:
            # 先把当前组结掉
            if current:
                groups.append(current)
                current = []
                size = 0
            for piece in _split_long_paragraph(item.text, max_chars):
                groups.append([_Item(page=item.page, text=piece)])
            continue
        if size + len(item.text) > max_chars and current:
            groups.append(current)
            current = []
            size = 0
        current.append(item)
        size += len(item.text)
        if size >= target:
            groups.append(current)
            current = []
            size = 0
    if current:
        groups.append(current)
    return groups


def _explode(items: list[_Item], max_chars: int) -> list[_Item]:
    """把超长段落按句子拆成多个单元，短的保持原样。"""
    out: list[_Item] = []
    for item in items:
        if len(item.text) <= max_chars:
            out.append(item)
            continue
        for piece in _split_long_paragraph(item.text, max_chars):
            out.append(_Item(page=item.page, text=piece))
    return out


def _join(items: list[_Item]) -> tuple[str, int, int]:
    text = _PARAGRAPH_SEP.join(item.text for item in items)
    pages = [item.page for item in items]
    return text, min(pages), max(pages)


def _overlap_of(group: list[_Item], max_chars: int) -> list[_Item]:
    """取一组末尾的若干**字符**做重叠，给下一组当前置上下文。

    关键在这里：重叠按**字符**截，不按整段搬。

    早期实现是把末尾的 unit 整个搬过来。而这本书的 unit 最长 1100 字符
    （作者的长句风格），所以"重叠 150 字符"实际变成了"重叠一整个段落"：
    实测 24% 的相邻子块对是"后者完整包含前者"，重复字符占子块总量 12%。
    后果是向量召回里同一段占掉两个名次（p184 同时占了第 1 和第 4 名），
    白白把别的段挤出去。

    开头还会在句子边界处切开 —— 半句话开头既不好读，向量也偏。
    """
    if not group or max_chars <= 0:
        return []

    # 从后往前凑，凑够 max_chars 字符为止
    picked: list[_Item] = []
    total = 0
    for item in reversed(group):
        picked.insert(0, item)
        total += len(item.text)
        if total >= max_chars:
            break

    # 最早那个 item 多出来的部分砍掉（从中间砍会切出半句）
    excess = total - max_chars
    if excess > 0 and picked:
        head = picked[0]
        if len(head.text) <= excess:
            picked.pop(0)
        else:
            picked[0] = _Item(page=head.page, text=head.text[excess:])

    # 开头对齐到句子边界：找第一个句末标点，在它后面切
    if picked:
        head = picked[0]
        # 中英文的句末边界不一样，先试中文的（中文句末标点是零宽的，
        # ``finditer`` 匹配到的是标点**后面**的位置），再试英文的。
        match = next(_CJK_SENTENCE_END.finditer(head.text), None)
        if match is None:
            match = next(_SENTENCE_END.finditer(head.text), None)
        if match is not None and match.end() <= len(head.text) // 2:
            head = _Item(page=head.page, text=head.text[match.end():])
            picked[0] = head

    return [item for item in picked if item.text.strip()]


def _child_groups(units: list[_Item]) -> list[tuple[list[_Item], list[_Item]]]:
    """把 unit 攒成子块组，并给每组配好前置重叠。

    返回 ``[(前置重叠, 本组内容), ...]``，第一组没有前置重叠。

    分组同时受两个约束：
    - 攒到 ``CHILD_TARGET_CHARS`` 就收口（保证块不会太小）
    - 再加一个 unit 会超过预算就提前收口（保证块不会太大）

    预算里已经扣掉了前置重叠的长度，所以**最终块长**不超过
    ``CHILD_MAX_CHARS``，而不是"组长 + 重叠"超出去。
    """
    raw: list[list[_Item]] = []
    current: list[_Item] = []
    size = 0
    # 本组内容要加上前置重叠才构成最终块，所以预算先扣掉重叠长度。
    # 第一组没有前置重叠，预算就是满的。
    budget = CHILD_MAX_CHARS
    for unit in units:
        if current and size + len(unit.text) > budget:
            raw.append(current)
            current = []
            size = 0
            # 下一组的前置重叠由刚收口的这一组决定
            budget = CHILD_MAX_CHARS - _overlap_len(
                _overlap_of(raw[-1], CHILD_OVERLAP_CHARS)
            )
        current.append(unit)
        size += len(unit.text)
        if size >= CHILD_TARGET_CHARS:
            raw.append(current)
            current = []
            size = 0
            budget = CHILD_MAX_CHARS - _overlap_len(
                _overlap_of(raw[-1], CHILD_OVERLAP_CHARS)
            )
    if current:
        raw.append(current)

    # 重新按"重叠来自上一组"配对
    result: list[tuple[list[_Item], list[_Item]]] = []
    overlap: list[_Item] = []
    for group in raw:
        result.append((overlap, group))
        overlap = _overlap_of(group, CHILD_OVERLAP_CHARS)
    return result


def _overlap_len(items: list[_Item]) -> int:
    return sum(len(item.text) for item in items)


def classify(
    text: str, section_title: str, in_appendix: bool = False
) -> str:
    """给块标一个知识类型。检索侧可以据此调整权重。"""
    lowered = text[:400].lower()
    title = section_title.lower()
    if "references by" in title or title.startswith("references"):
        return "reference"
    # 中文指南没有参考文献章节，但"名词解释"这一节是定义性的 ——
    # 标成 definition，检索时家长问"什么是挑战性行为"能优先命中。
    if "名词解释" in section_title:
        return "definition"
    if in_appendix:
        return "appendix"
    if "reflection question" in lowered:
        return "reflection"
    return "narrative"


def _chunk_atomic(
    items: list[_Item],
    *,
    section_code: str,
    section_title: str,
    in_appendix: bool,
    start_index: int,
    normalizer,
) -> list[Chunk]:
    """每个 item 一个块，不合并。

    **父块怎么办**：这里**不生成父块** —— 表格行本身就是完整语义，
    父块只是"把这些行拼起来"，对召回没有增量价值，还会占掉候选位。

    但父块是**引用给家长看**的载体（父块扩展会拿它当原文），
    所以保留一个父块，把所有行拼起来，子块都指回它。

    **重叠怎么办**：不重叠。表格行是并列关系（不同的策略），
    不是承接关系 —— 前一行不是后一行的上下文。重叠在这里只有坏处：
    每一行都会带上上一行的内容，向量被污染，关键词路命中也会串。

    **超长的行怎么办**：太长的行（比如附件 3 里那段几百字的
    "获得具体实物或机会的问题行为"）按句子拆开，拆出来的每一段
    仍然是一个独立块 —— 但它们共享同一个父块。
    """
    chunks: list[Chunk] = []
    index = start_index

    # ---- 父块：整个章节的行拼起来 ----
    parent_text, p_start, p_end = _join(items)
    parent_chunk = Chunk(
        content_original=parent_text,
        content_normalized=normalizer(parent_text),
        pdf_page_start=p_start,
        pdf_page_end=p_end,
        chunk_index=index,
        parent_index=0,
        is_parent=True,
        knowledge_type=classify(parent_text, section_title, in_appendix),
        section_code=section_code,
    )
    chunks.append(parent_chunk)
    parent_index_global = index
    index += 1

    # ---- 子块：每行一个 ----
    for item in items:
        pieces = (
            [item.text]
            if len(item.text) <= CHILD_MAX_CHARS
            else _split_long_paragraph(item.text, CHILD_MAX_CHARS)
        )
        for piece in pieces:
            chunks.append(
                Chunk(
                    content_original=piece,
                    content_normalized=normalizer(piece),
                    pdf_page_start=item.page,
                    pdf_page_end=item.page,
                    chunk_index=index,
                    parent_index=0,
                    is_parent=False,
                    knowledge_type=classify(piece, section_title, in_appendix),
                    section_code=section_code,
                    parent_chunk_index=parent_index_global,
                )
            )
            index += 1

    return chunks


def chunk_section(
    *,
    section_code: str,
    section_title: str,
    pages: list[tuple[int, str]],
    in_appendix: bool = False,
    start_index: int = 0,
    language: str = "en",
    atomic_items: bool = False,
) -> list[Chunk]:
    """把一个章节切成父块和子块。

    ``pages`` 是 [(PDF 页码, 清洗后的正文)]，按页序。
    返回的块里父块在前、子块在后，``chunk_index`` 全书连续。

    ``language`` 决定两件事：句子怎么切、规范化怎么做。

    - ``en``（默认）：英文书的路径，NFKC 规范化 + 英文句末边界。
    - ``zh``：中文指南的路径，NFC 规范化 + **jieba 分词**。

    中文路径必须分词，否则 ``content_normalized`` 是一整段连写的中文，
    ``to_tsvector('simple', ...)`` 只能切出一个词 —— 关键词路等于没有。

    ``atomic_items``：**每个输入项单独成一个块，不合并**。

    **什么时候要开**：指南附件 1–4 全是宽表格，一行就是一条完整的
    干预要点（"正强化：xxx" / "0~18 月龄：xxx"）。子块如果按
    ``CHILD_TARGET_CHARS`` 攒，会把 7 条策略挤进一个 543 字的块 ——
    问"孩子做对了要怎么奖励"（正强化）时，这个大块和讲挑战性行为的
    块竞争，正确答案挤不进前 5 名。

    开了之后每行一个块，召回粒度直接对上"针对某一条策略的干预"。
    """
    items: list[_Item] = []
    for page, text in pages:
        for paragraph in split_paragraphs(text):
            items.append(_Item(page=page, text=paragraph))

    if not items:
        return []

    normalizer = normalize_zh if language == "zh" else normalize

    if atomic_items:
        return _chunk_atomic(
            items,
            section_code=section_code,
            section_title=section_title,
            in_appendix=in_appendix,
            start_index=start_index,
            normalizer=normalizer,
        )

    parents = _group(items, PARENT_TARGET_CHARS, PARENT_MAX_CHARS)
    chunks: list[Chunk] = []
    index = start_index

    for parent_index, parent_items in enumerate(parents):
        parent_text, p_start, p_end = _join(parent_items)
        parent_chunk = Chunk(
            content_original=parent_text,
            content_normalized=normalizer(parent_text),
            pdf_page_start=p_start,
            pdf_page_end=p_end,
            chunk_index=index,
            parent_index=parent_index,
            is_parent=True,
            knowledge_type=classify(parent_text, section_title, in_appendix),
            section_code=section_code,
        )
        chunks.append(parent_chunk)
        parent_index_global = index
        index += 1

        # 子块：段落太长的先按句子拆成"单元"，再按目标长度攒组。
        # 这一步不能省 —— 这本书的段落动辄一两千字符（作者的长句风格），
        # 不拆的话子块平均 1800 字符，召回粒度太粗，命中之后给家长的
        # 引用也是一大段。
        units = _explode(parent_items, CHILD_UNIT_MAX_CHARS)
        for overlap, group in _child_groups(units):
            # 前置重叠 + 本组内容 = 最终块
            text, c_start, c_end = _join(overlap + group)
            chunks.append(
                Chunk(
                    content_original=text,
                    content_normalized=normalizer(text),
                    pdf_page_start=c_start,
                    pdf_page_end=c_end,
                    chunk_index=index,
                    parent_index=parent_index,
                    is_parent=False,
                    knowledge_type=classify(text, section_title, in_appendix),
                    section_code=section_code,
                    parent_chunk_index=parent_index_global,
                )
            )
            index += 1

    return chunks
