"""记忆的类目，以及"什么时候该合并"的判定。

**为什么要类目**：计划 §10.2 说 L1 稳定档案是"一份'当前怎么看这个孩子'
的快照"，但从来没定义过快照长什么样 —— 结果事实越攒越多，
每次回答都要靠关键词重合度从一堆散条里挑。

类目是那份快照的**目录**：一条记忆只归一类，归完之后同类可以合成一句，
这就是"合并"。

**类目怎么定的**：判据只有一条 ——
**同一句建议，在这个维度的背景不同时写法会变，就单列；不会变的归 other。**

比如"孩子在幼儿园门口哭"和"孩子在家也哭"——
前者决定交接策略，后者决定所有场景的安排，写法完全不同 → 归 ``separation``。
而"孩子今年 5 岁"放进哪个类目都不影响建议写法 → 归 ``other``。

**这个文件是纯规则**：不连库、不调模型。所有判定都能单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 七个类目。**顺序就是给家长看的展示顺序** ——
# 从"最影响建议怎么写"排到"最不影响"。
Category = Literal[
    "separation",
    "communication",
    "social",
    "emotion",
    "sensory_motor",
    "daily",
    "family",
    "other",
]

CATEGORIES: tuple[Category, ...] = (
    "separation",
    "communication",
    "social",
    "emotion",
    "sensory_motor",
    "daily",
    "family",
    "other",
)

# 展示名。给前端和给家长看的。
CATEGORY_LABEL: dict[str, str] = {
    "separation": "分离与过渡",
    "communication": "沟通与表达",
    "social": "社交与游戏",
    "emotion": "情绪与调节",
    "sensory_motor": "感觉与动作",
    "daily": "生活与自理",
    "family": "家庭与目标",
    "other": "其他",
}

# 类目摘要（给 prompt 看的判据）。
#
# **每个类目都要写"它决定建议的什么"** —— 只写"什么内容属于这里"
# 模型会按字面归类，把"幼儿园哭"归到 social 去。
CATEGORY_HINT: dict[str, str] = {
    "separation": "上幼儿园/托班、离开家长、换环境、换老师、交接过程。",
    "communication": "怎么表达需求、能不能听懂指令、发音、主动说话。",
    "social": "目光回应、轮流、分享、同伴和象征性游戏。",
    "emotion": "什么让他崩溃、怎么安抚、多久能缓过来、会不会伤人。",
    "sensory_motor": "对声音/光线/触碰的反应、刻板行为、大小肌肉动作。",
    "daily": "吃饭、穿衣、收拾、等待、遵守规则、作息。",
    "family": "家里情况、谁在带、家长目标和偏好、可接受的取舍。",
    "other": "其余不直接影响建议写法的信息（年龄、诊断史等）。",
}

# 判据：这条记忆的**内容**影响了建议的哪一部分。
#
# **写成"影响什么"而不是"是什么"** —— 归类错的最大原因
# 是按字面主题归（"哭"→emotion），而不是按它在方案里的作用归。
CATEGORY_AFFECTS: dict[str, str] = {
    "separation": "决定怎么安排交接、分离、多久能适应",
    "communication": "决定指令怎么说、说多复杂、要不要示范",
    "social": "决定互动从哪里起手、要几个大人配合",
    "emotion": "决定节奏、防崩预案、什么时候该停",
    "sensory_motor": "决定环境怎么布置、活动用什么形式",
    "daily": "决定能落到哪些具体步骤、一次做几件",
    "family": "决定方案的约束条件和可接受的取舍",
    "other": "不改变建议的写法",
}


# ------------------------------------------------------------------ 合并触发
#
# 合并的门槛是三个数，**调一个就要重跑一堆用例**，所以写死在这里，
# 不散落到各处。
#
# ``MIN_OBSERVATIONS``：少于这么多条，同一个类目下归纳不出"模式" ——
# 两条很可能只是巧合。没到这条线就只做"直接复述"（不调模型）。
MIN_OBSERVATIONS = 3

# 参与归纳的明细必须来自这么多个不同的 turn。
#
# **这是防"假模式"最关键的一条**：家长一段话里能抽出三四条观察
# （"哭了""抱腿""说不想去"），凑够 3 条就归纳，等于把一次事件
# 说成"孩子每次都这样"。跨 turn 才是"多次出现"。
MIN_DISTINCT_TURNS = 2


def should_use_llm(
    bucket: CategoryBucket,
    *,
    min_observations: int = MIN_OBSERVATIONS,
    min_turns: int = MIN_DISTINCT_TURNS,
) -> tuple[bool, str]:
    """这个类目够不够格让模型归纳。返回 ``(够不够, 原因)``。

    **不够格也要出小结** —— 只是内容是明细的直接复述，
    不引入任何新断言。见 ``categories_needing_update``。
    """
    if bucket.count < min_observations:
        return False, f"明细不足（{bucket.count} < {min_observations}）"
    if bucket.distinct_turns < min_turns:
        return False, (
            f"只来自 {bucket.distinct_turns} 轮，"
            f"不足 {min_turns} 轮 —— 同一次事件不算'多次出现'"
        )
    return True, "可以归纳"


@dataclass
class CategoryBucket:
    """一个类目下、等着被合并的明细。"""

    category: str
    # 明细的 memory_id（obs_xxx）
    observation_ids: list[str] = field(default_factory=list)
    # 明细的正文（用来喂归纳 prompt）
    texts: list[str] = field(default_factory=list)
    # 明细来自哪些轮次（去重后的 turn_id）
    turn_ids: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.observation_ids)

    @property
    def distinct_turns(self) -> int:
        return len(self.turn_ids)


def normalize_category(value: str) -> str:
    """把模型给的类目名规整成合法值。认不出来就归 ``other``。

    **不因为"归错类"就丢弃记忆** —— 归类只影响"怎么合、怎么展示"，
    不影响这条记忆本身对不对。丢了才是真丢信息。
    """
    text = (value or "").strip().lower()
    if text in CATEGORY_LABEL:
        return text
    # 模型偶尔会写成中文展示名。
    for key, label in CATEGORY_LABEL.items():
        if text == label:
            return key
    return "other"


def bucket_by_category(
    rows: list[dict[str, object]],
) -> dict[str, CategoryBucket]:
    """按类目把明细分桶。

    ``rows`` 每项要带 ``memory_id`` / ``category`` / ``text`` / ``turn_id``。
    缺 ``category`` 的归 ``other``。
    """
    buckets: dict[str, CategoryBucket] = {}
    for row in rows:
        category = normalize_category(str(row.get("category") or ""))
        bucket = buckets.setdefault(category, CategoryBucket(category=category))
        memory_id = str(row.get("memory_id") or "")
        if not memory_id:
            continue
        bucket.observation_ids.append(memory_id)
        bucket.texts.append(str(row.get("text") or ""))
        turn_id = str(row.get("turn_id") or "")
        if turn_id and turn_id not in bucket.turn_ids:
            bucket.turn_ids.append(turn_id)
    return buckets


def categories_needing_update(
    rows: list[dict[str, object]],
    *,
    covered_ids_by_category: dict[str, set[str]] | None = None,
    min_observations: int = MIN_OBSERVATIONS,
    min_turns: int = MIN_DISTINCT_TURNS,
) -> dict[str, tuple[CategoryBucket, bool, str]]:
    """找出**小结需要更新**的类目。

    返回 ``{类目: (桶, 要不要调模型, 原因)}``。

    **判据是"现有小结有没有覆盖这个类目现在的全部明细"**，
    不是"有没有新增几条"。

    为什么这么定：只按"新增 ≥ 2 条"判断的话，
    **一条新明细会一直落空** —— 它既没被旧小结覆盖，
    又不够触发重算，于是永远进不了 prompt。
    （这是"原始明细不参与方案制定"这条要求最容易漏的地方：
    漏掉一条明细，就是家长说过的一句话白说了。）

    所以规则是：**每个类目的小结必须覆盖该类目当前的全部明细**。
    覆盖了就没事，没覆盖就重算 —— 差一条也算没覆盖。

    覆盖了之后还要不要调模型，是另一回事（``should_use_llm``）：
    不够格就不调，内容是明细的直接复述。
    """
    covered_map = covered_ids_by_category or {}
    buckets = bucket_by_category(rows)
    out: dict[str, tuple[CategoryBucket, bool, str]] = {}
    for category, bucket in buckets.items():
        covered = covered_map.get(category, set())
        current = set(bucket.observation_ids)
        # 两个方向都要看：
        #   current - covered  有明细没被小结覆盖（新的，等着进 prompt）
        #   covered - current  小结引用了已经不存在的明细（家长删了）
        missing = current - covered
        gone = covered - current
        if not missing and not gone:
            continue
        use_llm, reason = should_use_llm(
            bucket,
            min_observations=min_observations,
            min_turns=min_turns,
        )
        if missing:
            reason = f"有 {len(missing)} 条明细没被小结覆盖"
        if gone:
            reason = f"小结里有 {len(gone)} 条明细已被删除，需要重算"
        out[category] = (bucket, use_llm, reason)
    return out


__all__ = [
    "CATEGORIES",
    "CATEGORY_AFFECTS",
    "CATEGORY_HINT",
    "CATEGORY_LABEL",
    "MIN_DISTINCT_TURNS",
    "MIN_OBSERVATIONS",
    "Category",
    "CategoryBucket",
    "bucket_by_category",
    "categories_needing_update",
    "normalize_category",
    "should_use_llm",
]
