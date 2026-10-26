// 出生日期 → 月龄（**前端这一份只用来显示**，判定用的是后端算的那份）。
//
// ★ 为什么后端已经算了一遍，前端还要再算一次：
//   建档表单里家长刚填完日期，还没点「开始建档」，总得先告诉他
//   「这个日期对应 2 岁 7 个月」对不对 —— 不然他无从判断自己填错了没有。
//   但**判定**（难度闸门、活动选型、日历）读的都是后端那份，
//   两处口径不一致时以**后端**为准。
//
// ★ 口径必须与 `asd_agent/child_age.py` 一致：
//   生日当天算 +1 个月，生日没过还算上个月（"2 岁 10 个月"就是这么说的）。
//   改一处就要改另一处 —— 后端那份有一组断言守着（child_age.py 的自检），
//   前端这份由 BirthDatePicker 的输入校验挡住非法值。

/** 今天（本地时区），格式 YYYY-MM-DD。 */
export function todayISO(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/**
 * 校验一个出生日期字符串。
 * 返回 { ok: true, date: Date } 或 { ok: false, reason: '...' }。
 *
 * ★ 为什么要自己再校验一遍：`<input type="date">` 在多数浏览器里
 *   允许把文本框里的内容删成半截（"2024-0"），此时 value 会变成空串 ——
 *   于是「没填」和「填坏了」长得一模一样，区别只在中间状态。
 *   顺着日期算出来的月龄必须是**真日期**：2024-02-31 会被 Date 滚到 3 月，
 *   于是家长填的 2 月变成了 3 月，而界面上一眼看不出。
 */
export function checkBirthDate(s, max = todayISO()) {
  if (!s || !String(s).trim()) return { ok: false, reason: '还没填出生日期' }
  const v = String(s).trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    return { ok: false, reason: '日期格式应为 YYYY-MM-DD（年-月-日）' }
  }
  const [y, m, d] = v.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  // Date 构造函数会把越界日期滚到下个月（2 月 31 日 → 3 月 2 日），
  // 所以要回头核对它还是不是家长填的那个日子。
  if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) {
    return { ok: false, reason: `${v} 不是真实存在的日期` }
  }
  if (v > max) return { ok: false, reason: '出生日期不能晚于今天' }
  return { ok: true, date: dt }
}

/** 出生日期 → 满月龄（生日当天 +1；生日没过算上个月）。 */
export function monthsOld(birth, today = new Date()) {
  const c = checkBirthDate(birth)
  if (!c.ok) return null
  const b = c.date
  let m = (today.getFullYear() - b.getFullYear()) * 12 + (today.getMonth() - b.getMonth())
  if (today.getDate() < b.getDate()) m -= 1
  return m < 0 ? 0 : m
}

/** 月龄 → 家长读得懂的说法（34 → "2 岁 10 个月"）。 */
export function ageText(months) {
  if (months == null || months < 0) return ''
  const y = Math.floor(months / 12), m = months % 12
  if (y && m) return `${y} 岁 ${m} 个月`
  if (y) return `${y} 岁`
  return `${m} 个月`
}

/** months 个月之前的某一天（当月没有那天就取当月最后一天）。 */
export function monthsAgoDate(months, today = new Date()) {
  const day = today.getDate()
  const d = new Date(today.getFullYear(), today.getMonth(), 1)
  d.setMonth(d.getMonth() - months)
  const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()
  d.setDate(Math.min(day, last))
  return todayISO(d)
}
