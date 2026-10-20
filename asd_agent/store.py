# -*- coding: utf-8 -*-
"""
长期记忆存储
============
PostgreSQL（psycopg3）+ 纯 SQL，不上 ORM。这是整个系统里唯一一个"跨天"的东西，
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

并发：**每线程一个连接，不加锁**。这一段是本文件迁移到 PostgreSQL 时
改动最大、也最有价值的地方，理由见 `connect()` 的 docstring。
**不要用 async psycopg** —— FastAPI 那边是同步路由（见 PROGRESS），
收益为零而复杂度翻倍。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

Connection = psycopg.Connection

DEFAULT_DSN = os.getenv(
    "ASD_PG_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/ganyu")
DEFAULT_DB = os.getenv("ASD_DB_PATH", str(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "asd_agent.db")))

_TLS = threading.local()      # 每线程一份连接，见 connect()


def _schema_of(path: str) -> str:
    """/tmp/feedback_check.db → "feedback_check"。

    **为什么要把「路径」翻译成「schema」而不是直接换成数据库名：**
    调用点传路径的目的是「拿一个干净、互不干扰的环境」——
    自检脚本删掉 /tmp/xxx.db 再连，就是为了让这张表从零开始。
    换成独立的 database 也能做到，但 `CREATE DATABASE` 不能在事务里跑、
    不能被连接池复用、删库还得先断连接，三处都比 schema 麻烦。
    schema 是同一个 database 里的命名空间，`DROP SCHEMA ... CASCADE`
    一条语句就等于「删掉这个 db 文件」，语义一一对应。
    """
    name = os.path.basename(path)
    if name.endswith(".db"):
        name = name[:-3]
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_").lower()
    return name or "ganyu"


def connect(path: str | None = None) -> psycopg.Connection:
    """**每线程一个连接。CLI 和服务都用这一个入口。**

    ★ 为什么每线程一个，而不是像迁移前那样按路径缓存一个共享连接：
      sqlite3 的**游标**不是线程安全的，`check_same_thread=False` 只是
      「不检查线程归属」而**不等于线程安全** —— 两个线程同时 `execute()`
      同一个连接会抛 `InterfaceError: bad parameter or other API misuse`。
      这不是理论：前端 `Promise.all` 并发打 `/api/progress` + `/api/summaries`，
      实测 180 个请求失败 8 个，而串行连打 15 次全绿。

      psycopg3 没有这个问题（服务端协议不依赖客户端游标状态，
      同一连接并发 execute 实测 12 线程 × 40 次查询 0 失败），
      但**仍然每线程一个连接** —— 共享连接会让所有请求排成一条队，
      psycopg 文档也建议每线程一个。这里把 `connect_thread_local()`
      合流进 `connect()`：迁移前它们是两个入口、各自一套缓存，
      合并后只有一个，**不会再出现「某个调用点忘了用 thread_local 版」**。

    ★ 为什么是 autocommit（2026-10-20 实测后改）
    --------------------------------------------------
    连接是**长连接**且按线程缓存的，所以在非 autocommit 下，任何一次
    普通 SELECT 都会开一个事务，而它**永远不会被提交或回滚** ——
    读操作全项目一次 `rollback()` 都没有。于是连接会在 `pg_stat_activity`
    里长期停在 `idle in transaction`。

    这不是洁癖问题，它有两个真实后果：
      1. `CREATE INDEX CONCURRENTLY` 等的是**整个 database** 的 virtualxid，
         与 schema 无关 → 任何一条 idle-in-transaction 都能把 checkpointer 的
         `setup()` 卡死（实测卡了 3 分钟不出）
      2. 长期空转事务会挡住 VACUUM

    改成 autocommit **语义零变化**：本项目所有写函数
    （ensure_child / save_profile / append_state / save_plan /
      log_session / log_observation / save_summary …）都是**各自结尾
    单独 `commit()`**，没有一处多语句事务，事务模型本来就是「一条语句
    一个事务」。那些 `commit()` 调用保留着——autocommit 下它们是无害的
    no-op（psycopg3 允许），万一哪天关掉 autocommit 仍然是正确的。

    实测：autocommit=False 时 SELECT 后 `transaction_status=2`（ACTIVE，事务开着），
    autocommit=True 时 `=0`（无事务）。
    """
    p = path or DEFAULT_DB
    sch = _schema_of(p)
    conns = getattr(_TLS, "conns", None)
    if conns is None:
        conns = _TLS.conns = {}
    if sch not in conns:
        c = psycopg.connect(DEFAULT_DSN, autocommit=True)
        c.row_factory = dict_row                  # 保住 row["col"] 与 dict(row)
        with c.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
            cur.execute(f'SET search_path TO "{sch}"')
        c.commit()
        _init(c)
        conns[sch] = c
    return conns[sch]


def connect_thread_local(path: str | None = None) -> psycopg.Connection:
    """**已废弃，只是 `connect()` 的别名。**

    保留它是因为 `server.py` 曾经用它绕开 sqlite 的并发 bug，
    而调用点不必为此改动。语义已经完全相同。
    """
    return connect(path)


def reset(path: str | None = None) -> psycopg.Connection:
    """★ 替代迁移前那些 `os.remove("/tmp/xxx.db" + 后缀)` 的自检开头。**

    「删掉临时库、重新连一个干净的」= `DROP SCHEMA ... CASCADE` + 建表。
    没有这一步，自检脚本第二次跑会因为上一轮的残留行而误报。
    """
    p = path or DEFAULT_DB
    sch = _schema_of(p)
    c = connect(p)
    c.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    c.commit()
    conns = getattr(_TLS, "conns", None)
    if conns and sch in conns:
        del conns[sch]        # 让下一次 connect() 重建 schema 与表
    return connect(p)


def _init(c: psycopg.Connection) -> None:
    # psycopg3 没有 executescript，但多语句单次 execute 是支持的（已实测）
    c.execute("""
    CREATE TABLE IF NOT EXISTS children (
        child_id      TEXT PRIMARY KEY,
        name          TEXT,
        age_months    INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );

    -- 能力画像会被新报告整体替换，所以这张表按 version 存多份，取最新一份
    CREATE TABLE IF NOT EXISTS ability_profile (
        id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        plan_date   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        payload     TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_child ON plans(child_id, plan_date DESC);

    CREATE TABLE IF NOT EXISTS sessions (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id      TEXT NOT NULL REFERENCES children(child_id),
        session_date  TEXT NOT NULL,
        activity_id   TEXT NOT NULL,
        outcome       TEXT NOT NULL,      -- success / partial / refused / skipped
        quality       REAL NOT NULL DEFAULT 0,
        -- ★ BOOLEAN 而不是 INTEGER 0/1：迁移前存的是 int(rec.out_of_routine)，
        --   读的时候又 bool(...) 包一层，两处都在假装这是个布尔。
        --   换成真 BOOLEAN 后 psycopg 直接给 Python bool，两处转换都可以删。
        out_of_routine BOOLEAN NOT NULL DEFAULT FALSE,
        note          TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_session_child ON sessions(child_id, session_date);
    CREATE INDEX IF NOT EXISTS idx_session_act   ON sessions(child_id, activity_id);

    CREATE TABLE IF NOT EXISTS observations (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id      TEXT NOT NULL REFERENCES children(child_id),
        observed_at   TEXT NOT NULL,
        redflag_triggered BOOLEAN NOT NULL DEFAULT FALSE,
        payload       TEXT NOT NULL,     -- Observation 的 JSON
        -- ★ 家长**整段原话**，同一段抽出的多条事件共享它。
        --   为什么必须单独存一列：payload 里只有单条事件的 raw_quote，
        --   而 coach.to_coach_inputs 的契约是 raw 填整段原话 —— 用碎片拼 corpus
        --   会让教练的引文回指校验把正常信号全判成编造（见 observe.py 的说明）。
        --   没有它，"把攒下的观察喂给明天的教练"这条路在跨进程后就没有原料了。
        source        TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_obs_child ON observations(child_id, observed_at DESC);

    CREATE TABLE IF NOT EXISTS materials_log (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        logged_at   TEXT NOT NULL,
        items       TEXT NOT NULL       -- JSON list
    );

    -- 日结：能区方向 / 跨天异常 / 给家长的一句话。
    -- ★ 这张表是"日结"第一次真正落地。原先 DomainEvidence / flags / parent_note
    --   只在 DailyEval 里被 render() 打印，然后丢掉 —— 全项目无人消费、不落库。
    --   也就是说，"今天整体在往哪走"这句话从头到尾没进过记忆。
    --   现在它落库，明天的 build_request 会读（loop.build_request 的 recent_summary），
    --   教练 Agent 第一次能拿到方向而不只是三个数字。
    CREATE TABLE IF NOT EXISTS daily_summary (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id    TEXT NOT NULL REFERENCES children(child_id),
        on_date     TEXT NOT NULL,
        payload     TEXT NOT NULL      -- DailySummary 的 JSON
    );
    CREATE INDEX IF NOT EXISTS idx_dsum_child ON daily_summary(child_id, on_date DESC);

    -- ★ Phase 2：红旗从「响应里飘一次」变成一条长期记录。
    --   在此之前红旗只活在一次 HTTP 响应里（RedFlagList.vue 一卸载就消失），
    --   而 observations.redflag_triggered 只是个布尔 —— 它回答不了
    --   「是哪一条信号、什么时候、家长原话里哪一段」。而那正是唯一有用的问题。
    --
    --   ★ 这是全项目**唯一一处可变的业务字段**（status），所以边界写在这里：
    --     · 行的**身份**（命中了什么、什么时候、哪段话）append-only，检测永不重写；
    --     · 只有**家长的确认**在动：open → acknowledged → closed。
    --     · 行永不删除。转移历史靠 status_changed_at 记时点，
    --       完整的转移流水留给 Phase 5 的 workflow_audit_events 一并做。
    --     · Phase 3 的 plans.status 会用同一套约定 —— 两处一起引入，
    --       免得相隔两个阶段各解释一遍"为什么这张表可以改"。
    CREATE TABLE IF NOT EXISTS red_flags (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        child_id      TEXT NOT NULL REFERENCES children(child_id),
        observed_at   TEXT NOT NULL,
        code          TEXT NOT NULL,
        severity      TEXT NOT NULL,        -- urgent / high / watch
        flags_group   TEXT NOT NULL DEFAULT '',   -- "group" 在 SQL 里是保留字
        quote         TEXT NOT NULL DEFAULT '',   -- 家长原话里命中的那一片
        ask           TEXT NOT NULL DEFAULT '',   -- 该追问家长的问题
        -- 转诊指引**跟着这一行走**，不跟着规则表走：规则表在代码里，
        -- 而"三个月前那次撞头当时该怎么处理"要能在 DB 里原样取回。
        referral_who  TEXT NOT NULL DEFAULT '',
        referral_do   TEXT NOT NULL DEFAULT '',
        referral_avoid TEXT NOT NULL DEFAULT '',   -- 防害字段，不允许为空
        referral_emergency TEXT NOT NULL DEFAULT '',
        -- ★ 全项目唯一一处可变的业务字段。理由与边界见下面的注释块。
        status        TEXT NOT NULL DEFAULT 'open',   -- open/acknowledged/closed
        status_changed_at TEXT NOT NULL DEFAULT '',
        source        TEXT NOT NULL DEFAULT '',   -- 被扫描的那整段话（人要看）
        source_hash   TEXT NOT NULL DEFAULT '',   -- 同一段话 + 同一条 code 只落一行
        source_kind   TEXT NOT NULL DEFAULT 'observation',  -- observation / report
        thread_id     TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL
    );
    -- ★ 幂等键。ON CONFLICT DO NOTHING 靠的就是它。
    --   为什么不用原文进唯一索引：btree unique index 遇到超长 text 会报
    --   "index row size exceeds maximum"，而 other_notes 允许 20000 字符。
    --   原文照样存 source 列，只是索引里放它的 sha256。
    --   为什么不用 SELECT-then-INSERT：connect() 是每线程一条连接，
    --   前端并发下两个线程都能看到"没有这行"然后都 INSERT。
    --   ★ 键里**不含 thread_id** —— 分叉出来的 thread 重跑同一段话，
    --     不应该在真实历史里多出一条红旗。
    CREATE UNIQUE INDEX IF NOT EXISTS uq_red_flag_once
        ON red_flags(child_id, observed_at, code, source_hash);
    CREATE INDEX IF NOT EXISTS idx_rf_child_status
        ON red_flags(child_id, status, observed_at DESC);
    """)
    c.commit()


# ================================================================== 写入
# ★ 迁移后这里不再有 _LOCK。sqlite 需要全局锁是因为「一个连接 + 非线程安全的游标」，
#   PostgreSQL 里每线程一个连接，写冲突由数据库的行锁解决
#   （我们全是单行 INSERT，不涉及行锁升级为表锁的经典死锁形状）。
#   留着那把锁只会让并发写重新排队，把刚解决的问题请回来。
def ensure_child(conn: psycopg.Connection, child_id: str, *, name: str = "",
                 age_months: int = 0) -> None:
    conn.execute(
        "INSERT INTO children(child_id,name,age_months,created_at) VALUES(%s,%s,%s,%s) "
        "ON CONFLICT(child_id) DO UPDATE SET age_months=excluded.age_months",
        (child_id, name, age_months, date.today().isoformat()))
    conn.commit()


def save_profile(conn: psycopg.Connection, profile) -> int:
    """存一份能力画像。**不替换旧的** —— 同一孩子可能先有旧报告再换新报告，
    保留历史才能回答"他是最近退步了还是一直这样"。取用时按 id 最大的一条。"""
    # RETURNING id：迁移前靠 sqlite 的 cursor.lastrowid，psycopg3 没有这个属性。
    # 这是本文件里唯一一处「驱动差异逼着改写法」的地方，其余 INSERT 只换占位符。
    row = conn.execute(
        "INSERT INTO ability_profile(child_id,created_at,report_date,"
        "overall_confidence,payload) VALUES(%s,%s,%s,%s,%s) RETURNING id",
        (profile.child_id, date.today().isoformat(), profile.report_date,
         profile.overall_confidence,
         profile.model_dump_json() if hasattr(profile, "model_dump_json")
         else json.dumps(profile, ensure_ascii=False))).fetchone()
    conn.commit()
    return int(row["id"])


def append_state(conn: psycopg.Connection, child_id: str, transition) -> int:
    """追加一条状态转移。**没有对应的 update/delete 接口，这是故意的。**"""
    row = conn.execute(
        "INSERT INTO skill_state(child_id,skill_item,domain,from_state,to_state,"
        "at_date,reason,evidence) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (child_id, transition.skill_item, transition.domain,
         transition.from_state, transition.to_state,
         transition.at_date, transition.reason,
         json.dumps(transition.evidence, ensure_ascii=False))).fetchone()
    conn.commit()
    return int(row["id"])


def save_plan(conn: psycopg.Connection, child_id: str, plan_date: str, payload) -> int:
    row = conn.execute(
        "INSERT INTO plans(child_id,plan_date,created_at,payload) "
        "VALUES(%s,%s,%s,%s) RETURNING id",
        (child_id, plan_date, date.today().isoformat(),
         payload.model_dump_json() if hasattr(payload, "model_dump_json")
         else json.dumps(payload, ensure_ascii=False))).fetchone()
    conn.commit()
    return int(row["id"])


def latest_plan(conn: psycopg.Connection, child_id: str, plan_date: str) -> dict | None:
    """取某一天已生成的计划。**没有就是 None** —— 调用方据此决定要不要重新生成。

    ★ 这条读取是"今天这张卡片只生成一次"的落点。
      之前每次进「今天」页都重跑一遍检索 + 两次 LLM（30-45s），
      而家长点开页面看到的其实是**同一份结果**（输入没变），
      变的只有等待时间。生成一次、之后直接读，落库比前端缓存强：
      前端缓存换个标签页就丢，而"今天的卡片"本就该跨会话不变。

      只追加不替换（同 ability_profile），取 id 最大的一条。
    """
    row = conn.execute(
        "SELECT payload FROM plans WHERE child_id=%s AND plan_date=%s "
        "ORDER BY id DESC LIMIT 1", (child_id, plan_date)).fetchone()
    return json.loads(row["payload"]) if row else None


def log_session(conn: psycopg.Connection, child_id: str, rec, note: str = "") -> int:
    """记一次练习。rec 是 statemachine.SessionRecord。

    note 之前没被写进 INSERT（表里有列，语句里没有）。接上闭环之后这成了一个
    真问题：同一天两条 refused session 里，第二条来自"同一次练习里的又一次抗拒"，
    而第一条是家长明确报告的那次 —— 两者 outcome/quality 完全一样，
    没有 note 就没法在 DB 里回答"为什么今天有两条"。
    """
    # out_of_routine 直接传 bool：列已经是 BOOLEAN，迁移前的 int(...) 是
    # 在假装它是整数。
    row = conn.execute(
        "INSERT INTO sessions(child_id,session_date,activity_id,outcome,quality,"
        "out_of_routine,note) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (child_id, rec.date, rec.activity_id, rec.outcome, rec.quality,
         bool(rec.out_of_routine), note)).fetchone()
    conn.commit()
    return int(row["id"])


def log_observation(conn: psycopg.Connection, child_id: str, observed_at: str,
                    obs, redflag_triggered: bool = False,
                    source: str = "") -> int:
    """source 是家长**整段原话**（同一段抽出的多条事件共享）。

    见 observations 表 source 列的注释：不存它，跨进程把观察喂给教练时就
    只能拼碎片，引文回指校验会把正常信号判成编造。
    """
    row = conn.execute(
        "INSERT INTO observations(child_id,observed_at,redflag_triggered,"
        "payload,source) VALUES(%s,%s,%s,%s,%s) RETURNING id",
        (child_id, observed_at, bool(redflag_triggered),
         obs.model_dump_json() if hasattr(obs, "model_dump_json")
         else json.dumps(obs, ensure_ascii=False), source)).fetchone()
    conn.commit()
    return int(row["id"])


def log_materials(conn: psycopg.Connection, child_id: str, items: list[str]) -> int:
    row = conn.execute(
        "INSERT INTO materials_log(child_id,logged_at,items) VALUES(%s,%s,%s) "
        "RETURNING id",
        (child_id, date.today().isoformat(), json.dumps(items, ensure_ascii=False))
    ).fetchone()
    conn.commit()
    return int(row["id"])


# ================================================================== 红旗（Phase 2）
# 判定的出处是 `redflag.scan`（纯正则、零 token）；这里只管存与取。
# 刻意不把判定搬进来 —— 否则将来有人改规则时会顺手改掉写库逻辑，
# 而"红旗永远不会漏掉"这件事正是靠规则与模型无关保证的（见 redflag.py 顶部）。
_RED_FLAG_COLS = ("code", "severity", "flags_group", "quote", "ask",
                   "referral_who", "referral_do", "referral_avoid", "referral_emergency")


def _source_hash(source: str) -> str:
    """幂等键的一半。**只在写路径与唯一索引上用**，不做查询谓词
    —— `nodes._source_published` 刻意不 hash 是因为它查的是非唯一列，
    一天几行，走等值比较即可；这里是 unique 约束，必须有长度上界。"""
    import hashlib
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


def append_red_flags(conn: psycopg.Connection, child_id: str, on_date: str,
                     rows: list[dict], *, source: str = "",
                     source_kind: str = "observation",
                     thread_id: str = "") -> list[int]:
    """落一批红旗，返回**真正新插入**的 id。

    `rows` 是 `redflag.rows_for(report)` 的产物（纯 dict）。

    ★ 返回值刻意只含新插入的行：重跑一次得到 `[]` 是对的，
      而如果它返回"库里现在有几条"，调用方就分不清"这次新增"和"本来就有"
      —— 前端要说"今天记下了 2 条红旗"时，那必须是新增。
    """
    if not rows:
        return []
    src_hash = _source_hash(source)
    created = datetime.now().isoformat(timespec="seconds")
    ids: list[int] = []
    for r in rows:
        row = conn.execute(
            "INSERT INTO red_flags(child_id,observed_at,code,severity,flags_group,"
            "quote,ask,referral_who,referral_do,referral_avoid,referral_emergency,"
            "source,source_hash,source_kind,thread_id,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (child_id, observed_at, code, source_hash) DO NOTHING "
            "RETURNING id",
            (child_id, on_date, r["code"], r["severity"], r.get("group", ""),
             r.get("quote", ""), r.get("ask", ""),
             r.get("referral_who", ""), r.get("referral_do", ""),
             r.get("referral_avoid", ""), r.get("referral_emergency", ""),
             source, src_hash, source_kind, thread_id, created)).fetchone()
        if row:
            ids.append(int(row["id"]))
    conn.commit()
    return ids


def set_red_flag_status(conn: psycopg.Connection, flag_id: int, status: str) -> None:
    """执行状态变更。**合法性不在这儿判** —— 判据是 `redflag.can_transition`，
    由调用方（HTTP 层）在拿到当前值后先判一次。

    刻意分成"判"与"执行"两半：判据是纯函数、可单测、无 token；
    而这里只需要一行 UPDATE。两件事混在一个函数里，
    就会出现"为了省一次查询，把判据也搬进 SQL"，那就再也测不动了。
    """
    conn.execute(
        "UPDATE red_flags SET status=%s, status_changed_at=%s WHERE id=%s",
        (status, datetime.now().isoformat(timespec="seconds"), flag_id))
    conn.commit()


# ================================================================== 读取
def list_red_flags(conn: psycopg.Connection, child_id: str, *,
                   status: str | None = None, limit: int = 50) -> list[dict]:
    """红旗列表。默认按「未关闭在前 + 时间新在前」排。

    ★ 排序里那个 `status='closed'` 的判断是排序键而不是过滤条件 ——
      家长要能在同一个列表里看到"还没处理的"和"处理过但仍需回看的"，
      只是前者排前面。全滤掉等于让"已处理"变成"消失"，
      而消失的那条恰恰是复诊时最该拿出来的那条。
    """
    sql = ("SELECT * FROM red_flags WHERE child_id=%s "
           + ("AND status=%s " if status else "")
           + "ORDER BY (status='closed'), observed_at DESC, id DESC LIMIT %s")
    args = (child_id, status, limit) if status else (child_id, limit)
    return list(conn.execute(sql, args).fetchall())


def get_red_flag(conn: psycopg.Connection, flag_id: int,
                 child_id: str | None = None) -> dict | None:
    """取一行。`child_id` 给了就顺带做归属校验。

    归属不对时返回 None 而不是抛 —— 调用方统一翻译成 404。
    为什么不是 403：这里根本没有多用户身份体系（Phase 6 才做），
    "这条不属于你"与"这条不存在"对调用方是同一件事。
    """
    row = conn.execute("SELECT * FROM red_flags WHERE id=%s",
                       (flag_id,)).fetchone()
    if not row:
        return None
    if child_id is not None and row["child_id"] != child_id:
        return None
    return row


def open_urgent_red_flags(conn: psycopg.Connection, child_id: str) -> list[dict]:
    """未关闭的 urgent 红旗。**暂停的根据，也是横幅要显示的内容 —— 一次查询两用。**"""
    return list(conn.execute(
        "SELECT * FROM red_flags WHERE child_id=%s AND severity='urgent' "
        "AND status<>'closed' ORDER BY observed_at DESC, id DESC",
        (child_id,)).fetchall())


def plan_paused(conn: psycopg.Connection, child_id: str) -> bool:
    """★ 「暂停计划生成」的**唯一口径**。Phase 3 的 generate_plan 必须调它。

    判据：存在一条 severity='urgent' 且 status<>'closed' 的红旗。

    两个刻意的选择：

    · 「未关闭」而不是「未看到」—— 家长点了"已看到"不代表这件事处理完了。
      临床上这就是 fail-closed 的默认值，也是 LANGGRAPH.md 那条
      「模型给证据，Python 做判定」的延伸：**拿不准的时候停下来问人**。

    · 按**孩子**判而不是按天判 —— 10-20 的自伤必须让 10-21 的计划也停着。
      按天的暂停会在第二天早上自己解开，而那正是它最不该发生的时候。

    没有为它单开一列或一张表：计划链 Phase 3 才加 plans.status，
    现在预造那套就是 builder.py 说的「画一版注定要改的图」。
    暂停的**判据**从现在起就是稳定的，缺的只是"这次决定"的记录，
    那属于 Phase 3。要加过期策略就改这一条 SQL，**不要**再加第二处定义。
    """
    return bool(conn.execute(
        "SELECT 1 FROM red_flags WHERE child_id=%s AND severity='urgent' "
        "AND status<>'closed' LIMIT 1", (child_id,)).fetchone())


def latest_profile(conn: psycopg.Connection, child_id: str):
    """取最新一份能力画像（失败时返回 None，不抛 —— 调用方决定要不要降级）。"""
    from capabilities import AbilityProfile
    row = conn.execute(
        "SELECT payload FROM ability_profile WHERE child_id=%s ORDER BY id DESC LIMIT 1",
        (child_id,)).fetchone()
    return AbilityProfile.model_validate_json(row["payload"]) if row else None


def latest_profile_id(conn: psycopg.Connection, child_id: str) -> int | None:
    """最新画像那行的 id。**用来判断已缓存的当天计划是不是过期了。**

    重新建档之后，画像变了（家长可能把每天时间从 30 改成 45），
    之前按旧画像装箱的那一天就该重算 —— 否则家长改了条件却看不到任何变化，
    而界面上也没有任何地方告诉他"这份计划是按旧条件排的"。
    """
    row = conn.execute(
        "SELECT id FROM ability_profile WHERE child_id=%s ORDER BY id DESC LIMIT 1",
        (child_id,)).fetchone()
    return int(row["id"]) if row else None


def current_states(conn: psycopg.Connection, child_id: str) -> dict[str, dict]:
    """每个技能细项的当前状态。取 skill_state 里该细项的最后一行。

    从没转移过的细项**不在结果里**（而不是给个 not_started）——
    "还没进入过状态机"和"进入了但还在未开始"是不同的两件事。
    """
    rows = conn.execute(
        "SELECT s.* FROM skill_state s "
        "JOIN (SELECT skill_item, MAX(id) AS mid FROM skill_state "
        "      WHERE child_id=%s GROUP BY skill_item) m ON s.id = m.mid",
        (child_id,)).fetchall()
    return {
        r["skill_item"]: {
            "state": r["to_state"], "domain": r["domain"],
            "since": r["at_date"], "reason": r["reason"],
            "evidence": json.loads(r["evidence"]),
        }
        for r in rows
    }


def state_history(conn: psycopg.Connection, child_id: str, skill_item: str) -> list[dict]:
    """某个细项的完整转移史。升级和回退都在。"""
    rows = conn.execute(
        "SELECT from_state,to_state,at_date,reason,evidence FROM skill_state "
        "WHERE child_id=%s AND skill_item=%s ORDER BY id",
        (child_id, skill_item)).fetchall()
    return [{**dict(r), "evidence": json.loads(r["evidence"])} for r in rows]


def sessions_for_skill(conn: psycopg.Connection, child_id: str, skill_item: str,
                       *, until: str | None = None):
    """某个技能细项的全部练习记录，转成 SessionRecord 直接喂给状态机。

    关联方式：sessions 里存的是 activity_id，技能的归属从活动库反查
    （一条活动可能练多个细项）。活动库在进程内且被 lru_cache，
    所以这里连一次 SQL 就够了，剩下的走内存。
    """
    from statemachine import SessionRecord

    sql = ("SELECT session_date,activity_id,outcome,quality,out_of_routine "
           "FROM sessions WHERE child_id=%s")
    args: list[Any] = [child_id]
    if until:
        sql += " AND session_date<=%s"
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
                                 out_of_routine=r["out_of_routine"]))
    return out


def recent_activity_ids(conn: psycopg.Connection, child_id: str, days: int = 3,
                        end_date: str | None = None) -> list[str]:
    """最近 N 天排过的活动（用于新鲜度 / 避免重复）。"""
    end = end_date or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT activity_id FROM sessions WHERE child_id=%s "
        "AND session_date BETWEEN %s AND %s",
        (child_id, start, end)).fetchall()
    return [r["activity_id"] for r in rows]


def refusal_counts(conn: psycopg.Connection, child_id: str,
                   lookback_days: int = 30) -> dict[str, int]:
    """activity_id -> 抗拒次数。检索器的冷却期判定要用。"""
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=%s AND outcome='refused' AND session_date>=%s GROUP BY activity_id",
        (child_id, start)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


def success_history_ids(conn: psycopg.Connection, child_id: str) -> set[str]:
    """成功过的活动（前置判定用）。一次成功即可 —— 前置是"这项能不能做"，
    不是"这项做得多好"，把标准卡太高会让依赖图在 demo 里完全走不动。"""
    rows = conn.execute(
        "SELECT DISTINCT activity_id FROM sessions "
        "WHERE child_id=%s AND outcome IN ('success','partial')",
        (child_id,)).fetchall()
    return {r["activity_id"] for r in rows}


def success_counts(conn: psycopg.Connection, child_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=%s AND outcome='success' GROUP BY activity_id",
        (child_id,)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


def recent_scenes(conn: psycopg.Connection, child_id: str, days: int = 14) -> list[str]:
    """近期高频场景，来自行为观察表。"""
    start = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT payload FROM observations WHERE child_id=%s AND observed_at>=%s "
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


def successes_outside_routine(conn: psycopg.Connection, child_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=%s AND outcome='success' AND out_of_routine GROUP BY activity_id",
        (child_id,)).fetchall()
    return {r["activity_id"]: r["n"] for r in rows}


# ================================================================== 日结
def save_summary(conn: psycopg.Connection, child_id: str, summary) -> int:
    """存一天的日结。summary 是 summary.DailySummary。

    同一天可以有多条（重跑），取用时按 on_date 取 id 最大的一条 ——
    与 ability_profile 同样的口径，不做 UPDATE。
    """
    row = conn.execute(
        "INSERT INTO daily_summary(child_id,on_date,payload) VALUES(%s,%s,%s) "
        "RETURNING id",
        (child_id, summary.date,
         summary.model_dump_json() if hasattr(summary, "model_dump_json")
         else json.dumps(summary, ensure_ascii=False))).fetchone()
    conn.commit()
    return int(row["id"])


def latest_summary(conn: psycopg.Connection, child_id: str, *,
                   days: int = 7, before: str | None = None) -> list[dict]:
    """最近 N 天的日结，按日期升序。**纯 SQL，不调 LLM。**

    before 用来排除"今天"：明天的教练 Agent 不该读到今天刚写的日结 ——
    那等于用今天的结果去解释今天要做什么，方向反了。反馈影响的是明天。
    """
    end = before or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT on_date,payload FROM daily_summary WHERE child_id=%s "
        "AND on_date>=%s AND on_date<%s ORDER BY on_date, id",
        (child_id, start, end)).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            continue
    return out


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


def what_last_week(conn: psycopg.Connection, child_id: str, end_date: str,
                   days: int = 7) -> WeeklyRecap:
    """上周回顾。一条 GROUP BY + 两条 EXISTS 式查询，无 LLM。"""
    start = (date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, outcome, COUNT(*) AS n FROM sessions "
        "WHERE child_id=%s AND session_date BETWEEN %s AND %s GROUP BY activity_id, outcome",
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
        "WHERE child_id=%s AND observed_at BETWEEN %s AND %s "
        # ★ COUNT(DISTINCT observed_at) 而不是 COUNT(*) ——
        # 一次家长输入会落成 N 条 observation，它们共享同一个 observed_at。
        # 用 COUNT(*) 时"今天说了一句含自伤的话"会被数成"今天有 3 条红旗"，
        # 而 WeeklyRecap.render() 写的是"本周有 N 条记录触发了红旗信号" ——
        # 家长关心的是**有几天遇到红旗**，不是记了几条。
        # 闭环接线之前 observations 恒为空，这个 bug 一直藏着没人看见。
        "AND redflag_triggered", (child_id, start, end_date)).fetchone()["days"]
    return r


def what_child_refuses(conn: psycopg.Connection, child_id: str,
                       lookback_days: int = 30) -> RefusalReport:
    """抗拒归因。**按活动聚合是主体，按材料聚合是补充** ——
    因为抗拒常常不是"不喜欢这个活动"而是"看到那个球就炸"，
    只按活动看会得出"换一个活动就好了"的错误建议。"""
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT activity_id, COUNT(*) AS n FROM sessions "
        "WHERE child_id=%s AND outcome='refused' AND session_date>=%s "
        "GROUP BY activity_id ORDER BY n DESC", (child_id, start)).fetchall()

    from activity_data import get as get_activity
    rep = RefusalReport(lookback_days=lookback_days)
    for row in rows:
        a = get_activity(row["activity_id"])
        rep.by_activity.append((row["activity_id"],
                                a.title if a else "（活动已下线）", row["n"]))

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE child_id=%s "
        "AND outcome IN ('success','partial','refused') AND session_date>=%s",
        (child_id, start)).fetchone()["n"]
    ref = sum(n for _, _, n in rep.by_activity)
    rep.total_sessions = total
    rep.refusal_rate = (ref / total) if total else 0.0
    return rep


def what_progressed(conn: psycopg.Connection, child_id: str,
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
        "WHERE child_id=%s ORDER BY skill_item, id", (child_id,)).fetchall()

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
                    "SELECT DISTINCT activity_id FROM sessions WHERE child_id=%s",
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
def _seed_demo(conn: psycopg.Connection, child_id: str = "demo-001",
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
    tmp = "/tmp/asd_agent_demo.db"
    conn = reset(tmp)          # ★ 一行代替原来删 db/wal/shm 三个文件
    cid = _seed_demo(conn, "demo-001", confidence=0.85)
    # 同一个孩子、同一份练习记录，只把画像可信度从 0.85 降到 0.30 再跑一遍。
    # 两份 skill_state 一对比，就是升级闸门在存储层的证据。
    _seed_demo(conn, "demo-002", confidence=0.30)

    print("=== 表 ===")
    # information_schema.tables 而不是 sqlite_master —— 后者是 sqlite 独有的系统表，
    # 且它列的是**整个库**；PG 下要用 current_schema() 过滤，否则会把 public 也列进来。
    for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() ORDER BY table_name"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {r['table_name']}").fetchone()["n"]
        print(f"  {r['table_name']:18s} {n:4d} 行")

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

    _cli_redflags(conn, cid)

    conn.close()
    # 迁移前这里是 shutil.rmtree(tmp) 删文件；现在是 schema，下次 reset() 会重建


def _cli_redflags(conn: psycopg.Connection, child_id: str) -> None:
    """Phase 2：红旗落库、幂等、状态流转、暂停判据。

    这个文件是 12 个离线回归之一，所以这几条断言在这里也必须成立 ——
    否则"改了 store 就得跑 graph.selfcheck"会成为一条总被跳过的步骤。
    """
    import redflag

    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    print("\n=== Phase 2：红旗 ===")
    urgent_text = ("他最近不太喜欢那个旋转的玩具，看到就走开，"
                   "然后会去咬自己的袖子。")
    rep = redflag.scan(urgent_text)
    rows = redflag.rows_for(rep)
    ids = append_red_flags(conn, child_id, "2026-10-20", rows, source=urgent_text)
    check("urgent 命中并落库", len(ids) == 1, ids)

    n1 = conn.execute("SELECT COUNT(*) AS n FROM red_flags WHERE child_id=%s",
                      (child_id,)).fetchone()["n"]
    ids2 = append_red_flags(conn, child_id, "2026-10-20", rows, source=urgent_text)
    n2 = conn.execute("SELECT COUNT(*) AS n FROM red_flags WHERE child_id=%s",
                      (child_id,)).fetchone()["n"]
    check("★ 同一段话 + 同一个 code 再落一次：只有一行，返回空新增",
          n1 == n2 and ids2 == [], f"{n1} → {n2}, 新增 {ids2}")

    other_text = "他今天早上出门前不肯穿鞋，我帮他他就打我的手，只好由着他光脚。"
    ids3 = append_red_flags(conn, child_id, "2026-10-21",
                            redflag.rows_for(redflag.scan(other_text)), source=other_text)
    check("★ 同一天的另一段话命中另一个 code → 另起一行（两次独立的报告不合并）",
          len(ids3) == 1, ids3)

    row = get_red_flag(conn, ids[0], child_id)
    check("行里带着该追问的问题与转诊指引",
          bool(row["ask"]) and bool(row["referral_who"])
          and bool(row["referral_avoid"]), row["referral_avoid"][:20])

    check("★ high 级不暂停计划", not plan_paused(conn, "demo-002"))
    check("★ urgent 存在 → 暂停", plan_paused(conn, child_id))
    set_red_flag_status(conn, ids[0], "acknowledged")
    check("★ 已看到不解除暂停（「已看到」≠「已处理」）",
          plan_paused(conn, child_id))
    set_red_flag_status(conn, ids[0], "closed")
    check("★ 已处理之后暂停解除", not plan_paused(conn, child_id))
    check("关掉的那条仍留在列表里（不消失，复诊要拿得出来）",
          len(list_red_flags(conn, child_id)) == 2,
          len(list_red_flags(conn, child_id)))
    check("?status=closed 过滤生效",
          len(list_red_flags(conn, child_id, status="closed")) == 1)

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
