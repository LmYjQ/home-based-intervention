# -*- coding: utf-8 -*-
"""
案例（case）层
==============

**一个"案例"是什么**：一个孩子的一段持续跟进 —— 家长前后多次描述同一个问题，
系统把它攒成一份**可追溯的评估**，并在缺信息时**主动追问**。

★ 为什么它不能复用现有的"观察链"
----------------------------------
观察链回答的是「今天发生了什么」（thread = `{child}:{date}`，一天一条，
追问最多一次）。案例回答的是「这个孩子到底怎么回事」（thread = `case:{case_id}`，
长期，追问最多三轮）。

两者是**并排长出来的第四张图**，不是把旧的改造：
  · 观察链落 `observations` / `red_flags`
  · 案例图落 `cases` / `case_evidence` / `assessment_versions`
写库节点各管各的表 → 现有观察链在阶段 2 **一行都不用改**。

★ 本阶段的取舍：先跑通，不做太多校验
------------------------------------
REFACTOR.md §2 原本列了 11 组自检断言、9 条家长措辞规则、四条硬不变量。
本轮只保留**四条硬不变量**（它们是"编造的引文能进 known_facts"那个破口的
唯一防线，代价是四个 if），其余（措辞字典的完整版、前端四栏的精细排版）
留到后面。理由：先把接口跑通，验证的应该是"能用"，不是"完备"。
"""

from .models import (  # noqa: F401
    Assessment,
    Contradiction,
    EvidenceBatch,
    EvidenceClaim,
    EvidenceSource,
    EvidenceStatus,
    EvidenceSupport,
)
from .lifecycle import (  # noqa: F401
    ALLOWED_TRANSITIONS,
    CASE_STATUSES,
    can_transition,
)
