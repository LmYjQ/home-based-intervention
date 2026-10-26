"""证据相关契约。

EvidenceItem 描述"指南里/研究里/观察里说了什么"，
EvidenceBundle 是一次检索的完整结果集，
EvidenceDecision 是证据门控（evidence_gate 节点）的裁决。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal[
    "guideline",
    "external_research",
    "professional_review",
    "child_observation",
    "application_pattern",
]


def _require_high_risk(schema: dict) -> None:
    """把 ``high_risk`` 塞进 json_schema 的 required 列表。

    在 ``EvidenceDecision.model_json_schema()`` 上原地改。

    **为什么必须要求模型显式表态**：``high_risk`` 是唯一能挡住
    建议卡的信号（2026-10-26 起）。它在 pydantic 上有默认值 False，
    所以本来不在 required 里 —— 结构化输出允许模型整张裁决都不写它，
    于是"模型省略了"和"模型确认没有风险"分不开。
    """
    required = schema.get("required")
    if isinstance(required, list) and "high_risk" not in required:
        required.append("high_risk")


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_type: SourceType
    title: str
    section_title: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    print_page_start: int | None = None
    print_page_end: int | None = None
    summary: str
    source_excerpt: str | None = None
    relevance_reason: str
    limitations: list[str] = Field(default_factory=list)
    review_status: str
    may_quote_to_user: bool = False
    # 向量路的余弦相似度（1 - 距离）。只有 FTS 命中、没进向量候选池时为 None。
    # 门控层用它判断"这条能不能当直接依据"（见 search.DIRECT_MAX_DISTANCE）。
    similarity: float | None = None
    # 是否被关键词路命中。精确术语命中是比距离更硬的信号：
    # 距离分不开"崩溃点理论"（0.5341，相关）和"红烧肉"（0.5345，无关），
    # FTS 命中能分开。
    fts_matched: bool = False


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_question: str
    case_summary: dict[str, object] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    retrieval_version: str


class EvidenceDecision(BaseModel):
    """证据门控的裁决 —— **模型只描述证据，代码决定走哪条路**（2026-10-26 简化）。

    **以前有两个模型自由填的字段决定着"给不给方案卡"**：
    ``can_support_personalized_advice``（证据能不能撑住一条个体化建议）
    和 ``decision``（流程决定）。实测它们经常和 ``supported_claims``
    自相矛盾（主张写得很具体，却把 can_support 填成 false），
    而且模型每次填的还不一样 —— 同一份证据（8/8 可用）两次门控
    一次出卡、一次只给家长一段话。家长拿到什么成了掷骰子。

    **现在这两个字段都没了**：
    - ``can_support_personalized_advice`` 已删除；
    - ``decision`` 不再由模型填 —— 门控节点用
      ``graph/evidence_gate.py::decide_flow`` 按硬规则算出来，
      写进 state 的 ``evidence_decision["decision"]``。

    模型现在只回答四件事：**证据有多直接**、**能支撑哪些主张**、
    **哪些主张撑不住**、**缺什么信息**、以及**有没有高风险**。

    **只有 ``high_risk`` 能挡住建议卡。** 证据够不够支撑一条具体建议，
    由引用校验（``graph/citations.py``）把关 —— 那才是硬闸：
    引用的 evidence_id 必须在证据包里、页码要对得上、
    主张里的术语必须真出现在那条证据里。
    """

    model_config = ConfigDict(
        extra="forbid",
        # 强制 ``high_risk`` 进 required。
        #
        # **为什么**：它是唯一能挡住建议卡的信号，不能靠模型省略。
        # 它在 pydantic 上有默认值 False，所以本来不在 required 里 ——
        # 结构化输出允许模型整张裁决都不写它，于是"模型没说有风险"
        # 和"模型确认没有风险"分不开。
        json_schema_extra=_require_high_risk,
    )

    evidence_status: Literal["direct", "indirect", "conflicting", "none", "risk"]
    supported_claims: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    # 家长报告了要立刻找专业人士的信号（自伤、伤到别人、失去意识、
    # 抽搐、拒绝进食进水这类）。**普通的哭闹、分离焦虑、害怕不算** ——
    # 那是这次要处理的问题本身。
    #
    # 填 true 这一轮就不给建议卡，改成请家长找专业的人。
    #
    # **代码不会把它降级**：万一真漏了一个严重信号，宁可少给一张卡，
    # 也不能让家长拿着建议回去。误报的后果是"多去找一次专业的人"，
    # 漏报的后果是"该找的人没找"—— 后者严重得多。
    #
    # 家长报告里的风险信号另有一道内容复核
    # （``evidence_gate.py::serious_signals``），作用在 intake 那一层，
    # 不是这一层。
    high_risk: bool = False
    # 门控不再追问（追问轮数上限是 0），所以这个字段永远是 false。
    needs_followup: bool = False
