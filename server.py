# -*- coding: utf-8 -*-
"""
FastAPI 包装层：把阶段 1-4b 的 Python 链路暴露成 HTTP
========================================================
    ../.venv/bin/python server.py            # http://127.0.0.1:8000
    VITE 后端代理默认指向这里（frontend/vite.config.js）

★ 这个文件里没有一行业务判断
------------------------------
它只做三件事：HTTP ↔ 函数调用的翻译、把 Pydantic/dataclass 转成 JSON、
以及给慢操作套一个 job + SSE 的壳。**判定全在 asd_agent/ 里** ——
quality 是 `feedback.quality_for()` 算的，状态转移是
`evaluator.apply_transitions()` 决定的，候选是 `activities.retrieve()` 排的。
后端一旦开始自己判断，就出现了第二套口径，而状态机的闸门会拿两套口径打架。

★ 三条不能破的接线约定（写在代码里，不只写在 PROGRESS.md 里）
------------------------------------------------------------
1. **`before=` 的方向。** 同一天里"发今天的卡片"和"算明天的候选"读的
   是**两份不同的记忆**：`build_request(before=<今天>)` 会把当天刚写的日结
   排除在外。不传 `before`，今天的反馈会回过头来解释今天该做什么 —— 方向反了。
   → `_build_request()` 把 `before` 做成必填参数，不给默认值。

2. **前端不算 quality。** 家长只传 `outcome` + `prompt_level`，
   `quality_for()` 是唯一的 quality 来源。响应里把它算好一起返回，
   前端只负责显示 —— 不是"也可以算"，是**没有第二处**可以算。

3. **day-close 里状态转移和日结的先后有讲究。**
   转移先跑（它纯 Python、零 token、确定），日结后跑且读的是 store 里
   **已经落库**的 sessions。反过来的话日结会看不到刚写的记录。

★ 关于 job + SSE
---------------
两条慢路径：`POST /api/profile`（18-47s）和 `POST /api/card`（两次串行 LLM，
30-45s）。它们走同一个 `_Job` 壳，前端用同一个 `streamJob()` 读。
`llm.chat_json` 的 `on_step` 回调把"第几次尝试""已 8s"这类进度推进 job 队列，
SSE 立刻吐给浏览器 —— 这是 PROGRESS 阶段 2 定的方案，不是新发明。

**每日链路（`/api/feedbacks`）不同步等。** 它在没有 note 时是纯 Python 映射、
毫秒级返回；只有家长写了补充描述的那些条目才会各触发一次抽取（可到 60s）。
所以前端要能区分这两种等待，接口要把 `degraded` 和 `llm_calls` 一起返回。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "asd_agent"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import llm
import observe
import vocab
import redflag
import store
import child_age
import summary as summary_mod
from activities import PlanRequest, ScoredActivity, candidates as retrieve_candidates
from activities import explain_retrieval, retrieve
from capabilities import AbilityProfile, ParentConstraints
from coach import (CoachOutcome, ObservationInput, card_from_activity,
                   coach_today, render_feedback_form)
# ★ Phase 4：`apply_transitions` / `summarize_day` / `apply_feedbacks`
#   **不再由本文件直接调用** —— 它们现在住在反馈链的节点里
#   （`graph/run_nodes.py`），而 HTTP 这一层只负责把图跑起来。
#   留着的 import 会让人以为"这里还有第二条落库路径"，
#   而那正是 Phase 4 要消掉的东西。
from feedback import ActivityFeedback, quality_for
from planner import pack_day
import loop as loop_mod
from schema import Observation

# ---- 编排层（Phase 1 接入）----
# ★ 图的编译与 saver 都在 graph.runtime 里按进程缓存 ——
#   `saver_for()` 每次调用都开一条新连接并（首次）跑一次 setup()
#   （里面含 CREATE INDEX CONCURRENTLY），绝不能每请求调一次。
from graph import draft as graph_draft
from graph.plan_graph import MAX_EDIT_LOOPS, plan_inputs
from graph.runtime import get_graph, get_draft_graph, get_plan_graph
from graph.state import ClarifyAnswer, plan_config, thread_config

app = FastAPI(title="家长干预教练 Agent", version="0.5")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# ★ 并发（迁移到 PostgreSQL 之后）
#
#   这里曾经有一把 `_IO_LOCK` 和一大段注释，记录的是 sqlite 的并发 500：
#   sqlite3 的游标不是线程安全的，两个线程同时用**同一个**连接做 SELECT 会抛
#   `InterfaceError`。FastAPI 的同步端点跑在线程池里，而 `store.connect()`
#   按路径缓存一个共享连接 —— 前端 `Promise.all` 并发打 progress + summaries
#   实测 180 个请求失败 8 个，串行连打 15 次却全绿。
#
#   换 PostgreSQL 之后这个问题从根上消失，因此**锁也一并删掉**：
#   留着它不会更安全，只会让所有写重新排成一队，
#   并且让下一个人以为"这里还有个必须加锁的坑"。
#   实测 12 线程 × 15 轮 × 4 个只读查询 = 720 次 0 失败；
#   12 线程 × 8 轮 × 3 种写 = 288 条 0 丢失。
#
#   约束仍然收在**连接获取**这一处（`store.connect()` 每线程一个连接），
#   端点可以忘，边界不会忘。

# 全局开关（/api/config），手动测试时用来切离线。
# 默认跟着 DEMO_MODE：现场用 replay 就别真的发请求。
_NO_LLM = os.getenv("ASD_NO_LLM", "").lower() in ("1", "true", "yes")


def _conn():
    """按线程拿连接。**每个线程一个连接，不是全局共享一个。**

    ★ 位置仍然关键：约束收在**连接获取**这个必经之路上，端点可以忘，边界不会忘。
      （迁移前这里是 `connect_thread_local()`，现已与 `connect()` 合流为一个入口 ——
      两个入口意味着"某个调用点忘了用 thread_local 版"这种可能，现在没有了。）

    `ASD_DB_PATH` 现在只用来决定 **schema 名**（`_schema_of`），
    真正的连接串是 `ASD_PG_DSN`。名字沿用是为了少改调用点。
    """
    return store.connect(os.getenv("ASD_DB_PATH"))


def _jsonable(v: Any) -> Any:
    """Pydantic v2 / dataclass / set / tuple → JSON。**唯一的序列化出口。**"""
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    if is_dataclass(v) and not isinstance(v, type):
        return {k: _jsonable(x) for k, x in asdict(v).items()}
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, set):
        return sorted(_jsonable(x) for x in v)
    return v


# ================================================================== job + SSE
class _Job:
    """一次慢操作。线程跑，进度推队列，SSE 读队列。"""

    def __init__(self, job_id: str, label: str):
        self.id = job_id
        self.label = label
        self.status = "running"          # running / done / error
        self.steps: list[dict] = []      # {t: 已过毫秒, text}
        self.result: Any = None
        self.error: str = ""
        self.t0 = time.time()
        self._cv = threading.Condition()

    def step(self, text: str) -> None:
        with self._cv:
            self.steps.append({"t": int((time.time() - self.t0) * 1000), "text": text})
            self._cv.notify_all()

    def finish(self, result: Any) -> None:
        with self._cv:
            self.status, self.result = "done", result
            self._cv.notify_all()

    def fail(self, msg: str) -> None:
        with self._cv:
            self.status, self.error = "error", msg
            self._cv.notify_all()


_JOBS: dict[str, _Job] = {}


def _start_job(label: str, fn) -> str:
    """在后台线程跑 fn(on_step)。fn 返回值作为 job.result。"""
    job_id = uuid.uuid4().hex[:12]
    job = _Job(job_id, label)
    _JOBS[job_id] = job

    def run():
        try:
            job.finish(fn(job.step))
        except Exception as e:                       # noqa: BLE001
            job.fail(f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ================================================================== 请求装配
def _build_request(conn, child_id: str, prof: AbilityProfile, *, before: str) -> PlanRequest:
    """转调 `loop.build_request`。**整个系统的接缝，只有一份实现。**

    ★ `before` 必填、不给默认值，见模块 docstring 的约定 1。
      loop 里第 1 步和第 7 步都传 `day.date`，服务器里每个算候选的端点也必须传。
    """
    return loop_mod.build_request(conn, child_id, prof, before=before)


def _profile_or_400(conn, child_id: str) -> AbilityProfile:
    prof = store.latest_profile(conn, child_id)
    if prof is None:
        raise HTTPException(400, "还没有这个孩子的画像，先走 POST /api/profile 建档")
    return prof


def _candidate_payload(req: PlanRequest, cands: list[ScoredActivity],
                       full: list[ScoredActivity]) -> dict:
    """候选 + 每条的"为什么推它"，外加被剔除的那些和原因。

    ★ 剔除原因必须从**全量 retrieve()** 取。早先的 bug 就是从 `candidates()`
      取的，而它返回的全是 passed=True 的对象 —— 按定义没有 hard_filter_reason，
      于是前端显示成空的"没推荐："，看起来像理由丢了。
    """
    reason_of = {x.activity.activity_id: x.hard_filter_reason
                 for x in full if not x.passed}
    passed_ids = {x.activity.activity_id for x in cands}
    out = []
    for s in cands:
        act = s.activity
        out.append({
            "activity_id": act.activity_id,
            "title": act.title,
            "score": round(s.score, 3),
            "duration_min": act.duration_min,
            "difficulty": act.difficulty,
            "domains": act.domains,
            "materials": [m.name for m in act.materials],
            "target_skill_items": act.target_skill_items,
            "why": explain_retrieval(req, s),
            "breakdown": {k: round(v, 3) for k, v in s.breakdown.items()},
            # ★ 每条候选**自己的**反馈表单。
            #   家长可以从候选里点「用它」把任意一条加进当天的反馈表，
            #   而每条活动的成功判据和步骤数都不一样。原先 /api/plan 不给这些，
            #   前端只能退回去用**卡片那条**活动的（n_steps / feedback_form），
            #   于是给 cog-001 生成的表单上印着 gro-007 的判据
            #   —— 家长会照着 gro-007 的标准去判断 cog-001 做没做到。
            #   和 /api/card 用同一个 render_feedback_form，两处不会漂移。
            "n_steps": len(act.steps),
            "feedback_form": render_feedback_form(act, min(len(act.steps), 6)),
        })
    excluded = [{"activity_id": aid, "reason": reason_of[aid]}
                for aid in reason_of if aid not in passed_ids]
    return {"candidates": out, "excluded": excluded}


# ================================================================== 请求模型
class ProfileIn(BaseModel):
    child_id: str = "demo-001"
    report_text: str = Field(min_length=1)
    # ★ 出生日期取代"手填月龄"。**必填** —— 月龄由系统算，家长只提供事实。
    #   旧接口那个手填的 age_months 已彻底下线：留着它就留着第二个口径，
    #   而月龄偏一个月就是活动难度偏一级（偏一级在干预里不是"不合适"，
    #   是练不到或练过头）。
    birth_date: str = Field(
        min_length=10, max_length=10,
        description="出生日期 YYYY-MM-DD。月龄由系统按它换算。",
    )
    daily_minutes: int = Field(default=30, ge=5, le=180)
    materials: list[str] = Field(default_factory=lambda: ["杯子", "绘本", "小汽车", "积木"])
    no_llm: bool | None = None       # 不传 = 跟随全局开关


def _resolve_age_months(birth_date: str) -> int:
    """出生日期 → 月龄。

    格式不对直接 422 —— 不猜、不静默回退到默认值，
    因为回退到某个默认月龄意味着**按错的年龄选活动**，而家长毫不知情。
    """
    try:
        return child_age.age_months_from_birth(birth_date)
    except ValueError as e:
        raise HTTPException(422, str(e))


class CardIn(BaseModel):
    child_id: str
    date: str
    force: bool = False      # True = 忽略已缓存的那份，重新生成
    no_llm: bool | None = None


class FeedbackIn(BaseModel):
    """★ 逐条反馈。家长点出来的，不是模型猜的。"""

    child_id: str
    date: str
    feedbacks: list[ActivityFeedback] = Field(default_factory=list)
    other_notes: str = Field(
        default="",
        deprecated=("「其他观察」改走 POST /api/observe/run（图接管，Phase 1）。"
                    "这个字段留着只为不让旧前端静默丢掉家长写的话 —— "
                    "本端点不再拿它去抽事件。"))
    no_llm: bool | None = None


# ================================================================== 图的载荷
class ObserveIn(BaseModel):
    """家长的一段自然语言 → 观察链。技术方案 §1.1 的第一步。"""

    child_id: str
    date: str
    text: str = Field(min_length=1)
    no_llm: bool | None = None


class ObserveResumeIn(BaseModel):
    """对澄清 interrupt 的回答。

    ★ 这里**不重复**声明 answer 的长度上界：`graph/nodes.py` 的 clarify 节点
      已经用 `interrupt(..., response_schema=ClarifyAnswer)` 挂好了 Pydantic，
      校验发生在**恢复那一刻**（技术方案 §5.4「前端提交必须经服务端 Schema
      校验」）。在这里再抄一份 max_length 就是两处口径 —— 抄的那份迟早会落后。
      FastAPI 这层只保证形状是 dict，语义交给图。
    """

    child_id: str
    date: str
    answer: str = Field(default="", max_length=2000)   # 只防超长请求体，不参与语义校验
    skip: bool = False
    no_llm: bool | None = None


class DraftRowIn(BaseModel):
    """草稿里的一行。**刻意比 `ActivityFeedback` 宽松** —— 草稿就是「填了一半」。

    ★ 不能复用 `ActivityFeedback`：它的 `outcome` 必填且取值受限，
      而家长刚点开页面时所有行都是空的 —— 存不进去。
    """

    model_config = ConfigDict(extra="forbid")

    activity_id: str
    outcome: str = Field(default="", max_length=16)
    prompt_level: int = Field(default=0, ge=0, le=3)   # 与 ActivityFeedback 同口径：int 0-3
    steps_done: int = Field(default=0, ge=0, le=20)
    note: str = Field(default="", max_length=2000)


class DraftIn(BaseModel):
    child_id: str
    date: str
    rows: list[DraftRowIn] = Field(default_factory=list)
    other_notes: str = Field(default="", max_length=20000)


class PlanIn(BaseModel):
    """跑一遍计划链。**不需要 no_llm** —— 计划链全程零 LLM（纯 Python 装箱）。"""

    model_config = {"extra": "forbid"}

    child_id: str
    date: str


class PlanReviewIn(BaseModel):
    """对审批 interrupt 的回答。

    ★ 这里**不**重复声明 action 的三个取值：图里 `approve_plan` 节点
      已经挂了 `response_schema=PlanReview`，校验发生在**恢复那一刻**。
      在 FastAPI 这层再抄一份 `Literal` 就是两处口径 ——
      抄的那份迟早会落后，而落后时前端传 `approv` 得到的是一个
      "参数不合法"却不知道该传什么的 422。
      这里只保证形状是 dict，语义交给图（与 ObserveResumeIn 同一条理由）。
    """

    model_config = {"extra": "forbid"}

    child_id: str
    date: str
    action: str = Field(min_length=1)
    note: str = Field(default="", max_length=500)


class RedFlagStatusIn(BaseModel):
    """改一条红旗的状态。

    ★ `Literal` 而不是 str，与 `graph/state.py` 的 `PlanReview` 同一条理由：
      前端提交的操作必须经过**服务端** schema 校验，多打一个词就该在
      进来那一刻被拒，而不是顺着流程流到落库那一步。
    `extra="forbid"`：不接受前端顺手多塞的字段，那通常意味着它以为自己
    改了别的东西（比如 severity），而服务端没照做。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "acknowledged", "closed"]


# ================================================================== 慢路径（job + SSE）
@app.post("/api/profile")
def api_profile(inp: ProfileIn):
    """建档：报告 → 画像。**18-47s，必须走 job**，同步等必然超时。"""
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    # ★ 月龄在这里就定下来：出生日期 → 月龄。放在 job **外面**，
    #   是为了让"出生日期填错"立刻 422，而不是让家长等 40 秒
    #   才在 job 里面炸掉。
    age_months = _resolve_age_months(inp.birth_date)
    constraints = ParentConstraints(
        daily_minutes_available=inp.daily_minutes,
        materials_at_home=inp.materials,
    )

    def work(step):
        from redflag import scan
        from profile import ProfileExtraction, extract_profile
        conn = _conn()
        if no_llm:
            step("离线模式：用确定性降级画像（可信度 0.1，状态机会冻结全部升级）")
            from profile import _fallback_profile
            prof = _fallback_profile(inp.child_id, age_months, constraints)
            ex = ProfileExtraction(
                profile=prof, redflags=scan(inp.report_text), degraded=True,
                note="降级画像：可信度 0.1 —— 低于升级闸门 0.5，状态机将冻结所有升级")
        else:
            step(f"读取报告 {len(inp.report_text)} 字，先跑红旗规则扫描（零 token）")
            ex = extract_profile(inp.report_text, inp.child_id, age_months,
                                 parent_constraints=constraints, on_step=step)
        step(f"回指校验完成，未通过 {len(ex.unverified)} 条；写库")
        # 顺序不变量：先 ensure_child 再 save_profile —— 后者有外键指向前者。
        # 迁移前这里靠 _IO_LOCK 串行化，迁移后每请求一个独立连接 + 单事务，
        # 由 PostgreSQL 自己保证顺序。
        store.ensure_child(conn, inp.child_id, age_months=age_months,
                           birth_date=inp.birth_date)
        store.save_profile(conn, ex.profile)
        # ★ 红旗落库（Phase 2）。**放在 ensure_child 之后** ——
        #   red_flags.child_id 有外键指向前者，而 scan() 是更早就跑完的。
        #   走的是与图那条路**完全相同的一对函数**
        #   （redflag.rows_for 纯 + store.append_red_flags 落），
        #   所以「建档时扫到的自伤」和「今天观察时扫到的自伤」
        #   在库里是同一种东西，前端不需要为它们写两套渲染。
        #   source_kind 区分来源，不进幂等键 —— 同一份报告重跑一次不会多出一行。
        rf_ids = store.append_red_flags(
            conn, inp.child_id,
            getattr(ex.profile, "report_date", None) or date.today().isoformat(),
            redflag.rows_for(ex.redflags), source=inp.report_text,
            source_kind="report")
        return {
            "profile": _jsonable(ex.profile),
            "age_months": age_months,
            "age_text": child_age.age_text(age_months),
            "birth_date": inp.birth_date,
            "redflags": _jsonable(ex.redflags),
            "red_flag_ids": rf_ids,
            "report_summary": ex.report_summary,
            "unverified": ex.unverified,
            "degraded": ex.degraded,
            "note": ex.note,
            "trace": _jsonable(ex.trace),
        }

    return {"job_id": _start_job(f"profile:{inp.child_id}", work)}


@app.post("/api/card")
def api_card(inp: CardIn):
    """今天的家长卡片。**一整天的 N 条，两次串行 LLM，30-45s，走 job。**

    ★ 为什么一次生成一整天的而不是"要哪条出哪条"：
      家长配置的是"每天 30 分钟"，而一次只给一条活动的话，
      他拿到的永远是 6-10 分钟，剩下的时间就空着 ——
      界面上的"每天可用 30 分钟"和实际给到的东西对不上。
      所以这里一次把当天装箱满（`planner.pack_day`），家长逐条做、逐条填。

    ★ 只生成一次。落 `plans` 表，之后直接读（`store.latest_plan`）。
      之前每次进「今天」页都重跑检索 + 两次 LLM：结果一样（输入没变），
      变的只有 30-45s 的等待，而且家长可能看到两次不同的推荐。
      `force=true` 才重算 —— 画像改了会**自动**失效（比对 profile_id），
      但"今天想换一批"这种主观需求得他自己点。

    诊断读的是**之前**攒下的观察（`before=inp.date`），不是今天刚写的 ——
    今天的话是今天情况的报告，它驱动的是明天。
    """
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    conn = _conn()
    prof = _profile_or_400(conn, inp.child_id)

    def work(step):
        # ---- 已生成过就直接读（换页面/刷新都不重算）----
        prof_id = store.latest_profile_id(conn, inp.child_id)
        # ★ Phase 4：**当天生效的那一版计划**也是缓存的键的一部分。
        #   以前只看 profile_id，于是"家长批准了第 2 版计划"这件事
        #   不会让旧卡片作废 —— 界面上还是第 1 版的那几条，
        #   而家长批的是第 2 版。批准就白批了。
        cur = store.active_plan(conn, inp.child_id, on=inp.date)
        cur_plan = (int(cur["id"]), int(cur["version"])) if cur else (None, 0)
        if not inp.force:
            saved = store.latest_card(conn, inp.child_id, inp.date)
            if (saved and saved.get("profile_id") == prof_id
                    and (saved.get("plan_id"), saved.get("plan_version")) == cur_plan):
                step(f"{inp.date} 已经生成过（{len(saved.get('activities') or [])} 条），直接读")
                return saved | {"cached": True}
            if saved:
                step("画像或已批准的计划变了，之前排的那份作废 → 重新排")

        req = _build_request(conn, inp.child_id, prof, before=inp.date)
        step(f"检索候选（纯 Python，{len(req.refusal_counts)} 条抗拒记录参与）")
        cands = retrieve_candidates(req, top_k=12)
        if not cands:
            step("候选表为空")
            return {"error": "候选表为空，今天不出卡片"}

        # ★ Phase 4：卡片**读**当天生效的那一版计划，而不是自己再排一次。
        #   以前两条路径各自 pack_day，结果**当前恰好一致**——
        #   但那是巧合，不是结构上保证一致：将来任何一个改了装箱规则，
        #   两条路就会分叉，而家长看到的卡片与被批准的计划不是同一份。
        plan_ids = list(((cur or {}).get("payload") or {}).get("activity_ids") or [])
        by_id = {s.activity.activity_id: s for s in cands}
        planned = [by_id[i] for i in plan_ids if i in by_id]
        if planned:
            step(f"当天有一份已批准的计划（{len(planned)} 条）→ 卡片按它出")
        else:
            step("当天没有已批准的计划 → 按今天的候选自己装箱")

        prev = _recent_obs_inputs(conn, inp.child_id, before=inp.date)
        step(f"喂给诊断的观察 {len(prev)} 条（都是 {inp.date} 之前的）")

        prefs = prof.parent_constraints.preferences
        focus_id = (planned[0].activity.activity_id if planned
                    else cands[0].activity.activity_id)
        if no_llm:
            step("离线模式：用活动定义直接生成兜底卡片")
            out = CoachOutcome(
                card=card_from_activity(by_id[focus_id].activity, prefs),
                activity=by_id[focus_id].activity, degraded=True,
                adjustment="（离线：使用活动定义直接生成的兜底卡片）")
        else:
            step("教练 Agent 调模型（2 次）")
            out = coach_today(
                cands, today_id=focus_id,
                observations=prev, req=req, recent_summary=req.recent_summary,
                # ★ 家长批准过的那一版不许被诊断换掉
                keep_focus=bool(planned),
                on_step=step)
        if out.adjustment:
            step(f"调整：{out.adjustment}")

        # ---- 装箱 ----
        if planned:
            # ★ 家长批准的就是要做的那几条：一个不多一个不少。
            #   不再按预算重新装箱 —— 重新装箱就可能排出一份**不一样**的安排，
            #   而那正是这一段要消灭的事。
            picks = planned
            focus = planned[0]
        else:
            # 诊断选定的焦点先入箱，剩下的分钟数分给其余候选。
            # 焦点可能已经被 _pick_adjustment 换过（ease/swap），所以要按换过之后的那条装箱，
            # 不然会出现"诊断说换成 fin-004，装箱里却还是原来那条"的错位。
            focus = next((x for x in cands
                          if x.activity.activity_id ==
                          (out.activity.activity_id if out.activity else cands[0].activity.activity_id)),
                         None) or cands[0]
            picks = pack_day(cands, req.daily_minutes, first=focus)
        spent = sum(x.activity.total_min + x.activity.prep_min for x in picks)
        step(f"装箱：{len(picks)} 条，共 {spent} 分钟（预算 {req.daily_minutes}）")

        # 焦点那条用模型出的卡片（含诊断与退路），其余用活动定义直接生成。
        # ★ 不是"降级"：活动定义本来就带 steps / success_criteria /
        #   prompt_hierarchy / error_correction，兜底卡片是完整的，
        #   只是没有针对今天这个状态写的 why_today。前端如实标注哪条是哪种。
        acts = []
        for i, s in enumerate(picks):
            act = s.activity
            is_focus = act.activity_id == focus.activity.activity_id
            card = out.card if is_focus else card_from_activity(act, prefs)
            acts.append({
                "activity": _jsonable(act),
                "card": _jsonable(card),
                "card_text": card.render(),
                "from_llm": bool(is_focus and not out.degraded),
                "why": explain_retrieval(req, s),
                # 每条**自己**的反馈表单。与 /api/plan 用同一个 render_feedback_form，
                # 两处不会漂移；少这一项前端就只能退回用焦点那条的，
                # 于是给别的活动印上焦点活动的判据（阶段 5 修过的那个 bug）。
                "feedback_form": render_feedback_form(act, min(len(act.steps), 6)),
                "n_steps": len(act.steps),
            })

        payload = {
            "date": inp.date,
            "profile_id": prof_id,
            # ★ Phase 4：这份卡片是**照着哪一版计划**出的。
            #   它是缓存的键的一部分（家长批准第 2 版 → 旧卡片作废），
            #   也是"看到的与批准的同一份"这条承诺的凭据。
            "plan_id": cur_plan[0],
            "plan_version": cur_plan[1],
            "budget_minutes": req.daily_minutes,
            "total_minutes": spent,
            "activities": acts,
            "analysis": _jsonable(out.analysis),
            "degraded": out.degraded,
            "adjustment": out.adjustment,
            "unverified_quotes": out.unverified_quotes,
            "traces": _jsonable(out.traces),
            "cached": False,
        }
        # 落库在最后：只有整份都算完才写，中途失败不会留下一份半截的缓存
        # —— 半截缓存会被下一次直接读出去，而读的人看不出它是半截的。
        #
        # ★ Phase 3：写的是 `day_cards`，**不再是 `plans`**。
        #   plans 从 Phase 3 起是「版本化的计划」，而这里存的是
        #   「今天这一张卡片」—— 两种东西挤在一张表上的后果见
        #   store._init 里 day_cards 那段注释（active_plan 会把卡片
        #   当成已批准的计划，于是**真变化再也不打断家长**）。
        store.save_card(conn, inp.child_id, inp.date, payload)
        return payload

    return {"job_id": _start_job(f"card:{inp.child_id}:{inp.date}", work)}


def _recent_obs_inputs(conn, child_id: str, *, before: str,
                       days: int = 3) -> list[ObservationInput]:
    """最近几天攒下的观察 → 教练的诊断输入。

    ★ 不含 `before` 当天：教练诊断读的是"之前"的情况，今天的话驱动的是明天。

    ★ 按 `source` 分组而不是按天去重。初版写的是"每天只取第一条"，
      理由是"同一段话抽出的 3 条事件是一件事" —— 但一天的 observations 里
      本来就有**多段**不同的话（逐条反馈里 3 条各带 note，再加「其他观察」框），
      按天去重等于把其中两段整段丢掉，教练只看得见第一条活动的话。
      正确粒度是**按 source 去重**：同 source 的多行是同一次抽取的产物。
    """
    rows = conn.execute(
        "SELECT payload, observed_at, source FROM observations "
        "WHERE child_id=%s AND observed_at<%s ORDER BY observed_at, id",
        (child_id, before)).fetchall()
    cutoff = (date.fromisoformat(before) - timedelta(days=days)).isoformat()
    groups: dict[tuple[str, str], observe.ObservationOutcome] = {}
    for r in rows:
        if r["observed_at"] < cutoff or not r["source"]:
            continue        # source 空 = 老数据，corpus 会是空串，只会污染回指校验
        key = (r["observed_at"], r["source"])
        if key not in groups:
            groups[key] = observe.ObservationOutcome(
                source_text=r["source"], date=r["observed_at"])
        try:
            groups[key].events.append(
                Observation.model_validate(json.loads(r["payload"])))
        except Exception:
            continue
    out: list[ObservationInput] = []
    for g in groups.values():
        out.extend(observe.to_coach_inputs(g))
    return out


# ================================================================== 快路径（同步）
@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job 不存在（服务器重启就没了）")
    return {"status": job.status, "steps": job.steps,
            "error": job.error, "result": job.result}


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    """SSE 进度。`llm.chat_json` 的 on_step 在这里变成浏览器里的一行行字。"""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job 不存在")

    async def gen():
        i = 0
        while True:
            with job._cv:
                if i < len(job.steps):
                    chunk, i = job.steps[i:], len(job.steps)
                    done = job.status != "running"
                else:
                    chunk, done = [], job.status != "running"
                if not done:
                    job._cv.wait(timeout=1.0)
            for s in chunk:
                yield f"data: {json.dumps(s, ensure_ascii=False)}\n\n"
            if done:
                payload = json.dumps({"status": job.status, "steps": job.steps,
                                      "error": job.error, "result": job.result},
                                     ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n"
                return
            yield ": keepalive\n\n"          # 防止代理掐掉静默连接

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/state/{child_id}")
def api_state(child_id: str):
    """建档页之后所有页面的公共前置：有没有画像、当前状态、日结数。"""
    conn = _conn()
    prof = store.latest_profile(conn, child_id)
    states = store.current_states(conn, child_id)
    kid = store.child(conn, child_id) or {}
    return {
        "child_id": child_id,
        "has_profile": prof is not None,
        "profile_confidence": prof.overall_confidence if prof else None,
        # ★ 年龄：出生日期是事实，月龄是算出来的。
        #   两者都给前端 —— 前端显示"出生日期"，家长核对的是自己填的那一栏。
        "birth_date": kid.get("birth_date"),
        "age_months": kid.get("age_months") if kid else None,
        "age_text": child_age.age_text(kid["age_months"]) if kid and kid.get("age_months") else None,
        "levels": prof.levels() if prof else {},
        "priority_domains": prof.domains_of_interest() if prof else [],
        "constraints": _jsonable(prof.parent_constraints) if prof else None,
        # ★ 完整画像。原先只给 levels（技能名→等级），前端顶栏就只能显示
        #   一个可信度数字 —— 建档最该看的东西（每条结论的**证据引文**、
        #   来源是报告明说的还是推断的、置信度多少）全看不见，
        #   而 ProfileForm 在建档完成后就被 v-if 卸载了，于是彻底无从查看。
        #   assessments 原样给，渲染留给前端。
        "assessments": _jsonable(prof.assessments) if prof else [],
        # 能区中文名由后端给 —— 前端不该自己抄一份 ABILITY_DOMAIN，
        # 抄了就有两份会漂移的词表（同一个坑：提示词与实现各改一处）。
        "domain_cn": vocab.ABILITY_DOMAIN,
        "profile_report_date": prof.report_date if prof else None,
        "profile_degraded": bool(prof.needs_clarification) if prof else None,
        "states": states,
        "refusal_counts": store.refusal_counts(conn, child_id),
        "n_summaries": len(store.latest_summary(conn, child_id, days=999)),
        "no_llm": _NO_LLM,
        "llm_mode": os.getenv("DEMO_MODE", "live"),
    }


# ★ 注册位置很要紧：`/api/plan/{child_id}` 这条路由的参数在路径里，
#   FastAPI 按**声明顺序**匹配，而 `/api/plan/status` 也是 `/api/plan/xxx` 的一种。
#   把这一段放在 `{child_id}` 之后，`GET /api/plan/status` 会被当成
#   `child_id="status"` 命中那条老路由 —— 症状是 400「还没有这个孩子的画像」，
#   而那条路由根本没读过 profile。**报错信息完全指不到真正的原因。**
# ================================================================== 计划链（Phase 3）
def _plan_result(g, cfg: dict) -> dict:
    """把计划链的 checkpoint 读成前端要的形状。**纯读**。

    三种 phase（与观察链同款的三态协议）：
      idle    —— 从没跑过
      review  —— 停在 ⚡ approve_plan 上，等家长拍板
      done    —— 跑完了（含"因为红旗停着"这一种）

    ★ 中断判定用 `snap.next` / `snap.interrupts`，**不用** state 里的
      `needs_review`：resume 跑完之后那个字段是上一次的值，
      拿它判会把「已经批过了」误报成「还在等」——
      这是观察链踩过的同一个坑（见 `_observe_result` 的注释）。
    """
    snap = g.get_state(cfg)
    vals = snap.values or {}
    nxt = tuple(snap.next or ())
    ints = list(getattr(snap, "interrupts", None) or [])

    if not vals and not nxt:
        return {"phase": "idle", "thread_id": cfg["configurable"]["thread_id"],
                "interrupt": None, "paused": False, "changes": [],
                "plan_id": None, "plan_version": None, "trace": []}

    interrupted = bool(nxt) and bool(ints)
    return {
        "phase": "review" if interrupted else "done",
        "thread_id": cfg["configurable"]["thread_id"],
        "interrupt": _jsonable(ints[0].value) if ints else None,
        "plan_id": vals.get("plan_id"),
        "plan_version": vals.get("plan_version"),
        "plan_status": vals.get("plan_status"),
        # ★ 暂停时前端要显示"今天没排"，并指向红旗横幅。
        "paused": bool(vals.get("plan_paused")),
        "activities": list(vals.get("plan_activity_ids") or []),
        "total_minutes": vals.get("plan_total_minutes", 0),
        "focus_domain": vals.get("focus_domain", ""),
        "focus_reason": vals.get("focus_reason", ""),
        "changes": list(vals.get("changes") or []),
        "trace": list(vals.get("trace") or []),
    }


@app.post("/api/plan/run")
def api_plan_run(inp: PlanIn):
    """跑计划链。**毫秒级，不用 job** —— 全程纯 Python（检索 + 装箱），零 LLM。

    ★ 与观察链最大的不同：这条路径**通常不会停**。
      `needs_review=False` 时一路跑到 `publish_plan`，返回 `phase="done"`；
      只有"相对当前生效的那一版有实质变更"或"第一版"才会停在审批上。
      这正是"只在真变化时审批"这条判据的对外表现。

    ★ `force` 参数刻意没有：`thread_id` 天然按天唯一（`plan:{child}:{date}`），
      同一���天重复调用会接着上次的 checkpoint 跑（实测 ④），
      所以"重新生成"是 `POST /api/plan/review` + `edit` 那条路，
      而不是在这里加一个 force。
    """
    conn = _conn()
    _profile_or_400(conn, inp.child_id)     # 没画像不该走到这一步
    g = get_plan_graph()
    cfg = plan_config(inp.child_id, inp.date)
    try:
        g.invoke(plan_inputs(conn, inp.child_id, inp.date), cfg)
    except LookupError as e:
        raise HTTPException(400, str(e))
    return _plan_result(g, cfg)


@app.post("/api/plan/review")
def api_plan_review(inp: PlanReviewIn):
    """回答审批 interrupt，让图接着跑。

    ★★ 没有待审批的计划时**必须 409** —— 与 `/api/observe/resume` 完全同款：
      `Command(resume=...)` 打在没有 pending interrupt 的 thread 上，
      LangGraph 会把它当成**新输入从头跑一遍整条图**，
      于是 plans 表里多出一行版本 —— **不报错、不抛异常，就是数据多了一版**。
      守卫必须在 `graph.invoke` 之前。

    ★ 400 而不是 422：`action` 的合法取值由图里的 `PlanReview` 判，
      传错时是 `graph.invoke` 抛的 Pydantic ValidationError，
      翻译成 400 并把图自己的报错原样带出去 ——
      家长点错按钮时看到的是"approve/edit/reject 三个里选一个"。
    """
    g = get_plan_graph()
    cfg = plan_config(inp.child_id, inp.date)
    snap = g.get_state(cfg)
    if not (getattr(snap, "interrupts", None) or []):
        raise HTTPException(
            409, "这一天没有待审批的计划（可能已经批过了，或者压根没跑过计划链）")
    try:
        g.invoke(Command(resume={"action": inp.action, "note": inp.note}), cfg)
    except ValidationError as e:
        raise HTTPException(400, f"审批动作不合法：{e.errors()[0]['msg']}")
    return _plan_result(g, cfg)


@app.get("/api/plan/status")
def api_plan_status(child_id: str, date: str):
    """计划链现在停在哪。**纯读 checkpoint，毫秒级。**

    刷新页面后靠它发现"还有个计划等你批" ——
    前端不持有任何 interrupt 状态（刷新就没了，服务端不会）。
    """
    return _plan_result(get_plan_graph(), plan_config(child_id, date))


@app.get("/api/plan/versions")
def api_plan_versions(child_id: str, date: str):
    """某一天的全部版本。家长改过几次、哪一版被拒了，都在这里。"""
    conn = _conn()
    return {"versions": store.plan_versions(conn, child_id, date)}


@app.get("/api/plan/{child_id}")
def api_plan(child_id: str, on: str | None = None):
    """候选 + 每条的"为什么推/为什么不推"。**纯 Python，毫秒级。**

    ★ 参数叫 `on`（算哪一天的候选）而不是 `date`：函数里要用
      `date.today()` 兜底默认值，参数取名 `date` 会把它遮掉 ——
      第一次写就是这么写的，`date.today()` 在函数体内解析成那个字符串参数，
      非 None 时正好走不到，非 None 的路径一旦走到就是 `str.today()` 崩掉。
    """
    conn = _conn()
    prof = _profile_or_400(conn, child_id)
    before = on or date.today().isoformat()
    req = _build_request(conn, child_id, prof, before=before)
    cands = retrieve_candidates(req, top_k=12)
    full = retrieve(req)
    return _candidate_payload(req, cands, full) | {
        "before": before,
        "memory": {
            "refusal_counts": req.refusal_counts,
            "recent_scenes": req.recent_scenes,
            "target_states": req.target_states,
            "recent_summary_dates": [s.get("date") for s in req.recent_summary],
        },
    }


# ================================================================== 观察链（Phase 1）
# ★ 为什么「其他观察」改走图，而逐条活动的 note 抽取留在 apply_feedbacks
# ------------------------------------------------------------------
# 一天里有**两段**自由文本，它们语义不同、抽取模式也不同：
#   · 逐条活动的 note  → `SYSTEM_SINGLE`，只抽这一条活动里发生的事，
#                        而且家长已经点过 outcome 了，抽取只补细节
#   · 「其他观察」整段  → `SYSTEM`，一次话里多次事件要抽全
# Phase 0 只有图一个写入口，所以 `publish` 的幂等判据按「这一天」写就成立。
# 图一接进 HTTP，`/api/feedbacks` 也在同一天写 observations 行，那个粗判据
# 就会把「其他观察」误判成「今天已落库」而**静默跳过**（graph/nodes.py 里有
# 完整说明）。所以「其他观察」这一段整体交给图的 publish，逐条 note 那条路
# **原样保留** —— sessions→outcomes 的语义拆分是 Phase 4 的活，
# 而 `loop.py` 的 CLI 也走 apply_feedbacks 的 other_notes 路径。


def _observe_result(g, cfg: dict) -> dict:
    """把 checkpoint 上的状态读成前端要的形状。

    ★ 中断判定用 `snap.next` / `snap.interrupts`，**不用** state 里的
      `needs_clarification`：resume 跑完之后那个字段是**上一次**的值，
      拿它判会把「已经答过了」误报成「还在等」。

    三种 phase：
      idle    —— 这个 thread 从没跑过（前端据此显示「还没有记录」）
      clarify —— 停在 ⚡ interrupt 上，等家长回答
      done    —— 跑完了（可能走了 escalate 那条路）
    """
    snap = g.get_state(cfg)
    vals = snap.values or {}
    nxt = tuple(snap.next or ())
    ints = list(getattr(snap, "interrupts", None) or [])

    if not vals and not nxt:
        return {"phase": "idle", "thread_id": cfg["configurable"]["thread_id"],
                "interrupt": None, "observation_ids": [],
                "clarify_asked": False, "degraded": False, "trace": []}

    obs = vals.get("observation") or {}
    interrupted = bool(nxt) and bool(ints)
    return {
        "phase": "clarify" if interrupted else "done",
        "thread_id": cfg["configurable"]["thread_id"],
        # 就是 nodes.py 里 interrupt() 传进去的那个 payload（含 question）
        "interrupt": _jsonable(ints[0].value) if ints else None,
        "observation_ids": list(vals.get("observation_ids") or []),
        # ★ 本次**新增**的红旗行（重跑一次是 []）。前端要显示
        #   「今天记下了 N 条红旗」时必须是新增而不是库里总数 ——
        #   否则家长会看到同一个数字被重复宣布。
        "red_flag_ids": list(vals.get("red_flag_ids") or []),
        "clarify_asked": bool(vals.get("clarify_asked")),
        # 澄清往返的产物：家长的回答 + 合并后的语料。
        # 回指校验比的是**整段**原话，所以回答是追加进去的而不是替换 ——
        # 展示出来是为了让家长看到「系统把你说的两段话合起来重新理解了一遍」。
        "clarification_answer": vals.get("clarification_answer") or "",
        "clarified_text": vals.get("clarified_text") or "",
        "degraded": bool(obs.get("degraded")),
        # 四层：给人看的部分。空是合法的（降级时就是全空）
        "parent_interpretation": obs.get("parent_interpretation"),
        "hypotheses": list(obs.get("hypotheses") or []),
        "missing_information": list(obs.get("missing_information") or []),
        "n_events": int(vals.get("n_events") or 0),
        "verdict": _jsonable(vals.get("verdict") or {}),
        "escalation": _jsonable(vals.get("escalation")),
        "trace": list(vals.get("trace") or []),
    }


@app.post("/api/observe/run")
def api_observe_run(inp: ObserveIn):
    """跑观察链。慢路径（抽取可到 60s）→ job + SSE。

    ★ **两阶段协议**。这���端点可能返回 `phase="clarify"` —— 也就是图停在
      ⚡ interrupt 上等家长回答。那不是错误，是一个**正常结果**。
      第二阶段是 `POST /api/observe/resume`。

    ★ 为什么 resume 的身份是 `(child_id, date)` 而不是 job_id：
      `_Job` 是进程内的一次性容器（重启即失、没有 resume 概念），
      而 thread_id 是 `{child_id}:{date}`，天然对应「这一天的那次观察」。
      连续性在 checkpoint 里 —— 这正是引入 checkpointer 的理由。
    """
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    _profile_or_400(_conn(), inp.child_id)      # 没有画像的孩子不该走到这一步

    def work(step):
        t0 = time.time()
        g = get_graph()
        step("跑观察链…")
        out = g.invoke({"child_id": inp.child_id, "date": inp.date,
                        "parent_text": inp.text, "no_llm": no_llm},
                       thread_config(inp.child_id, inp.date))
        step("观察链结束")
        r = _observe_result(g, thread_config(inp.child_id, inp.date))
        r["llm_calls"] = 0 if no_llm else 1
        r["elapsed_ms"] = int((time.time() - t0) * 1000)
        return r

    return {"job_id": _start_job("observe", work)}


@app.post("/api/observe/resume")
def api_observe_resume(inp: ObserveResumeIn):
    """回答澄清，让图接着跑。

    ★★ 没有待回答的澄清时**必须 409** ——
      `Command(resume=...)` 打在一个没有 pending interrupt 的 thread 上，
      LangGraph 会把它当成**新输入**从头跑一遍整条图：重新抽取、重新落库。
      **不报错、不抛异常，就是数据写了两遍。**
      所以这个判断必须在 `graph.invoke` 之前挡掉。
    """
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    g = get_graph()
    cfg = thread_config(inp.child_id, inp.date)

    snap = g.get_state(cfg)
    pending = bool(getattr(snap, "interrupts", None) or [])
    if not pending:
        raise HTTPException(
            409, "这一天没有待回答的澄清（可能已经答过了，或者压根没跑过观察）")

    def work(step):
        t0 = time.time()
        step("接着跑观察链…")
        # 传 dict 而不是 ClarifyAnswer 实例：让 nodes.py 里已经挂好的
        # response_schema 在**恢复那一刻**做校验（技术方案 §5.4）。
        g.invoke(Command(resume={"answer": inp.answer, "skip": inp.skip}), cfg)
        step("观察链结束")
        r = _observe_result(g, cfg)
        r["llm_calls"] = 0 if no_llm else 1
        r["elapsed_ms"] = int((time.time() - t0) * 1000)
        return r

    return {"job_id": _start_job("observe-resume", work)}


@app.get("/api/observe/status")
def api_observe_status(child_id: str, date: str):
    """这一天观察链现在停在哪。**纯读 checkpoint，毫秒级，不起 job。**

    ★ 刷新页面后靠它知道「还有个问题没答」—— 前端不持有任何 interrupt 状态，
      那是服务端的事（前端一刷新就没了，服务端不会）。
    """
    g = get_graph()
    return _observe_result(g, thread_config(child_id, date))


# ================================================================== 红旗（Phase 2）
# ★ 为什么不复用 `_observe_result` 里那个 `escalation`：那个是**事件**
#   （"这段话我读到了什么"），会随刷新/换日期消失；这里是**当前状态**
#   （"还有哪些没处理"），要能跨天跨页面读。两者回答的不是同一个问题。
@app.get("/api/redflags")
def api_redflags(child_id: str, status: str | None = None):
    """红旗列表。**纯读 checkpoint 之外的业务表，毫秒级，不起 job。**

    ★ `paused` 与列表**同一次查询算出来**（`store.plan_paused`），
      前端不许自己从列表推 —— 两套口径必然打架，
      而打架的那个后果是"横幅说暂停着、计划却照生成"。
      Phase 3 的 `generate_plan` 读的是同一条查询。
    """
    conn = _conn()
    return {
        "flags": _jsonable(store.list_red_flags(conn, child_id, status=status)),
        "paused": store.plan_paused(conn, child_id),
    }


@app.patch("/api/redflags/{flag_id}/status")
def api_redflag_status(flag_id: int, inp: RedFlagStatusIn, child_id: str):
    """改一条红旗的状态。**判据在 `redflag.can_transition`，不在这里。**

    ★ 必须**先读当前值再判**：状态是会被另一条请求改的，
      拿客户端传来的旧状态判等于没判。

    ★ 409 而不是 422：请求本身合法（"closed" 是合法状态），
      冲突的是当前状态 —— 与 `/api/observe/resume` 那个 409 同一类。
      422 留给"你发的东西根本不是合法状态"，那种在 Pydantic 层就拒了。
    """
    conn = _conn()
    row = store.get_red_flag(conn, flag_id, child_id)
    if not row:
        # 404 而不是 403：这里没有多用户身份体系（Phase 6 才做），
        # 「这条不属于你」与「这条不存在」对调用方是同一件事。
        raise HTTPException(404, f"红旗 {flag_id} 不存在")
    if not redflag.can_transition(row["status"], inp.status):
        raise HTTPException(
            409, f"不能从「{row['status']}」直接变成「{inp.status}」")
    store.set_red_flag_status(conn, flag_id, inp.status)
    return {"flag": _jsonable(store.get_red_flag(conn, flag_id, child_id)),
            "paused": store.plan_paused(conn, child_id)}


# ================================================================== 草稿
@app.get("/api/draft")
def api_draft_get(child_id: str, date: str):
    """读草稿。**没有就是 `draft: null`**，不是空壳 —— 调用方据此决定要不要恢复。"""
    g = get_draft_graph()
    return {"draft": _jsonable(graph_draft.load_draft(g, child_id=child_id, date=date))}


@app.put("/api/draft")
def api_draft_put(inp: DraftIn):
    """覆盖式存草稿。家长改的是同一份，不是记历史。

    ★ 走 checkpoint（`draft:{child_id}:{date}` 独立命名空间），不碰业务表 ——
      草稿图的节点永远不会被执行（只用 update_state 写），所以
      「interrupt 恢复会重跑节点」那条硬约束在这里结构上不适用。
    """
    g = get_draft_graph()
    saved_at = datetime.now().isoformat(timespec="seconds")
    graph_draft.save_draft(
        g, child_id=inp.child_id, date=inp.date,
        rows=[r.model_dump() for r in inp.rows],
        other_notes=inp.other_notes, saved_at=saved_at)
    return {"thread_id": graph_draft.draft_thread_id(inp.child_id, inp.date),
            "saved_at": saved_at}


@app.delete("/api/draft")
def api_draft_delete(child_id: str, date: str):
    """草稿提交成功后删掉（语义上的删除，不是写一份空的）。"""
    g = get_draft_graph()
    graph_draft.clear_draft(g.checkpointer, child_id=child_id, date=date)
    return {"cleared": True,
            "thread_id": graph_draft.draft_thread_id(child_id, date)}


@app.post("/api/feedbacks")
def api_feedbacks(inp: FeedbackIn):
    """★ 逐条反馈 → 走**反馈链**（Phase 4）。**唯一有业务语义的新增路由。**

    **不调 LLM 就能秒回**（outcome/quality 全是纯 Python 映射）；
    只有 note 非空的条目才各触发一次抽取，另加一次日结（可到 60s）。
    响应里的 `llm_calls` 告诉前端"刚才那几秒是在抽事件"，
    否则家长会以为界面卡死了。

    ★ Phase 1：`other_notes` **不再**拿��去抽事件 ——「其他观察」改由
      `POST /api/observe/run` 的图接管。这里传空字符串，抽取只走逐条 note。
      `llm_calls` 的计数同步不再把它算进去。

    ★ Phase 4：这条路由**不再自己落库**，改走反馈链
      （`record_execution → analyze_outcome → next_action → apply_transitions`）。
      路由形状、响应字段**一个没变** —— 前端不用改。
      换来的是两件以前做不到的事：
        1. **重复提交不多写一行**（以前 refusal_counts 会翻倍，
           当天误触冷却阈值 —— 而界面上看不出为什么）。
        2. **「接下来怎么办」有了一个可判定的答案**
           （continue / adjust / pause / escalate），随响应一起给前端。
    """
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    conn = _conn()
    _profile_or_400(conn, inp.child_id)

    n_notes = sum(1 for f in inp.feedbacks if f.note.strip())

    t0 = time.time()
    out = _run_feedback_chain(conn, inp.child_id, inp.date, inp.feedbacks,
                               no_llm=no_llm)
    # ★ llm_calls 在**跑完之后**才算 —— 因为命中 checkpoint 守卫时
    #   一次 LLM 都没付（那正是这道守卫的意义：重复提交不该让家长再等 20-60 秒）。
    #   以前它在跑之前算，于是前端会显示"刚才那几秒是在抽事件"，
    #   而实际上什么都没发生。
    # 日结那一次 LLM 也算进去（以前它藏在 /api/close 里，
    # 父路由的 llm_calls 没算它 —— 前端于是以为"没有慢调用"）。
    llm_calls = 0 if (out.get("_idempotent") or no_llm) else n_notes + 1

    return _feedback_response(conn, inp.child_id, inp.date, out,
                               llm_calls=llm_calls,
                               elapsed_ms=int((time.time() - t0) * 1000))


def _run_feedback_chain(conn, child_id: str, date: str, feedbacks, *,
                         no_llm: bool, force: bool = False) -> dict:
    """跑反馈链，返回它的最终 state。

    ★ ★ 两层幂等，缺一不可
    ----------------------
    1. **checkpoint 那层**（这里）：同一天提交**一模一样**的内容 →
       指纹（`feedback.feedback_digest`）相同 → **不重跑图**，
       直接返回上次存下来的结果。
       为什么需要它：图里有一次 LLM（日结），
       只做第 2 层的话数据是对的，但家长要为一次重复提交再等 20-60 秒。

    2. **落库那层**（唯一索引 + upsert）：同一次执行重跑一次不多写一行；
       家长更正了某一条会**覆盖**当天那一条。

    ★ 指纹变了（家长更正了）就该重跑 —— 这时第 2 层接手，
       于是"更正"表现为覆盖而不是多出一行。
    """
    from graph.run_graph import run_inputs
    from graph.runtime import get_run_graph
    from graph.state import run_config

    g = get_run_graph()
    cfg = run_config(child_id, date)
    inp = run_inputs(conn, child_id, date, feedbacks, no_llm=no_llm)

    if not force:
        prev = (g.get_state(cfg).values or {})
        if (prev.get("input_digest") and prev["input_digest"] == inp["input_digest"]
                and (prev.get("trace") or [])[-1:] == ["apply_transitions"]):
            prev = dict(prev)
            prev["_idempotent"] = True
            return prev

    out = dict(g.invoke(inp, cfg))
    out["_idempotent"] = False
    return out


def _feedback_response(conn, child_id: str, date: str, out: dict, *,
                       llm_calls: int, elapsed_ms: int) -> dict:
    """把反馈链的最终 state 拼成 HTTP 响应。**字段与 Phase 3 一致**，
    外加 Phase 4 新增的三样：`next_action` / `action_reason` / `red_flag_ids`。"""
    from feedback import render_day

    sessions = out.get("sessions") or []
    hits = out.get("refusal_hits") or []
    summary = out.get("daily_summary") or {}
    transitions = out.get("transitions") or []
    degraded = _degraded(out)

    # ★ 响应里带上 refusal_counts：手动测试清单里那条核心断言
    #   （Day1 只填 gro-007，fin-001/gro-006 点「跳过」→ 必须是 {'gro-007': 1}）
    #   就在这个字段上核对。
    close = {
        "date": date,
        "transitions": _jsonable(transitions),
        "transition_text": "\n".join(_transition_line(t) for t in transitions)
                          or "今天没有转移 —— 升级闸门要求画像可信度 ≥ 0.5",
        "summary": _jsonable(summary),
        "summary_text": _render_summary(summary) if summary else "",
        "states": store.current_states(conn, child_id),
        "already_closed": False,
    }
    return {
        "date": date,
        "sessions": _jsonable(sessions),
        "session_ids": out.get("session_ids") or [],
        "refusal_hits": _jsonable(hits),
        "refusal_counts": store.refusal_counts(conn, child_id),
        "observations": _jsonable(out.get("outcomes") or []),
        "n_observations": len(out.get("observation_ids") or []),
        "degraded": degraded,
        "llm_calls": llm_calls,
        "elapsed_ms": elapsed_ms,
        "render": render_day(date, sessions, hits, degraded),
        "close": close,
        # ★ Phase 4 新增
        "next_action": out.get("next_action") or "continue",
        "action_reason": out.get("action_reason") or "",
        "transitions": _jsonable(transitions),
        "red_flag_ids": out.get("red_flag_ids") or [],
        "outcome_stats": out.get("outcome_stats") or {},
        "idempotent": bool(out.get("_idempotent")),
        "trace": out.get("trace") or [],
    }


def _degraded(out: dict) -> bool:
    """这一天有没有降级（模型不可用）。"""
    for o in (out.get("outcomes") or []):
        if isinstance(o, dict) and o.get("degraded"):
            return True
    return bool((out.get("daily_summary") or {}).get("degraded"))


def _transition_line(t: dict) -> str:
    """一条转移的人话。

    ★ 刻意**还原成 `statemachine.StateTransition` 再调它的 `render`**，
      而不是在这里另写一遍箭头与措辞 ——
      两处各写一遍文案必然漂移，而漂移的后果是
      "前端显示的转移"与"库里那条转移"长得不一样。
    """
    from statemachine import StateTransition
    try:
        return StateTransition(
            skill_item=t.get("skill_item", ""), domain=t.get("domain", ""),
            from_state=t.get("from_state", ""), to_state=t.get("to_state", ""),
            reason=t.get("reason", ""), evidence=dict(t.get("evidence") or {}),
            at_date=t.get("at_date", "")).render()
    except Exception:
        return str(t)


@app.post("/api/close/{child_id}")
def api_close(child_id: str, on: str, force: bool = False):
    """**日结**：状态转移 → 日结 Agent。转移先跑（零 token），日结读已落库的 sessions。

    ★ 现在由 `/api/feedbacks` 在家长提交当天最后一条反馈后**自动触发**，
      不再是前端要点第二个按钮。理由不是省一次点击，是那条顺序不变量
      不该由前端负责维持：转移规则全是时间窗统计、要求当天记录已落库，
      拆成两个端点就等于把这个约束交给调用方记着。

    ★ Phase 4：这条路也**走反馈链**了（不提交新的反馈 = 重算模式）。
      以前它自己调 `apply_transitions` + `summarize_day`，
      于是"同一天收尾两次"要靠这一层自己判断（"已收尾就返回"）——
      那是**调用方的自觉**，换个入口就没有了。
      现在幂等是数据层的性质（唯一键），这一层只是**快**一点。

    ★ 仍然保留"已收尾过就直接返回"这一道：它省掉一次 LLM 调用，
      而家长不会因为点两下"收尾"就等两遍日结。
    """
    no_llm = _NO_LLM
    conn = _conn()
    _profile_or_400(conn, child_id)

    # ---- 幂等：当天已经收尾过就不重跑 ----
    # 判据是"这天有没有日结行"，不是 session 数。理由：`daily_summary` 是
    # 只追加的（同一天可有多条，取用按 id 最大），但**转移不是** ——
    # 见上面的说明。家长收尾后又补一条反馈的情况，前端显式传 force=true。
    if not force:
        done = store.latest_summary(conn, child_id, days=1, before=_plus1(on))
        if done and done[-1].get("date") == on:
            # ★ 「接下来怎么办」从 checkpoint 里读 —— 反馈链跑完时算过一遍，
            #   存在那儿了。读它不用再跑一次图，也不用付那次 LLM。
            #   读不到（这一天是别处收尾的，比如 CLI）就退回"continue"，
            #   而不是假装算过。
            act, why = _stored_next_action(child_id, on)
            return {"date": on, "transitions": [], "transition_text": "（今天已经收尾过了）",
                    "summary": done[-1], "summary_text": _render_summary(done[-1]),
                    "states": store.current_states(conn, child_id), "already_closed": True,
                    "next_action": act, "action_reason": why}

    # ★ 走反馈链，**不提交新的反馈** → 图的 `record_execution` 从库里读
    #   那天已经落了什么（重算模式）。于是这里算出来的与
    #   `/api/feedbacks` 那次算的是**同一套口径**。
    out = _run_feedback_chain(conn, child_id, on, [], no_llm=no_llm, force=True)
    transitions = out.get("transitions") or []
    summary = out.get("daily_summary") or {}
    return {
        "date": on,
        "transitions": _jsonable(transitions),
        "transition_text": "\n".join(_transition_line(t) for t in transitions) or
                          "今天没有转移 —— 升级闸门要求画像可信度 ≥ 0.5",
        "summary": _jsonable(summary),
        "summary_text": _render_summary(summary) if summary else "",
        "states": store.current_states(conn, child_id),
        "already_closed": False,
        # ★ Phase 4 新增
        "next_action": out.get("next_action") or "continue",
        "action_reason": out.get("action_reason") or "",
    }


def _plus1(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _stored_next_action(child_id: str, date: str) -> tuple[str, str]:
    """从反馈链的 checkpoint 里读上次算出来的「接下来怎么办」。**纯读。**

    读不到就退回 ("continue", "") —— 读不到意味着这一天是别处收尾的
    （比如 CLI 跑的），那就没有"接下来怎么办"可读。
    宁可说"照常"，也不要编一个。
    """
    from graph.runtime import get_run_graph
    from graph.state import run_config
    try:
        vals = (get_run_graph().get_state(run_config(child_id, date)).values or {})
    except Exception:
        return "continue", ""
    return (vals.get("next_action") or "continue",
            vals.get("action_reason") or "")


def _render_summary(row: dict) -> str:
    try:
        return summary_mod.DailySummary.model_validate(row).render()
    except Exception:
        return ""


@app.get("/api/summaries/{child_id}")
def api_summaries(child_id: str, days: int = 7, before: str | None = None):
    """最近 N 天日结。**教练 Agent 读的就是这一批**（经 `latest_summary(before=)`）。"""
    conn = _conn()
    return {"summaries": store.latest_summary(conn, child_id, days=days, before=before)}


@app.get("/api/progress/{child_id}")
def api_progress(child_id: str, days: int = 30):
    """谁进步了。**纯 SQL，秒回。**降级画像下"卡在未开始，本可到入门"是正确输出。"""
    conn = _conn()
    prof = store.latest_profile(conn, child_id)
    rep = store.what_progressed(conn, child_id, lookback_days=days,
                                profile_confidence=prof.overall_confidence if prof else None)
    return {"report": _jsonable(rep), "text": rep.render(),
            "states": store.current_states(conn, child_id)}


@app.get("/api/transitions/{child_id}")
def api_transitions(child_id: str, skill_item: str | None = None):
    """状态历史。★ 转移由 Python 计数规则决定，与 LLM 无关 —— 所以这里秒回。"""
    conn = _conn()
    if skill_item:
        return {"history": store.state_history(conn, child_id, skill_item)}
    return {"states": store.current_states(conn, child_id),
            "refusal_counts": store.refusal_counts(conn, child_id)}


@app.post("/api/config")
def api_config(no_llm: bool = True):
    """切离线/在线。手动测试时用来在"每次都等 45s"和"0.0s"之间来回切。"""
    global _NO_LLM
    _NO_LLM = no_llm
    return {"no_llm": _NO_LLM, "llm_mode": os.getenv("DEMO_MODE", "live")}


@app.get("/api/health")
def api_health():
    return {"ok": True, "no_llm": _NO_LLM, "mode": os.getenv("DEMO_MODE", "live")}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
