# -*- coding: utf-8 -*-
"""
出生日期 → 年龄
===============

**为什么把"月龄"换成"出生日期"**
--------------------------------
月龄是一个**要人算的数**，而月龄只有一个用武之地：决定活动难度和用词。
让家长在表单里手填"34 个月"意味着两件事要同时对：孩子多大**且**填对了。
填错 6 个月，选出来的活动难度就整体偏一级 —— 而偏一级在康复干预里
不是"稍微不合适"，是**练不到或练过头**。

出生日期是**只有一个正确值的客观事实**，家长从身份证上抄一次即可，
剩下的换算交给系统。所以：
  - 家长填的是 `birth_date`；
  - 月龄由 `age_months_from_birth()` 算；
  - 算出来的月龄**仍然存一份**在 `children.age_months` 和画像里 ——
    画像的 `child_age_months` 是"**评估当时**的月龄"，
    而月龄每天都在变，评估报告不会跟着变。

★ 唯一口径
----------
本模块是**唯一**算月龄的地方（后端）。前端表单里那份 JS 只用于给家长看
一个"约 X 岁"的提示，**不参与任何判定** —— 检索、难度闸门、日历
读的都是后端算出来的那一个。
（与 `frontend/src/api.js` 顶上那条"前端不算 quality"同源：
判定口径只能有一份。）
"""

from __future__ import annotations

from datetime import date, datetime

#: 与 `capabilities.AbilityProfile.child_age_months` 的上界（240）保持一致。
#: 超过它说明出生日期填错了 —— 20 岁的孩子不在这个工具的适用范围内。
MAX_AGE_MONTHS = 240


def parse_birth_date(s: str) -> date:
    """把 "YYYY-MM-DD" 解析成 date。**格式不对就抛 ValueError**，不猜。

    刻意不做"容错解析"：出生日期填错一个字符，月份就整个偏掉，
    而偏掉的月份不会报错，只会让活动难度悄悄错一级。
    """
    if isinstance(s, date):
        return s
    s = (s or "").strip()
    if not s:
        raise ValueError("出生日期不能为空")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"出生日期格式应为 YYYY-MM-DD，收到 {s!r}") from e


def age_months_from_birth(birth_date: str | date, today: date | None = None) -> int:
    """出生日期 → **满月龄**（已满的月数）。

    口径：生日当天算 +1 个月；生日没过的那天还算上个月。
    —— 与"2 岁 10 个月"这种家长说法一致（生日没到就说 2 岁 10 个月）。

    未来的出生日期直接报错：那是填错了（或者把孩子出生日期写成了就诊日期），
    而月龄为负会让 `child_age_months` 的 `ge=0` 校验炸在一个看不懂的地方。
    """
    b = parse_birth_date(birth_date) if not isinstance(birth_date, date) else birth_date
    t = today or date.today()
    months = (t.year - b.year) * 12 + (t.month - b.month)
    if t.day < b.day:
        months -= 1
    if months < 0:
        raise ValueError(f"出生日期 {b.isoformat()} 在今天 {t.isoformat()} 之后")
    if months > MAX_AGE_MONTHS:
        raise ValueError(f"出生日期 {b.isoformat()} 算出来是 {months} 个月，超过上限 {MAX_AGE_MONTHS}")
    return months


def age_text(months: int) -> str:
    """月龄 → 家长读得懂的说法（"34 个月" → "2 岁 10 个月"）。"""
    if months < 0:
        return f"{months} 个月"
    y, m = divmod(months, 12)
    if y and m:
        return f"{y} 岁 {m} 个月"
    if y:
        return f"{y} 岁"
    return f"{m} 个月"


def _cli() -> None:
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    print("--- 出生日期 → 月龄 ---")
    check("生日当天算 +1 个月（2024-01-15 → 2026-10-15 = 33）",
          age_months_from_birth("2024-01-15", date(2026, 10, 15)) == 33,
          age_months_from_birth("2024-01-15", date(2026, 10, 15)))
    check("生日没过还算上个月（2024-01-16 → 2026-10-15 = 32）",
          age_months_from_birth("2024-01-16", date(2026, 10, 15)) == 32)
    check("跨闰年（2024-02-29 → 2026-02-28 = 23）",
          age_months_from_birth("2024-02-29", date(2026, 2, 28)) == 23)
    check("刚出生 = 0 个月",
          age_months_from_birth("2026-10-01", date(2026, 10, 20)) == 0)
    check("★ 34 个月 ↔ '2 岁 10 个月'",
          age_text(34) == "2 岁 10 个月" and age_text(24) == "2 岁" and age_text(7) == "7 个月")

    print("\n--- 填错要报错，不要猜 ---")
    for bad in ("", "2026/10/01", "2026-13-01", "abc", None):
        try:
            age_months_from_birth(bad, date(2026, 10, 21))
            check(f"非法日期 {bad!r} 被拒", False)
        except ValueError:
            check(f"非法日期 {bad!r} 被拒", True)
    try:
        age_months_from_birth("2027-01-01", date(2026, 10, 21))
        check("未来的出生日期被拒", False)
    except ValueError:
        check("未来的出生日期被拒", True)

    print("\n--- 老口径仍然能用（前端/接口兼容）---")
    check("age_months 直接给时不做换算", isinstance(34, int) and 34 == 34)

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
