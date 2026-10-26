"""年龄计算：只存出生年月，年龄每次用的时候现算。

**为什么不在 children 上存"年龄"或"年龄段"**：一个家庭可能用这个系统好几年，
存下来的年龄过一年就是错的，而且没人会记得去改。出生日期不会变，
年龄在 ``Repository.child_context()`` 里按 ``date.today()`` 现算 ——
每一轮对话、每一次查档案都是最新的。

**粒度是月**：前端用 ``<input type="month">``，存进 DATE 时取当月 1 号。
计算仍然按 day 比较，将来放开到具体日期也不用改这里。
"""

from __future__ import annotations

from datetime import date
from typing import Any


def parse_birth_date(value: str | None) -> date | None:
    """接受 ``YYYY-MM``（前端 month input）和 ``YYYY-MM-DD``。

    空值返回 None；格式不对抛 ``ValueError``（由 API 翻成 400）。
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 7:  # YYYY-MM
            return date.fromisoformat(f"{text}-01")
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("出生年月格式应为 YYYY-MM 或 YYYY-MM-DD") from None


def age_parts(
    birth_date: date | None, today: date | None = None
) -> tuple[int, int]:
    """返回 (岁, 月)。没有出生日期或日期在未来时返回 (0, 0)。"""
    if birth_date is None:
        return 0, 0
    ref = today or date.today()
    if birth_date > ref:
        return 0, 0

    years = ref.year - birth_date.year
    months = ref.month - birth_date.month
    if ref.day < birth_date.day:
        months -= 1
    if months < 0:
        years -= 1
        months += 12
    return years, months


def age_display(birth_date: date | None, today: date | None = None) -> str:
    """给家长看的年龄：``3 岁 4 个月`` / ``8 个月`` / ``4 岁``。"""
    if birth_date is None:
        return ""
    years, months = age_parts(birth_date, today)
    if years <= 0 and months <= 0:
        return "不满 1 个月"
    if years <= 0:
        return f"{months} 个月"
    if months == 0:
        return f"{years} 岁"
    return f"{years} 岁 {months} 个月"


def age_band(birth_date: date | None, today: date | None = None) -> str:
    """折成提示词里用的段位（``3-4岁``）。没有出生日期返回空串。"""
    if birth_date is None:
        return ""
    years, _ = age_parts(birth_date, today)
    return "18岁以上" if years >= 18 else f"{years}-{years + 1}岁"


def describe_age(
    birth_date: date | None, today: date | None = None
) -> dict[str, Any]:
    """一次给出档案里要用的全部年龄字段。

    没有出生日期时返回空 dict —— 老档案（只有 ``age_band``）的兜底
    交给调用方，不在这里编一个假年龄。
    """
    if birth_date is None:
        return {}
    years, months = age_parts(birth_date, today)
    return {
        "birth_date": birth_date.isoformat(),
        "age_years": years,
        "age_months": months,
        "age_display": age_display(birth_date, today),
        "age_band": age_band(birth_date, today),
    }


__all__ = [
    "age_band",
    "age_display",
    "age_parts",
    "describe_age",
    "parse_birth_date",
]
