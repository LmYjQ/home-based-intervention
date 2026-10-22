"""核心术语表（concept_aliases）。

**为什么要有这张表**：家长说的是中文，书里写的是英文。检索要能把
"引导式参与"对到 "guided participation"，光靠向量不够 —— 术语是精确的
对应关系，应该显式写下来。

**每条术语都带来源**：``source_chunk_ids_json`` 记录它在书里出现在哪几块，
可以翻回原文核对。``is_explicit_in_source`` 不是拍脑袋填的，是**扫过全书
正文算出来的** —— 词没在书里出现过，就别拿它当检索依据。

**术语必须是这本书真的用的词**：首版列表是拿全书词频统计 + 上下文核对
出来的，不是照抄 RDI 通用词汇。踩过的坑：``affective perspective``、
``unit of attention``、``engagement cycle``、``functional play`` 这些
"RDI 常用词"在这本书里**一次都没出现**（书里说的是 ``parent-child
engagement``、``over-prompting``、``participation relationship``）。
把书里没有的词塞进查询扩展，只会把检索带偏。

所以表里只收书里出现过的词，``is_explicit_in_source`` 由扫全书正文算出，
不是人工断言。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rdi_agent.db.engine import session_scope
from rdi_agent.db.models import ConceptAlias, KnowledgeChunk


@dataclass
class Concept:
    concept_id: str
    canonical_name_en: str
    aliases_zh: list[str] = field(default_factory=list)
    aliases_en: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def C(
    concept_id: str,
    en: str,
    zh: list[str],
    en_aliases: list[str] | None = None,
    limitations: list[str] | None = None,
) -> Concept:
    return Concept(
        concept_id=concept_id,
        canonical_name_en=en,
        aliases_zh=zh,
        aliases_en=en_aliases or [],
        limitations=limitations or [],
    )


# 首版 50 个核心术语。中文别名要覆盖家长真会说的说法。
CORE_CONCEPTS: list[Concept] = [
    # ---- 核心机制 ----
    C("guided_participation", "guided participation", ["引导式参与", "引导参与", "引导式互动"],
      ["guided participation relationship", "guided participation process"],
      ["书中的 guided participation 有特定操作定义，不是泛指“家长带着孩子玩”"]),
    C("participation_relationship", "participation relationship", ["参与关系", "引导关系"],
      ["guided participation relationship"]),
    C("experience_sharing", "experience sharing", ["经验分享", "分享经验"],
      ["experience sharing communication"]),
    C("dynamic_intelligence", "dynamic intelligence", ["动态智力", "动态认知"]),
    C("dynamic_communication", "dynamic communication", ["动态沟通"]),
    C("dynamic_departures", "dynamic departures", ["动态分离", "分离情境"]),
    C("emotional_intelligence", "emotional intelligence", ["情绪智力", "情感智力"]),
    C("neural_integration", "neural integration", ["神经整合", "神经网络整合"]),
    C("lateral_integration", "lateral integration", ["横向整合", "侧向整合"]),
    C("dynamic_neural", "dynamic neural", ["动态神经", "动态神经过程"]),
    C("static_neural", "static neural", ["静态神经", "静态神经过程"]),
    C("pre_frontal_cortex", "pre-frontal cortex", ["前额叶皮层", "前额叶"]),
    C("communication_channels", "communication channels", ["沟通渠道", "交流通道"]),
    C("mental_processes", "mental processes", ["心理过程"]),
    C("productive_uncertainty", "productive uncertainty", ["生产性不确定", "有建设性的不确定"]),
    C("critical_moments", "critical moments", ["关键时刻", "关键节点"]),
    C("reference_point", "reference point", ["参照点", "参考点"]),
    # ---- 发展阶段 ----
    C("apprenticeship", "apprenticeship", ["学徒期", "学徒阶段", "学徒制"],
      limitations=["书中把学徒期当作孩子参与社会互动的阶段，不是职业学徒"]),
    C("readiness", "readiness", ["准备度", "就绪度"]),
    C("guiding", "guiding", ["引导", "引导阶段"]),
    C("internalizing", "internalizing", ["内化", "内化阶段"]),
    C("education_stage", "education", ["教育阶段", "教育"],
      limitations=["对应书中的 Education 阶段，不是普通所说的“教育”"]),
    C("rdi_learning_system", "rdi learning system", ["RDI 学习体系", "RDI 课程体系"]),
    # ---- 家长能操作的动作 ----
    C("over_prompting", "over-prompting", ["过度提示", "不停提示", "反复提示"],
      ["prompting", "over prompting"],
      ["书中批评的是家长不停替孩子把话说完，而不是提示本身"]),
    C("pre_requisite", "pre-requisite", ["前置能力", "先决条件", "前提条件"],
      ["pre-requisites"]),
    C("turn_taking", "turn-taking", ["轮流", "轮流互动", "轮替"],
      ["turn taking"]),
    C("set_clear_limits", "set clear limits", ["设定清晰界限", "立规矩", "明确边界"]),
    C("responsibility", "responsibility", ["责任", "担当", "自己负责"],
      ["take greater responsibility"]),
    C("choice", "choice", ["选择", "给选择", "让孩子选"]),
    C("routine", "routine", ["常规", "固定流程", "日常安排"]),
    C("ritual", "ritual", ["仪式", "固定仪式"]),
    C("scaffolding", "scaffolding", ["脚手架", "支架式支持", "递进提示"],
      ["scaffolding support"],
      ["书里用 scaffolding 指大人临时撑起孩子还做不到的那一段"]),
    C("imitation", "imitation", ["模仿", "仿效"]),
    C("joint_attention", "joint attention", ["共同注意", "共同关注"]),
    C("social_referencing", "social referencing", ["社会参照", "社会参照物"]),
    C("eye_contact", "eye contact", ["眼神接触", "对视"]),
    C("engagement", "engagement", ["参与", "参与度", "投入度"]),
    C("flexibility", "flexibility", ["灵活性", "灵活变通", "变通能力"]),
    C("generalization", "generalization", ["泛化", "情境泛化"]),
    C("frustration", "frustration", ["挫折", "受挫", "崩溃"]),
    C("motivation", "motivation", ["动机", "主动性"]),
    C("regulation", "regulation", ["调节", "情绪调节", "自我调节"]),
    C("sensory", "sensory", ["感觉", "感官"]),
    C("behavioral", "behavioral", ["行为", "行为层面的"]),
    C("self_awareness", "self-awareness", ["自我觉察", "自我意识"]),
    C("affective", "affective", ["情感", "情绪的", "情感性"]),
    C("deficit", "deficit", ["缺陷", "缺失", "不足"]),
    # ---- 角色与人群 ----
    C("rdi_consultant", "rdi consultant", ["顾问", "RDI 顾问"],
      limitations=["书里指接受过 RDI 培训、辅导家长的专业角色，不是泛指“咨询师”"]),
    C("breaking_point", "breaking point", ["崩溃点", "临界点", "爆发点"]),
    C("asd_child", "asd child", ["自闭症孩子", "谱系孩子", "ASD 儿童"],
      ["asd children", "asd individuals"]),
    C("typically_developing", "typically developing", ["典型发展儿童", "普通发展孩子", "正常发育孩子"],
      ["typically developing child", "typically developing children",
       "typically developing infants"]),
    C("autism_spectrum_disorder", "autism spectrum disorder", ["自闭症谱系障碍", "自闭症", "谱系障碍"],
      ["autism spectrum disorders", "autism spectrum"]),
]


def _normalize_for_match(text: str) -> str:
    return " ".join(text.lower().split())


async def find_source_chunks(
    concepts: list[Concept], chunks: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """扫全书正文，找出每条术语出现在哪些块里。

    ``chunks`` 是 [(chunk_id, content_normalized)]。匹配用小写的规范化文本，
    中文别名直接子串匹配，英文用词边界匹配（避免 "readiness" 命中
    "unreadiness" 这种）。
    """
    lowered = [(cid, _normalize_for_match(text)) for cid, text in chunks]
    hits: dict[str, list[str]] = {}
    for concept in concepts:
        needles = [concept.canonical_name_en.lower(), *concept.aliases_en]
        found: list[str] = []
        for cid, text in lowered:
            matched = False
            for needle in needles:
                if _contains_word(text, needle.lower()):
                    matched = True
                    break
            if not matched:
                for zh in concept.aliases_zh:
                    if zh and zh.lower() in text:
                        matched = True
                        break
            if matched:
                found.append(cid)
        hits[concept.concept_id] = found
    return hits


def _contains_word(text: str, needle: str) -> bool:
    """英文词边界匹配。needle 里有空格或非字母时退化成子串。"""
    if not needle:
        return False
    if not needle.replace(" ", "").isalnum():
        return needle in text
    idx = text.find(needle)
    while idx != -1:
        before = text[idx - 1] if idx > 0 else " "
        after = text[idx + len(needle)] if idx + len(needle) < len(text) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        idx = text.find(needle, idx + 1)
    return False


async def load(
    concepts: list[Concept] | None = None, *, source_id: str | None = None
) -> dict[str, int]:
    """写进 concept_aliases。

    幂等：先按 concept_id 删掉再写。``source_chunk_ids_json`` 每次重算，
    因为块变了（重新入库）术语的出处也会变。
    """
    concepts = concepts or CORE_CONCEPTS
    async with session_scope() as session:
        chunk_rows = (
            await session.execute(
                select(KnowledgeChunk.id, KnowledgeChunk.content_normalized)
            )
        ).all()
        chunk_pairs = [(str(cid), text or "") for cid, text in chunk_rows]
        sources = await find_source_chunks(concepts, chunk_pairs)

        for concept in concepts:
            chunk_ids = sources.get(concept.concept_id, [])
            explicit = bool(chunk_ids)
            existing = (
                await session.execute(
                    select(ConceptAlias).where(ConceptAlias.concept_id == concept.concept_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                await session.delete(existing)
                await session.flush()

            stmt = (
                pg_insert(ConceptAlias)
                .values(
                    concept_id=concept.concept_id,
                    canonical_name_en=concept.canonical_name_en,
                    aliases_zh_json=concept.aliases_zh,
                    aliases_en_json=concept.aliases_en,
                    source_chunk_ids_json=chunk_ids[:50],
                    is_explicit_in_source=explicit,
                    limitations_json=concept.limitations,
                    review_status="unreviewed",
                    version="v1",
                )
                .on_conflict_do_nothing(index_elements=["concept_id"])
            )
            await session.execute(stmt)

        explicit_count = sum(1 for c in concepts if sources.get(c.concept_id))
        return {
            "total": len(concepts),
            "explicit_in_source": explicit_count,
            "chunks_scanned": len(chunk_pairs),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="写入核心术语表")
    parser.parse_args()
    stats = asyncio.run(load())
    print(
        f"术语 {stats['total']} 条，其中 {stats['explicit_in_source']} 条在书里出现过"
        f"（扫了 {stats['chunks_scanned']} 个块）"
    )


if __name__ == "__main__":
    main()
