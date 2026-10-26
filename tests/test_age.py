"""年龄计算（纯函数，不连库）。

年龄只从出生日期现算，所以这些边界值就是唯一的真相：
跨年、跨月、生日还没到、不满一岁。
"""

from __future__ import annotations

from datetime import date

import pytest

from rdi_agent.age import (
    age_band,
    age_display,
    age_parts,
    describe_age,
    parse_birth_date,
)


class TestParse:
    def test_month_input(self):
        assert parse_birth_date("2020-05") == date(2020, 5, 1)

    def test_full_date(self):
        assert parse_birth_date("2020-05-15") == date(2020, 5, 15)

    def test_empty_is_none(self):
        assert parse_birth_date("") is None
        assert parse_birth_date(None) is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_birth_date("2020/05")
        with pytest.raises(ValueError):
            parse_birth_date("2020-13")


class TestAgeParts:
    def test_exact_years_and_months(self):
        # 2020-05-15 → 2026-10-25：6 岁 5 个月
        assert age_parts(date(2020, 5, 15), date(2026, 10, 25)) == (6, 5)

    def test_birthday_not_reached_this_month(self):
        # 20 号出生，15 号算：还差 5 天，只能算 6 岁 4 个月
        assert age_parts(date(2020, 5, 20), date(2026, 10, 15)) == (6, 4)

    def test_just_born(self):
        assert age_parts(date(2026, 10, 25), date(2026, 10, 25)) == (0, 0)

    def test_future_date_is_zero(self):
        assert age_parts(date(2027, 1, 1), date(2026, 10, 25)) == (0, 0)

    def test_missing_birth_date(self):
        assert age_parts(None, date(2026, 10, 25)) == (0, 0)


class TestDisplay:
    def test_years_and_months(self):
        assert age_display(date(2020, 5, 1), date(2026, 10, 25)) == "6 岁 5 个月"

    def test_exact_years(self):
        assert age_display(date(2023, 6, 1), date(2026, 6, 1)) == "3 岁"

    def test_under_one_year(self):
        assert age_display(date(2026, 2, 1), date(2026, 10, 25)) == "8 个月"

    def test_newborn(self):
        assert age_display(date(2026, 10, 25), date(2026, 10, 25)) == "不满 1 个月"

    def test_no_birth_date(self):
        assert age_display(None) == ""


class TestBand:
    def test_band_follows_age(self):
        assert age_band(date(2022, 5, 1), date(2026, 10, 25)) == "4-5岁"

    def test_infant_band(self):
        assert age_band(date(2026, 2, 1), date(2026, 10, 25)) == "0-1岁"

    def test_no_birth_date(self):
        assert age_band(None) == ""


def test_describe_age_shape():
    age = describe_age(date(2020, 5, 1), date(2026, 10, 25))
    assert age == {
        "birth_date": "2020-05-01",
        "age_years": 6,
        "age_months": 5,
        "age_display": "6 岁 5 个月",
        "age_band": "6-7岁",
    }
    assert describe_age(None) == {}
