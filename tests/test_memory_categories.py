"""类目与合并触发的纯规则（不连库、不调模型）。

**测的是"什么该被合并"** —— 这条规则错了，整层记忆就用不上：
合得太少 → prompt 里全是散条；合得太多 → 一次事件被说成"孩子就是这样"。

用例按最怕出什么事组织：
- 一次事件被当成"多次出现"（跨轮次不够就合）
- 同一批明细被反复归纳（增量判定失效）
- 归错的类目把记忆弄丢
"""

from __future__ import annotations

from rdi_agent.memory.categories import (
    CATEGORIES,
    MIN_DISTINCT_TURNS,
    MIN_OBSERVATIONS,
    bucket_by_category,
    categories_needing_update,
    normalize_category,
    should_use_llm,
)


def _row(memory_id: str, category: str, turn_id: str, text: str = "在幼儿园门口哭"):
    return {
        "memory_id": memory_id,
        "category": category,
        "turn_id": turn_id,
        "text": text,
    }


class TestNormalizeCategory:
    def test_known_category_passes_through(self):
        assert normalize_category("separation") == "separation"

    def test_chinese_label_is_accepted(self):
        assert normalize_category("分离与过渡") == "separation"

    def test_unknown_category_falls_back_to_other(self):
        """归错类**不能丢记忆** —— 只影响它被归到哪一组。"""
        assert normalize_category("瞎写的") == "other"
        assert normalize_category("") == "other"

    def test_every_category_has_a_label_and_hint(self):
        from rdi_agent.memory.categories import CATEGORY_AFFECTS, CATEGORY_HINT, CATEGORY_LABEL

        for category in CATEGORIES:
            assert category in CATEGORY_LABEL
            assert category in CATEGORY_HINT
            assert category in CATEGORY_AFFECTS


class TestBuckets:
    def test_rows_are_grouped_by_category(self):
        buckets = bucket_by_category(
            [
                _row("obs_1", "separation", "t1"),
                _row("obs_2", "separation", "t2"),
                _row("obs_3", "emotion", "t1"),
            ]
        )
        assert set(buckets) == {"separation", "emotion"}
        assert buckets["separation"].count == 2
        assert buckets["separation"].distinct_turns == 2

    def test_rows_without_id_are_skipped(self):
        buckets = bucket_by_category([_row("", "separation", "t1")])
        assert buckets["separation"].count == 0


class TestShouldUseLLM:
    def test_too_few_observations_does_not_use_llm(self):
        """2 条可能是巧合 —— 3 条才有"模式"可言。"""
        bucket = bucket_by_category(
            [_row("obs_1", "separation", "t1"), _row("obs_2", "separation", "t2")]
        )["separation"]
        ok, reason = should_use_llm(bucket)
        assert not ok
        assert "明细不足" in reason

    def test_same_turn_only_does_not_use_llm(self):
        """**最关键的一条**：家长一段话里能抽出三四条观察。

        凑够 3 条就归纳，等于把一次事件说成"孩子每次都这样"。
        跨轮次才是"多次出现"。
        """
        bucket = bucket_by_category(
            [
                _row("obs_1", "separation", "t1"),
                _row("obs_2", "separation", "t1"),
                _row("obs_3", "separation", "t1"),
            ]
        )["separation"]
        ok, reason = should_use_llm(bucket)
        assert not ok
        assert "轮" in reason

    def test_enough_observations_across_turns_uses_llm(self):
        bucket = bucket_by_category(
            [
                _row("obs_1", "separation", "t1"),
                _row("obs_2", "separation", "t2"),
                _row("obs_3", "separation", "t2"),
            ]
        )["separation"]
        ok, _reason = should_use_llm(bucket)
        assert ok


class TestCategoriesNeedingUpdate:
    def test_covered_category_needs_no_update(self):
        """小结已经覆盖了全部明细 —— 不用再动。"""
        rows = [
            _row("obs_1", "separation", "t1"),
            _row("obs_2", "separation", "t2"),
        ]
        updates = categories_needing_update(
            rows,
            covered_ids_by_category={"separation": {"obs_1", "obs_2"}},
        )
        assert "separation" not in updates

    def test_a_single_uncovered_detail_triggers_update(self):
        """**一条漏掉的明细也要触发**。

        按"新增 ≥ 2 条"判断的话，这条会一直落空 ——
        既没被旧小结覆盖，又不够触发重算，于是永远进不了 prompt。
        家长说过的一句话就这么白说了。
        """
        rows = [
            _row("obs_1", "separation", "t1"),
            _row("obs_2", "separation", "t2"),
            _row("obs_3", "separation", "t2"),
        ]
        updates = categories_needing_update(
            rows,
            covered_ids_by_category={"separation": {"obs_1", "obs_2"}},
        )
        assert "separation" in updates
        _bucket, use_llm, _reason = updates["separation"]
        assert use_llm

    def test_deleted_detail_makes_the_category_stale(self):
        """家长删了一条明细 → 小结必须重算。

        不重算的话，小结里还留着家长已经删掉的内容 ——
        等于把删掉的东西又喂回模型。
        """
        rows = [_row("obs_1", "separation", "t1")]
        updates = categories_needing_update(
            rows,
            covered_ids_by_category={"separation": {"obs_1", "obs_deleted"}},
        )
        assert "separation" in updates

    def test_category_with_too_few_details_still_gets_a_summary(self):
        """明细不够 3 条 → 不调模型，但**仍然要出小结**。

        家长第一轮就说了"在幼儿园门口哭"，没到 3 条。
        没有这一档，这句话就永远进不了 prompt。
        """
        rows = [_row("obs_1", "separation", "t1")]
        updates = categories_needing_update(rows)
        assert "separation" in updates
        _bucket, use_llm, _reason = updates["separation"]
        assert not use_llm

    def test_participating_ids_are_all_of_the_bucket(self):
        """参与合并的是**全部**明细，不只是新增的那些。

        小结要覆盖这一类的全部情况，重算才完整。
        """
        rows = [
            _row("obs_1", "separation", "t1"),
            _row("obs_2", "separation", "t2"),
            _row("obs_3", "separation", "t2"),
        ]
        updates = categories_needing_update(rows)
        bucket, _use_llm, _reason = updates["separation"]
        assert bucket.observation_ids == ["obs_1", "obs_2", "obs_3"]


class TestThresholds:
    def test_thresholds_are_what_the_design_says(self):
        """改这几个数就是改产品行为 —— 钉住它们，别被顺手改掉。"""
        assert MIN_OBSERVATIONS == 3
        assert MIN_DISTINCT_TURNS == 2
