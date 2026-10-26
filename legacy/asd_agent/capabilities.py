# -*- coding: utf-8 -*-
"""
能力画像的数据模型
==================
把一份**医生的评估报告**变成一份**结构化、可判定、会被每天读改写的能力画像**。

三条设计约束
------------
1. **`source` 字段是整套设计的地基。**
   报告上写"能听懂单词"和模型从"语言发育落后"四个字里推出"大概能听懂单词"，
   是两种性质完全不同的知识。混淆它们的代价是：状态机拿着一条**编出来的**起点
   开始往上爬，而它爬得越快越显得系统聪明 —— 直到某天家长发现全错。
   所以：报告没直说的，一律 `inferred` + `confidence < 0.4`，并由状态机
   拿 confidence 当升级闸门。

2. **画像 ≠ 家长约束。**
   `AbilityProfile` 是"医生评估出来的能力"，
   `ParentConstraints` 是"家长这周的条件"。来源不同、置信度不同、可变性不同
   （画像一个多月改一次，家长约束每天变），混在一起 `overall_confidence` 就失去意义。

3. **模型只被允许做"读报告"，不被允许做"算数"。**
   等级是模型给的（语义判断），但 `age_equivalent_months` 这类需要算的东西不给，
   缺就填 null —— 逼出一个估算值比留个空洞危害大。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from activities import PreferenceSet
from vocab import ABILITY_DOMAIN, SKILL_ITEMS, SKILL_ITEM_DOMAIN

Domain = Literal[tuple(ABILITY_DOMAIN)]  # type: ignore[valid-type]

# 等级的语义。与 vocab.STATE_TARGET_DIFFICULTY 呼应但不重合：
# level 描述"他现在能做到什么程度"，state 描述"这项能力练到哪一步了"。
LEVEL_CN = {
    0: "不会",
    1: "全身协助下能做",
    2: "部分协助下能做",
    3: "能独立完成，但慢",
    4: "能独立稳定完成",
    5: "能迁移到新场景/新形式",
}
INFERRED_MAX_CONFIDENCE = 0.35   # 推断类条目的置信度上限，必须 <0.4 才有"不是报告事实"的语义

LEVEL_HINT = (
    "0=从来不做；1=全身协助下能做；2=部分协助下能做；3=能独立完成但慢；"
    "4=能独立稳定完成；5=换到新场景/新形式也能用。"
    "**0 的门槛很高**：只有报告明确说'从不''完全没有''不会'时才给 0。"
    "见到'偶有''有时''短暂''不稳定''需要提醒'这类描述，一律给 1 或 2 —— "
    "偶尔能做到就是会，只是还不稳。把偶有判成 0 会直接让难度闸门把这条技能的所有"
    "进阶活动全部挡掉，系统会退化成永远只推最简单的活动。"
    "报告没明确说到的等级，宁可给低一级并标 inferred，也不要给高。"
)


class SkillAssessment(BaseModel):
    """一个技能细项的评估结论。"""

    model_config = ConfigDict(extra="forbid")

    domain: Domain = Field(description="所属能区 code")
    skill_item: str = Field(
        min_length=1, max_length=20,
        description="技能细项，必须逐字取自给你的技能细项清单，且属于该能区",
    )
    level: int = Field(ge=0, le=5, description=LEVEL_HINT)
    age_equivalent_months: int | None = Field(
        default=None, ge=0, le=120,
        description="报告里明确写的等效月龄；没写就填 null，不要自己折算",
    )
    evidence_quote: str = Field(
        default="",
        description="报告原文里的连续片段，逐字复制。只要报告没有针对这个细项的"
                    "明确描述就填空串 —— 填空串表示'这是推断'，编一句会被下面回指校验拦下",
    )
    source: Literal["report_stated", "inferred"] = Field(
        description="report_stated=报告明确写到这一项；inferred=由笼统描述推断而来"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="source=inferred 时必须 <0.4。这个值是状态机的升级闸门",
    )

    @model_validator(mode="after")
    def _enforce_invariants(self) -> "SkillAssessment":
        """两条不变量，都在 Python 侧强制（JSON Schema 表达不了跨字段约束）。

        ★ 两条的**处理方式刻意不同**，理由是失败的代价不对称：

        1. skill_item 不在词表里 → **硬报错**。
           这是真的编造，下游无法安全修复，必须让 llm.py 的修复重试去纠正它。
           （而"能区与细项不匹配"这种软一点的情况可以直接纠正，见下。）

        2. inferred 却给了高 confidence → **静默压到 0.35，不报错**。
           这条规则要保证的只是"推断出来的能力不得充当升级依据"，
           压低就完全满足。而报错的后果是：整份能力画像作废、落进降级路径、
           家长拿到的全 0 起始画像 —— 为了一个软数据质量问题付出这么大代价，
           是把两件事的量级搞反了。实测 7B 在这里几乎必然踩中（它倾向给所有条目
           同样的高 confidence），所以报错会让降级路径变成常态路径。
           prompt 里仍然要求 <0.4，钳制只是兜底。
        """
        # ---- 1. 词表外的细项：真编造，硬报错 ----
        real_domain = SKILL_ITEM_DOMAIN.get(self.skill_item)
        if real_domain is None:
            raise ValueError(
                f"技能细项 {self.skill_item!r} 不在任何能区的 SKILL_ITEMS 里，"
                f"这是编造的技能名"
            )

        # ---- 1b. 细项合法但能区填错：可以确定地纠正，不该丢数据 ----
        if real_domain != self.domain:
            object.__setattr__(self, "domain", real_domain)

        # ---- 2. inferred 的置信度钳制 ----
        # 用 >= 而非 >：0.40 恰好卡在"不是报告事实"的语义边界上，放它过去
        # 就等于让一条推断条目混进直述条目里而不被识别。
        if self.source == "inferred" and self.confidence >= 0.4:
            object.__setattr__(self, "confidence", INFERRED_MAX_CONFIDENCE)
        return self

    @property
    def key(self) -> str:
        """跨天稳定的唯一键。画像每天被读改写，key 变了状态机就断了。"""
        return self.skill_item

    def describe(self) -> str:
        cn = ABILITY_DOMAIN.get(self.domain, self.domain)
        return (f"{cn}/{self.skill_item} L{self.level}({LEVEL_CN[self.level]}) "
                f"[{'报告直述' if self.source == 'report_stated' else '推断'}"
                f" {self.confidence:.2f}]")


class ParentConstraints(BaseModel):
    """家长这周的实际条件。与 AbilityProfile 分开的原因见模块说明。"""

    model_config = ConfigDict(extra="forbid")

    daily_minutes_available: int = Field(
        ge=5, le=180, default=30,
        description="家长每天真正能挤出来的时间（分钟）。宁可往低了填，"
                    "计划排太满是这类工具被弃用的头号原因",
    )
    materials_at_home: list[str] = Field(
        default_factory=list, max_length=30,
        description="家里现成的材料/物品，如 杯子、绘本、积木、小汽车",
    )
    alone_only: bool = Field(
        default=True, description="是否只有一位家长能带（True=不需要第二个人配合）",
    )
    known_refusals: list[str] = Field(
        default_factory=list, max_length=10,
        description="家长明说孩子抗拒的东西（活动名或材料名）",
    )
    preferences: PreferenceSet = Field(
        default_factory=PreferenceSet,
        description="孩子近期喜好。刻意不并进 assessments —— 那是医生评估出来的能力，"
                    "喜好是家长这周观察到的偏好，来源和可变性都不同",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_sensory(cls, data: Any) -> Any:
        """丢掉老画像里的 `avoid_load_sensory`（这条约束已下线）。

        ★ 为什么必须显式丢，而不是"留着字段但不用它"：
          `ParentConstraints` 是 `extra="forbid"`，而老画像的 payload 里
          带着这个键 —— 不丢的话，**整份画像读不出来**（ValidationError），
          而画像读不出来意味着这个孩子当天没有计划、没有卡片。
          一个下线的功能不该有能力把老数据变成不可读。

        丢在**校验之前**而不是之后：Pydantic 的 extra 判定在字段解析前，
          到不了"字段不存在就跳过"那一步。
        """
        if isinstance(data, dict) and "avoid_load_sensory" in data:
            data = {k: v for k, v in data.items() if k != "avoid_load_sensory"}
        return data


class AbilityProfile(BaseModel):
    """一个孩子的能力画像。会随新的评估报告被整体替换。"""

    model_config = ConfigDict(extra="forbid")

    child_id: str = Field(min_length=1, max_length=40)
    child_age_months: int = Field(ge=0, le=240, description="评估时的月龄")
    report_date: str | None = Field(
        default=None, max_length=20, description="评估报告日期 YYYY-MM-DD；报告上没写就填 null",
    )
    assessments: list[SkillAssessment] = Field(
        min_length=1, max_length=40,
        description="评估结论。**只写报告真正涉及到的细项** —— "
                    "报告没提到的细项应当缺席，而不是被填一个 level=0。"
                    "填 0 等于断言'他不会'，而'报告没评估'和'评估为不会'是两件事",
    )
    priority_domains: list[Domain] = Field(
        default_factory=list, max_length=3,
        description="优先干预的能区，最多 3 个。超过 3 个等于没有优先级",
    )
    parent_constraints: ParentConstraints = Field(default_factory=ParentConstraints)
    overall_confidence: float = Field(
        ge=0.0, le=1.0,
        description="整份画像的可信度。取各条 confidence 的加权平均（直述的权重更高）。"
                    "低于 0.5 时状态机禁止升级",
    )
    needs_clarification: bool = Field(
        default=False, description="证据不足以给出可靠画像时置 true",
    )
    clarifying_question: str | None = Field(
        default=None, max_length=200,
        description="needs_clarification 为 true 时，向家长追问的那一个最关键的问题",
    )

    # ---------------------------------------------------------------- 查询
    def assessment_of(self, skill_item: str) -> SkillAssessment | None:
        for a in self.assessments:
            if a.skill_item == skill_item:
                return a
        return None

    def levels(self) -> dict[str, int]:
        """skill_item -> level。直接喂给 PlanRequest。"""
        return {a.skill_item: a.level for a in self.assessments}

    def domains_of_interest(self) -> list[str]:
        """需要干预的能区。没显式给 priority_domains 就取出现过的全部能区。"""
        return self.priority_domains or sorted({a.domain for a in self.assessments})

    def confidence_of(self, skill_item: str) -> float:
        a = self.assessment_of(skill_item)
        return a.confidence if a else 0.0

    def stated_domains(self) -> list[str]:
        return sorted({a.domain for a in self.assessments if a.source == "report_stated"})

    def render(self, *, limit: int = 24) -> str:
        """给 prompt 用的紧凑渲染。**不是**给家长看的。"""
        lines = [f"孩子月龄 {self.child_age_months}"
                 + (f"，报告日期 {self.report_date}" if self.report_date else "")
                 + f"，画像可信度 {self.overall_confidence:.2f}"]
        lines.append("优先能区：" + ("、".join(ABILITY_DOMAIN[d] for d in self.domains_of_interest())
                                   or "（未指定）"))
        lines.append("评估结论：")
        for a in self.assessments[:limit]:
            mark = "报告直述" if a.source == "report_stated" else "推断"
            lines.append(f"  · {a.describe()}" + (f"  ←「{a.evidence_quote[:30]}」" if a.evidence_quote else ""))
        if len(self.assessments) > limit:
            lines.append(f"  · …另有 {len(self.assessments) - limit} 项")
        return "\n".join(lines)


def default_overall_confidence(assessments: list[SkillAssessment]) -> float:
    """整体可信度 = 直述条目加权后与推断条目取加权平均。

    直述的权重是推断的 3 倍。全体都是 inferred 时整体压到 0.35 以下，
    状态机随即禁止该画像下任何技能升级 —— 这是"宁可不动也不要错爬"的落点。
    """
    if not assessments:
        return 0.0
    num = sum(a.confidence * (3.0 if a.source == "report_stated" else 1.0) for a in assessments)
    den = sum(3.0 if a.source == "report_stated" else 1.0 for a in assessments)
    return round(num / den, 4)


def skill_item_domain(skill_item: str) -> str | None:
    return SKILL_ITEM_DOMAIN.get(skill_item)


def _cli() -> None:
    import json
    print(json.dumps(AbilityProfile.model_json_schema(), ensure_ascii=False, indent=2)[:1200])
    print("\n...\n")
    p = AbilityProfile(
        child_id="demo-001",
        child_age_months=34,
        report_date="2026-09-20",
        assessments=[
            SkillAssessment(domain="language", skill_item="听懂单词", level=3,
                            evidence_quote="能听懂简单的名词性指令",
                            source="report_stated", confidence=0.85),
            SkillAssessment(domain="social_adapt", skill_item="共同注意", level=2,
                            source="inferred", confidence=0.3),
        ],
        priority_domains=["social_adapt", "language"],
        # 整体可信度**不由模型给** —— 它是各条 confidence 的加权平均，
        # 算数的事交给 Python，见 default_overall_confidence()。
        overall_confidence=default_overall_confidence([
            SkillAssessment(domain="language", skill_item="听懂单词", level=3,
                            source="report_stated", confidence=0.85),
            SkillAssessment(domain="social_adapt", skill_item="共同注意", level=2,
                            source="inferred", confidence=0.3),
        ]),
    )
    print(p.render())
    print(f"\nlevels = {p.levels()}")
    print(f"需要干预的能区 = {p.domains_of_interest()}")
    print("\n-- 不变量的两种处理方式 --")
    # A. 编造的技能名 → 硬报错
    try:
        SkillAssessment(domain="language", skill_item="社交主动性", level=2,
                        source="report_stated", confidence=0.8)
        print("  ✗ 编造的技能名竟然通过了")
    except Exception as e:
        print(f"  ✓ 编造的技能名被拒：{str(e).splitlines()[1].strip()[:70]}")

    # B. 能区填错 → 自动纠正（细项唯一，能区可确定地还原）
    fixed = SkillAssessment(domain="language", skill_item="踢球", level=2,
                            source="report_stated", confidence=0.8)
    print(f"  ✓ 能区填错自动纠正：language/踢球 → {fixed.domain}/踢球")

    # C. inferred 却高置信 → 钳制而非报错
    clamped = SkillAssessment(domain="language", skill_item="听懂双指令", level=1,
                              source="inferred", confidence=0.95)
    print(f"  ✓ inferred 高置信被钳制：0.95 → {clamped.confidence}"
          f"（仍 <0.4，状态机闸门可识别）")

    # D. level 越界 → 硬报错（JSON Schema 已拦，这里是第二道）
    try:
        SkillAssessment(domain="language", skill_item="听懂单词", level=9)
        print("  ✗ level=9 竟然通过了")
    except Exception as e:
        print(f"  ✓ level 越界被拒：{str(e).splitlines()[1].strip()[:60]}")

    # E. ★ 老画像 payload 里带着已下线的 avoid_load_sensory —— 仍要读得出来。
    #    `extra="forbid"` 会让整份画像 ValidationError，而画像读不出来
    #    = 这个孩子当天没有计划 —— 一个下线的功能不该有能力把老数据变成不可读。
    legacy = json.loads(p.model_dump_json())
    legacy["parent_constraints"] = {"daily_minutes_available": 30,
                                     "materials_at_home": ["杯子"],
                                     "avoid_load_sensory": True}
    try:
        back = AbilityProfile.model_validate(legacy)
        print("  ✓ ★ 老画像（带已下线的 avoid_load_sensory）仍能读出来："
              f"constraints={back.parent_constraints.model_dump()}")
    except Exception as e:
        print(f"  ✗ 老画像读不出来：{str(e).splitlines()[1].strip()[:70]}")


if __name__ == "__main__":
    _cli()
