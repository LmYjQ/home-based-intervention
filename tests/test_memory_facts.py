"""``children.confirmed_facts_json`` 的读写视图。

**这里最要紧的一条是兼容旧数据**：阶段 3 之前这张字段存的是裸字符串
（``["孩子 4 岁"]``）。升级之后如果读不出来，家长看到的就是
"我明明说过，系统却说没有" —— 比不记还糟。
"""

from __future__ import annotations

from rdi_agent.memory.facts import (
    FactItem,
    active_facts,
    forbidden_fact_texts,
    new_fact_item,
    normalize_fact_items,
    next_fact_id,
    pending_facts,
)
from rdi_agent.memory.ids import content_hash, fact_memory_id


class TestLegacyCompatibility:
    def test_plain_strings_are_read_as_confirmed_facts(self):
        items = normalize_fact_items(["孩子 4 岁", "喜欢恐龙"])
        assert len(items) == 2
        assert all(x.status == "confirmed" for x in items)
        assert all(x.memory_type == "fact" for x in items)
        assert all(x.source_kind == "legacy" for x in items)

    def test_legacy_ids_are_stable_across_reads(self):
        """同一个字符串每次读到的 id 必须一样。

        不一样的话，前端刷新一次就换一个 id，纠正按钮立刻失效。
        """
        first = normalize_fact_items(["孩子 4 岁"])[0].id
        second = normalize_fact_items(["孩子 4 岁"])[0].id
        assert first == second
        assert first.startswith("fact_")

    def test_too_short_entries_are_dropped(self):
        assert normalize_fact_items(["嗯", "", "   "]) == []

    def test_round_trip(self):
        item = FactItem(
            id="fact_x",
            content="孩子 5 岁",
            content_hash=content_hash("fact", "孩子 5 岁"),
            status="confirmed",
        )
        restored = normalize_fact_items([item.to_dict()])[0]
        assert restored.content == "孩子 5 岁"
        assert restored.id == "fact_x"


class TestStatusViews:
    def _items(self):
        return [
            FactItem(id="fact_a", content="活着的事实", status="confirmed"),
            FactItem(id="fact_b", content="待确认的事实", status="pending_confirmation"),
            FactItem(id="fact_c", content="作废的事实", status="superseded"),
            FactItem(
                id="fact_d",
                content="删掉的事实",
                status="deleted",
                deleted_at="2026-10-25T00:00:00+00:00",
            ),
        ]

    def test_active_only_returns_confirmed(self):
        assert [x.id for x in active_facts(self._items())] == ["fact_a"]

    def test_pending_is_separate(self):
        assert [x.id for x in pending_facts(self._items())] == ["fact_b"]

    def test_forbidden_covers_superseded_and_deleted(self):
        assert forbidden_fact_texts(self._items()) == ["作废的事实", "删掉的事实"]


class TestIds:
    def test_generation_suffix_avoids_collision(self):
        digest = content_hash("fact", "孩子 5 岁")
        items = [FactItem(id=fact_memory_id("fact", digest))]
        assert next_fact_id(items, "fact", digest) == f"{fact_memory_id('fact', digest)}_2"

    def test_preference_prefix(self):
        digest = content_hash("preference", "不希望孩子吃药")
        assert fact_memory_id("preference", digest).startswith("pref_")

    def test_new_item_links_to_superseded(self):
        item = new_fact_item(
            memory_type="fact",
            content="孩子 5 岁",
            digest=content_hash("fact", "孩子 5 岁"),
            existing=[],
            supersedes_memory_id="fact_old",
        )
        assert item.supersedes_memory_id == "fact_old"
        assert item.status == "confirmed"
