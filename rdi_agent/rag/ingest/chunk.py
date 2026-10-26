"""父子块切分（中文指南，简单规则版）。

**规则就四条**（人工定的，不再纠结"攒多少字""重叠多少字"）：

1. **父块 = 一个 Markdown 标题到下一个标题之间的全部内容**
2. **子块 = 父块内按换行切，一行 = 一个子块**
3. **遇到 ``<br>`` 也切成两段**（**表格行例外** —— 格子里的内容
   不拆，一行表格还是一块）
4. **跨页合并**：每页最后一段如果最后一个字符不是句末标点，
   就和下一页的第一段合并成一个子块

**为什么不用"按字数攒块"**：那套是从英文书搬过来的（父块 2400 字、
子块 500 字、重叠 150 字）。指南的段落本来就是作者划好的语义边界，
一行一条（"（二）个体化干预。孤独症儿童发育水平各不相同…"），
按行切出来的粒度正好对上"针对某一条策略的干预"这种问题。
按字数攒反而会把 7 条策略挤进同一个块，正确答案挤不进前 5 名。

**父块正文 = 标题 + 各子块正文**。父块带上标题，
一是引用给家长看时有上下文，二是短章节的父块不会和它唯一的子块一字不差
（检索只回子块，父块白占一个向量、还和子块抢召回位）。

**父块没有正文就不产块**（``## 三、干预康复内容`` 后面直接是 ``###``），
但章节本身保留 —— 章节树不断，库里不出现空块。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rdi_agent.rag.ingest.clean import normalize, normalize_zh
from rdi_agent.rag.ingest.parse_md import Block
from rdi_agent.rag.ingest.sections import Section, SectionMap
from rdi_agent.rag.ingest.sections_zh import clean_title, slug_zh

__all__ = [
    "Chunk",
    "chunk_document",
    "classify",
]


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


def classify(text: str, section_title: str, in_appendix: bool = False) -> str:
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


def _in_appendix(title: str) -> bool:
    """附件 1-4 是宽表格，家长最用得上的实操内容。"""
    return title.startswith("附件")


def chunk_document(
    blocks: list[Block],
    *,
    source_id: str = "",
    pdf: str = "",
    total_pages: int = 0,
    language: str = "zh",
) -> tuple[SectionMap, list[Chunk]]:
    """整篇文档 -> (章节树, 父块 + 子块)。

    ``blocks`` 是**整本文档按顺序**的块（``parse_md.parse_blocks`` 产出，
    再过一遍 ``parse_md.merge_page_breaks``）。

    返回的块里父块在前、子块在后，``chunk_index`` 全书连续。

    ``language`` 决定规范化怎么做：``zh`` 走 NFC + jieba 分词
    （``content_tsv`` 是 ``to_tsvector('simple', content_normalized)``
    的生成列，不分词的话整段中文是一个 lexeme，关键词路等于没有），
    其它走 NFKC。
    """
    normalizer = normalize_zh if language == "zh" else normalize

    sections: list[Section] = []
    chunks: list[Chunk] = []
    stack: list[Section] = []  # 标题栈，用来定章节的父子关系
    current: Section | None = None  # 当前父块对应的章节
    pending: list[Chunk] = []  # 当前父块的子块
    index = 0

    def flush() -> None:
        """收口当前父块：产父块 + 挂上它的子块。"""
        nonlocal current, pending, index
        if current is None:
            return
        if not pending:
            # 没有正文的标题（"## 三、干预康复内容" 后面直接是 "###"）：
            # 只留章节，不产父块 —— 库里不出现空块。
            current = None
            pending = []
            return

        current.pdf_page_start = min(
            current.pdf_page_start, min(c.pdf_page_start for c in pending)
        )
        current.pdf_page_end = max(
            current.pdf_page_end, max(c.pdf_page_end for c in pending)
        )

        text = current.title + "\n" + "\n".join(c.content_original for c in pending)
        parent_index = index
        chunks.append(
            Chunk(
                content_original=text,
                content_normalized=normalizer(text),
                pdf_page_start=current.pdf_page_start,
                pdf_page_end=current.pdf_page_end,
                chunk_index=index,
                parent_index=0,
                is_parent=True,
                knowledge_type=classify(text, current.title, _in_appendix(current.title)),
                section_code=current.section_code,
            )
        )
        index += 1

        for child in pending:
            child.chunk_index = index
            child.parent_chunk_index = parent_index
            chunks.append(child)
            index += 1

        current = None
        pending = []

    for block in blocks:
        if block.kind == "heading":
            flush()
            level = max(1, block.level)
            # 弹掉同级和更深的标题，栈顶剩下的就是这一节的父章节
            while stack and stack[-1].toc_level >= level:
                stack.pop()
            section = Section(
                section_code=f"{len(sections):03d}_{slug_zh(block.text)}",
                title=block.text,
                toc_level=level,
                pdf_page_start=block.page,
                pdf_page_end=block.last_page,
                order=len(sections),
                parent_code=stack[-1].section_code if stack else None,
                title_zh=clean_title(block.text),
            )
            sections.append(section)
            stack.append(section)
            current = section
            pending = []
            continue

        if current is None:
            # 正文出现在任何标题之前 —— 没有父块可挂，丢掉。
            continue
        pending.append(
            Chunk(
                content_original=block.text,
                content_normalized=normalizer(block.text),
                pdf_page_start=block.page,
                pdf_page_end=block.last_page,
                chunk_index=0,  # flush 时统一分配
                parent_index=0,
                is_parent=False,
                knowledge_type=classify(
                    block.text, current.title, _in_appendix(current.title)
                ),
                section_code=current.section_code,
            )
        )

    flush()

    section_map = SectionMap(
        source_id=source_id,
        pdf=pdf,
        total_pages=total_pages,
        sections=sections,
    )
    return section_map, chunks
