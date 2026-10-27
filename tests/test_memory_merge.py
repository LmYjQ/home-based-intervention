"""记忆合并规则（纯逻辑，不连库、不调模型）。

**这是整层记忆最该被测透的一段**：它决定"什么能自动记进档案"。
规则错了，之后每一轮都会拿着错的内容当背景，而且很难察觉。

用例按"系统最怕出什么事"组织：
- 记下家长没说过、也没原话的东西
- 家长纠正了，系统还沿用旧值
- 同一句被记十遍
- 把系统自己的猜测当成家长说过的话
"""

from __future__ import annotations

import pytest

from rdi_agent.memory.ids import note_content_hash
from rdi_agent.memory.merge import (
    ExistingSnapshot,
    merge_candidates,
    quote_is_verbatim,
)
from rdi_agent.schemas.memory import MemoryCandidate

QUOTE = "孩子今年五岁了，早上出门一直抱着我的腿"


def note(
    content: str = "孩子 5 岁",
    *,
    quote: str = "孩子今年五岁了",
    decision: str = "auto_commit",
    inference: bool = False,
    is_preference: bool = False,
    replaces: str = "",
    memory_id: str = "",
    status: str = "active",
    category: str = "other",
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        category=category,
        is_preference=is_preference,
        inference=inference,
        source_quote=quote,
        proposed_decision=decision,  # type: ignore[arg-type]
        replaces_memory_id=replaces,
        content_hash=note_content_hash(
            content, is_preference=is_preference, inference=inference
        ),
        memory_id=memory_id,
        status=status,
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


class TestNotes:
    def test_explicit_note_is_committed(self, snapshot):
        result = merge_candidates([note()], snapshot)
        assert result.created
        assert _only(result).decision == "auto_commit"

    def test_note_without_verbatim_quote_is_discarded(self, snapshot):
        """引不出原话 → 直接不记。

        家长没法核对的东西不该被当成"他说过的"用出去。
        """
        result = merge_candidates([note(quote="孩子五岁")], snapshot)
        assert not result.created
        assert result.skipped
        assert _only(result).decision == "discard"

    def test_too_short_is_discarded(self, snapshot):
        result = merge_candidates([note(content="好", quote="好")], snapshot)
        assert result.skipped
        assert _only(result).decision == "discard"

    def test_diagnostic_label_is_discarded(self, snapshot):
        result = merge_candidates([note("孩子是自闭症")], snapshot)
        assert result.skipped and not result.created
        assert _only(result).decision == "discard"

    def test_sensitive_content_with_quote_is_committed(self, snapshot):
        """敏感内容家长明说的，照记。

        这是他孩子的真实情况，藏着反而可能藏掉该找专业人士的风险信号。
        闸门在别处：必须有逐字原话。
        """
        snapshot.source_text = "孩子有癫痫，在服药"
        candidate = note("孩子有癫痫，在服药", quote="孩子有癫痫，在服药")
        result = merge_candidates([candidate], snapshot)
        assert result.created
        assert _only(result).decision == "auto_commit"

    def test_duplicate_of_existing_is_discarded(self, snapshot):
        snapshot.items = [note(memory_id="mi_existing")]
        result = merge_candidates([note()], snapshot)
        assert result.skipped and not result.created
        assert _only(result).target_memory_id == "mi_existing"

    def test_duplicate_inside_batch_is_discarded(self, snapshot):
        result = merge_candidates([note(), note()], snapshot)
        assert len(result.created) == 1
        assert len(result.skipped) == 1

    def test_model_discard_is_respected(self, snapshot):
        result = merge_candidates([note(decision="discard")], snapshot)
        assert result.skipped and not result.created


class TestPreferences:
    def test_preference_is_committed(self, snapshot):
        """家长的偏好是硬约束，照记。"""
        snapshot.source_text = "我不希望他被批评"
        candidate = note(
            "我不希望他被批评",
            quote="我不希望他被批评",
            is_preference=True,
        )
        result = merge_candidates([candidate], snapshot)
        assert result.created
        assert result.created[0].is_preference

    def test_preference_without_quote_is_discarded(self, snapshot):
        snapshot.source_text = "我不希望他被批评"
        candidate = note("我不希望他被批评", quote="别骂他", is_preference=True)
        result = merge_candidates([candidate], snapshot)
        assert result.skipped

    def test_preference_and_note_do_not_dedupe_each_other(self, snapshot):
        """同一句话当偏好和当普通短句是两种东西 —— 哈希要分开。"""
        snapshot.source_text = "不要批评他"
        pref = note("不要批评他", quote="不要批评他", is_preference=True)
        plain = note("不要批评他", quote="不要批评他")
        assert pref.content_hash != plain.content_hash
        result = merge_candidates([pref, plain], snapshot)
        assert len(result.created) == 2


class TestInference:
    def test_inference_is_committed_without_a_quote(self, snapshot):
        """推断没有家长原话 —— 但它照记，只是标成推断。

        不写才是浪费：模型看出来的模式，下一轮可能正好用得上。
        """
        candidate = note("可能是分离焦虑", quote="", inference=True)
        result = merge_candidates([candidate], snapshot)
        assert result.created
        assert result.created[0].inference

    def test_fake_quote_on_an_inference_is_dropped(self, snapshot):
        """推断硬凑一个凑不上的原话 —— 那个原话是假的，清掉。"""
        candidate = note("可能是分离焦虑", quote="他不高兴", inference=True)
        result = merge_candidates([candidate], snapshot)
        assert result.created
        assert result.created[0].source_quote == ""

    def test_inference_with_diagnostic_label_is_dropped(self, snapshot):
        candidate = note("孩子可能是自闭症", quote="", inference=True)
        result = merge_candidates([candidate], snapshot)
        assert result.skipped


class TestCorrection:
    def test_explicit_correction_supersedes_old_value(self, snapshot):
        snapshot.items = [note(content="孩子 4 岁", memory_id="mi_old")]
        candidate = note(content="孩子 5 岁", replaces="mi_old")
        decision = _only(merge_candidates([candidate], snapshot))
        assert decision.decision == "auto_commit"
        assert decision.action == "supersede"
        assert decision.target_memory_id == "mi_old"

    def test_correction_without_valid_target_is_just_a_new_note(self, snapshot):
        candidate = note(content="孩子 5 岁", replaces="mi_not_there")
        decision = _only(merge_candidates([candidate], snapshot))
        assert decision.action == "create"

    def test_correction_with_unverifiable_quote_does_not_touch_old_value(self, snapshot):
        """原话对不上 → 既不动旧值，也不记新的。"""
        snapshot.items = [note(content="孩子 4 岁", memory_id="mi_old")]
        candidate = note(content="孩子 5 岁", replaces="mi_old", quote="他五岁")
        decision = _only(merge_candidates([candidate], snapshot))
        assert decision.action == "none"
        assert decision.decision == "discard"


class TestCaps:
    def test_batch_cap_discards_the_overflow(self, snapshot):
        candidates = [
            note(content=f"短句第 {i} 条", quote=QUOTE) for i in range(5)
        ]
        result = merge_candidates(candidates, snapshot, max_candidates=2)
        assert len(result.decisions) == 5
        assert len(result.skipped) == 3
