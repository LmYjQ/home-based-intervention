# -*- coding: utf-8 -*-
"""
活动库的数据模型与检索器
========================
这是整个 demo 里"看起来像 RAG、但其实不是 RAG"的部分 —— 而且**这正是要点**。

为什么不上向量检索
------------------
50 条活动、字段全部结构化、检索维度全部可判定（时长够不够、材料齐不齐、
难度对不对得上状态、前置做完了没）。这种场景下 embedding 只会带来两个损失：
  1. 多一个模型下载依赖，面试现场多一个能翻车的点
  2. **覆盖不了 essential=False 这类硬约束** —— "材料全都在家"是布尔判断，
     语义相似度算不出这个
真正的检索价值在于结构化过滤的组合爆炸，不是语义匹配。
"我知道什么时候不该上 RAG"本身就是能力展示，面试时这句话可以直接讲。

所以这里的设计是：**硬过滤（带可读的剔除原因）+ 加权打分（带分项得分）**。
两者都保证每个结果都能回答"为什么推荐它 / 为什么没推荐它"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vocab import (
    ABILITY_DOMAIN,
    ABILITY_STATE_CN,
    ALWAYS_AT_HOME,
    BEHAVIOR,
    MATERIAL_CLASSES,
    SCENE,
    SKILL_ITEMS,
    SKILL_ITEM_DOMAIN,
    STATE_TARGET_DIFFICULTY,
)

PROMPT_LEVEL = Literal["natural", "gesture", "verbal", "model", "physical"]
PROMPT_LEVEL_CN = {
    "natural": "自然情境（不额外提示）",
    "gesture": "手势提示",
    "verbal": "口头提示",
    "model": "示范",
    "physical": "手把手辅助",
}

OUTCOME = Literal["success", "partial", "refused", "skipped", "no_data"]


# ================================================================== 数据模型
class Material(BaseModel):
    name: str
    material_class: Literal[tuple(MATERIAL_CLASSES)]
    essential: bool = True   # False = 有更好，没有也能做


class Step(BaseModel):
    """一个教学步骤。ESDM 的核心是把"等待"显式写出来，而不是留给家长自由发挥。"""

    model_config = ConfigDict(extra="forbid")
    step_id: int = Field(ge=1, le=8)
    action: str = Field(description="家长做什么，祈使句")
    child_target_response: str = Field(description="期望孩子出现什么反应")
    time_limit_sec: int | None = Field(
        default=None, ge=1, le=120, description="等待孩子反应的上限秒数；ESDM 的关键参数")
    prompt_level: PROMPT_LEVEL = "natural"


class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str = Field(pattern=r"^[a-z]{2,4}-\d{3}$")   # 如 "soc-014"
    title: str
    domains: list[Literal[tuple(ABILITY_DOMAIN)]] = Field(min_length=1, max_length=2)
    target_skill_items: list[str] = Field(min_length=1)

    # ---- 可执行性约束：检索的硬过滤全靠这些字段 ----
    duration_min: int = Field(ge=2, le=20)
    sessions_per_day: int = Field(ge=1, le=3, default=1)
    # 允许为空：拍手、数数、上下楼这类活动不需要任何道具 ——
    # 而"零准备成本"恰恰是最容易被家长坚持下来的活动，不该被约束逼着塞道具。
    materials: list[Material] = Field(default_factory=list)
    adult_count: Literal[1] = 1        # 单一家长可执行，模板级保证
    setting: list[Literal["indoor_home", "outdoor", "anywhere"]]
    sensory_load: Literal["low", "medium", "high"] = "low"
    prep_min: int = Field(default=0, ge=0, le=10)   # 准备时间，不计入 duration

    # ---- 教学元数据 ----
    # RBT 已从词表移除，见 sources.md §5：中文义=DTT 别称（要求回合结构），
    # 英文义=Registered Behavior Technician（是岗位不是方法），两种下都不该出现。
    # 从枚举里删掉而不只是删数据 —— 留着会让"看起来严谨"的标签继续被塞进新数据。
    method: list[Literal["NDBI", "ESDM", "ABA", "PRT"]] = Field(min_length=1)
    method_rationale: str = Field(description="一句话：为什么这个方法适合这个技能")
    difficulty: Literal[1, 2, 3]      # 1入门 2练习 3泛化
    prerequisites: list[str] = Field(default_factory=list, description="前置 activity_id")
    prerequisite_skills: list[str] = Field(default_factory=list)

    # ---- 教练 Agent 的原料，也是家长卡片的内容 ----
    setup: list[str] = Field(default_factory=list, max_length=3)
    steps: list[Step] = Field(min_length=3, max_length=8)
    success_criteria: list[str] = Field(min_length=1, max_length=3)
    prompt_hierarchy: list[str] = Field(min_length=1, max_length=4)
    error_correction: str
    generalization_cues: list[str] = Field(default_factory=list)

    # ---- 与行为观察的接口：计划 ↔ 行为记录闭环的接缝 ----
    success_behaviors: list[str] = Field(min_length=1)
    refusal_behaviors: list[str] = Field(min_length=1)
    linked_scenes: list[str] = Field(min_length=1)

    @property
    def total_min(self) -> int:
        return self.duration_min * self.sessions_per_day

    @property
    def essential_materials(self) -> list[Material]:
        return [m for m in self.materials if m.essential]

    def fits_time_budget(self, daily_minutes: int) -> bool:
        return self.total_min + self.prep_min <= daily_minutes

    def materials_at_home(self, available: set[str]) -> bool:
        return all(material_available(m.name, available) or m.material_class in ALWAYS_AT_HOME
                   for m in self.essential_materials)

    def brief(self) -> str:
        """给 LLM 看的候选卡 —— 刻意**不含 steps / setup / error_correction**。

        因为 schema 的膨胀是最大的 token 风险之一：活动定义整份塞进 schema
        会让每个候选都带着几十行步骤。这里只给规划器做决策必需的字段，
        完整定义留到教练 Agent 真正要用那一条时再单独取。
        """
        return (f"{self.activity_id} | {self.title} | "
                f"{'/'.join(ABILITY_DOMAIN[d] for d in self.domains)} | "
                f"目标:{'、'.join(self.target_skill_items)} | "
                f"{self.total_min}分钟×{self.sessions_per_day} | "
                f"难度{self.difficulty} | "
                f"材料:{'、'.join(m.name for m in self.materials)} | "
                f"方法:{'+'.join(self.method)}")


# ================================================================== 检索输入输出
# 量词与数量词。比较材料名之前先剥掉，否则"一辆小车"永远匹配不上"小车"。
_MEASURE_CHARS = "一辆一只一张一块一片一根一把一支一条一本一台一份下个次几多"
# 高频通用字。参与字符重叠会带来大量误判（"故事" vs "事故" 都含"事"），先排除。
_STOP_CHARS = set("的和了在是有过也就都还会能")


def material_available(name: str, available: set[str]) -> bool:
    """材料是否可得。三层匹配，从严到宽。

    只用相等比较的话，"小车" 匹配不上活动里的"一辆小车"，
    "只用家里常见物品"这条最关键的约束会在真实输入下失效。

    1. 完全相等 —— 最严，零误判
    2. 双向子串 —— "小车" vs "一辆小车"，"绘本" vs "一本绘本"
    3. 字符重叠 —— "小汽车" vs "一辆小车"。剥掉量词后仍不相交时兜底：
       要求共有实义词 ≥2 个，且重叠率 ≥0.6。
       这层会漏（"水杯" vs "杯子" 共有 1 字，不算匹配），
       **刻意让它漏而不是猜** —— 材料匹配错了会让家长准备一堆没用的东西，
       比少匹配一条活动糟糕得多。
    """
    n = name.strip()
    if not n:
        return False
    if n in available:
        return True

    strip = lambda s: "".join(ch for ch in s if ch not in _MEASURE_CHARS)  # noqa: E731
    nn = strip(n)
    for a in available:
        a = a.strip()
        if not a:
            continue
        if a in n or n in a or (nn and (strip(a) in nn or nn in strip(a))):
            return True

    ns = set(n) - _STOP_CHARS
    for a in available:
        overlap = ns & (set(a.strip()) - _STOP_CHARS)
        if len(overlap) >= 2 and len(overlap) / min(len(n), len(a.strip())) >= 0.6:
            return True
    return False


class PreferenceItem(BaseModel):
    """一条家长录入的喜好。

    四种 kind 的区别在于**能不能被确定性匹配**：
      activity  —— activity_id，能精确匹配
      material  —— 材料名，走 material_available 的子串匹配
      scene     —— SCENE code，能精确匹配
      free_text —— 自由描述，**检索阶段不参与**，只喂给教练 Agent

    free_text 单独拆出来是刻意的：让 LLM 去猜"他喜欢玩水"对应哪个活动，
    等于把排序权交给一个不可复现的黑箱。这里宁可少一分也不做。
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["activity", "material", "scene", "free_text"]
    value: str = Field(min_length=1)
    sentiment: Literal["like", "dislike"]

    @property
    def deterministic(self) -> bool:
        """能不能进检索打分。"""
        return self.kind != "free_text"


class PreferenceSet(BaseModel):
    """家长配置的孩子近期喜好。挂在 ParentConstraints 下（阶段 2 建）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[PreferenceItem] = Field(default_factory=list, max_length=30)
    free_notes: str = Field(
        default="", max_length=500,
        description="家长的一段话描述。只进教练 Agent 的 prompt，检索阶段不解析")

    def likes(self, kind: str | None = None) -> list[str]:
        return [i.value for i in self.items
                if i.sentiment == "like" and (kind is None or i.kind == kind)]

    def dislikes(self, kind: str | None = None) -> list[str]:
        return [i.value for i in self.items
                if i.sentiment == "dislike" and (kind is None or i.kind == kind)]

    def free_texts(self) -> list[str]:
        return [i.value for i in self.items if i.kind == "free_text"]


@dataclass
class PlanRequest:
    """一次检索的全部上下文。刻意做成 dataclass 而不是 Pydantic ——
    它不进任何 schema，是纯 Python 侧的编排数据。"""

    levels: dict[str, int] = field(default_factory=dict)          # skill_item -> 0..5
    target_states: dict[str, str] = field(default_factory=dict)    # domain -> 状态 code
    priority_domains: list[str] = field(default_factory=list)
    daily_minutes: int = 30
    materials_at_home: set[str] = field(default_factory=set)
    avoid_load_sensory: bool = False
    known_refusals: list[str] = field(default_factory=list)       # 家长明说抗拒的标题/材料
    refusal_counts: dict[str, int] = field(default_factory=dict)  # activity_id -> 抗拒次数
    success_counts: dict[str, int] = field(default_factory=dict)
    recent_activity_ids: list[str] = field(default_factory=list)  # 最近 3 天已排的
    success_history_ids: set[str] = field(default_factory=set)    # 历史成功过的（前置判定用）
    recent_scenes: list[str] = field(default_factory=list)        # 最近高频场景（来自记忆库）
    successes_outside_home: dict[str, int] = field(default_factory=dict)  # 泛化判定用
    preferences: PreferenceSet = field(default_factory=PreferenceSet)
    # ★ 刻意**不参与打分**，只进教练 Agent 的 prompt。
    #   它是"这周在往哪走"的语义判断，而打分全是可判定的计数 ——
    #   把一段 LLM 写的话混进 0.30/0.15/0.14 那套权重里，
    #   就等于把检索的正确性交给一个不可复现的字符串。
    #   位置对了，它才有价值：让教练知道"精细动作连着三天没进展"，
    #   然后由教练在**今天做什么**这一层体现调整。
    recent_summary: list[dict] = field(default_factory=list)     # store.latest_summary

    def state_of(self, domain: str) -> str:
        return self.target_states.get(domain, "not_started")


@dataclass
class ScoredActivity:
    activity: Activity
    score: float = 0.0
    passed: bool = True
    hard_filter_reason: str = ""   # 被剔除时给 UI 看的原因（不显示给 LLM）
    breakdown: dict[str, float] = field(default_factory=dict)   # 各分项原始分，供调试


# ================================================================== 硬过滤
def _hard_filter(req: PlanRequest, a: Activity) -> str:
    """返回空串 = 通过；返回非空 = 剔除原因（中文，直接展示给家长）。"""

    if not a.fits_time_budget(req.daily_minutes):
        need = a.total_min + a.prep_min
        return f"超出每日时间预算（需要 {need} 分钟，预算 {req.daily_minutes} 分钟）"

    missing = [m.name for m in a.essential_materials
               if not material_available(m.name, req.materials_at_home)
               and m.material_class not in ALWAYS_AT_HOME]
    if missing:
        return f"家里没有必需材料：{'、'.join(missing)}"

    if req.refusal_counts.get(a.activity_id, 0) >= 2:
        return f"已连续抗拒 {req.refusal_counts[a.activity_id]} 次，进入冷却期"

    missing_prereq = [p for p in a.prerequisites if p not in req.success_history_ids]
    if missing_prereq:
        return f"前置活动尚未完成：{'、'.join(missing_prereq)}"

    # 水平不够就不给高难度活动 —— 这是"因材施教"最硬的一条
    for item in a.target_skill_items:
        lvl = req.levels.get(item)
        if lvl is not None and lvl < 2 and a.difficulty > 1:
            return f"当前水平（{item} {lvl}级）不适合难度{a.difficulty}的活动"

    if req.avoid_load_sensory and a.sensory_load == "high":
        return "感官负荷偏高，该孩子需要避开"

    if a.activity_id in req.recent_activity_ids:
        return "最近 3 天刚练过，避免重复"

    return ""


# ================================================================== 打分
#
# 权重设计的核心约束：**state_match 必须仍是最高项**。
# 家长说"他最喜欢转圈玩"时，转圈是刻板行为，不该因为家长喜欢就排到前面。
# 喜好只能改变"同等适配度"活动之间的排序，永远不能推翻教学判断 ——
# 所以它排在 state_match 之后，且它只能加分，不能让一条被硬过滤掉的活动复活。
#
#        0.30  状态匹配    ← 状态机说了算，不可动摇
#        0.15  技能差距    短板优先
#        0.14  前置就绪
#  +     0.10  喜好命中    ← 新增
#        0.10  新鲜度
#        0.08  材料契合
#        0.08  方法契合
#        0.05  场景贴合
#        ------
#        1.00
WEIGHTS = {
    "state_match":   0.30,
    "skill_gap":     0.15,
    "prereq_ready":  0.14,
    "preference":    0.10,
    "diversity":     0.10,
    "material_fit":  0.08,
    "method_fit":    0.08,
    "parent_pref":   0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "权重之和必须为 1.0"


def _score(req: PlanRequest, a: Activity) -> dict[str, float]:
    """各分项得分，**不乘权重** —— breakdown 保留原始分便于调试和解释。"""

    # 1. 状态匹配：活动难度是否对得上该能区当前状态要求的目标难度。
    #    这是状态机与检索器的接缝 —— 状态不是只用来显示的。
    want = {STATE_TARGET_DIFFICULTY[req.state_of(d)] for d in a.domains}
    got = a.difficulty
    if got in want:
        state_match = 1.0
    elif got == min(want) - 1:
        state_match = 0.6      # 稍微超前一点点，可以接受
    else:
        state_match = 0.3      # 太难或太简单

    # 2. 技能差距：水平越低越该练。0.3 是基线偏移 —— level=0 时 gap=1.3，level=5 时 0.3，
    #    意味着已经掌握的技能也会拿到 0.1 分，不会完全被遗忘。
    gaps = []
    for item in a.target_skill_items:
        lvl = req.levels.get(item)
        if lvl is None:
            gaps.append(1.0)   # 报告没提到 = 未知 = 值得探一下
        else:
            gaps.append(max(0.0, min(1.5, 1 - lvl / 5 + 0.3)))
    skill_gap = min(1.0, sum(gaps) / 3.0)

    # 3. 前置就绪：硬过滤已经剔掉前置未完成的，这里给"部分前置已做"的补偿分
    if not a.prerequisites:
        prereq_ready = 1.0
    else:
        done = sum(1 for p in a.prerequisites if p in req.success_history_ids)
        prereq_ready = 0.5 + 0.5 * done / len(a.prerequisites)

    # 4. 材料契合：essential 满足记满分，optional 满足记半分。
    #    零道具活动记 1.0 —— 不需要准备正是这类活动的优势，不该因为分母为 0 崩掉。
    if not a.materials:
        material_fit = 1.0
    else:
        mat = sum((1.0 if (material_available(m.name, req.materials_at_home)
                           or m.material_class in ALWAYS_AT_HOME)
                   else (0.5 if not m.essential else 0.0)) for m in a.materials)
        material_fit = mat / len(a.materials)

    # 5. 新鲜度：已经成功很多次的降权，避免孩子练腻
    diversity = 1.0 - min(1.0, req.success_counts.get(a.activity_id, 0) / 4)

    # 6. 方法契合：命中家长优先关注的能力区加分
    method_fit = 0.5 if set(a.domains) & set(req.priority_domains) else 0.1

    # 7. 场景贴合：这个活动常发生的场景，和孩子近期的活动场景重合就加分
    parent_pref = 1.0 if set(a.linked_scenes) & set(req.recent_scenes) else 0.5

    # 8. 喜好命中：家长明确说喜欢的东西。只认三种确定性可匹配的 kind
    #    （activity / material / scene），free_text 不参与 —— 见 PreferenceItem 的说明。
    pref = _preference_score(req.preferences, a)

    return {
        "state_match": state_match,
        "skill_gap": skill_gap,
        "prereq_ready": prereq_ready,
        "material_fit": material_fit,
        "diversity": diversity,
        "method_fit": method_fit,
        "parent_pref": parent_pref,
        "preference": pref,
    }


def _preference_score(pref: PreferenceSet, a: Activity) -> float:
    """喜好命中度 0.0–1.0。

    命中活动本身 = 1.0；命中材料或场景 = 0.7（间接命中）。
    不喜欢不在这函数里处理 —— 不喜欢走 negative_penalty()，
    因为"扣分"和"加分"是两套语义，混在一起会让 breakdown 难以解释。
    """
    likes_activity = a.activity_id in pref.likes("activity")
    if likes_activity:
        return 1.0

    liked_materials = pref.likes("material")
    liked_scenes = pref.likes("scene")
    hit_material = any(material_available(m.name, {x}) for m in a.materials
                       for x in liked_materials)
    hit_scene = bool(set(a.linked_scenes) & set(liked_scenes))
    if hit_material or hit_scene:
        return 0.7
    return 0.0


def negative_penalty(pref: PreferenceSet, a: Activity, known_refusals: list[str]) -> float:
    """不喜欢的扣分。返回负值，供 retrieve() 累加到 raw score 上。

    同样**不硬剔除**：一条活动被家长标了不喜欢但教学上完全合适时，
    正确做法是排到最后并说明原因，而不是静悄悄地消失 —— 家长需要知道
    我们听到了他的偏好，以及为什么这条还是出现了。
    """
    pen = 0.0
    if a.activity_id in pref.dislikes("activity"):
        pen -= 1.0
    else:
        for m in a.materials:
            if any(material_available(m.name, {x}) for x in pref.dislikes("material")):
                pen -= 0.6
                break
        if set(a.linked_scenes) & set(pref.dislikes("scene")):
            pen -= 0.4
    for kw in known_refusals:
        if kw.strip() and (kw in a.title or any(kw in m.name for m in a.materials)):
            pen -= 1.0
            break
    return pen


def retrieve(req: PlanRequest, *, pool: list[Activity] | None = None) -> list[ScoredActivity]:
    """返回**全部**活动，带 passed 标记和分项得分。

    刻意不做 top_k 截断 —— 被剔除的活动带着原因一并返回，前端可以显示
    "因为您说抗拒 XX，所以今天没推这条"。让检索结果可解释，比给一个数字有用。
    """
    from activity_data import load_activities
    pool = pool if pool is not None else load_activities()

    out: list[ScoredActivity] = []
    for a in pool:
        reason = _hard_filter(req, a)
        if reason:
            out.append(ScoredActivity(activity=a, passed=False,
                                      hard_filter_reason=reason))
            continue
        bd = _score(req, a)
        raw = sum(WEIGHTS[k] * v for k, v in bd.items())

        # 家长标了不喜欢的东西：不硬剔除，而是扣分留在榜上。
        # 这样前端能解释"因为您说喜欢 XX，所以今天没推这条"，而不是静悄悄地消失。
        raw += negative_penalty(req.preferences, a, req.known_refusals)

        out.append(ScoredActivity(activity=a, score=round(raw, 4), passed=True, breakdown=bd))

    out.sort(key=lambda s: (not s.passed, -s.score))
    return out


def _hits_known_refusal(req: PlanRequest, a: Activity) -> bool:
    for kw in req.known_refusals:
        kw = kw.strip()
        if not kw:
            continue
        if kw in a.title:
            return True
        if any(kw in m.name for m in a.materials):
            return True
    return False


def preferred_hooks(pref: PreferenceSet, a: Activity) -> list[str]:
    """这条活动上可以挂的"动机钩子" —— 家长说他喜欢、而这条活动正好用得到的东西。

    **这是喜好真正的价值所在。** 检索排序只决定"今天做哪条"，但家长能不能
    坚持做下去，取决于孩子看到那条活动时有没有熟悉的、喜欢的东西垫在里面。
    教练 Agent 拿这个列表去写 why_today 和步骤措辞：
        "用他最喜欢的小汽车当诱饵做共同注意"
    一次命中返回一项。匹配不上的活动返回空列表 —— 不硬凑。
    """
    hooks: list[str] = []
    for x in pref.likes("material"):
        for m in a.materials:
            if material_available(m.name, {x}):
                hooks.append(f"他喜欢的「{m.name}」")
                break
    for s in pref.likes("scene"):
        if s in a.linked_scenes and s in SCENE:
            hooks.append(f"他喜欢的「{SCENE[s]}」场景")
    for ft in pref.free_texts():
        # 自由描述不做解析，只作为提示词里的背景信息传给教练
        hooks.append(ft)
    return hooks


def render_preferences(pref: PreferenceSet, *, limit: int = 8) -> str:
    """渲染成给教练 Agent 看的 prompt 片段。"""
    lines: list[str] = []
    likes = pref.likes()
    dislikes = pref.dislikes()
    if likes:
        lines.append("【孩子最近喜欢的】")
        for x in likes:
            lines.append(f"  · {x}")
    if pref.free_notes:
        lines.append("【家长的原话】")
        lines.append(f"  {pref.free_notes}")
    if dislikes:
        lines.append("【孩子最近不喜欢的】")
        for x in dislikes:
            lines.append(f"  · {x}")
    if not lines:
        return "（家长尚未提供喜好信息）"
    lines.append("")
    lines.append("请在这些喜好被用得上时，体现在 why_today 和步骤措辞里 —— "
                 "用孩子喜欢的东西当诱饵，比生硬地要求有效得多。")
    return "\n".join(lines)


def candidates(req: PlanRequest, *, top_k: int = 12, pool=None) -> list[ScoredActivity]:
    """只要通过硬过滤的，按分数取前 top_k。给规划器当候选表用。"""
    passed = [s for s in retrieve(req, pool=pool) if s.passed]
    return passed[:top_k]


def explain_retrieval(req: PlanRequest, s: ScoredActivity) -> str:
    """给家长看的一句话：为什么推它。**这是整个 demo 最能打动面试官的一行输出。**"""
    a = s.activity
    if not s.passed:
        return f"没推荐：{a.hard_filter_reason}"

    bits = []
    dom = "/".join(ABILITY_DOMAIN[d] for d in a.domains)
    st = req.state_of(a.domains[0])
    bits.append(f"{dom}·当前{ABILITY_STATE_CN.get(st, st)}")
    bits.append(f"难度{a.difficulty}匹配")

    if all(material_available(m.name, req.materials_at_home)
           or m.material_class in ALWAYS_AT_HOME for m in a.essential_materials):
        bits.append("材料全部在家")

    gaps = [f"{it}({req.levels.get(it)}级)" for it in a.target_skill_items
            if req.levels.get(it) is not None and req.levels[it] <= 2]
    if gaps:
        bits.append("短板：" + "、".join(gaps))

    if req.refusal_counts.get(a.activity_id, 0) == 0:
        bits.append("此前未被抗拒")

    if set(a.linked_scenes) & set(req.recent_scenes):
        cn = [SCENE[s] for s in a.linked_scenes if s in SCENE and s in req.recent_scenes]
        if cn:
            bits.append(f"贴合近期场景：{cn[0]}")

    return " · ".join(bits)


# ================================================================== 自检
def validate_all(pool: list[Activity] | None = None) -> list[str]:
    """数据完整性检查。`python activities.py --validate` 的实现。

    这条自检挡掉 90% 的手误：引用了不存在的 skill_item、引用了不存在的
    activity_id、写了词表里没有的 behavior code —— 这些在运行时才会炸，
    而且报错信息很难懂。50 条数据手工维护，没有这层检查一定会出错。
    """
    from activity_data import load_activities
    pool = pool if pool is not None else load_activities()
    errs: list[str] = []
    ids = {a.activity_id for a in pool}

    def err(aid: str, msg: str) -> None:
        errs.append(f"{aid}: {msg}")

    for a in pool:
        for d in a.domains:
            if d not in ABILITY_DOMAIN:
                err(a.activity_id, f"未知能区 {d}")
        for item in a.target_skill_items:
            dom = SKILL_ITEM_DOMAIN.get(item)
            if dom is None:
                err(a.activity_id, f"未知技能细项 {item!r}（不在任何能区的 SKILL_ITEMS 里）")
            elif item not in SKILL_ITEMS[dom]:
                err(a.activity_id, f"技能细项 {item!r} 不属于它声称的能区 {d}")
            elif dom not in a.domains:
                # ★ 这条检查以前是缺的。原先只验证"细项在自己的能区里"，
                # 却不验证"细项的能区被这条活动声明了" —— 于是 cog-006
                # （domains=["cognition"] 却练语言的两步指令）一路畅通到运行时。
                # 后果：state_match 拿活动的 domains 去查目标难度，
                # 算的是 cognition 的难度，而实际练的是语言的技能 —— 打分完全错位。
                err(a.activity_id,
                    f"技能细项 {item!r} 属于「{ABILITY_DOMAIN[dom]}」，"
                    f"但这条活动只声明了 {'/'.join(ABILITY_DOMAIN[x] for x in a.domains)}")
        for item in a.prerequisite_skills:
            if item not in {i for v in SKILL_ITEMS.values() for i in v}:
                err(a.activity_id, f"未知前置技能 {item!r}")
        for m in a.materials:
            if m.material_class not in MATERIAL_CLASSES:
                err(a.activity_id, f"未知材料类别 {m.material_class}")
        for b in a.success_behaviors:
            if b not in BEHAVIOR:
                err(a.activity_id, f"success_behavior 不在词表：{b}")
        for b in a.refusal_behaviors:
            if b not in BEHAVIOR:
                err(a.activity_id, f"refusal_behavior 不在词表：{b}")
        for s in a.linked_scenes:
            if s not in SCENE:
                err(a.activity_id, f"linked_scene 不在词表：{s}")
        for p in a.prerequisites:
            if p == a.activity_id:
                err(a.activity_id, "自环依赖")
            elif p not in ids:
                err(a.activity_id, f"前置 activity_id 不存在：{p}")

    # 依赖图不得有环
    by_id = {a.activity_id: a for a in pool}
    state: dict[str, int] = {}
    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            errs.append(f"依赖成环：{' -> '.join(stack + [n])}")
            return
        state[n] = 1
        for p in by_id[n].prerequisites if n in by_id else []:
            visit(p, stack + [n])
        state[n] = 2
    for aid in ids:
        visit(aid, [])

    # 难度不得高于其前置活动的难度
    for a in pool:
        for p in a.prerequisites:
            if p in by_id and a.difficulty < by_id[p].difficulty:
                err(a.activity_id, f"难度{a.difficulty}低于前置 {p} 的难度{by_id[p].difficulty}")

    return errs


def _cli() -> None:
    import sys
    args = sys.argv[1:]
    if "--validate" in args:
        pool = load_all_for_cli()
        errs = validate_all(pool)
        print(f"活动库：{len(pool)} 条")
        print(f"  能区分布：" + "  ".join(
            f"{ABILITY_DOMAIN[d]}={sum(1 for a in pool if d in a.domains)}"
            for d in ABILITY_DOMAIN))
        # 方法清单从数据里推导，不写死 —— 写死过一次，
        # 撤掉 RBT 之后就少统计一列而不报错，那种"少了一列"没人会注意到。
        used_methods = sorted({m for a in pool for m in a.method})
        print(f"  方法分布：" + "  ".join(
            f"{m}={sum(1 for a in pool if m in a.method)}" for m in used_methods))
        if errs:
            print(f"\n✗ {len(errs)} 处问题：")
            for e in errs:
                print("   " + e)
            sys.exit(1)
        print("\n✓ 全部通过：技能细项/材料类别/行为词表/场景词表/依赖图 均无问题")
        return

    if "--show" in args:
        aid = args[args.index("--show") + 1]
        pool = {a.activity_id: a for a in load_all_for_cli()}
        a = pool.get(aid)
        if not a:
            print(f"没有这条活动：{aid}")
            sys.exit(1)
        print(f"{a.activity_id}  {a.title}")
        print(f"  能区      ：{'/'.join(ABILITY_DOMAIN[d] for d in a.domains)}")
        print(f"  目标技能  ：{'、'.join(a.target_skill_items)}")
        print(f"  时长      ：{a.total_min} 分钟 × {a.sessions_per_day} 次"
              f"（准备 {a.prep_min} 分钟）")
        print(f"  材料      ：{'、'.join(f'{m.name}[{m.material_class}]' + ('' if m.essential else '(非必需)') for m in a.materials)}")
        print(f"  方法      ：{'+'.join(a.method)} —— {a.method_rationale}")
        print(f"  难度      ：{a.difficulty}    感官负荷：{a.sensory_load}")
        if a.prerequisites:
            print(f"  前置活动  ：{'、'.join(a.prerequisites)}")
        print(f"\n  准备：")
        for i, s in enumerate(a.setup, 1):
            print(f"    {i}. {s}")
        print(f"  步骤：")
        for st in a.steps:
            w = f"等待{st.time_limit_sec}秒" if st.time_limit_sec else ""
            lv = PROMPT_LEVEL_CN[st.prompt_level]
            print(f"    {st.step_id}. {st.action} → 期望: {st.child_target_response}"
                  f"  [{lv} {w}]".rstrip())
        print(f"  成功判据：")
        for c in a.success_criteria:
            print(f"    · {c}")
        print(f"  提示阶梯：")
        for i, p in enumerate(a.prompt_hierarchy, 1):
            print(f"    {i}. {p}")
        print(f"  出错处理：{a.error_correction}")
        if a.generalization_cues:
            print(f"  泛化提示：{'、'.join(a.generalization_cues)}")
        print(f"\n  成功行为（行为词表）：{'、'.join(a.success_behaviors)}")
        print(f"  抗拒行为（行为词表）：{'、'.join(a.refusal_behaviors)}")
        print(f"  适用场景（场景词表）：{'、'.join(SCENE[s] for s in a.linked_scenes)}")
        return

    print(__doc__)
    print("用法：python activities.py --validate | python activities.py --show soc-014")


def load_all_for_cli() -> list[Activity]:
    from activity_data import load_activities
    return load_activities()


if __name__ == "__main__":
    _cli()


# ================================================================== PlanRequest 序列化
def request_to_dict(req: PlanRequest) -> dict:
    """`PlanRequest` → **纯 dict**（给 checkpoint 用）。

    ★ 为什么必须转：LangGraph 的 checkpointer 用 msgpack 序列化 state，
      而 `PlanRequest` 是 dataclass、`PreferenceSet` 是 Pydantic 模型 ——
      两者都"不在默认允许的模块里"。实测存进去时已经打出警告：
      `Deserializing unregistered type activities.PlanRequest from checkpoint.
      This will be blocked in a future version.`
      也就是说**今天能读、明天可能直接 raise**，而 checkpoint 的寿命比代码长。

      与 `graph/state.py` 里 `observation` 存 dict 而不是对象是同一条理由，
      只是这里更隐蔽：对象存在时序列化看着是成功的。

    两个集合字段（`materials_at_home` / `success_history_ids`）转成 list，
    反序列化时再转回 set —— 检索器里它们只被 `in` / 差集用，
    而把 set 存成 list 会让"集合相等"这个判据变得不可靠
    （`{'a'}` 与 `['a']` 在 msgpack 里是两个不同的值）。
    """
    return {
        "levels": dict(req.levels),
        "target_states": dict(req.target_states),
        "priority_domains": list(req.priority_domains),
        "daily_minutes": req.daily_minutes,
        "materials_at_home": sorted(req.materials_at_home),
        "avoid_load_sensory": req.avoid_load_sensory,
        "known_refusals": list(req.known_refusals),
        "refusal_counts": dict(req.refusal_counts),
        "success_counts": dict(req.success_counts),
        "recent_activity_ids": list(req.recent_activity_ids),
        "success_history_ids": sorted(req.success_history_ids),
        "recent_scenes": list(req.recent_scenes),
        "successes_outside_home": dict(req.successes_outside_home),
        "preferences": req.preferences.model_dump(mode="json"),
        "recent_summary": list(req.recent_summary),
    }


def request_from_dict(d: dict) -> PlanRequest:
    """`request_to_dict` 的逆。**老 checkpoint 缺字段时取默认值** ——
    加字段不能让半年前那次 run 读不出来。"""
    return PlanRequest(
        levels=d.get("levels") or {},
        target_states=d.get("target_states") or {},
        priority_domains=d.get("priority_domains") or [],
        daily_minutes=int(d.get("daily_minutes") or 30),
        materials_at_home=set(d.get("materials_at_home") or []),
        avoid_load_sensory=bool(d.get("avoid_load_sensory")),
        known_refusals=list(d.get("known_refusals") or []),
        refusal_counts=d.get("refusal_counts") or {},
        success_counts=d.get("success_counts") or {},
        recent_activity_ids=list(d.get("recent_activity_ids") or []),
        success_history_ids=set(d.get("success_history_ids") or []),
        recent_scenes=list(d.get("recent_scenes") or []),
        successes_outside_home=d.get("successes_outside_home") or {},
        preferences=PreferenceSet.model_validate(d.get("preferences") or {}),
        recent_summary=list(d.get("recent_summary") or []),
    )
