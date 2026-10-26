"""记忆合并规则（纯逻辑，不连库、不调模型）。

**这是阶段 3 最该被测透的一段**：它决定"什么能自动记进档案"。
规则错了，之后每一轮都会拿着错的事实当背景，而且很难察觉。

用例按"系统最怕出什么事"组织：
- 把模型的猜测当成家长说过的事实
- 家长纠正了，系统还沿用旧值
- 反例出现了，假设纹丝不动
- 同一句事实被记十遍
"""

from __future__ import annotations

import pytest

from rdi_agent.memory.ids import content_hash
from rdi_agent.memory.merge import (
    ExistingSnapshot,
    merge_candidates,
    quote_is_verbatim,
)
from rdi_agent.schemas.memory import MemoryCandidate

QUOTE = "孩子今年五岁了，早上出门一直抱着我的腿"


def fact(
    content: str = "孩子 5 岁",
    *,
    quote: str = "孩子今年五岁了",
    decision: str = "auto_commit",
    inference: bool = False,
    replaces: str = "",
    memory_id: str = "",
    status: str = "confirmed",
    memory_type: str = "fact",
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type=memory_type,
        content=content,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        inference=inference,
        replaces_memory_id=replaces,
        content_hash=content_hash(memory_type, content),
        memory_id=memory_id,
        status=status,
    )


def episode(
    content: str = "早上出门抱着腿不说话",
    *,
    quote: str = "早上出门一直抱着我的腿",
    contradiction_ids: list[str] | None = None,
    decision: str = "auto_commit",
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="episode",
        content=content,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        observable_behavior="抱着腿不说话",
        setting="家门口",
        contradicts_memory_ids=list(contradiction_ids or []),
        content_hash=content_hash("episode", content),
    )


def hypothesis(
    content: str = "可能在和妈妈分开时会紧张",
    *,
    quote: str = "",
    decision: str = "auto_commit",
    memory_id: str = "hyp_aaaa",
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="hypothesis",
        content=content,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        inference=True,
        scope_conditions=["早上出门时"],
        content_hash=content_hash("hypothesis", content),
        memory_id=memory_id,
    )


@pytest.fixture
def snapshot() -> ExistingSnapshot:
    return ExistingSnapshot(source_text=QUOTE)


def _only(result):
    assert len(result.decisions) == 1
    return result.decisions[0]


class TestQuoteVerification:
    def test_verbatim_quote_passes(self):
        assert quote_is_verbatim("早上出门一直抱着我的腿", QUOTE)

    def test_whitespace_and_fullwidth_are_tolerated(self):
        assert quote_is_verbatim("孩子今年 五岁 了", "孩子今年五岁了")

    def test_paraphrase_fails(self):
        assert not quote_is_verbatim("孩子五岁", "孩子今年五岁了")


class TestFacts:
    def test_explicit_fact_is_committed(self, snapshot):
        result = merge_candidates([fact()], snapshot)
        assert result.created and not result.pending_confirmation
        assert _only(result).decision == "auto_commit"

    def test_fact_without_verbatim_quote_needs_confirmation(self, snapshot):
        result = merge_candidates([fact(quote="孩子五岁")], snapshot)
        assert not result.created
        assert result.pending_confirmation

    def test_inference_never_becomes_a_fact(self, snapshot):
        """模型推断只能变成假设 —— 计划 §10.3 的硬规则。"""
        candidate = fact("孩子怕生", quote="", inference=True)
        result = merge_candidates([candidate], snapshot)
        assert not result.created
        assert result.pending_confirmation[0].memory_type == "hypothesis"
        assert _only(result).decision == "needs_confirmation"

    def test_diagnostic_label_is_discarded(self, snapshot):
        result = merge_candidates([fact("孩子是自闭症")], snapshot)
        assert result.skipped and not result.created
        assert _only(result).decision == "discard"

    def test_sensitive_content_needs_confirmation(self, snapshot):
        result = merge_candidates(
            [fact("孩子有癫痫，在服药")], snapshot
        )
        assert not result.created
        assert result.pending_confirmation

    def test_duplicate_of_existing_fact_is_discarded(self, snapshot):
        snapshot.facts = [
            fact(memory_id="fact_existing", status="confirmed")
        ]
        result = merge_candidates([fact()], snapshot)
        assert result.skipped and not result.created
        assert _only(result).target_memory_id == "fact_existing"

    def test_duplicate_inside_batch_is_discarded(self, snapshot):
        result = merge_candidates([fact(), fact()], snapshot)
        assert len(result.created) == 1
        assert len(result.skipped) == 1

    def test_model_discard_is_respected(self, snapshot):
        result = merge_candidates([fact(decision="discard")], snapshot)
        assert result.skipped and not result.created


class TestCorrection:
    def test_explicit_correction_supersedes_old_value(self, snapshot):
        snapshot.facts = [
            fact(
                content="孩子 4 岁",
                memory_id="fact_old",
                status="confirmed",
            )
        ]
        candidate = fact(content="孩子 5 岁", replaces="fact_old")
        result = merge_candidates([candidate], snapshot)
        decision = _only(result)
        assert decision.decision == "auto_commit"
        assert decision.action == "supersede"
        assert decision.target_memory_id == "fact_old"

    def test_correction_can_upgrade_a_cautious_model(self, snapshot):
        """模型只给 needs_confirmation，但家长明确纠正了 —— 要立刻生效。

        否则本轮回答还会用旧值，验收第 2 条直接失败。
        """
        snapshot.facts = [
            fact(content="孩子 4 岁", memory_id="fact_old")
        ]
        candidate = fact(
            content="孩子 5 岁", replaces="fact_old", decision="needs_confirmation"
        )
        assert _only(merge_candidates([candidate], snapshot)).decision == "auto_commit"

    def test_correction_without_valid_target_is_just_a_new_fact(self, snapshot):
        candidate = fact(content="孩子 5 岁", replaces="fact_not_there")
        decision = _only(merge_candidates([candidate], snapshot))
        assert decision.action == "create"

    def test_correction_with_unverifiable_quote_does_not_touch_old_value(self, snapshot):
        snapshot.facts = [
            fact(content="孩子 4 岁", memory_id="fact_old")
        ]
        candidate = fact(
            content="孩子 5 岁", replaces="fact_old", quote="他五岁"
        )
        decision = _only(merge_candidates([candidate], snapshot))
        assert decision.action == "create"
        assert decision.decision == "needs_confirmation"


class TestEpisodes:
    def test_concrete_episode_is_committed(self, snapshot):
        result = merge_candidates([episode()], snapshot)
        assert result.created

    def test_episode_without_concrete_detail_is_discarded(self, snapshot):
        candidate = episode()
        candidate.observable_behavior = ""
        candidate.antecedent = ""
        result = merge_candidates([candidate], snapshot)
        assert result.skipped

    def test_duplicate_observation_is_discarded(self, snapshot):
        existing = episode()
        existing.memory_id = "obs_existing"
        snapshot.observations = [existing]
        result = merge_candidates([episode()], snapshot)
        assert result.skipped

    def test_duplicate_observation_with_new_contradiction_is_linked(self, snapshot):
        """同一条反例又说了一遍 —— 不能静默丢，否则假设永远不会被削弱。"""
        existing = episode()
        existing.memory_id = "obs_existing"
        snapshot.observations = [existing]
        snapshot.hypotheses = [hypothesis(memory_id="hyp_aaaa")]

        result = merge_candidates(
            [episode(contradiction_ids=["hyp_aaaa"])], snapshot
        )
        decision = _only(result)
        assert decision.action == "link_contradiction"
        assert decision.target_memory_id == "obs_existing"
        assert decision.contradiction_ids == ["hyp_aaaa"]

    def test_invalid_contradiction_id_is_dropped(self, snapshot):
        result = merge_candidates(
            [episode(contradiction_ids=["hyp_missing"])], snapshot
        )
        decision = _only(result)
        assert decision.contradiction_ids == []
        assert "hyp_missing" in decision.reason

    def test_sensitive_episode_needs_confirmation(self, snapshot):
        candidate = episode(content="他说不想活了")
        candidate.source_quote = "他说不想活了"
        result = merge_candidates([candidate], snapshot)
        assert result.pending_confirmation


class TestHypotheses:
    def test_hypothesis_is_never_auto_confirmed(self, snapshot):
        result = merge_candidates([hypothesis()], snapshot)
        assert not result.created
        assert result.pending_confirmation
        assert _only(result).decision == "needs_confirmation"

    def test_hypothesis_with_diagnostic_label_is_dropped(self, snapshot):
        result = merge_candidates([hypothesis("孩子可能是自闭症")], snapshot)
        assert result.skipped

    def test_hypothesis_without_scope_is_flagged(self, snapshot):
        candidate = hypothesis()
        candidate.scope_conditions = []
        decision = _only(merge_candidates([candidate], snapshot))
        assert "适用条件" in decision.reason


class TestCaps:
    def test_batch_cap_discards_the_overflow(self, snapshot):
        candidates = [
            fact(content=f"事实第 {i} 条", quote=QUOTE) for i in range(5)
        ]
        result = merge_candidates(candidates, snapshot, max_candidates=2)
        assert len(result.decisions) == 5
        assert len(result.skipped) == 3

    def test_fact_cap_downgrades_non_corrections(self, snapshot):
        snapshot.facts = [
            fact(content=f"旧事实 {i}", memory_id=f"fact_{i}") for i in range(2)
        ]
        result = merge_candidates(
            [fact(content="孩子最近晚上睡不好")], snapshot, max_active_facts=2
        )
        assert not result.created
        assert result.pending_confirmation
