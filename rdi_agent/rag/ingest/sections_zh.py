"""中文指南的章节树：从 Markdown 标题推导。

**为什么单独一个模块**：``sections.build_sections`` 依赖 PDF 的目录
（``doc.get_toc()``）—— 那是英文书特有的结构。这份指南是扫描件，
没有 PDF 目录可读，章节只能从 **Markdown 标题**推导。

**和英文书那套的关键差别**：

- 英文书：目录 -> 章节 -> **整页** 归属（一个章节占连续的页）。
- 中文指南：一页里可能有**多个标题**（第 3 页同时有"三、干预康复内容"
  和"（一）健康教育"和"（二）针对缺陷行为的干预康复"）。
  所以**不能按页归章节**，必须按**块**归。

这一点决定了整个切块管线：块的粒度从"页"降到"块"，
每个块知道自己属于哪个标题。

**section_code 用中文标题**：``003_针对挑战性行为的干预``。
原来英文书的 slug 是 ASCII（``013_readiness``），中文标题过不了
``[^0-9a-zA-Z]`` 那套正则 —— 而且**用中文标题当 code 反而更好**：
评测集里写 ``expected_sections`` 时能直接看懂，不用去查一张对照表。

code 的组成是 ``序号_标题``，序号保证稳定排序，标题保证可读。
"""

from __future__ import annotations

import re

from rdi_agent.rag.ingest.parse_md import Block
from rdi_agent.rag.ingest.sections import (
    Section,
    SectionMap,
    load_section_map,
    write_section_map,
)

__all__ = [
    "Section",
    "SectionMap",
    "build_sections_zh",
    "assign_blocks",
    "slug_zh",
    "load_section_map",
    "write_section_map",
]

# 标题里要剥掉的前缀："一、" "（一）" "附件 1" 这些
_ENUM_PREFIX = re.compile(r"^([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\(\s*[一二三四五六七八九十]+\s*\))\s*")
_APPENDIX_PREFIX = re.compile(r"^附件\s*\d+\s*")

# 标题里不适合做 code 的字符（code 会进 URL 和外键）
_CODE_UNSAFE = re.compile(r"[^\w一-鿿]+")

# 章节 code 的最大长度。Postgres 里是 VARCHAR(64)，
# 中文一个字算一个字符 —— 留点余量，别顶到 64。
MAX_CODE_LEN = 60


def slug_zh(title: str) -> str:
    """把中文标题变成可用的 code 片段。

    保留汉字和字母数字，其余换成下划线。空标题返回 ``section``。
    """
    text = title.strip()
    text = _ENUM_PREFIX.sub("", text)
    text = _APPENDIX_PREFIX.sub("", text)
    text = _CODE_UNSAFE.sub("_", text).strip("_").lower()
    return text[:MAX_CODE_LEN] or "section"


def _clean_title(title: str) -> str:
    """去掉标题里的编号前缀，但保留原文的完整标题在 ``title`` 里。

    ``title_original`` 保留原样（"（四）针对挑战性行为的干预"），
    ``title_zh`` 存去掉编号的版本 —— 前端和提示词里显示后者更干净。
    """
    cleaned = _ENUM_PREFIX.sub("", title.strip())
    cleaned = _APPENDIX_PREFIX.sub("", cleaned)
    return cleaned or title.strip()


def build_sections_zh(
    blocks: list[Block],
    *,
    source_id: str,
    total_pages: int,
    pdf: str = "",
) -> SectionMap:
    """从 Markdown 块推导章节树。

    **层级规则**：``#`` 是文档标题（不进章节树），``##`` 是一级章节，
    ``###`` 是二级章节。二级章节挂在它前面那个一级章节下面。

    **页区间**：章节的起止页 = 它自己第一个块所在页 → 下一个**同级或更高级**
    章节的第一个块所在页 - 1。
    """
    notes: list[str] = []
    sections: list[Section] = []
    stack: list[Section] = []

    for block in blocks:
        if block.kind != "heading" or block.level <= 1:
            # 一级标题（文档名）不进章节树 —— 它是书名，不是章节
            continue
        level = block.level - 1  # ## -> 1，### -> 2
        section = Section(
            section_code="",  # 下面统一编号
            title=block.text,
            toc_level=level,
            pdf_page_start=block.page,
            pdf_page_end=block.page,
            order=len(sections),
            title_zh=_clean_title(block.text),
        )
        sections.append(section)
        stack.append(section)

    # 编号 + 父子关系 + 页区间
    for order, section in enumerate(sections):
        section.order = order
        section.section_code = f"{order:03d}_{slug_zh(section.title)}"

    for index, section in enumerate(sections):
        # 父级：往前找最近一个层级更浅的
        parent: Section | None = None
        for previous in reversed(sections[:index]):
            if previous.toc_level < section.toc_level:
                parent = previous
                break
        section.parent_code = parent.section_code if parent else None

        # 结束页：下一个同级或更高级章节的起始页 - 1
        end = total_pages
        for following in sections[index + 1 :]:
            if following.toc_level <= section.toc_level:
                end = following.pdf_page_start - 1
                break
        if end < section.pdf_page_start:
            end = section.pdf_page_start
            notes.append(
                f"{section.title}(p{section.pdf_page_start}) 与下一条同页，区间按 1 页计"
            )
        section.pdf_page_end = end

    return SectionMap(
        source_id=source_id,
        pdf=pdf,
        total_pages=total_pages,
        sections=sections,
        notes=notes,
    )


def assign_blocks(
    blocks: list[Block], section_map: SectionMap
) -> list[tuple[Section, list[tuple[int, str]]]]:
    """把块按章节归位。

    返回 ``[(章节, [(页码, 文本), ...]), ...]``。

    **规则**：一个块属于**它前面最近的那个标题**。标题本身的文字
    不进正文（它是章节名，单独存在 ``book_sections.title_original`` 里），
    但表格和段落进。

    **为什么要"标题本身不进正文"**：章节名"针对挑战性行为的干预"会出现在
    每一条该章节的块里 —— 召回时这些块看起来一模一样，向量区分度下降。
    而且引用时"章节 + 页码"已经能定位，重复一遍标题是冗余。

    **一个块跨页怎么办**：块只属于它自己所在的那一页（指南的表格
    跨页时被拆成"（饮食问题续）"这样的续行 —— 那是 OCR 的产物，
    入库时按原样保留，但页码指向它实际所在的那页）。
    """
    # 每个标题位置 -> 章节
    title_to_section: dict[tuple[int, str], Section] = {}
    for section in section_map.sections:
        title_to_section[(section.pdf_page_start, section.title)] = section

    result: dict[str, tuple[Section, list[tuple[int, str]]]] = {}
    current: Section | None = None
    for block in blocks:
        if block.kind == "heading":
            if block.level <= 1:
                current = None
                continue
            current = title_to_section.get((block.page, block.text))
            if current is not None:
                result.setdefault(current.section_code, (current, []))
            continue
        if current is None:
            continue
        entry = result.setdefault(current.section_code, (current, []))
        entry[1].append((block.page, block.text))

    # 按章节顺序返回
    ordered: list[tuple[Section, list[tuple[int, str]]]] = []
    for section in section_map.sections:
        entry = result.get(section.section_code)
        if entry and entry[1]:
            ordered.append((section, entry[1]))
    return ordered
