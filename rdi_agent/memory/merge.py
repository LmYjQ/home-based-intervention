"""候选合并：决定"哪条能直接记、哪条要问家长、哪条不要"。

**这是整个阶段 3 最该被怀疑的一段代码**，所以它是纯函数 ——
不连库、不调模型，输入输出都是普通对象。规则错了整层记忆就不可信，
而它应该能被几十个用例在毫秒内验完。

**总的立场**：模型的判断只是**建议**，代码可以往下压，不能往上抬。

    模型说 auto_commit，代码觉得不可靠 → 压成 needs_confirmation
    模型说 needs_confirmation，代码觉得挺可靠 → **仍然不自动记**
    （唯一例外：家长明确纠正旧事实，见 ``_decide_fact``）

为什么宁可保守：自动记错一条事实，之后每一轮都会拿它当背景；
而"多问家长一句"的代价只是一次确认。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rdi_agent.memory.ids import MIN_CONTENT_CHARS, content_hash, normalize_content
from rdi_agent.schemas.memory import (
    DecisionType,
    MemoryCandidate,
    MemoryDecision,
    MemoryUpdateResult,
)

# 越靠后越保守。取 max 就等于"只能降级"。
_RANK: dict[str, int] = {
    "auto_commit": 0,
    "needs_confirmation": 1,
    "discard": 2,
}

# 高敏感内容：写进去之前必须让家长确认。
#
# **这不是一个完整的隐私分类器**，只是一张"宁可多问一句"的清单 ——
# 计划 §10.5 要求重要变更需家长确认，这些内容够"重要"。
_SENSITIVE_MARKERS = (
    "自杀", "自伤", "自残", "不想活", "轻生", "虐待", "被打", "性侵",
    "诊断", "确诊", "服药", "用药", "剂量", "住院", "癫痫", "抽搐",
    "手术", "过敏", "家族史", "离婚", "去世", "死亡",
)

# 诊断标签：系统不做诊断（见 ``graph/safety.py``），记忆里也不存。
_DIAGNOSTIC_LABELS = (
    "自闭症", "孤独症", "自闭", "阿斯伯格", "谱系", "多动症", "ADHD",
    "智力障碍", "发育迟缓", "抑郁症", "焦虑症", "语言障碍", "感统失调",
)

# "孩子总是/从来不……" 这类概括不是观察，是判断。
_GENERALIZATION_MARKERS = (
    "总是", "从来", "每次都", "一向", "天生", "本质上", "这种孩子", "这类孩子",
)


@dataclass
class ExistingSnapshot:
    """合并时要看一遍的现有记忆。由 service 层从库里读出来。"""

    # 活着的（confirmed）和待确认的（pending_confirmation）事实/偏好。
    # 待确认的也要参与去重 —— 否则每一轮都会把同一条再记一遍。
    facts: list[MemoryCandidate] = field(default_factory=list)
    # candidate / active / weakened 的假设
    hypotheses: list[MemoryCandidate] = field(default_factory=list)
    # 最近的观察（去重用）
    observations: list[MemoryCandidate] = field(default_factory=list)
    # 家长这一次说的原话。``source_quote`` 必须能在里面找到。
    source_text: str = ""

    @property
    def confirmed_fact_count(self) -> int:
        return sum(1 for x in self.facts if x.status == "confirmed")

    def fact_by_id(self, memory_id: str) -> MemoryCandidate | None:
        return next((x for x in self.facts if x.memory_id == memory_id), None)

    def hypothesis_by_id(self, memory_id: str) -> MemoryCandidate | None:
        return next((x for x in self.hypotheses if x.memory_id == memory_id), None)

    def observation_by_hash(self, digest: str) -> MemoryCandidate | None:
        return next(
            (x for x in self.observations if x.content_hash == digest), None
        )


def _squash(text: str) -> str:
    """只保留字符本身：全半角统一、去掉所有空白、小写。"""
    out = unicodedata.normalize("NFKC", text or "")
    return "".join(out.split()).casefold()


def quote_is_verbatim(quote: str, source_text: str) -> bool:
    """原话能不能在家长这一轮说的话里逐字找到。

    只做空白/全半角的归一 —— 那是排版差异，不是措辞差异。
    标点不改：改标点就等于允许模型"差不多地引用"，那就没法核对了。
    """
    quote_squashed = _squash(quote)
    if len(quote_squashed) < 2:
        return False
    return quote_squashed in _squash(source_text)


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _worse(current: DecisionType, floor: DecisionType) -> DecisionType:
    return current if _RANK[current] >= _RANK[floor] else floor


def _episode_text(candidate: MemoryCandidate) -> str:
    return " ".join(
        part
        for part in (
            candidate.content,
            candidate.observable_behavior,
            candidate.antecedent,
            candidate.outcome,
        )
        if part
    )


def merge_candidates(
    candidates: list[MemoryCandidate],
    existing: ExistingSnapshot,
    *,
    max_candidates: int = 6,
    max_active_facts: int = 50,
) -> MemoryUpdateResult:
    """给每条候选定去向，产出可以直接交给 commit 的结果。"""
    result = MemoryUpdateResult()
    seen_hashes: set[str] = set()

    ordered = list(candidates[:max_candidates])
    for overflow in candidates[max_candidates:]:
        result.skipped.append(overflow)
        result.decisions.append(
            MemoryDecision(
                candidate=overflow,
                decision="discard",
                action="none",
                reason=f"batch_cap：一轮最多处理 {max_candidates} 条",
            )
        )

    for candidate in ordered:
        decision = _decide(
            candidate,
            existing,
            seen_hashes=seen_hashes,
            max_active_facts=max_active_facts,
        )
        result.decisions.append(decision)

        if decision.decision == "discard":
            result.skipped.append(candidate)
            continue
        if decision.decision == "needs_confirmation":
            result.pending_confirmation.append(candidate)
            continue
        if decision.action == "link_contradiction":
            result.updated.append(candidate)
        else:
            result.created.append(candidate)

    return result


def _decide(
    candidate: MemoryCandidate,
    existing: ExistingSnapshot,
    *,
    seen_hashes: set[str],
    max_active_facts: int,
) -> MemoryDecision:
    digest = candidate.content_hash or ""
    text = (candidate.content or "").strip()
    combined = _episode_text(candidate)

    # ---- 1. 内容本身有没有信息量 ----
    if len(normalize_content(text)) < MIN_CONTENT_CHARS:
        return _discard(candidate, "内容过短，当记忆存下来只会污染上下文")

    # ---- 2. 批内去重 ----
    if digest and digest in seen_hashes:
        return _discard(candidate, "同一批里重复")
    if digest:
        seen_hashes.add(digest)

    quote_ok = quote_is_verbatim(candidate.source_quote, existing.source_text)

    # ---- 3. 与库里的活记忆重复 ----
    if candidate.memory_type in ("fact", "preference"):
        duplicate = next(
            (x for x in existing.facts if digest and x.content_hash == digest),
            None,
        )
        if duplicate is not None:
            return _discard(
                candidate, "这条事实已经记过了", target=duplicate.memory_id
            )
        return _decide_fact(
            candidate,
            existing,
            quote_ok=quote_ok,
            max_active_facts=max_active_facts,
        )

    if candidate.memory_type == "hypothesis":
        duplicate = next(
            (x for x in existing.hypotheses if digest and x.content_hash == digest),
            None,
        )
        if duplicate is not None:
            return _discard(
                candidate, "这个假设已经记过了", target=duplicate.memory_id
            )
        return _decide_hypothesis(candidate, quote_ok=quote_ok)

    return _decide_episode(
        candidate,
        existing,
        combined=combined,
        digest=digest,
        quote_ok=quote_ok,
    )


def _discard(
    candidate: MemoryCandidate, reason: str, *, target: str = ""
) -> MemoryDecision:
    return MemoryDecision(
        candidate=candidate,
        decision="discard",
        action="none",
        reason=reason,
        target_memory_id=target,
    )


def _decide_fact(
    candidate: MemoryCandidate,
    existing: ExistingSnapshot,
    *,
    quote_ok: bool,
    max_active_facts: int,
) -> MemoryDecision:
    floor: DecisionType = "auto_commit"

    # 诊断标签：不记。系统不给孩子贴标签，记忆里也不存。
    if _has_any(candidate.content, _DIAGNOSTIC_LABELS):
        return _discard(candidate, "带诊断标签，系统不做诊断也不存标签")

    # 模型推断**不能**成为事实。降级为假设候选 —— 这是计划 §10.3 的硬规则。
    if candidate.inference:
        candidate.memory_type = "hypothesis"
        candidate.proposed_decision = "needs_confirmation"
        # **哈希要按新类型重算**：类型是去重键的一部分。
        # 不重算的话，同一条推断走"事实降级"和直接当假设这两条路
        # 会算出两个不同的哈希，库里那条"内容相同"的去重就失效了。
        candidate.content_hash = content_hash("hypothesis", candidate.content)
        return MemoryDecision(
            candidate=candidate,
            decision="needs_confirmation",
            action="create",
            reason="模型推断只能成为待验证假设，不能直接写进事实",
        )

    if _has_any(candidate.content, _SENSITIVE_MARKERS):
        floor = _worse(floor, "needs_confirmation")

    if not quote_ok:
        floor = _worse(floor, "needs_confirmation")

    # ---- 明确纠正：唯一允许代码"往上抬"的地方 ----
    #
    # 家长把"4 岁"改成"5 岁"，这是系统能拿到的最硬的信号：
    # 有被纠正的旧记录、有逐字原话、不是推断。
    # 如果这里还等家长再点一次"确认"，本轮回答就会继续用旧值 ——
    # 验收第 2 条（纠正后不再沿用旧值）直接失败。
    target = existing.fact_by_id(candidate.replaces_memory_id or "")
    if (
        target is not None
        and target.status == "confirmed"
        and target.content_hash != candidate.content_hash
    ):
        if quote_ok and not candidate.inference:
            return MemoryDecision(
                candidate=candidate,
                decision="auto_commit",
                action="supersede",
                reason="家长明确纠正了旧事实，且有逐字原话",
                target_memory_id=target.memory_id,
            )
        # 指向了旧记录但原话对不上 —— 不能动旧值，只能挂着等确认。
        return MemoryDecision(
            candidate=candidate,
            decision="needs_confirmation",
            action="create",
            reason="这条看起来在纠正旧事实，但原话对不上，需要家长确认",
            target_memory_id=target.memory_id,
        )

    if existing.confirmed_fact_count >= max_active_facts:
        floor = _worse(floor, "needs_confirmation")

    final = _worse(candidate.proposed_decision, floor)
    if final == "discard":
        return _discard(candidate, "模型判断不需要记，且没有足够证据反驳")
    return MemoryDecision(
        candidate=candidate,
        decision=final,
        action="create",
        reason=candidate.reason or "家长明确说过，且有逐字原话",
    )


def _decide_episode(
    candidate: MemoryCandidate,
    existing: ExistingSnapshot,
    *,
    combined: str,
    digest: str,
    quote_ok: bool,
) -> MemoryDecision:
    # 反例必须挂在真实存在的假设上；编造的 id 直接丢掉。
    valid_contradictions = [
        hid
        for hid in candidate.contradicts_memory_ids
        if hid and existing.hypothesis_by_id(hid) is not None
    ]
    dropped = set(candidate.contradicts_memory_ids) - set(valid_contradictions)
    candidate.contradicts_memory_ids = valid_contradictions

    concrete = bool(
        (candidate.observable_behavior or "").strip()
        or (candidate.antecedent or "").strip()
    )
    if not concrete:
        return _discard(candidate, "观察没有写清具体发生了什么，抽象成标签的不记")
    if _has_any(combined, _GENERALIZATION_MARKERS) and not candidate.observable_behavior:
        return _discard(candidate, "把一次观察写成了概括（总是/从来不），不当观察存")

    # 同一条观察又提了一次，但它带来了**新的反例** ——
    # 不能静默丢掉，否则"反例削弱假设"就永远不生效（验收第 7 条）。
    duplicate = existing.observation_by_hash(digest) if digest else None
    if duplicate is not None:
        if valid_contradictions:
            return MemoryDecision(
                candidate=candidate,
                decision="auto_commit",
                action="link_contradiction",
                reason="这条观察之前记过，但它带来了新的反例",
                target_memory_id=duplicate.memory_id,
                contradiction_ids=valid_contradictions,
            )
        return _discard(
            candidate, "这条观察已经记过了", target=duplicate.memory_id
        )

    floor: DecisionType = "auto_commit"
    if not quote_ok:
        floor = _worse(floor, "needs_confirmation")
    if _has_any(combined, _SENSITIVE_MARKERS):
        floor = _worse(floor, "needs_confirmation")

    final = _worse(candidate.proposed_decision, floor)
    if final == "discard":
        return _discard(candidate, "模型判断不需要记，且没有足够证据反驳")
    decision = MemoryDecision(
        candidate=candidate,
        decision=final,
        action="create",
        reason=candidate.reason or "家长描述了一个具体事件",
        contradiction_ids=valid_contradictions,
    )
    if dropped:
        decision.reason += f"（忽略不存在的假设 id：{'、'.join(sorted(dropped))}）"
    return decision


def _decide_hypothesis(
    candidate: MemoryCandidate, *, quote_ok: bool
) -> MemoryDecision:
    if _has_any(candidate.content, _DIAGNOSTIC_LABELS):
        return _discard(candidate, "带诊断标签，系统不做诊断也不存标签")

    # 假设永远是候选状态，最多等到家长确认。
    final = _worse(candidate.proposed_decision, "needs_confirmation")
    if final == "discard":
        return _discard(candidate, "模型判断不需要记")
    reason = candidate.reason or "模型推断出的待验证假设"
    if not candidate.scope_conditions:
        reason += "；没有写适用条件，只能当很弱的猜测"
    if not quote_ok:
        reason += "；没有可核对的原话"
    return MemoryDecision(
        candidate=candidate,
        decision=final,
        action="create",
        reason=reason,
    )


def apply_supersede(
    items: list[Any], decision: MemoryDecision
) -> None:
    """在事实列表里把被纠正的那条标成 superseded。

    单独抽出来是为了让 service 层不用重复这段赋值 ——
    "旧记录要同时改两个字段"这件事很容易漏掉一个。
    """
    target_id = decision.target_memory_id
    if not target_id:
        return
    for item in items:
        if getattr(item, "id", "") == target_id:
            item.status = "superseded"
            item.superseded_by_memory_id = decision.candidate.memory_id or None
            return
