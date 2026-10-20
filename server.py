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
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "asd_agent"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import llm
import observe
import vocab
import store
import summary as summary_mod
from activities import PlanRequest, ScoredActivity, candidates as retrieve_candidates
from activities import explain_retrieval, retrieve
from capabilities import AbilityProfile, ParentConstraints
from coach import (CoachOutcome, ObservationInput, card_from_activity,
                   coach_today, render_feedback_form)
from evaluator import TransitionsInput, apply_transitions
from feedback import ActivityFeedback, apply_feedbacks, quality_for
from planner import pack_day
from summary import summarize_day
import loop as loop_mod
from schema import Observation

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
    age_months: int = Field(default=34, ge=0, le=240)
    daily_minutes: int = Field(default=30, ge=5, le=180)
    materials: list[str] = Field(default_factory=lambda: ["杯子", "绘本", "小汽车", "积木"])
    avoid_load_sensory: bool = True
    no_llm: bool | None = None       # 不传 = 跟随全局开关


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
    other_notes: str = ""
    no_llm: bool | None = None


# ================================================================== 慢路径（job + SSE）
@app.post("/api/profile")
def api_profile(inp: ProfileIn):
    """建档：报告 → 画像。**18-47s，必须走 job**，同步等必然超时。"""
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    constraints = ParentConstraints(
        daily_minutes_available=inp.daily_minutes,
        materials_at_home=inp.materials,
        avoid_load_sensory=inp.avoid_load_sensory,
    )

    def work(step):
        from redflag import scan
        from profile import ProfileExtraction, extract_profile
        conn = _conn()
        if no_llm:
            step("离线模式：用确定性降级画像（可信度 0.1，状态机会冻结全部升级）")
            from profile import _fallback_profile
            prof = _fallback_profile(inp.child_id, inp.age_months, constraints)
            ex = ProfileExtraction(
                profile=prof, redflags=scan(inp.report_text), degraded=True,
                note="降级画像：可信度 0.1 —— 低于升级闸门 0.5，状态机将冻结所有升级")
        else:
            step(f"读取报告 {len(inp.report_text)} 字，先跑红旗规则扫描（零 token）")
            ex = extract_profile(inp.report_text, inp.child_id, inp.age_months,
                                 parent_constraints=constraints, on_step=step)
        step(f"回指校验完成，未通过 {len(ex.unverified)} 条；写库")
        # 顺序不变量：先 ensure_child 再 save_profile —— 后者有外键指向前者。
        # 迁移前这里靠 _IO_LOCK 串行化，迁移后每请求一个独立连接 + 单事务，
        # 由 PostgreSQL 自己保证顺序。
        store.ensure_child(conn, inp.child_id, age_months=inp.age_months)
        store.save_profile(conn, ex.profile)
        return {
            "profile": _jsonable(ex.profile),
            "redflags": _jsonable(ex.redflags),
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
        if not inp.force:
            saved = store.latest_plan(conn, inp.child_id, inp.date)
            if saved and saved.get("profile_id") == prof_id:
                step(f"{inp.date} 已经生成过（{len(saved.get('activities') or [])} 条），直接读")
                return saved | {"cached": True}
            if saved:
                step("画像已更新，之前排的那份作废 → 重新排")

        req = _build_request(conn, inp.child_id, prof, before=inp.date)
        step(f"检索候选（纯 Python，{len(req.refusal_counts)} 条抗拒记录参与）")
        cands = retrieve_candidates(req, top_k=12)
        if not cands:
            step("候选表为空")
            return {"error": "候选表为空，今天不出卡片"}

        prev = _recent_obs_inputs(conn, inp.child_id, before=inp.date)
        step(f"喂给诊断的观察 {len(prev)} 条（都是 {inp.date} 之前的）")

        prefs = prof.parent_constraints.preferences
        if no_llm:
            step("离线模式：用活动定义直接生成兜底卡片")
            out = CoachOutcome(
                card=card_from_activity(cands[0].activity, prefs),
                activity=cands[0].activity, degraded=True,
                adjustment="（离线：使用活动定义直接生成的兜底卡片）")
        else:
            step("教练 Agent 调模型（2 次）")
            out = coach_today(
                cands, today_id=cands[0].activity.activity_id,
                observations=prev, req=req, recent_summary=req.recent_summary,
                on_step=step)
        if out.adjustment:
            step(f"调整：{out.adjustment}")

        # ---- 装箱：诊断选定的焦点先入箱，剩下的分钟数分给其余候选 ----
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
        store.save_plan(conn, inp.child_id, inp.date, payload)
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
    return {
        "child_id": child_id,
        "has_profile": prof is not None,
        "profile_confidence": prof.overall_confidence if prof else None,
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


@app.post("/api/feedbacks")
def api_feedbacks(inp: FeedbackIn):
    """★ 逐条反馈 → sessions + observations。**唯一有业务语义的新增路由。**

    **不调 LLM 就能秒回**（outcome/quality 全是纯 Python 映射）；
    只有 note 非空的条目才各触发一次抽取（可到 60s）。
    响应里的 `llm_calls` 告诉前端"刚才那几秒是在抽事件"，
    否则家长会以为界面卡死了。
    """
    no_llm = _NO_LLM if inp.no_llm is None else inp.no_llm
    conn = _conn()
    _profile_or_400(conn, inp.child_id)

    n_notes = sum(1 for f in inp.feedbacks if f.note.strip())
    n_notes += 1 if inp.other_notes.strip() else 0
    llm_calls = 0 if no_llm else n_notes

    t0 = time.time()
    res = apply_feedbacks(conn, inp.child_id, inp.date, inp.feedbacks,
                          other_notes=inp.other_notes, no_llm=no_llm)
    # ★ 家长提交当天最后一条反馈 → 自动收尾（状态转移 + 日结）。
    #   不做成前端点第二个按钮：那条顺序不变量（记录先落库、再转移再日结）
    #   是后端的性质，不该由前端记着。收尾本身幂等，重复提交不会重复转移。
    close = api_close(inp.child_id, inp.date)
    # ★ 响应里带上 refusal_counts：手动测试清单里那条核心断言
    #   （Day1 只填 gro-007，fin-001/gro-006 点「跳过」→ 必须是 {'gro-007': 1}）
    #   就在这个字段上核对。
    return {
        "date": res.date,
        "sessions": _jsonable(res.sessions),
        "session_ids": res.session_ids,
        "refusal_hits": _jsonable(res.refusal_hits),
        "refusal_counts": store.refusal_counts(conn, inp.child_id),
        "observations": _jsonable(res.outcomes),
        "n_observations": len(res.observation_ids),
        "degraded": res.degraded,
        "llm_calls": llm_calls,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "render": res.render(),
        "close": close,
    }


@app.post("/api/close/{child_id}")
def api_close(child_id: str, on: str, force: bool = False):
    """**日结**：状态转移 → 日结 Agent。转移先跑（零 token），日结读已落库的 sessions。

    ★ 现在由 `/api/feedbacks` 在家长提交当天最后一条反馈后**自动触发**，
      不再是前端要点第二个按钮。理由不是省一次点击，是那条顺序不变量
      不该由前端负责维持：转移规则全是时间窗统计、要求当天记录已落库，
      拆成两个端点就等于把这个约束交给调用方记着。

    ★ 幂等。自动触发意味着可能被重复调用（家长重复提交、网络重试），
      而 `apply_transitions` **不幂等**：它 append_state 只追加，
      跑第二次会读到第一次刚写入的状态（`since` 已推进），可能再转移一次
      —— 就是 PROGRESS 坑 4 那个"状态抖动"。
      `save_summary` 同理会插重复行。
      → 已收尾过就直接返回落库的那份，不重跑。`force=true` 强制重算。
    """
    no_llm = _NO_LLM
    conn = _conn()
    prof = _profile_or_400(conn, child_id)

    # ---- 幂等：当天已经收尾过就不重跑 ----
    # 判据是"这天有没有日结行"，不是 session 数。理由：`daily_summary` 是
    # 只追加的（同一天可有多条，取用按 id 最大），但**转移不是** ——
    # 见上面的说明。家长收尾后又补一条反馈的情况，前端显式传 force=true。
    if not force:
        done = store.latest_summary(conn, child_id, days=1, before=_plus1(on))
        if done and done[-1].get("date") == on:
            return {"date": on, "transitions": [], "transition_text": "（今天已经收尾过了）",
                    "summary": done[-1], "summary_text": _render_summary(done[-1]),
                    "states": store.current_states(conn, child_id), "already_closed": True}

    sessions = summary_mod._sessions_on(conn, child_id, on)
    # 顺序不变量：转移全部算完再逐条追加 —— apply_transitions 读的是
    # append_state 之前的 current_states，边算边写会让同一天的后一条
    # 转移看到前一条的结果。迁移前靠 _IO_LOCK 保证这段不被别的请求插进来。
    ts = apply_transitions(TransitionsInput(date=on, sessions=sessions),
                          store.current_states(conn, child_id), prof,
                          conn=conn, child_id=child_id)
    for t in ts:
        store.append_state(conn, child_id, t)
    summ = summarize_day(conn, child_id, on, sessions=sessions, no_llm=no_llm)
    return {
        "date": on,
        "transitions": [_jsonable(t) for t in ts],
        "transition_text": "\n".join(t.render() for t in ts) or
                          "今天没有转移 —— 升级闸门要求画像可信度 ≥ 0.5",
        "summary": _jsonable(summ),
        "summary_text": summ.render(),
        "states": store.current_states(conn, child_id),
        "already_closed": False,
    }


def _plus1(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


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
