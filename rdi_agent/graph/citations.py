"""引用校验器（计划 §9.3「引用校验（必须实现）」）。

**要防的是"编造引用"**：模型写了一句"书里说……"，然后在 ``source_citations``
里填了一个看起来像模像样的 id。家长点进去发现书里根本没这段话 ——
这个系统就废了。

计划 §4.3 的硬规则原文：

> ``RecommendationCard`` 的每一条 ``source_citations`` 必须能在当前
> ``EvidenceBundle`` 中找到对应 ``evidence_id``；找不到就判定为编造引用，
> 整张卡重试或降级为 ``limited_answer``。

**字段语义的坑**（交接说明 §2.3）：``SourceCitation.source_id`` 里放的是
**``evidence_id``（chunk 的 UUID）**，不是 ``knowledge_sources.source_id``
（``the_rdi_book_2009``）。本模块按这个约定校验。

校验四条：

1. ``source_id`` 必须在当前证据包的 ``evidence_id`` 集合里
2. 页码 / 章节必须与对应的 ``EvidenceItem`` 一致
3. ``claim_supported`` 必须能被该片段支持
4. ``application_label == "book_summary"`` 时至少有一条引用

第 3 条怎么自动判：证据的 ``summary`` 和 ``source_excerpt`` 现在是
**中文原文**（知识库换成了中文指南），而 ``claim_supported`` 也是中文 ——
两边能对上，所以可以做词级核对了。

**判据是"专有词"**：模型如果写了"指南提到消退"，那 "消退" 必须真的
出现在那条证据的摘要或原文里，否则就是编的。

**为什么不是全字重合度**：主张是模型的**转述**，用词和原文不一样是正常的
（"建议尽早干预" vs 原文"实施干预越早越好"）。要求逐字重合会把
正确的引用也判成编造。所以只查**足够具体的词** —— 术语（"消退"
"前因干预"）和英文词。这些词不会凭空出现。

**纯泛泛的中文主张不做校验**：查不了就不假装能查。

---

**片段级引用**（``explanation_parts``）：

卡级引用只能回答"这张卡依据了哪几条"，回答不了"**这句话**依据的是哪一条"。
所以说明被拆成片段，每段带自己的 ``evidence_ids``。这一层校验：

1. 片段引用的 ``evidence_id`` 必须在本次证据包里
2. 片段里的**指南术语**必须出现在它引用的那些证据里
3. 标成 ``source_summary`` 的卡，带术语的片段必须挂证据

**给模型看的编号是 E1/E2**，不是 UUID —— UUID 有 36 个字符，
模型抄错一位就变成"编造引用"，整张卡被丢。短编号由代码映射回真实 id。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rdi_agent.rag.ingest.clean import GUIDELINE_TERMS
from rdi_agent.schemas.evidence import EvidenceBundle, EvidenceItem
from rdi_agent.schemas.recommendation import RecommendationCard, SourceCitation

# 大段原文的判定长度（连续字符数）。
QUOTE_MIN_CHARS = 120

# 英文停用词：这些词在主张里出现不代表引用了什么具体内容。
_EN_STOPWORDS = {
    "about", "after", "again", "also", "always", "because", "been", "before",
    "being", "between", "both", "child", "children", "could", "does", "doing",
    "down", "even", "every", "from", "have", "having", "here", "into", "just",
    "like", "make", "makes", "made", "many", "more", "most", "much", "must",
    "need", "needs", "only", "other", "others", "over", "parent", "parents",
    "same", "should", "some", "such", "than", "that", "their", "them", "then",
    "there", "these", "they", "thing", "things", "this", "those", "through",
    "under", "very", "want", "well", "were", "what", "when", "where", "which",
    "while", "will", "with", "would", "your", "you",
}

# 英文词（含缩写和带连字符的词）。
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")

# 中文停用词 —— 主张里出现这些不代表引用了什么具体内容。
_ZH_STOPWORDS = {
    "的", "了", "和", "与", "或", "在", "是", "有", "对", "把", "被", "让",
    "给", "从", "到", "为", "以", "就", "都", "也", "还", "并", "而", "但",
    "孩子", "儿童", "家长", "可以", "应该", "需要", "进行", "通过", "以及",
    "这个", "那个", "我们", "他们", "如果", "因为", "所以", "这样", "什么",
    "怎么", "时候", "之后", "之前", "目前", "现在", "可以", "建议",
}

# 指南术语表 —— 长的优先匹配，避免"行为"抢走"挑战性行为"的匹配机会。
_ZH_TERMS_SORTED = tuple(sorted(GUIDELINE_TERMS, key=len, reverse=True))


@dataclass
class CitationCheck:
    """一条引用的校验结果。"""

    citation: SourceCitation
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _english_terms(text: str) -> set[str]:
    """取出文本里的英文实词（去停用词、小写）。"""
    return {
        w.lower()
        for w in _EN_WORD.findall(text or "")
        if w.lower() not in _EN_STOPWORDS
    }


def _evidence_text(item: EvidenceItem) -> str:
    """证据的可核对文本：摘要 + 原文片段。"""
    parts = [item.summary or "", item.source_excerpt or ""]
    return " ".join(parts).lower()


def _chinese_terms(text: str) -> set[str]:
    """取出文本里的中文专有词（指南术语）。

    **只认术语表里的词**：随便一个词都查的话，"建议""观察"这种词
    必然在原文里出现，查了等于没查；而不该出现的词（模型编的）
    反而可能因为字面巧合而通过。

    术语表是**指南自己的词** —— 主张里出现"消退""前因干预"
    "挑战性行为"，那就必须在证据里能找到，否则就是编的。

    **长的优先**：先按长术语匹配，命中的区域不再被短术语切开
    （"挑战性行为"不该同时被拆出"行为"）。
    """
    found: set[str] = set()
    remaining = text
    for term in _ZH_TERMS_SORTED:
        if term in remaining:
            found.add(term)
            remaining = remaining.replace(term, "　" * len(term))
    return found


def _check_claim_supported(citation: SourceCitation, item: EvidenceItem) -> list[str]:
    """主张里的专有词必须出现在这条证据里。

    中文和英文都查 —— 知识库现在是中文指南，两边都是中文。

    **查不到的就不查**：主张里既没有指南术语也没有英文专有词，
    说明它是个泛泛的转述，没有可核对的锚点 —— 这种情况返回空列表
    （不报错），因为我们没有依据说它编了。
    """
    claim = (citation.claim_supported or "").strip()
    if not claim:
        return ["claim_supported 是空的，说清楚这条引用支撑了哪句话"]
    haystack = _evidence_text(item)

    terms = _english_terms(claim) | _chinese_terms(claim)
    if not terms:
        return []
    missing = sorted(t for t in terms if t not in haystack)
    if missing:
        return [
            f"主张里的术语 {'、'.join(missing)} 不在证据 {item.evidence_id} 的内容里，"
            "这条引用可能编造了指南里没有的说法"
        ]
    return []


def _check_pages(citation: SourceCitation, item: EvidenceItem) -> list[str]:
    """页码必须和证据一致。"""
    errors: list[str] = []
    if citation.pdf_page_start is not None and item.pdf_page_start is not None:
        if citation.pdf_page_start != item.pdf_page_start:
            errors.append(
                f"pdf_page_start 写的是 {citation.pdf_page_start}，"
                f"证据 {item.evidence_id} 实际是 {item.pdf_page_start}"
            )
    if citation.pdf_page_end is not None and item.pdf_page_end is not None:
        if citation.pdf_page_end != item.pdf_page_end:
            errors.append(
                f"pdf_page_end 写的是 {citation.pdf_page_end}，"
                f"证据 {item.evidence_id} 实际是 {item.pdf_page_end}"
            )
    return errors


def _check_chapter(citation: SourceCitation, item: EvidenceItem) -> list[str]:
    """章节名要一致。允许模型写章节标题的一部分，所以只查包含关系。"""
    chapter = (citation.chapter or "").strip()
    if not chapter:
        return []
    title = (item.section_title or item.title or "").strip()
    if not title:
        return []
    if chapter not in title and title not in chapter:
        return [
            f"章节写的是「{chapter}」，证据 {item.evidence_id} 实际是「{title}」"
        ]
    return []


def check_citation(
    citation: SourceCitation,
    bundle: EvidenceBundle,
) -> CitationCheck:
    """校验单条引用。"""
    index = {e.evidence_id: e for e in bundle.evidence}
    item = index.get(citation.source_id)
    if item is None:
        return CitationCheck(
            citation=citation,
            errors=[
                f"source_id「{citation.source_id}」不在本次证据包里，"
                "这是编造的引用（只能填下面 evidence 清单里的 id）"
            ],
        )
    errors: list[str] = []
    errors += _check_pages(citation, item)
    errors += _check_chapter(citation, item)
    errors += _check_claim_supported(citation, item)
    if item.source_type == "guideline" and citation.source_type != "guideline":
        errors.append(
            f"证据是指南里的内容，source_type 应该是 guideline，"
            f"实际写了 {citation.source_type}"
        )
    return CitationCheck(citation=citation, errors=errors)


def validate_card_citations(
    card: RecommendationCard,
    bundle: EvidenceBundle,
) -> list[str]:
    """校验一张建议卡上的所有引用。返回错误列表，空列表 = 通过。

    **这条是 2C 的核心验收点**：编造的引用必须在这里被拦下来。
    """
    errors: list[str] = []

    if card.application_label == "source_summary" and not card.source_citations:
        errors.append(
            "application_label 是 source_summary（表示有知识库原文依据），"
            "但一条引用都没有"
        )

    if not bundle.evidence and card.source_citations:
        errors.append("本次没有检索到任何证据，不能引用指南里的内容")

    for citation in card.source_citations:
        result = check_citation(citation, bundle)
        for err in result.errors:
            errors.append(f"[{citation.source_id[:8]}] {err}")

    return errors


def validate_set_citations(
    cards: list[RecommendationCard],
    bundle: EvidenceBundle,
) -> dict[str, list[str]]:
    """校验一组建议卡。返回 {plan_id: 错误列表}。

    只返回**有错**的卡 —— 调用方据此决定丢哪张。
    """
    problems: dict[str, list[str]] = {}
    for card in cards:
        errors = validate_card_citations(card, bundle)
        if errors:
            problems[card.plan_id] = errors
    return problems


# --------------------------------------------------------------- 片段级
def validate_card_fragments(
    card: RecommendationCard,
    bundle: EvidenceBundle,
) -> list[str]:
    """片段级引用校验。返回错误列表，空列表 = 通过。

    校验三件事：

    1. 片段引用的 ``evidence_id`` 必须在本次证据包里
    2. 片段里的**指南术语**必须出现在它引用的那些证据里
       （术语在别的证据里有不算 —— 挂错证据和编造一样是错的）
    3. 标成 ``source_summary`` 的卡，带术语的片段必须挂证据
       （"这段是指南里的"却没指出是哪一段 —— 家长核不了）
    """
    errors: list[str] = []
    index = {e.evidence_id: e for e in bundle.evidence}
    # source_summary 的卡才要求"带术语就得挂证据"：
    # ai_adaptation 的卡本来就是在讲"我们怎么用"，术语出现是正常的。
    require_binding = card.application_label == "source_summary"

    for i, part in enumerate(card.explanation_parts, start=1):
        errors += _check_fragment(
            part.text,
            part.evidence_ids,
            index,
            where=f"说明第 {i} 段",
            require_binding=require_binding,
        )

    for i, action in enumerate(card.parent_actions, start=1):
        text = " ".join(
            [
                action.action,
                action.context,
                *action.steps,
                *action.observe,
                *action.reduce_or_stop_if,
            ]
        )
        errors += _check_fragment(
            text,
            action.evidence_ids,
            index,
            where=f"动作 {i}（{action.action}）",
            require_binding=require_binding,
        )
    return errors


def _check_fragment(
    text: str,
    evidence_ids: list[str],
    index: dict[str, EvidenceItem],
    *,
    where: str,
    require_binding: bool,
) -> list[str]:
    """一个片段的校验。"""
    errors: list[str] = []
    cited: list[EvidenceItem] = []
    for eid in evidence_ids:
        item = index.get(eid)
        if item is None:
            errors.append(
                f"{where}：引用的证据 {eid} 不在本次证据包里"
                "（只能填证据清单里的编号）"
            )
        else:
            cited.append(item)

    terms = _chinese_terms(text) | _english_terms(text)
    if not terms:
        # 这段话里没有可核对的专有词 —— 查不了就不假装能查
        return errors

    if not cited:
        if require_binding:
            errors.append(
                f"{where}：提到了指南术语 {'、'.join(sorted(terms))}，"
                "却没有挂任何证据 —— 这张卡标的是 source_summary，"
                "每一句专业主张都要指出依据"
            )
        return errors

    haystack = " ".join(_evidence_text(item) for item in cited).lower()
    missing = sorted(t for t in terms if t not in haystack)
    if missing:
        errors.append(
            f"{where}：术语 {'、'.join(missing)} 不在它引用的证据里，"
            "要么挂错了证据，要么这段话是编的"
        )
    return errors


def validate_cards(
    cards: list[RecommendationCard],
    bundle: EvidenceBundle,
) -> dict[str, list[str]]:
    """卡级 + 片段级一起校验。返回 {plan_id: 错误列表}。"""
    problems: dict[str, list[str]] = {}
    for card in cards:
        errors = validate_card_citations(card, bundle)
        errors += validate_card_fragments(card, bundle)
        if errors:
            problems[card.plan_id] = errors
    return problems


# --------------------------------------------------------------- 短编号
# 模型看到的编号：E1、E2……
#
# **为什么不用 UUID**：evidence_id 是 chunk 的 UUID，36 个字符，
# 模型抄错一位在下游看起来就是"引用了一段不存在的内容" —— 整张卡被丢。
# 短编号短得多，而且映射由代码维护：编号写错最多是"引用不到"，
# 不会变成伪造的出处。
_SHORT_ID = re.compile(r"^E\d+$", re.IGNORECASE)


def build_evidence_catalogue(
    evidence: list[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    """把证据包渲染成"可引用的证据"清单 + 短编号映射。

    返回 ``(清单文本, {"E1": evidence_id, ...})``。

    清单和映射必须**一次生成**：编号和真实 id 的对应关系一旦错位，
    模型引用的每一句都会指向错误的段落。
    """
    mapping: dict[str, str] = {}
    lines: list[str] = []
    for index, item in enumerate(evidence, start=1):
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id:
            continue
        short = f"E{len(mapping) + 1}"
        mapping[short] = evidence_id
        pages = ""
        if item.get("pdf_page_start") is not None:
            pages = f"，PDF 第 {item['pdf_page_start']}-{item['pdf_page_end']} 页"
        lines.append(
            f"- {short}｜《{item.get('section_title') or item.get('title') or '-'}》{pages}\n"
            f"  摘要：{item.get('summary') or '-'}\n"
            f"  来源标签：{item.get('source_type')}"
            f"　审核状态：{item.get('review_status')}"
        )
    if not lines:
        return "（这次没有检索到可引用的证据）", mapping
    return "\n".join(lines), mapping


def _resolve_short_id(value: str, mapping: dict[str, str]) -> str:
    """把一个引用值翻译成真实的 evidence_id。

    已经是真实 id 的原样返回 —— 模型偶尔会直接填 UUID（清单里也看得到），
    那种情况不该被判成"引用不到"。
    """
    text = str(value or "").strip()
    if not text:
        return text
    hit = mapping.get(text.upper())
    if hit:
        return hit
    if _SHORT_ID.match(text):
        # 编号但映射里没有 —— 原样留着，让校验器报错
        return text
    return text


def validate_evidence_notes(
    notes: list[Any],
    bundle: EvidenceBundle,
    *,
    require_binding: bool = False,
) -> list[str]:
    """校验"一段话 + 证据编号"的结构（反馈调整建议用）。

    和片段级引用同一把尺子：编号必须真实存在，提到的指南术语必须
    出现在**它引用的那条**证据里。返回错误列表。
    """
    index = {e.evidence_id: e for e in bundle.evidence}
    errors: list[str] = []
    for i, note in enumerate(notes, start=1):
        text = str(getattr(note, "text", "") or "")
        ids = list(getattr(note, "evidence_ids", None) or [])
        errors += _check_fragment(
            text,
            ids,
            index,
            where=f"调整建议第 {i} 段",
            require_binding=require_binding,
        )
    return errors


def resolve_short_ids(
    values: list[str], mapping: dict[str, str]
) -> list[str]:
    """把一组短编号（E1/E2）换成真实 evidence_id。

    给非建议卡的结构用（比如反馈调整建议里的 ``evidence_notes``）。
    """
    return [_resolve_short_id(value, mapping) for value in values]


def translate_short_ids(
    cards: list[RecommendationCard],
    mapping: dict[str, str],
) -> int:
    """把卡里的短编号（E1/E2）换成真实的 evidence_id。返回替换次数。

    翻译之后，卡上的 id 就和 ``EvidenceBundle`` 里的 id 是同一套，
    后面所有校验都只认真实 id。
    """
    if not mapping:
        return 0
    count = 0
    for card in cards:
        for citation in card.source_citations:
            new = _resolve_short_id(citation.source_id, mapping)
            if new != citation.source_id:
                citation.source_id = new
                count += 1
        for part in card.explanation_parts:
            resolved = [_resolve_short_id(x, mapping) for x in part.evidence_ids]
            count += sum(1 for a, b in zip(part.evidence_ids, resolved) if a != b)
            part.evidence_ids = resolved
        for action in card.parent_actions:
            resolved = [_resolve_short_id(x, mapping) for x in action.evidence_ids]
            count += sum(1 for a, b in zip(action.evidence_ids, resolved) if a != b)
            action.evidence_ids = resolved
    return count
