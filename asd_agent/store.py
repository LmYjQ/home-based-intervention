# -*- coding: utf-8 -*-
"""
长期记忆存储
============
标准库 sqlite3，零依赖，不上 ORM。这是整个系统里唯一一个"跨天"的东西，
所以它的两条设计决定值得说明。

一、**skill_state 只追加，永不 UPDATE**
-----------------------------------------
能力状态历史就是审计日志。面试时可以直接说：
"每次状态变更可追溯，升级和回退对称。"
这不是为了炫技，是被逼出来的 —— 状态机有回退（练习中 → 入门），
而**"一条能力为什么会退回去"恰恰是家长最需要知道、也最难自己看清的事**。
只存当前值的话，界面上只能显示"入门"，家长看到的是孩子退步了，
看不到"因为连续五天抗拒率 45%"这种解释。存成历史行，两者都能给。

当前状态由"最后一行"得出，所以 append-only 不影响查询效率。

二、**纯 SQL，不拼字符串**
--------------------------
三个必需查询（what_last_week / what_child_refuses / what_progressed）全部返回
Pydantic 对象直接进 prompt，**不在这里拼任何给 LLM 看的字符串**。
理由是拼字符串的地方必然会出现"某天有人改了格式但忘了改提示词"的漂移，
而提示词和 SQL 各改一处是这类系统最常见的线上事故。
渲染留给 `activities.render_*` 这类已经存在的地方。

并发：`threading.Lock` 包住所有 DB 操作 + `PRAGMA journal_mode=WAL`。
**不要用 async sqlite** —— FastAPI 那边已经是同步路由（见 PROGRESS），
收益为零而复杂度翻倍。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_DB = os.getenv("ASD_DB_PATH", str(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "asd_agent.db")))

_LOCK = threading.Lock()          # sqlite3 的连接不可跨线程共享；这里用一把全局锁串行化
_CONNS: dict[str, sqlite3.Connection] = {}


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DEFAULT_DB
    with _LOCK:
        if p not in _CONNS:
            c = sqlite3.connect(p, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")     # 读写不互相阻塞
            c.execute("PRAGMA foreign_keys=ON")
            _init(c)
            c.commit()
            _CONNS[p] = c
        return _CONNS[p]


def _init(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS children (
        child_id      TEXT PRIMARY KEY,
        name          TEXT,
        age_months    INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );

    -- 能力画像会被新报告整体替换，所以这张表按 version 存多份，取最新一份
    CREATE TABLE IF NOT EXISTS ability_profile (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id            TEXT NOT NULL REFERENCES children(child_id),
        created_at          TEXT NOT NULL,
        report_date         TEXT,
        overall_confidence  REAL NOT NULL,
        payload             TEXT NOT NULL      -- AbilityProfile 的 JSON
    );
    CREATE INDEX IF NOT EXISTS idx_profile_child
        ON ability_profile(child_id, id DESC);

    -- ★ 只追加，永不 UPDATE。状态历史即审计日志。
    CREATE TABLE IF NOT EXISTS skill_state (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        skill_item  TEXT NOT NULL,
        domain      TEXT NOT NULL,
        from_state  TEXT NOT NULL,
        to_state    TEXT NOT NULL,
        at_date     TEXT NOT NULL,
        reason      TEXT NOT NULL DEFAULT '',
        evidence    TEXT NOT NULL DEFAULT '{}'   -- 触发它的那几个数字
    );
    CREATE INDEX IF NOT EXISTS idx_state_child ON skill_state(child_id, skill_item, id);

    CREATE TABLE IF NOT EXISTS plans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        plan_date   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        payload     TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_child ON plans(child_id, plan_date DESC);

    CREATE TABLE IF NOT EXISTS sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id      TEXT NOT NULL REFERENCES children(child_id),
        session_date  TEXT NOT NULL,
        activity_id   TEXT NOT NULL,
        outcome       TEXT NOT NULL,      -- success / partial / refused / skipped
        quality       REAL NOT NULL DEFAULT 0,
        out_of_routine INTEGER NOT NULL DEFAULT 0,
        note          TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_session_child ON sessions(child_id, session_date);
    CREATE INDEX IF NOT EXISTS idx_session_act   ON sessions(child_id, activity_id);

    CREATE TABLE IF NOT EXISTS observations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id      TEXT NOT NULL REFERENCES children(child_id),
        observed_at   TEXT NOT NULL,
        redflag_triggered INTEGER NOT NULL DEFAULT 0,
        payload       TEXT NOT NULL      -- Observation 的 JSON
    );
    CREATE INDEX IF NOT EXISTS idx_obs_child ON observations(child_id, observed_at DESC);

    CREATE TABLE IF NOT EXISTS materials_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        logged_at   TEXT NOT NULL,
        items       TEXT NOT NULL       -- JSON list
    );
    """)


# ================================================================== 写入
def ensure_child(conn: sqlite3.Connection, child_id: str, *, name: str = "",
                 age_months: int = 0) -> None:
    with _LOCK:
        conn.execute(
            "INSERT INTO children(child_id,name,age_months,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(child_id) DO UPDATE SET age_months=excluded.age_months",
            (child_id, name, age_months, date.today().isoformat()))
        conn.commit()


def save_profile(conn: sqlite3.Connection, profile) -> int:
    """存一份能力画像。**不替换旧的** —— 同一孩子可能先有旧报告再换新报告，
    保留历史才能回答"他是最近退步了还是一直这样"。取用时按 id 最大的一条。"""
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO ability_profile(child_id,created_at,report_date,"
            "overall_confidence,payload) VALUES(?,?,?,?,?)",
            (profile.child_id, date.today().isoformat(), profile.report_date,
             profile.overall_confidence,
             profile.model_dump_json() if hasattr(profile, "model_dump_json")
             else json.dumps(profile, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid)


def append_state(conn: sqlite3.Connection, child_id: str, transition) -> int:
    """追加一条状态转移。**没有对应的 update/delete 接口，这是故意的。**"""
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO skill_state(child_id,skill_item,domain,from_state,to_state,"
            "at_date,reason,evidence) VALUES(?,?,?,?,?,?,?,?)",
            (child_id, transition.skill_item, transition.domain,
             transition.from_state, transition.to_state,
             transition.at_date, transition.reason,
             json.dumps(transition.evidence, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid)


def save_plan(conn: sqlite3.Connection, child_id: str, plan_date: str, payload) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO plans(child_id,plan_date,created_at,payload) VALUES(?,?,?,?)",
            (child_id, plan_date, date.today().isoformat(),
             payload.model_dump_json() if hasattr(payload, "model_dump_json")
             else json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid)


def log_session(conn: sqlite3.Connection, child_id: str, rec, note: str = "") -> int:
    """记一次练习。rec 是 statemachine.SessionRecord。

    note 之前没被写进 INSERT（表里有列，语句里没有）。接上闭环之后这成了一个
    真问题：同一天两条 refused session 里，第二条来自"同一次练习里的又一次抗拒"，
    而第一条是家长明确报告的那次 —— 两者 outcome/quality 完全一样，
    没有 note 就没法在 DB 里回答"为什么今天有两条"。
    """
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO sessions(child_id,session_date,activity_id,outcome,quality,"
            "out_of_routine,note) VALUES(?,?,?,?,?,?,?)",
            (child_id, rec.date, rec.activity_id, rec.outcome, rec.quality,
             int(rec.out_of_routine), note))
        conn.commit()
        return int(cur.lastrowid)


def log_observation(conn: sqlite3.Connection, child_id: str, observed_at: str,
                    obs, redflag_triggered: bool = False) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO observations(child_id,observed_at,redflag_triggered,payload) "
            "VALUES(?,?,?,?)",
            (child_id, observed_at, int(redflag_triggered),
             obs.model_dump_json() if hasattr(obs, "model_dump_json")
             else json.dumps(obs, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid)


def log_materials(conn: sqlite3.Connection, child_id: str, items: list[str]) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO materials_log(child_id,logged_at,items) VALUES(?,?,?)",
            (child_id, date.today().isoformat(), json.dumps(items, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid)


# ================================================================== 读取
def latest_profile(conn: sqlite3.Connection, child_id: str):
    """取最新一份能力画像（失败时返回 None，不抛 —— 调用方决定要不要降级）。"""
    from capabilities import AbilityProfile
    row = conn.execute(
        "SELECT payload FROM ability_profile WHERE child_id=? ORDER BY id DESC LIMIT 1",
        (child_id,)).fetchone()
    return AbilityProfile.model_validate_json(row["payload"]) if row else None


def current_states(conn: sqlite3.Connection, child_id: str) -> dict[str, dict]:
    """每个技能细项的当前状态。取 skill_state 里该细项的最后一行。

    从没转移过的细项**不在结果里**（而不是给个 not_started）——
    "还没进入过状态机"和"进入了但还在未开始"是不同的两件事。
    """
    rows = conn.execute(
        "SELECT s.* FROM skill_state s "
        "JOIN (SELECT skill_item, MAX(id) AS mid FROM skill_state "
        "      WHERE child_id=? GROUP BY skill_item) m ON s.id = m.mid",
        (child_id,)).fetchall()
    return {
        r["skill_item"]: {
            "state": r["to_state"], "domain": r["domain"],
            "since": r["at_date"], "reason": r["reason"],
            "evidence": json.loads(r["evidence"]),
        }
        for r in rows
    }


def state_history(conn: sqlite3.Connection, child_id: str, skill_item: str) -> list[dict]:
    """某个细项的完整转移史。升级和回退都在。"""
    rows = conn.execute(
        "SELECT from_state,to_state,at_date,reason,evidence FROM skill_state "
        "WHERE child_id=? AND skill_item=? ORDER BY id",
        (child_id, skill_item)).fetchall()
    return [{**dict(r), "evidence": json.loads(r["evidence"])} for r in rows]


def sessions_for_skill(conn: sqlite3.Connection, child_id: str, skill_item: str,
                       *, until: str | None = None):
    """某个技能细项的全部练习记录，转成 SessionRecord 直接喂给状态机。

    关联方式：sessions 里存的是 activity_id，技能的归属从活动库反查
    （一条活动可能练多个细项）。活动库在进程内且被 lru_cache，
    所以这里连一次 SQL 就够了，剩下的走内存。
    """
    from statemachine import SessionRecord

    sql = ("SELECT session_date,activity_id,outcome,quality,out_of_routine "
           "FROM sessions WHERE child_id=?")
    args: list[Any] = [child_id]
    if until:
        sql += " AND session_date<=?"
        args.append(until)
    sql += " ORDER BY session_date"

    by_id = {}
    try:
        from activity_data import load_activities
        by_id = {a.activity_id: a for a in load_activities()}
    except Exception:
        pass   # 活动库不可用时退化为"没有细项归属"，历史按活动粒度保留

    out: list[SessionRecord] = []
    for r in conn.execute(sql, args).fetchall():
        a = by_id.get(r["activity_id"])
        if a is not None and skill_item not in a.target_skill_items:
            continue
        out.append(SessionRecord(date=r["session_date"], activity_id=r["activity_id"],
                                 outcome=r["outcome"], quality=r["quality"],
                                 out_of_routine=bool(r["out_of_routine"])))
    return out


def recent_activity_ids(conn: sqlite3.Connection, child_id: str, days: int = 3,
                        end_date: str | None = None) -> list[str]:
    """最近 N 天排过的活动（用于新鲜度 / 避免重复）。"""
    end = end_date or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT activity_id FROM sessions WHERE child_id=? "
        "AND session_date BETWEEN ? AND ?",
        (child_id, start, end)).fetchall()
    return [r["activity_id"] for r in rows]


def refusal_counts(conn: sqlite3.Connection, child_id: str,
                   lookback_days: int = 30) -> dict[str, int]:
    """activity_id -> 抗拒次数。检索器的冷却期判定要用。"""
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=? AND outcome='refused' AND session_date>=? GROUP BY activity_id",
        (child_id, start)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


def success_history_ids(conn: sqlite3.Connection, child_id: str) -> set[str]:
    """成功过的活动（前置判定用）。一次成功即可 —— 前置是"这项能不能做"，
    不是"这项做得多好"，把标准卡太高会让依赖图在 demo 里完全走不动。"""
    rows = conn.execute(
        "SELECT DISTINCT activity_id FROM sessions "
        "WHERE child_id=? AND outcome IN ('success','partial')",
        (child_id,)).fetchall()
    return {r["activity_id"] for r in rows}


def success_counts(conn: sqlite3.Connection, child_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=? AND outcome='success' GROUP BY activity_id",
        (child_id,)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


def recent_scenes(conn: sqlite3.Connection, child_id: str, days: int = 14) -> list[str]:
    """近期高频场景，来自行为观察表。"""
    start = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT payload FROM observations WHERE child_id=? AND observed_at>=? "
        "ORDER BY observed_at DESC LIMIT 40", (child_id, start)).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            sc = json.loads(r["payload"]).get("scene")
        except Exception:
            continue
        if sc:
            counts[sc] = counts.get(sc, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]


def successes_outside_routine(conn: sqlite3.Connection, child_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=? AND outcome='success' AND out_of_routine=1 GROUP BY activity_id",
        (child_id,)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


# ================================================================== 三个必需查询
class WeeklyRecap(BaseModel):
    """上周发生了什么。给教练 Agent 当上下文。"""
    start: str
    end: str
    session_count: int = 0
    successes: int = 0
    partials: int = 0
    refusals: int = 0
    redflags: int = 0
    activities: list[str] = Field(default_factory=list)
    top_scenes: list[str] = Field(default_factory=list)

    def render(self) -> str:
        if self.session_count == 0 and not self.redflags:
            return f"{self.start} 至 {self.end}：没有记录。"
        return (f"{self.start} 至 {self.end}：做了 {self.session_count} 次"
                f"（成功 {self.successes} / 部分 {self.partials} / 抗拒 {self.refusals}）；"
                f"活动 {'、'.join(self.activities) or '无'}"
                + (f"；常见场景 {'、'.join(self.top_scenes)}" if self.top_scenes else "")
                + (f"；★ 本周有 {self.redflags} 条记录触发了红旗信号" if self.redflags else ""))


class RefusalReport(BaseModel):
    """孩子抗拒什么。**这是家长最常问、也最答不上来的问题。**"""
    lookback_days: int
    total_sessions: int = 0
    refusal_rate: float = 0.0
    by_activity: list[tuple[str, str, int]] = Field(default_factory=list)  # (id, title, 次数)
    by_material: list[tuple[str, int]] = Field(default_factory=list)

    def render(self) -> str:
        if not self.by_activity:
            return f"最近 {self.lookback_days} 天：没有记录到抗拒。"
        lines = [f"最近 {self.lookback_days} 天共 {self.total_sessions} 次练习，"
                 f"其中抗拒 {self.total_scenes_()} 次（{self.refusal_rate:.0%}）："]
        for aid, title, n in self.by_activity:
            lines.append(f"  · {aid} {title} —— 抗拒 {n} 次")
        for m, n in self.by_material:
            lines.append(f"  · 材料「{m}」被拒 {n} 次")
        return "\n".join(lines)

    def total_scenes_(self) -> int:
        return sum(n for _, _, n in self.by_activity)


class ProgressReport(BaseModel):
    """谁进步了。**面试时当场演示的最佳素材**：一条 SQL + 状态机聚合，
    **不需要 LLM，秒回。**"""
    lookback_days: int
    promoted: list[tuple[str, str, str]] = Field(default_factory=list)  # (item, 窗口初, 当前)
    demoted: list[tuple[str, str, str]] = Field(default_factory=list)
    # 因画像可信度不足被闸门挡住、否则本会发生的升级：(item, 实际卡在, 本可到)
    frozen: list[tuple[str, str, str]] = Field(default_factory=list)
    unchanged_count: int = 0

    def render(self) -> str:
        from vocab import ABILITY_STATE_CN
        cn = lambda s: ABILITY_STATE_CN.get(s, s)  # noqa: E731
        if not (self.promoted or self.demoted or self.frozen):
            return f"最近 {self.lookback_days} 天：没有任何技能状态变化。"
        lines = [f"最近 {self.lookback_days} 天的状态变化（按窗口初 → 当前）："]
        for item, a, b in self.promoted:
            lines.append(f"  ↑ {item}：{cn(a)} → {cn(b)}")
        for item, a, b in self.demoted:
            lines.append(f"  ↓ {item}：{cn(a)} → {cn(b)}")
        for item, a, b in self.frozen:
            lines.append(f"  ⏸ {item}：卡在{cn(a)}，本可到{cn(b)} —— "
                         f"画像可信度不足，升级闸门拦下了（数据不够，不猜）")
        lines.append(f"  其余 {self.unchanged_count} 项无变化")
        return "\n".join(lines)


def what_last_week(conn: sqlite3.Connection, child_id: str, end_date: str,
                   days: int = 7) -> WeeklyRecap:
    """上周回顾。一条 GROUP BY + 两条 EXISTS 式查询，无 LLM。"""
    start = (date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, outcome, COUNT(*) AS n FROM sessions "
        "WHERE child_id=? AND session_date BETWEEN ? AND ? GROUP BY activity_id, outcome",
        (child_id, start, end_date)).fetchall()

    from activity_data import get as get_activity
    acts: dict[str, int] = {}
    r = WeeklyRecap(start=start, end=end_date)
    for row in rows:
        n = row["n"]
        r.session_count += n
        acts[row["activity_id"]] = acts.get(row["activity_id"], 0) + n
        if row["outcome"] == "success":
            r.successes += n
        elif row["outcome"] == "partial":
            r.partials += n
        elif row["outcome"] == "refused":
            r.refusals += n

    titles = []
    for aid in sorted(acts):
        a = get_activity(aid)
        titles.append(f"{aid} {a.title}" if a else aid)
    r.activities = titles[:8]
    r.top_scenes = recent_scenes(conn, child_id, days=days)
    r.redflags = conn.execute(
        "SELECT COUNT(DISTINCT observed_at) AS days FROM observations "
        "WHERE child_id=? AND observed_at BETWEEN ? AND ? "
        # ★ COUNT(DISTINCT observed_at) 而不是 COUNT(*) ——
        # 一次家长输入会落成 N 条 observation，它们共享同一个 observed_at。
        # 用 COUNT(*) 时"今天说了一句含自伤的话"会被数成"今天有 3 条红旗"，
        # 而 WeeklyRecap.render() 写的是"本周有 N 条记录触发了红旗信号" ——
        # 家长关心的是**有几天遇到红旗**，不是记了几条。
        # 闭环接线之前 observations 恒为空，这个 bug 一直藏着没人看见。
        "AND redflag_triggered=1", (child_id, start, end_date)).fetchone()["days"]
    return r


def what_child_refuses(conn: sqlite3.Connection, child_id: str,
                       lookback_days: int = 30) -> RefusalReport:
    """抗拒归因。**按活动聚合是主体，按材料聚合是补充** ——
    因为抗拒常常不是"不喜欢这个活动"而是"看到那个球就炸"，
    只按活动看会得出"换一个活动就好了"的错误建议。"""
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=? AND outcome='refused' AND session_date>=? "
        "GROUP BY activity_id ORDER BY n DESC", (child_id, start)).fetchall()

    from activity_data import get as get_activity
    rep = RefusalReport(lookback_days=lookback_days)
    for row in rows:
        a = get_activity(row["activity_id"])
        rep.by_activity.append((row["activity_id"],
                                a.title if a else "（活动已下线）", row["n"]))

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE child_id=? "
        "AND outcome IN ('success','partial','refused') AND session_date>=?",
        (child_id, start)).fetchone()["n"]
    ref = sum(n for _, _, n in rep.by_activity)
    rep.total_sessions = total
    rep.refusal_rate = (ref / total) if total else 0.0
    return rep


def what_progressed(conn: sqlite3.Connection, child_id: str,
                    lookback_days: int = 30,
                    profile_confidence: float | None = None) -> ProgressReport:
    """谁进步了。纯 SQL 聚合状态历史表，**不调 LLM**。

    比较的是**窗口开始时的状态**与**当前状态**，而不是"第一行 vs 最后一行"。
    早期版本比的是后者，于是窗口内先升到泛化、又因连续抗拒退回来这种情况
    会被报成"↑ 泛化中 → 已掌握" —— 方向正好反了，而且掩盖了先升后降的事实。
    窗口口径回答的是家长真正关心的问题："这一个月，他总体上是在进步还是退步。"

    profile_confidence 传进来时会**重新跑一遍状态机**（闸门全开），
    报告"本可到达却被闸门拦下"的状态。这个对照很有说服力：
    它证明闸门是真的在拦，而不是摆设。
    """
    from statemachine import RULES, walk
    from vocab import ABILITY_STATE

    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    today = date.today().isoformat()
    rep = ProgressReport(lookback_days=lookback_days)

    rows = conn.execute(
        "SELECT skill_item, domain, from_state, to_state, at_date FROM skill_state "
        "WHERE child_id=? ORDER BY skill_item, id", (child_id,)).fetchall()

    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for row in rows:
        r = dict(row)
        first.setdefault(r["skill_item"], r)      # 该细项的第一条：从哪来
        last[r["skill_item"]] = r

    for item, lin in last.items():
        # 窗口开始时他处在哪个状态：早于窗口的第一条取它的 from_state
        win = [r for r in rows if r["skill_item"] == item and r["at_date"] >= start]
        s0 = win[0]["from_state"] if win else first[item]["from_state"]
        s1 = lin["to_state"]
        if ABILITY_STATE.index(s1) > ABILITY_STATE.index(s0):
            rep.promoted.append((item, s0, s1))
        elif ABILITY_STATE.index(s1) < ABILITY_STATE.index(s0):
            rep.demoted.append((item, s0, s1))
        else:
            rep.unchanged_count += 1

    # ---- 闸门对照：把升级闸门全开重跑一遍，看差在哪 ----
    # 候选来自 sessions 而不只是 skill_state：闸门如果从第一天就把所有升级挡住，
    # skill_state 里会**一行都没有**。只遍历已记录状态的话，这条对照永远看不到
    # 自己 —— 恰恰是最该被看见的那种情况。
    if profile_confidence is not None and profile_confidence < RULES["upgrade_min_confidence"]:
        from activity_data import load_activities
        from vocab import SKILL_ITEM_DOMAIN

        candidates: dict[str, str] = {item: r["domain"] for item, r in last.items()}
        try:
            by_id = {a.activity_id: a for a in load_activities()}
            for r in conn.execute(
                    "SELECT DISTINCT activity_id FROM sessions WHERE child_id=?",
                    (child_id,)).fetchall():
                a = by_id.get(r["activity_id"])
                if a is None:
                    continue
                for it in a.target_skill_items:
                    candidates.setdefault(it, SKILL_ITEM_DOMAIN.get(it, ""))
        except Exception:
            pass   # 活动库不可用时只用已记录的项，闸门对照退化为空

        for item, dom in candidates.items():
            hist = sessions_for_skill(conn, child_id, item)
            if not hist:
                continue
            dates = sorted({h.date for h in hist})
            ts = walk(skill_item=item, domain=dom, initial_state="not_started",
                      initial_level=1, history=hist, dates=dates,
                      profile_confidence=RULES["upgrade_min_confidence"])  # 闸门全开
            open_state = ts[-1].to_state if ts else "not_started"
            actual = last[item]["to_state"] if item in last else "not_started"
            if ABILITY_STATE.index(open_state) > ABILITY_STATE.index(actual):
                rep.frozen.append((item, actual, open_state))
    return rep


# ================================================================== 自检
def _seed_demo(conn: sqlite3.Connection, child_id: str = "demo-001",
               confidence: float = 0.85) -> str:
    """灌一份两周的 demo 数据。**刻意用真实的 skill_state 转移行，
    而不是直接 UPDATE 当前值** —— 这样 what_progressed 才有东西可查。"""
    from capabilities import AbilityProfile, ParentConstraints, SkillAssessment
    from statemachine import SessionRecord, walk

    cid = child_id
    ensure_child(conn, cid, name="小宇", age_months=34)
    profile = AbilityProfile(
        child_id=cid, child_age_months=34, report_date="2026-09-20",
        assessments=[
            SkillAssessment(domain="social_adapt", skill_item="共同注意", level=1,
                            evidence_quote="很少出现共同注意", source="report_stated",
                            confidence=0.85),
            SkillAssessment(domain="language", skill_item="听懂单词", level=3,
                            evidence_quote="能听懂简单的名词性指令", source="report_stated",
                            confidence=0.85),
        ],
        priority_domains=["social_adapt", "language"],
        parent_constraints=ParentConstraints(daily_minutes_available=30,
                                             materials_at_home=["杯子", "绘本", "小汽车"]),
        overall_confidence=confidence,
    )
    save_profile(conn, profile)

    # sessions
    import statemachine as sm
    history = sm._demo_history()
    for rec in history:
        log_session(conn, cid, rec)

    # 状态转移：**真的走一遍状态机再落库**，保证 DB 里的历史与算法完全一致。
    # 直接手写几行 INSERT 看着快，但那样 demo 展示的"状态历史"就和
    # statemachine.py 的真实规则脱钩了 —— 改阈值后演示会撒谎。
    dates = sorted({r.date for r in history})
    for t in walk(skill_item="共同注意", domain="social_adapt", initial_state="not_started",
                  initial_level=1, history=history, dates=dates,
                  profile_confidence=confidence):
        append_state(conn, cid, t)     # t.at_date 由 propose_transition 自己填
    return cid


def _cli() -> None:
    import shutil
    tmp = "/tmp/asd_agent_demo.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suffix)
        except FileNotFoundError:
            pass

    conn = connect(tmp)
    cid = _seed_demo(conn, "demo-001", confidence=0.85)
    # 同一个孩子、同一份练习记录，只把画像可信度从 0.85 降到 0.30 再跑一遍。
    # 两份 skill_state 一对比，就是升级闸门在存储层的证据。
    _seed_demo(conn, "demo-002", confidence=0.30)

    print("=== 表 ===")
    for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {r['name']}").fetchone()["n"]
        print(f"  {r['name']:18s} {n:4d} 行")

    print("\n=== what_last_week（最近 7 天，截至今天）===")
    print(what_last_week(conn, cid, date.today().isoformat()).render())

    print("\n=== what_child_refuses（30 天）===")
    print(what_child_refuses(conn, cid, 30).render())

    print("\n=== what_progressed（30 天）===")
    print(what_progressed(conn, cid, 30, profile_confidence=0.85).render())

    print("\n=== ★ 升级闸门在存储层的证据：同一份练习记录，只改画像可信度 ===")
    print(f"  demo-001（画像可信度 0.85，闸门开）  {len(state_history(conn, 'demo-001', '共同注意'))} 次转移")
    for h in state_history(conn, "demo-001", "共同注意"):
        print(f"      {h['at_date']}  → {h['to_state']}")
    print(f"  demo-002（画像可信度 0.30，闸门关）  {len(state_history(conn, 'demo-002', '共同注意'))} 次转移")
    for h in state_history(conn, "demo-002", "共同注意"):
        print(f"      {h['at_date']}  → {h['to_state']}")
    print("  ^ 35 条练习记录一模一样，状态轨迹完全不同。")
    print("    闸门挡住的不是「不做事」，是「在一条可能本就不存在的起点上继续往上盖」。")

    print("\n  what_progressed(demo-002) —— 同时报出「本可到哪」：")
    print(what_progressed(conn, "demo-002", 30, profile_confidence=0.30).render())

    print("\n=== current_states ===")
    for item, s in current_states(conn, cid).items():
        print(f"  {item:10s} {s['state']:12s} 自 {s['since']} 起  ({s['reason'][:36]})")

    print("\n=== state_history('共同注意') —— append-only 的收益 ===")
    for h in state_history(conn, cid, "共同注意"):
        arrow = "↑" if h["to_state"] != h["from_state"] else "="
        print(f"  {h['at_date']}  {h['from_state']:12s} → {h['to_state']:12s}  {h['reason'][:44]}")

    print("\n=== sessions_for_skill('共同注意') → 状态机输入 ===")
    recs = sessions_for_skill(conn, cid, "共同注意")
    print(f"  {len(recs)} 条，首条 {recs[0].date} 末条 {recs[-1].date}")

    print("\n=== 检索器要用的记忆切片 ===")
    print(f"  refusal_counts        {refusal_counts(conn, cid)}")
    print(f"  success_counts        {success_counts(conn, cid)}")
    print(f"  recent_activity_ids   {recent_activity_ids(conn, cid, days=3)}")
    print(f"  success_history_ids   {len(success_history_ids(conn, cid))} 条")
    print(f"  recent_scenes         {recent_scenes(conn, cid)}")

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _cli()
