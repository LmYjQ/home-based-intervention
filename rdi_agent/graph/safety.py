"""安全审查（计划 §9.3）。

**规则优先，LLM 复核为辅。** 规则层是硬闸 —— LLM 只能加严，不能放宽。

为什么规则优先：这个系统给的是**家长会照着做的指令**。"必须看着我的眼睛"
"不管他"这类话一旦出现在建议里，伤害是实际发生的，不会因为说得客气就消失。
规则判定是确定性的、可单测的、不依赖模型今天心情好不好。

六条规则（计划 §9.3）：

1. 诊断性结论
2. 建议强迫眼神接触 / 压制拒绝 / 忽视明显不适
3. 缺停止条件
4. 大段原文
5. 把 AI 转化说成原书原话
6. （附加）引用与证据对不上 —— 由 :mod:`rdi_agent.graph.citations` 负责，这里不重复

结果三选一：

- ``pass`` → 正常输出
- ``blocked`` → 走安全兜底话术
- ``needs_human_review`` → 首版不建审核队列，降级为有限回答并标记
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rdi_agent.schemas.conversation import SafetyReview
from rdi_agent.schemas.evidence import EvidenceBundle
from rdi_agent.schemas.recommendation import RecommendationCard

# 大段原文的判定长度（连续字符数）。
LONG_QUOTE_CHARS = 120


@dataclass(frozen=True)
class Rule:
    """一条安全规则。"""

    name: str
    severity: str  # hard -> blocked；soft -> needs_human_review
    message: str
    patterns: tuple[str, ...] = ()
    # 需要同时出现第二个关键词才算命中（rule = patterns + 需要搭配的词）
    also_require: tuple[str, ...] = ()
    # 出现这些否定说法时不算命中 —— "不要忽视孩子的情绪"是正面用法
    negations: tuple[str, ...] = ()


# ---------------------------------------------------------------- 规则表
#
# **诊断规则要按语境判，不能按词判**。
#
# 知识库现在是《孤独症谱系障碍儿童干预康复指南》—— "孤独症" "自闭症"
# "谱系" "确诊" 这些词是**指南自己的标题和正文词汇**，一条基于指南的
# 建议卡几乎必然会提到它们。原来的规则是"命中词就拦"，换成中文指南
# 之后会把所有正经的卡都拦掉。
#
# 所以拆成两层：
#
# 1. **诊断性断言**（硬拦）：说**这个孩子**是什么 diagnoses。
#    判据是"孩子 + 诊断动词/系动词 + 病名"三者靠近，
#    或者直接出现"确诊""疑似患有"这种断言动词。
# 2. **术语提及**（不拦）：只是提到病名，或者在讲指南怎么说、
#    或者在讲"疑似"要不要转诊 —— 都不拦。
#
# 英文那边（autism / diagnosis）保持原样：那边知识库是英文书，
# 词不会随便撞上；而且英文语境下 "The child may have autism"
# 就是断言，没有歧义。
DIAGNOSTIC_RULE = Rule(
    name="diagnostic_language",
    severity="hard",
    message="出现了诊断性结论。系统只描述观察到的事实，不给孩子贴标签、不下诊断。",
    patterns=(
        # 英文：断言就是断言，没有歧义
        "autism", "autistic", "asperger", "adhd",
        "diagnosis", "diagnosed", "diagnosed with",
        "has disorder", "has syndrome",
        # 中文断言：**直接断言诊断行为**的动词短语
        "确诊了", "确诊为", "确诊是", "已经确诊", "确诊的患儿",
        "疑似患有", "疑似自闭", "疑似孤独症",
        "患有", "得了", "病人", "患者",
        # 给孩子贴标签的固定说法
        "这孩子是自闭", "孩子是自闭", "他是自闭", "她是自闭",
        "是个自闭症", "是自闭症孩子", "属于自闭症", "属于谱系",
        "自闭症孩子", "谱系孩子",
    ),
)

# "疑似"单独出现不拦 —— 指南里"针对初筛、复筛阶段符合转诊条件的儿童"
# 这类话是正常的。但"疑似自闭症"（对着一个孩子说）要拦。
#
# 所以这里用 also_require 做了个折中：**"疑似" + 病名**才算命中。
DIAGNOSTIC_SUSPECTED_RULE = Rule(
    name="diagnostic_suspected",
    severity="hard",
    message="出现了疑似诊断的结论。系统不给孩子下判断。",
    patterns=("疑似",),
    also_require=("自闭症", "孤独症", "自闭", "谱系", "发育迟缓", "智力障碍"),
)

# **术语提及不算诊断** —— 明确列出来，免得以后有人"顺手"把它们加回
# DIAGNOSTIC_RULE。指南正文里的这些词是正常的：
#   "孤独症儿童的缺陷行为" "针对挑战性行为的干预" "孤独症相关知识"
_DIAGNOSTIC_ALLOWED_TERMS = (
    "孤独症儿童",
    "孤独症患儿",
    "孤独症谱系障碍",
    "孤独症的相关",
    "孤独症相关",
    "针对孤独症",
    "孤独症的核心症状",
    "孤独症谱系",
)

# **给孩子贴标签的句式**。按**句子**判，不按整张卡判 ——
# 一张卡里可能既说"指南建议早期干预"（正常），又说"这孩子可能是自闭症"
# （要拦）。整卡连成一个字符串判，两种情况会互相干扰。
#
# 句式分两组：
#
# - **孩子 + 诊断**：主语是孩子，谓语是判断，病名在后
#   "这孩子可能是自闭症" "他患有孤独症"
# - **病名 + 孩子**：病名在前
#   "自闭症的孩子" "孤独症患儿"
_DIAGNOSTIC_CLAIM_PATTERNS = (
    # 病名
    "自闭症", "孤独症", "自闭", "谱系障碍", "谱系",
    "发育迟缓", "智力障碍", "唐氏",
    # 主语：这个孩子
    "这孩子", "这个孩子", "孩子", "他", "她", "幼儿", "宝宝", "患儿",
    # 判断：确诊 / 疑似 / 是不是 / 可能就是
    "确诊", "疑似", "患有", "得了", "是不是", "可能就是", "基本就是",
    "应该是", "属于", "患有",
)

# 句子里的**豁免**：这些说法是在讲知识库或讲一般情况，不是给孩子下判断。
#
# "健康教育""宣教"这类是**指南里的章节名和做法** ——
# "健康教育这一节讲的是孤独症相关知识的家长宣教"是在介绍知识库的结构，
# 不是在给眼前这个孩子贴标签。
_DIAGNOSTIC_SENTENCE_EXEMPT = (
    "指南", "附件", "第", "页", "依据", "原则", "指出", "规定", "建议",
    "术语", "定义", "名词解释", "共病", "筛查", "转诊", "初筛", "复筛",
    "健康教育", "宣教", "家长教育", "知识普及", "讲的是", "这一节",
)

COERCION_RULE = Rule(
    name="coercion",
    severity="hard",
    message="出现了强迫行为。引导要靠孩子跟得上，不是靠压住他的反应。",
    patterns=(
        "强迫", "强行", "按住", "压住", "不许拒绝", "不许看别处", "不许哭",
        "不许喊", "逼他", "逼她", "逼孩子",
        "必须看着我的眼睛", "必须看着你的眼睛", "必须对视", "一定要对视",
        "必须抬头看", "强行抱住", "拖走", "拽着",
    ),
)

# **惩罚类的词要单独一条规则**。
#
# 原来"惩罚""罚站"和"强迫""按住"在一起。但**指南自己就讲惩罚**：
# 附件 2 有"负惩罚"策略的完整说明（"应谨慎使用负惩罚相关策略，
# 如罚站、罚出房间、扣玩游戏时间"），正文也提了 8 次"惩罚"。
#
# 规则要做的是拦**建议里的惩罚**，不是拦**知识库里的惩罚知识**。
# 两者怎么区分？看是不是在**建议家长去做**：
#
# - "指南附件 2 讲负惩罚策略，说应谨慎使用，如罚站、扣玩游戏时间" —— 复述 → 放行
# - "他再闹就罚站十分钟" —— 建议 → 拦
#
# **判据是句内共现，不是整卡共现**。这一点很重要：原来的 ``also_require``
# 是在**整张卡**的文本里找搭配词，而"就""如果""可以"这些词在卡里
# 到处都是（"他明显抗拒就停下来"就带一个"就"）—— 结果一张**只是
# 引用了指南**的卡也会被判命中。
#
# 所以这里用**句级**判断：惩罚词和"建议性"词必须出现在**同一句**里。
PUNISHMENT_RULE = Rule(
    name="punishment_advice",
    severity="hard",
    message="出现了用惩罚来管孩子的建议。惩罚容易被孩子理解为惩罚的是他自己。",
    patterns=(
        "罚站", "罚出", "罚他", "罚她", "罚孩子", "罚一下", "罚他们",
        "让他罚", "罚关", "关禁闭", "关小黑屋",
    ),
)

# 惩罚词旁边的"建议性"词 —— 出现在**同一句**里才算是在建议。
_PUNISHMENT_SUGGESTIVE = (
    "你就", "你可以", "建议", "让他", "让她", "给他", "给她",
    "下次", "当场", "再闹", "不听话就", "犯错了就", "闹就", "哭就",
)

# 引用知识库的信号 —— 出现在同一句里就放过。
_PUNISHMENT_QUOTING = (
    "指南", "附件", "策略", "谨慎", "说明", "提到", "指出", "例如",
    "如罚站", "包括", "例如罚",
)

# "忽视明显不适"：要两个词一起出现才算命中。
# 单说"忽视"可能是"别忽视孩子的情绪"这种正面用法；
# "不适"单独出现也是中性描述。所以搭配词只取**明确的痛苦信号**，
# 不取"情绪""生气"这种中性描述。
IGNORE_DISTRESS_RULE = Rule(
    name="ignore_distress",
    severity="hard",
    message="出现了忽视明显不适的建议。孩子明显难受时要停下来，不要硬推。",
    patterns=(
        "忽视", "忽略", "不用管", "不管他", "不管她", "不理他", "不理她",
        "由他去", "任他", "任她", "别管",
    ),
    also_require=(
        "不适", "难受", "痛苦", "抗拒", "尖叫", "崩溃", "挣扎", "生病",
        "发烧", "哭闹", "大哭", "嚎", "发抖", "抽搐", "脸色",
    ),
    negations=(
        "不要忽视", "别忽视", "不能忽视", "不应忽视", "不忽视",
        "不要忽略", "别忽略", "不能忽略", "不应忽略", "不忽略",
        "不要无视", "别无视",
    ),
)

# 停止条件写得没有可操作性 —— 软规则，降级为 needs_human_review。
VAGUE_STOP_RULE = Rule(
    name="vague_stop_conditions",
    severity="soft",
    message="停止条件写得没法执行，家长不知道什么时候该停。",
    patterns=("视情况而定", "自行判断", "酌情", "看情况", "家长决定", "量力而行"),
)

# 把 AI 的转化说成知识库原文 —— 只在 ai_adaptation 的卡上算命中。
#
# 词从"原书/书里"换成"指南" —— 知识库换了，措辞也要跟着换。
# 旧的"原书说"这些词留着无害（不会再出现），但会让规则看起来像是
# 针对一本已经不存在的书写的 —— 删掉更干净。
MISATTRIBUTION_RULE = Rule(
    name="misattribution",
    severity="hard",
    message="把 AI 的应用转化说成了指南原文。",
    patterns=(
        "指南说", "指南写的", "指南写道", "指南原话", "指南明确说",
        "指南明确写道", "指南里明确", "指南明确",
        "原文写道", "文件原话",
    ),
)

ALL_RULES: tuple[Rule, ...] = (
    DIAGNOSTIC_RULE,
    DIAGNOSTIC_SUSPECTED_RULE,
    COERCION_RULE,
    PUNISHMENT_RULE,
    IGNORE_DISTRESS_RULE,
    VAGUE_STOP_RULE,
    MISATTRIBUTION_RULE,
)


# ---------------------------------------------------------------- 工具
def _card_texts(card: RecommendationCard) -> list[str]:
    """卡里所有可能出现在家长眼前的文字。"""
    texts: list[str] = [
        card.title,
        card.explanation,
        *card.observed_facts,
        *card.tentative_interpretations,
        *card.uncertainty_notes,
        *card.follow_up_questions,
    ]
    for action in card.parent_actions:
        texts.append(action.action)
        texts.append(action.context)
        texts.extend(action.steps)
        texts.extend(action.observe)
        texts.extend(action.reduce_or_stop_if)
    texts.extend(card.success_indicators)
    texts.extend(card.stop_conditions)
    texts.extend(c.claim_supported for c in card.source_citations)
    return [t for t in texts if t]


def _norm(text: str) -> str:
    return " ".join((text or "").split())


# 句子边界。中英文都算。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])|(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    """按句子切分。

    **为什么要按句子判**：一张卡里可能既有"指南建议早期干预"（正常）
    又有"这孩子可能是自闭症"（要拦）。整张卡连成一个字符串判，
    两种情况会互相干扰 —— 前者把后者稀释掉，后者把前者误伤。
    """
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _hits_punishment(texts: list[str]) -> bool:
    """判有没有**建议**家长用惩罚。

    **句级判断**：惩罚词和建议性词必须出现在**同一句**里。

    为什么不能整卡判：卡里到处都是"就""如果""可以"这类词
    （"他明显抗拒就停下来"就带一个"就"），整卡判的话，
    一张只是**引用了指南里的负惩罚说明**的卡也会被判命中。

    为什么不能只看惩罚词：指南附件 2 整节都在讲负惩罚策略，
    见到就拦等于把指南最有实操价值的一节封掉。

    顺序很重要：**先看引用信号，命中就放过这一句**。
    "指南附件 2 讲负惩罚策略，说应谨慎使用，如罚站、扣玩游戏时间"
    这一句里有"指南""谨慎""策略"—— 是在讲知识库，放行。
    "他再闹就罚站十分钟" 没有引用信号，有建议信号 → 拦。
    """
    for text in texts:
        for sentence in _sentences(text):
            has_punishment = any(
                p in sentence for p in PUNISHMENT_RULE.patterns
            )
            if not has_punishment:
                continue
            if any(q in sentence for q in _PUNISHMENT_QUOTING):
                continue
            if any(s in sentence for s in _PUNISHMENT_SUGGESTIVE):
                return True
    return False


def _hits_diagnostic(texts: list[str]) -> bool:
    """判有没有给孩子下诊断结论。

    **逐句判，句内才看上下文**：

    1. 先看整句有没有"豁免词"（指南、附件、第X页、原则…）——
       有就放过这一句。那是**引用知识库**，不是给孩子下判断。
    2. 句内同时出现**病名**和**孩子的指称**，才判命中。

    第 2 步是关键："孤独症儿童的缺陷行为要减少"里有病名，
    但没有"这孩子/他/她"这种主语 —— 那是泛指，不是给某个孩子贴标签。
    "这孩子可能有自闭症"两样都有 → 拦。

    **英文单独判**：``autism`` / ``diagnosis`` 在英文里出现就是断言，
    不用这套主语-病名的推断（"the child may have autism" 是明确断言，
    但 "autism is a neurodevelopmental disorder" 是陈述事实 —— 这种
    区别在英文语境里也难做，所以英文保守一点：只要出现就拦，
    和原来的行为一致）。
    """
    for text in texts:
        lowered = text.lower()
        # 英文：见到就拦（保持原行为）
        for pattern in _EN_DIAGNOSTIC_TERMS:
            if pattern in lowered:
                return True
        for sentence in _sentences(text):
            if any(exempt in sentence for exempt in _DIAGNOSTIC_SENTENCE_EXEMPT):
                continue
            # **无条件断言**：这些说法本身就是给某个人下了诊断，
            # 不需要再问"句子里有没有主语和病名"——
            # "先确认他是不是确诊了" 里没有病名，但它就是在谈诊断。
            if any(p in sentence for p in _DIAGNOSTIC_ASSERTIONS):
                return True
            has_condition = any(
                name in sentence for name in _DIAGNOSTIC_CONDITIONS
            )
            has_subject = any(
                subject in sentence for subject in _DIAGNOSTIC_SUBJECTS
            )
            if has_condition and has_subject:
                return True
    return False


# **无条件断言**：出现就是在给某个人下诊断，不需要再推断主语和病名。
#
# 之所以和"主语 + 病名"分开：断言动词本身就够了 ——
# "先确认他是不是确诊了" 没有出现病名，但它就是在谈诊断这件事。
#
# **注意**：这里不能放"确诊患儿""确诊初期"（指南原话里有的说法），
# 那是指"已经被确诊的孩子"这个群体，不是给眼前这个孩子下判断。
_DIAGNOSTIC_ASSERTIONS = (
    "确诊了", "确诊为", "确诊是", "已经确诊", "确诊的",
    "疑似患有", "疑似自闭", "疑似孤独症",
    "患有", "得了", "病人", "患者",
    "是自闭症", "是孤独症", "属于自闭症", "属于谱系",
    "自闭症孩子", "谱系孩子",
)


# 英文诊断词。见到就拦 —— 保持英文书时代的行为。
_EN_DIAGNOSTIC_TERMS = (
    "autism", "autistic", "asperger", "adhd",
    "diagnos", "disorder", "syndrome",
)

# 中文病名
_DIAGNOSTIC_CONDITIONS = (
    "自闭症", "孤独症", "自闭", "谱系障碍", "谱系",
    "发育迟缓", "智力障碍", "唐氏", "语言障碍",
)

# 中文主语：孩子的指称
_DIAGNOSTIC_SUBJECTS = (
    "这孩子", "这个孩子", "你的孩子", "我家孩子", "孩子",
    "他", "她", "幼儿", "宝宝", "患儿", "小孩", "小朋友",
)


def _hits(rule: Rule, texts: list[str]) -> bool:
    blob = " ".join(texts).lower()
    if rule.negations and any(n.lower() in blob for n in rule.negations):
        # "不要忽视孩子的情绪"这种正面用法不算命中
        return False
    for pattern in rule.patterns:
        if pattern.lower() in blob:
            if not rule.also_require:
                return True
            if any(w.lower() in blob for w in rule.also_require):
                return True
    return False


def _has_long_quote(texts: list[str], bundle: EvidenceBundle | None) -> str | None:
    """卡里有没有大段照抄书里的原文。

    版权边界：``may_quote_to_user`` 恒为 False（这本书的授权没核实过），
    所以哪怕是"书里的原话"也不能整段给家长。
    """
    if not bundle:
        return None
    normalized = [_norm(t) for t in texts]
    for item in bundle.evidence:
        excerpt = _norm(item.source_excerpt or "")
        if len(excerpt) < LONG_QUOTE_CHARS:
            continue
        for start in range(0, len(excerpt) - LONG_QUOTE_CHARS, 20):
            window = excerpt[start : start + LONG_QUOTE_CHARS]
            for text in normalized:
                if window in text:
                    return (
                        f"建议里出现了书里原文的连续 {LONG_QUOTE_CHARS} 字以上"
                        f"（证据 {item.evidence_id[:8]}）。这本书的授权未核实，"
                        "只能给摘要和页码，不能给大段原文。"
                    )
    return None


def review_safety(
    cards: list[RecommendationCard],
    bundle: EvidenceBundle | None = None,
    answer_text: str = "",
) -> SafetyReview:
    """规则层的安全审查。纯函数，不调模型。

    ``answer_text`` 是要直接给家长看的文字（有限回答那一路），
    也要过一遍规则 —— 卡片和正文都不能出问题。
    """
    findings: list[str] = []
    rules_hit: list[str] = []
    hard = False
    soft = False

    for card in cards:
        texts = _card_texts(card)
        for rule in ALL_RULES:
            if rule.name == MISATTRIBUTION_RULE.name and (
                card.application_label != "ai_adaptation"
            ):
                continue
            if rule.name == DIAGNOSTIC_RULE.name:
                # 诊断规则走**语境判定**（见 _hits_diagnostic）——
                # 中文指南里"孤独症""谱系"是知识库自己的词汇，
                # 按词判会把所有正经的卡都拦掉。
                hit = _hits_diagnostic(texts)
            elif rule.name == PUNISHMENT_RULE.name:
                # 惩罚规则走**句级判定**（见 _hits_punishment）——
                # 指南自己就讲负惩罚策略，按词判会把附件 2 封掉。
                hit = _hits_punishment(texts)
            else:
                hit = _hits(rule, texts)
            if hit:
                rules_hit.append(rule.name)
                findings.append(f"[{card.plan_id}] {rule.message}")
                if rule.severity == "hard":
                    hard = True
                else:
                    soft = True

        quote = _has_long_quote(texts, bundle)
        if quote:
            rules_hit.append("long_quote")
            findings.append(f"[{card.plan_id}] {quote}")
            hard = True

    # 有限回答那一路没有卡片，正文本身要过规则
    if answer_text:
        answer_texts = [answer_text]
        for rule in ALL_RULES:
            if rule.name == MISATTRIBUTION_RULE.name:
                # 正文里提到"指南"未必是误标 —— 只有卡才按 application_label 判
                continue
            if rule.name == DIAGNOSTIC_RULE.name:
                hit = _hits_diagnostic(answer_texts)
            elif rule.name == PUNISHMENT_RULE.name:
                hit = _hits_punishment(answer_texts)
            else:
                hit = _hits(rule, answer_texts)
            if hit:
                rules_hit.append(rule.name)
                findings.append(f"[回答] {rule.message}")
                if rule.severity == "hard":
                    hard = True
                else:
                    soft = True
        quote = _has_long_quote(answer_texts, bundle)
        if quote:
            rules_hit.append("long_quote")
            findings.append(f"[回答] {quote}")
            hard = True

    if hard:
        result = "blocked"
    elif soft:
        result = "needs_human_review"
    else:
        result = "pass"

    return SafetyReview(
        result=result,
        findings=findings,
        rules_hit=sorted(set(rules_hit)),
    )


# 命中硬规则时给家长的兜底话术。
#
# **为什么不给"修正版建议"**：审查被拦下的原因通常不是某一句话写坏了，
# 而是整张卡的方向有问题（要诊断、要强迫）。临场改写等于放行一个没审过的版本。
# 所以这一路只给一段安全的话，不给任何操作指令。
BLOCKED_FALLBACK = (
    "这条建议没有通过安全检查，我先不给你了 —— "
    "里面可能有不适合你孩子当下情况的说法，或者说得太绝对了。"
    "你可以先做一件最简单的事：把发生的时间、当时的情形、孩子的反应记下来，"
    "下次我们再一起看这些记录。"
    "另外，如果孩子有明显的不适、情绪反复很剧烈，或者你有拿不准的地方，"
    "请找儿童保健医生或儿童心理方面的专业人员面对面评估一次。"
)
