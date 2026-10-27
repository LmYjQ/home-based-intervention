"""候选合并：决定"哪条能直接记、哪条不要"。

**这是整层记忆最该被怀疑的一段代码**，所以它是纯函数 ——
不连库、不调模型，输入输出都是普通对象。规则错了整层记忆就不可信，
而它应该能被几十个用例在毫秒内验完。

**总的立场**：模型的判断只是**建议**，代码可以往下压，不能往上抬。

    模型说 auto_commit，代码觉得不可靠 → 压掉
    模型说 discard，代码觉得挺可靠 → **仍然不记**

**记忆只有一种东西：短句**。以前还有事实 / 观察 / 假设三种，
让"这条怎么用"取决于它被归成哪一类 —— 实测抽取那一步模型几乎总是
判成"事实"，另外两类永远是空的。现在所有短句走同一条判定链，
区别只有两个开关（``is_preference`` / ``inference``），
它们决定**这条进不进 prompt、参不参与合并**，不影响"能不能记"。

**没有"等家长确认"这一档**。原来有三档（auto_commit /
needs_confirmation / discard），中间那档是"写进库、标成待确认、
等家长在面板上点一下"。实测这套是死的：

- 面板就静静躺在右栏，**没有任何地方提示家长去点**；
- 于是 ``pending_confirmation`` 的内容永远不生效 ——
  家长明确说过的事，因为系统不敢替他做主，就当没发生过；
- 而"多问一句"这个设计前提是家长**有空**来管这件事。
  一个带自闭症孩子的家长累了一天回来，还要逐条点"确认"，
  这产品就已经死了。

所以现在只有**记**和**不记**两档，闸门从"家长逐条点头"换成
**"系统自己保守 + 家长一句话能推翻"**：

- **引不出原话 → 直接不记**。家长没法核对的东西，
  留在库里只会在将来某一轮被当成事实用出去。
- **敏感内容 → 家长明说的照记**。这是他孩子的真实情况，
  藏着反而可能藏掉风险信号；**系统推断的照旧不记**。
- **诊断标签 → 不记**。系统不给孩子贴标签。

家长想推翻某条记忆，有两条路，都不用进面板：
在对话里说"不是这样"（走 ``replaces_memory_id`` 的纠正链路），
或者在面板里删掉它。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rdi_agent.memory.ids import MIN_CONTENT_CHARS, normalize_content
from rdi_agent.schemas.memory import (
    DecisionType,
    MemoryCandidate,
    MemoryDecision,
    MemoryUpdateResult,
)

# 诊断标签：系统不做诊断（见 ``graph/safety.py``），记忆里也不存。
_DIAGNOSTIC_LABELS = (
    "自闭症", "孤独症", "自闭", "阿斯伯格", "谱系", "多动症", "ADHD",
    "智力障碍", "发育迟缓", "抑郁症", "焦虑症", "语言障碍", "感统失调",
)


@dataclass
class ExistingSnapshot:
    """合并时要看一遍的现有记忆。由 service 层从库里读出来。"""

    # 已有的短句（去重用，也给"纠正"指目标）。
    items: list[MemoryCandidate] = field(default_factory=list)
    # 家长这一次说的原话。``source_quote`` 必须能在里面找到。
    source_text: str = ""

    def item_by_id(self, memory_id: str) -> MemoryCandidate | None:
        return next((x for x in self.items if x.memory_id == memory_id), None)

    def item_by_hash(self, digest: str) -> MemoryCandidate | None:
        return next((x for x in self.items if x.content_hash == digest), None)


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


def _model_verdict(current: DecisionType) -> DecisionType:
    """模型给的那一档，映射到现在只有两档的世界里。

    模型说 ``discard`` 就是"不需要记"。以前还有中间那档
    ``needs_confirmation``（"我不太确定"），现在没了 ——
    模型说"不确定"就丢掉，等于让模型的保守偏好
    变成整层记忆的召回上限 —— 而这个上限从来没被验证过。
    """
    if current == "discard":
        return "discard"
    return "auto_commit"


def merge_candidates(
    candidates: list[MemoryCandidate],
    existing: ExistingSnapshot,
    *,
    max_candidates: int = 6,
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
        decision = _decide(candidate, existing, seen_hashes=seen_hashes)
        result.decisions.append(decision)

        if decision.decision == "discard":
            result.skipped.append(candidate)
            continue
        if decision.action == "supersede":
            result.updated.append(candidate)
        else:
            result.created.append(candidate)

    return result


def _decide(
    candidate: MemoryCandidate,
    existing: ExistingSnapshot,
    *,
    seen_hashes: set[str],
) -> MemoryDecision:
    digest = candidate.content_hash or ""
    text = (candidate.content or "").strip()

    # ---- 1. 内容本身有没有信息量 ----
    if len(normalize_content(text)) < MIN_CONTENT_CHARS:
        return _discard(candidate, "内容过短，当记忆存下来只会污染上下文")

    # ---- 2. 批内去重 ----
    if digest and digest in seen_hashes:
        return _discard(candidate, "同一批里重复")
    if digest:
        seen_hashes.add(digest)

    # ---- 3. 诊断标签：不记。系统不给孩子贴标签，记忆里也不存。 ----
    if _has_any(text, _DIAGNOSTIC_LABELS):
        return _discard(candidate, "带诊断标签，系统不做诊断也不存标签")

    # ---- 4. 与库里的活记忆重复 ----
    duplicate = existing.item_by_hash(digest) if digest else None
    if duplicate is not None:
        return _discard(candidate, "这条已经记过了", target=duplicate.memory_id)

    quote_ok = quote_is_verbatim(candidate.source_quote, existing.source_text)

    # ---- 5. 引不出原话 → 直接不记 ----
    #
    # 家长没法核对的东西，不该在将来某一轮被当成"他说的"用出去。
    #
    # **推断例外**：推断本来就没有家长原话 —— 但如果模型硬凑了一个
    # 凑不上的原话，那个原话是假的，清掉。
    if not quote_ok:
        if not candidate.inference:
            return _discard(
                candidate,
                "引不出家长的逐字原话，不记 —— 家长没法核对的东西不能当记忆",
            )
        candidate.source_quote = ""

    # ---- 6. 明确纠正 ----
    #
    # 家长把"4 岁"改成"5 岁"，这是系统能拿到的最硬的信号：
    # 有被纠正的旧记录、有逐字原话、不是推断。
    target = existing.item_by_id(candidate.replaces_memory_id or "")
    if target is not None and target.content_hash != candidate.content_hash:
        if quote_ok or candidate.inference:
            return MemoryDecision(
                candidate=candidate,
                decision="auto_commit",
                action="supersede",
                reason="家长明确纠正了旧记录，且有逐字原话",
                target_memory_id=target.memory_id,
            )
        # 指向了旧记录但原话对不上 —— 不能动旧值。
        return _discard(
            candidate,
            "这条看起来在纠正旧记录，但原话对不上，不动旧值也不记新的",
            target=target.memory_id,
        )

    # ---- 7. 模型说不记，就不记 ----
    final = _model_verdict(candidate.proposed_decision)
    if final == "discard":
        return _discard(candidate, "模型判断不需要记，且没有足够证据反驳")

    return MemoryDecision(
        candidate=candidate,
        decision="auto_commit",
        action="create",
        reason=candidate.reason or "家长明确说过，且有逐字原话",
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


def apply_supersede(
    items: list[Any], decision: MemoryDecision
) -> None:
    """在短句列表里把被纠正的那条标成 superseded。

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
