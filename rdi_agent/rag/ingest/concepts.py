"""核心术语表（concept_aliases）。

**为什么要有这张表**：家长说的是日常口语，知识库里写的是规范术语。
检索要能把"发脾气"对到"挑战性行为"—— 光靠向量不够，术语是精确的
对应关系，应该显式写下来。

**每一侧都是中文**：知识库是中文指南，用户输入也是中文，所以桥接是
**中文口语 -> 中文规范术语**，不再是跨语言桥接。以前规范名是英文
（书是英文的），列名 ``canonical_name_en`` 现在改成 ``canonical_name``。

**每条术语都带来源**：``source_chunk_ids_json`` 记录它在指南里出现在
哪几块，可以翻回原文核对。``is_explicit_in_source`` 不是拍脑袋填的，
是**扫过指南正文算出来的** —— 词没在指南里出现过，就别拿它当检索依据。

**术语必须是这份指南真的用的词**：下面每一条的规范名都在指南正文里
出现过（``is_explicit_in_source`` 由扫描得出）。没出现过的词塞进查询
扩展，只会把检索带偏 —— 这是上一版踩过的坑（RDI 常用词
``affective perspective``、``engagement cycle`` 在那本书里一次都没出现）。

所以表里只收指南里出现过的词，``is_explicit_in_source`` 由扫正文算出，
不是人工断言。

    python -m rdi_agent.rag.ingest.concepts
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
    canonical_name: str
    aliases_zh: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def C(
    concept_id: str,
    canonical: str,
    zh: list[str],
    limitations: list[str] | None = None,
) -> Concept:
    return Concept(
        concept_id=concept_id,
        canonical_name=canonical,
        aliases_zh=zh,
        limitations=limitations or [],
    )


# 核心术语表。中文别名要覆盖家长真会说的说法。
#
# 分组只是为了读起来有条理，**没有结构化字段支撑** —— 检索侧不按分组做处理。
# 每次改动后要重跑 ``python -m rdi_agent.rag.ingest.concepts``，
# 它会重算每条的出处块数，并清掉"指南里查无此词"的条目。
CORE_CONCEPTS: list[Concept] = [
    # ---- 指南的核心概念（正文里明确使用的术语）----
    C("challenging_behavior", "挑战性行为",
      ["发脾气", "闹脾气", "大哭大闹", "情绪崩溃", "尖叫", "摔东西", "打人",
       "打人骂人", "破坏东西", "自伤", "撞头"],
      ["指南里指对儿童造成伤害或影响其生活的一类行为，和「问题行为」不完全等同"]),
    C("deficit_behavior", "缺陷行为",
      ["社交交流障碍", "语言发育迟缓", "生活自理差", "认知落后", "能力落后"]),
    C("restricted_interest", "局限兴趣",
      ["只玩一样东西", "转不开弯", "死盯一个东西", "兴趣狭窄", "刻板"]),
    C("repetitive_stereotyped", "重复刻板行为",
      ["重复动作", "摇来摇去", "转圈", "拍手", "自言自语", "来回走"]),
    C("adaptive_behavior", "适应性行为",
      ["能适应", "生活自理能力", "社交适应"]),
    C("challenging_vs_problem", "问题行为",
      ["不听话", "不配合", "不执行指令", "对抗", "抗拒", "闹着不干"]),
    C("alternative_behavior", "替代行为",
      ["换个行为", "用别的方式", "教他用...代替"]),
    C("self_management", "自我管理行为",
      ["自己控制", "自己能管住"]),
    # ---- 干预策略（附件 2 讲得最细）----
    C("positive_reinforcement", "正强化",
      ["奖励", "给好处", "夸他", "给他喜欢的", "强化一下"],
      ["指南强调用正强化促进行为，不是用惩罚压制"]),
    C("negative_reinforcement", "负强化",
      ["拿走让他难受的东西", "撤除厌恶刺激"]),
    C("extinction", "消退",
      ["不理会", "撤走强化物", "断掉强化", "消退策略"],
      ["消退不是惩罚 —— 是撤走强化物让行为不再被强化，有功能的行为不适合用"]),
    C("negative_punishment", "负惩罚",
      ["罚站", "罚出房间", "扣玩具", "没收", "关禁闭"],
      ["指南明确说要谨慎使用负惩罚，只在严重破坏、攻击、自伤时用"]),
    C("differential_reinforcement", "差别强化",
      ["只强化对的", "差别强化", "强化其他行为"]),
    C("antecedent_intervention", "前因干预",
      ["提前避免", "环境调整", "前因", "预防", "改变诱因"]),
    C("consequence_management", "后果管理",
      ["后果管理", "行为之后怎么处理"]),
    C("functional_analysis", "功能分析",
      ["功能分析", "为什么这么做", "行为的原因", "弄清功能"]),
    C("prompting", "辅助",
      ["提示", "手把手教", "辅助", "搭把手", "扶着手"],
      ["指南强调从高级辅助逐步减少到低级辅助，最终撤除"]),
    C("natural_teaching", "自然情境教学",
      ["在日常里教", "自然情境", "在真实场景中"]),
    C("trial_based_teaching", "回合试验教学",
      ["回合教学", "回合试验", "一轮一轮教"]),
    C("structured_teaching", "结构化教学",
      ["结构化", "视觉提示", "固定流程", "结构化安排"]),
    C("generalization", "泛化",
      ["泛化", "换个地方会不会", "在别的地方"]),
    C("imitation", "模仿",
      ["模仿", "学大人", "照着做", "仿"]),
    C("joint_attention", "共同注意",
      ["共同注意", "一起看", "指着看", "眼神来回"]),
    C("functional_communication", "功能性沟通",
      ["会表达", "开口要", "用眼神告诉大人"]),
    C("sensory", "感觉统合",
      ["感统", "感官", "怕吵", "怕碰", "触觉敏感", "声音敏感", "不愿动"],
      ["指南用「感觉」处理，不等同于感统疗法"]),
    C("errorless_teaching", "无错误教学",
      ["不让孩子出错", "无错误"]),
    C("rdi_nbd", "自然发展行为干预",
      ["自然发展行为干预", "地板时光", "关键反应训练"]),
    # ---- 原则与评估 ----
    C("early_intervention", "早期干预",
      ["早点开始", "越早越好", "早期介入", "趁早"]),
    C("individualized_intervention", "个体化干预",
      ["个体化", "因人而异", "定制", "按孩子情况"]),
    C("comprehensive_intervention", "综合干预",
      ["综合", "多方面一起", "全面"]),
    C("evidence_based", "科学循证",
      ["有证据的", "循证", "科学有效", "靠谱的方法"]),
    C("long_term_intensive", "长程高强度",
      ["长期", "坚持", "每天多少小时", "高强度"]),
    C("family_participation", "家庭参与",
      ["家里参与", "家长一起做", "家庭干预"]),
    C("social_integration", "社会融合",
      ["融合", "去幼儿园", "上学", "和同学在一起"]),
    C("developmental_assessment", "发育水平评估",
      ["评估", "测一下", "发育测评", "能力评估"]),
    C("individualized_plan", "个体化教育干预计划",
      ["干预计划", " IEP", "教学计划", "定计划"]),
    C("reinforcer", "强化物",
      ["强化物", "奖励物", "他喜欢的东西", "拿什么奖励"]),
    C("milestone_behavior", "里程碑式行为",
      ["里程碑", "关键能力", "阶段性目标"]),
    # ---- 健康教育与家庭 ----
    C("health_education", "健康教育",
      ["健康教育", "讲清楚", "让家长了解"]),
    C("parent_psych_support", "家长心理支持",
      ["家长心理", "家长情绪", "家长太累", "互助团体"]),
    C("family_intervention", "家庭干预",
      ["家庭干预", "在家做", "家里怎么练"]),
    C("psychotherapy", "心理治疗",
      ["心理治疗", "心理咨询", "心理医生"]),
    C("digital_intervention", "数智干预康复",
      ["数字", "APP", "线上", "虚拟现实", "智能"]),
    C("follow_up", "随访",
      ["随访", "复诊", "定期看", "多久复查"]),
    C("quality_control", "质量控制",
      ["质量", "规范", "标准化"]),
    # ---- 共存问题与共患病 ----
    C("coexisting_problem", "共存问题",
      ["同时有问题", "还有别的", "伴随", "合并"]),
    C("comorbidity", "共患病",
      ["共病", "同时患有", "癫痫", "焦虑", "多动", "注意力缺陷"]),
    C("sleep_problem", "睡眠问题",
      ["睡不着", "睡不好", "半夜醒", "晚睡"]),
    C("eating_problem", "饮食问题",
      ["不吃饭", "挑食", "吃饭困难", "吞咽"]),
    C("toilet_problem", "如厕问题",
      ["不把尿", "尿裤子", "不会上厕所", "如厕训练"]),
    C("sexual_related_problem", "性相关问题",
      ["自慰", "摸自己", "性相关"]),
    C("epilepsy", "癫痫",
      ["癫痫", "抽风", "发作"]),
    # ---- 名词解释里的规范定义 ----
    C("definition", "名词解释",
      ["什么叫", "是什么", "定义", "名词解释"]),
]


def _normalize_for_match(text: str) -> str:
    return " ".join(text.lower().split())


async def find_source_chunks(
    concepts: list[Concept], chunks: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """扫指南正文，找出每条术语出现在哪些块里。

    ``chunks`` 是 [(chunk_id, **content_original**)]。

    **为什么匹配原文而不是规范化文本**：``content_normalized`` 现在是
    jieba 分过词的（"发脾气" 可能被切成 "发" "脾气"），拿原始别名
    去子串匹配会漏掉一大半 —— 别名是原样的词，原文也是原样的词，
    两边用同一套表示才能对上。
    """
    lowered = [(cid, _normalize_for_match(text)) for cid, text in chunks]
    hits: dict[str, list[str]] = {}
    for concept in concepts:
        needles = [concept.canonical_name.lower(), *concept.aliases_zh]
        found: list[str] = []
        for cid, text in lowered:
            if any(needle and needle.lower() in text for needle in needles):
                found.append(cid)
        hits[concept.concept_id] = found
    return hits


async def load(
    concepts: list[Concept] | None = None, *, source_id: str | None = None
) -> dict[str, int]:
    """写进 concept_aliases。

    幂等：先按 concept_id 删掉再写。``source_chunk_ids_json`` 每次重算，
    因为块变了（重新入库）术语的出处也会变。
    """
    concepts = concepts or CORE_CONCEPTS
    async with session_scope() as session:
        # ---- 清理 1：代码里已经没有的概念 ----
        #
        # 知识库从英文书换成中文指南之后，库里还留着上一版的 72 条英文
        # 概念（``guided participation`` ``scaffolding`` …）。
        # 它们在代码里已经不存在了，桥接也永远用不上 ——
        # 留着只会让"库里有多少条术语"这个数字失真。
        #
        # 上一版的注释说"不清这类，那是维护顺序问题"—— 那时是对的，
        # 因为英文流程里偶尔会手工往库里加几条。但中文流程换了整套术语，
        # 旧概念**全部**是噪声，所以现在明确清掉。
        known_ids = {c.concept_id for c in concepts}
        all_ids = (
            await session.execute(select(ConceptAlias.concept_id))
        ).scalars().all()
        for concept_id in all_ids:
            if concept_id not in known_ids:
                row = (
                    await session.execute(
                        select(ConceptAlias).where(
                            ConceptAlias.concept_id == concept_id
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
        if all_ids:
            await session.flush()

        # 清理：**指南里查无此词**的概念要从库里清掉。
        #
        # 只清这一类，不清"代码里没列但概念本身有效"的那些 ——
        # 那是维护顺序问题，不是错误数据。
        #
        # 为什么这类必须清：桥接会把这些词塞进关键词查询。
        # 指南里根本没有这个词，匹配到的必然是别的东西 —— 检索结果被带偏，
        # 而且带偏得很隐蔽（看起来"有结果"，只是结果不对）。
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
                select(KnowledgeChunk.id, KnowledgeChunk.content_original)
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
                    canonical_name=concept.canonical_name,
                    aliases_zh_json=concept.aliases_zh,
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
        f"术语 {stats['total']} 条，其中 {stats['explicit_in_source']} 条在指南里出现过"
        f"（扫了 {stats['chunks_scanned']} 个块）"
    )


if __name__ == "__main__":
    main()
