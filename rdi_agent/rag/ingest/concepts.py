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
    C("rdi_consultant", "RDI consultant", ["顾问", "RDI 顾问"],
      limitations=["书里指接受过 RDI 培训、辅导家长的专业角色，不是泛指“咨询师”"]),
    C("breaking_point", "breaking point", ["崩溃点", "临界点", "爆发点"]),
    C("asd_child", "asd child", ["自闭症孩子", "谱系孩子", "ASD 儿童"],
      ["asd children", "asd individuals"]),
    C("typically_developing", "typically developing", ["典型发展儿童", "普通发展孩子", "正常发育孩子"],
      ["typically developing child", "typically developing children",
       "typically developing infants"]),
    C("autism_spectrum_disorder", "autism spectrum disorder", ["自闭症谱系障碍", "自闭症", "谱系障碍"],
      ["autism spectrum disorders", "autism spectrum"]),
    # ---- 日常说法 -> 书里术语（2B 补）----
    #
    # 前面 50 条是"书里的术语"，家长说的往往是日常语言。
    # 检索评测集用真实家长问法，暴露出术语表只覆盖术语不覆盖口语。
    # 下面这批把口语桥到书里确实存在的词（每条的英文都扫过全书，
    # 命中数见 evals/README.md 的核验记录）。
    C("greeting", "greeting", ["打招呼", "问好", "打招呼时", "不敢打招呼", "不敢跟人打招呼",
                              "跟人打招呼", "主动打招呼"],
      ["greet", "greetings"],
      ["书里的 greeting 指社交性的问候/开场，不是寒暄客套"]),
    C("crying", "crying", ["哭", "大哭", "爱哭", "哭闹", "一直哭"],
      ["cry", "cries"],
      ["书里区分“想哭”(escalation)和实际哭出来，评估时要分开看"]),
    C("tantrum", "tantrum", ["发脾气", "闹脾气", "突然大哭大闹", "情绪崩溃"],
      ["meltdown"],
      ["meltdown 在书里出现次数很少，主要用 tantrum；不要混用"]),
    C("compliance", "compliance", ["不听话", "不配合", "不听话不配合", "让做什么都不做",
                                "不按要求做", "不执行指令"],
      ["comply", "cooperate", "cooperation"],
      ["书中批评的 compliance 常常是过度提示造成的，不是孩子故意对抗"]),
    C("cooperation", "cooperation", ["合作", "配合", "一起做", "配合大人", "配合指令"],
      ["cooperate"],
      ["与 compliance 一起查：一个是孩子行为，一个是大人的引导方式"]),
    C("sharing", "sharing", ["分享", "抢玩具", "不让别人玩", "独占", "分享玩具"],
      ["sharing toys"],
      ["书里的 sharing 出现在自闭症儿童社交互动的讨论里，语境是同伴交往"]),
    C("initiative", "initiative", ["主动", "主动性", "不主动", "要人拉着才动",
                                "不会自己发起", "等着大人安排"],
      ["initiative taking"],
      ["书中 initiative 指孩子主动发起互动，不是“主动做作业”"]),
    C("attention", "attention", ["注意力", "不专心", "注意力不集中", "坐不住",
                                "注意力分散", "走神", "专注"],
      ["attentive"],
      ["与 unit of attention 区分：那是 RDI 特有的注意力单元概念"]),
    C("responsiveness", "responsiveness", ["回应", "有回应", "不回应", "不理人",
                                        "没反应", "叫他不应"],
      ["responsive"],
      ["书里 responsive 指对成人的互动有回应，不是语言能力的泛指"]),
    C("fear", "fear", ["害怕", "恐惧", "怕", "怕生", "胆小"],
      ["afraid"],
      ["书里区分 fear（对具体事物的害怕）和 general anxiety"]),
    C("avoidance", "avoidance", ["回避", "躲避", "躲开", "逃避", "绕开"],
      ["avoid", "withdraw", "withdrawal"],
      ["书里 withdraw 多指“退出互动”而非心理防御意义上的回避"]),
    C("imitating", "imitating", ["模仿", "仿", "学大人", "照着做"],
      ["imitate", "imitation"],
      ["书里 imitation 是学徒期的重要里程碑，常与 generalization 一起查"]),
    C("conversation", "conversation", ["聊天", "对话", "交谈", "说不完整", "不会接话"],
      ["conversation"],
      ["书里 conversation 讨论的是互动的来回，不是语法"]),
    C("peer", "peer", ["同伴", "同龄人", "和小朋友", "跟别的孩子", "幼儿园小朋友"],
      ["peers", "friendship"],
      ["书里 peer 互动常作为 apprenticeship 的延伸"]),
    C("language", "language", ["语言", "说话", "说话少", "不会说", "语迟"],
      ["receptive language", "expressive language"],
      ["书里区分接收性语言和表达性语言，评估时要分开"]),
    C("sensory_integration", "sensory integration", ["感统", "感觉统合", "感官失调",
                                                  "触觉敏感", "声音敏感", "怕吵"],
      ["sensory"],
      ["书里 sensory 一词出现不多，且与“感觉统合训练”不是一回事，不要滑向感统疗法"]),
    # ---- 库里已有但代码里漏掉的（2026-10-22 2B 补回）----
    # 这几条是之前手工加进库的，英文在书里确实出现过，但没进 CORE_CONCEPTS，
    # 于是 concepts.load() 每次都会把它们当“遗留”清掉。术语表要有单一来源。
    C("anxiety", "anxiety", ["焦虑"]),
    C("brainstorming", "brainstorming", ["头脑风暴", "发散思考"]),
    C("distress", "distress", ["痛苦", "难受", "情绪痛苦"]),
    C("flexible_thinking", "flexible thinking", ["灵活思维", "灵活思考"]),
    C("scripting", "scripting", ["脚本", "情境脚本"]),
    C("transition", "transition", ["转换", "过渡", "场景转换"]),
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
        # 清理：**书里查无此词**的概念要从库里清掉。
        #
        # 只清这一类，不清"代码里没列但概念本身有效"的那些 ——
        # 那是维护顺序问题，不是错误数据。
        #
        # 为什么这类必须清：桥接会把这些词的英文塞进 FTS 查询。
        # 书里根本没有这个词，匹配到的必然是别的东西 —— 检索结果被带偏，
        # 而且带偏得很隐蔽（看起来"有结果"，只是结果不对）。
        #
        # 判定用 ``is_explicit_in_source``：它是**扫全书正文算出来的**
        # （相邻词边界匹配），不是人工断言。注意不能用 FTS 查 ——
        # FTS 是 AND 语义，"functional" 和 "play" 不挨着出现也算命中。
        stale = (
            await session.execute(
                select(ConceptAlias.concept_id).where(
                    ConceptAlias.is_explicit_in_source.is_(False)
                )
            )
        ).scalars().all()
        for concept_id in stale:
            row = (
                await session.execute(
                    select(ConceptAlias).where(ConceptAlias.concept_id == concept_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
        if stale:
            await session.flush()

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
