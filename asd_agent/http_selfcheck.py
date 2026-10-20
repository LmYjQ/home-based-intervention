# -*- coding: utf-8 -*-
"""
HTTP 层自检
===========

    cd asd_agent && ASD_DB_PATH=/tmp/http_selfcheck.db ../.venv/bin/python http_selfcheck.py

★ 为什么这个文件存在
--------------------
`server.py` **不在任何回归集里** —— 12 个离线回归跑的是 asd_agent/ 的模块，
`/api/card` 那种跨进程接线漏改了没有任何东西会报错（PROGRESS.md 阶段 5 的原话：
「12 个回归全绿但 /api/card 500」）。Phase 1 往 server.py 里接了图，
不补这一层就等于把刚做对的东西放在没人守的地方。

沿用仓库现成的写法（`graph/selfcheck.py` / `feedback.py::_cli` 同款）：
`python x.py` + 打印 ✓/✗，**不引入测试框架**。`TestClient` 本来就装了
（fastapi 0.146.0 / httpx 0.28.1）。

★ 最要紧的一条是 [6]
--------------------
「没有待回答的澄清时 resume 必须 409」。`Command(resume=...)` 打在一个
没有 pending interrupt 的 thread 上，LangGraph 会把它当成**新输入**从头跑
一遍整条图 —— 于是重新抽取、重新落库。**没有报错，没有异常，就是数据写了两遍。**
这条断言是 Phase 1 里唯一「不测就会静默出事」的地方。
"""

from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import store                                          # noqa: E402

DB = os.getenv("ASD_DB_PATH", "/tmp/http_selfcheck.db")
CID = "http-check"


def _seed_child() -> None:
    """建一个孩子 + 一份**降级画像**（不调模型）。

    ★ 为什么必须先有画像：观察链的端点调 `_profile_or_400`，而
      `observations.child_id` 对 `children` 有外键 —— 没有这一行，
      publish 落库时会直接撞外键。
      用 `profile._fallback_profile` 而不是手搓一份：它是生产代码里
      「LLM 不可用时给的那份诚实画像」，字段全、约束全，
      比在这里手写一个 AbilityProfile 更贴近真实形状。
    """
    from capabilities import ParentConstraints
    from profile import _fallback_profile

    conn = store.connect(DB)
    store.ensure_child(conn, CID, name="自检娃", age_months=34)
    prof = _fallback_profile(CID, 34, ParentConstraints())
    store.save_profile(conn, prof)


def main() -> int:
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    store.reset(DB)
    from fastapi.testclient import TestClient
    import server                                       # noqa: E402

    _seed_child()
    c = TestClient(server.app)

    # =============================================== 1. 基本连通
    print("\n[1] 基本连通")
    r = c.get("/api/health")
    check("/api/health 200", r.status_code == 200, r.status_code)

    # =============================================== 2. 草稿往返
    print("\n[2] 草稿：存 checkpoint")
    d = "2026-10-20"
    r = c.get("/api/draft", params={"child_id": CID, "date": d})
    check("空草稿返回 draft=null", r.status_code == 200 and r.json()["draft"] is None,
          r.status_code)

    rows = [{"activity_id": "gro-007", "outcome": "", "prompt_level": 0, "note": "试了两次"}]
    r = c.put("/api/draft", json={"child_id": CID, "date": d,
                                   "rows": rows, "other_notes": "超市里他捂耳朵"})
    check("PUT 草稿 200", r.status_code == 200, r.status_code)

    got = c.get("/api/draft", params={"child_id": CID, "date": d}).json()["draft"]
    check("★ 半填的行能存下来（outcome 为空也存得下）",
          got is not None and got["rows"][0]["outcome"] == "", got)
    check("自由文本存取一致", got is not None and got["other_notes"] == "超市里他捂耳朵")

    # 草稿不该有任何业务副作用
    conn = store.connect(DB)
    n_biz = conn.execute(
        "SELECT (SELECT count(*) FROM observations WHERE child_id=%s) + "
        "(SELECT count(*) FROM sessions WHERE child_id=%s) AS n",
        (CID, CID)).fetchone()["n"]
    check("★ 存草稿一个业务表都没碰", n_biz == 0, n_biz)

    # =============================================== 3. 草稿的上界
    print("\n[3] 草稿不是垃圾桶（宽松 ≠ 没有上界）")
    r = c.put("/api/draft", json={"child_id": CID, "date": d,
                                   "rows": [{"activity_id": "x", "prompt_level": 9}]})
    check("prompt_level=9 被拒（提示层级是 0-3 的 int）", r.status_code == 422, r.status_code)

    r = c.delete("/api/draft", params={"child_id": CID, "date": d})
    check("DELETE 草稿 200", r.status_code == 200, r.status_code)
    check("清空后读回 null",
          c.get("/api/draft", params={"child_id": CID, "date": d}).json()["draft"] is None)

    # =============================================== 4. 跑观察链（离线）
    print("\n[4] 观察链跑通（no-llm）")
    text = "超市里他蹲下来捂住耳朵了，我没理他，过了一会儿自己好了。"
    r = c.post("/api/observe/run", json={"child_id": CID, "date": d,
                                         "text": text, "no_llm": True})
    check("POST /api/observe/run 返回 job_id", r.status_code == 200 and "job_id" in r.json(),
          r.status_code)
    job = r.json()["job_id"]

    res = _await(c, job)
    check("job 跑完且没报错", res is not None and res.get("status") == "done",
          res.get("error") if res else "超时")
    result = (res or {}).get("result") or {}
    check("★ 离线时 phase=done（降级恒不打断，问也只会得到「我不知道」）",
          result.get("phase") == "done", result.get("phase"))
    check("★ 降级时 observation_ids 为空（一条都不编）",
          result.get("observation_ids") == [], result.get("observation_ids"))
    check("★ 降级时四层全空",
          (result.get("parent_interpretation"), result.get("hypotheses"),
           result.get("missing_information")) == (None, [], []),
          (result.get("parent_interpretation"), result.get("hypotheses"),
           result.get("missing_information")))
    check("响应里有 thread_id（resume 靠它，不靠 job）",
          result.get("thread_id"), result.get("thread_id"))
    check("响应里有 trace（能看清这次走了哪几个节点）",
          bool(result.get("trace")), result.get("trace"))

    # =============================================== 5. status 与 run 一致
    print("\n[5] status（刷新后靠它知道有没有待答的问题）")
    st = c.get("/api/observe/status", params={"child_id": CID, "date": d})
    check("GET /api/observe/status 200", st.status_code == 200, st.status_code)
    body = st.json()
    check("status 与 run 结果一致（同一个 thread 的同一个状态）",
          body.get("phase") == result.get("phase")
          and body.get("thread_id") == result.get("thread_id"),
          body.get("phase"))

    st2 = c.get("/api/observe/status", params={"child_id": CID, "date": "2099-01-01"})
    check("★ 从没跑过的那天 → idle（不是 500）",
          st2.status_code == 200 and st2.json().get("phase") == "idle", st2.status_code)

    # =============================================== 6. ★ resume 的 409 守卫
    print("\n[6] ★ 没有待回答的澄清时 resume 必须 409")
    n_obs_before = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s", (CID,)).fetchone()["n"]
    r = c.post("/api/observe/resume", json={"child_id": CID, "date": d,
                                             "answer": "随便答一句", "no_llm": True})
    check("★ resume 被拒（409）", r.status_code == 409, r.status_code)
    n_obs_after = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s", (CID,)).fetchone()["n"]
    check("★ 被拒后 observations 行数没变（没被静默重跑一遍）",
          n_obs_after == n_obs_before, f"{n_obs_before} → {n_obs_after}")

    # 从没跑过的那天 resume 同样要 409，而不是从头跑一遍
    r = c.post("/api/observe/resume", json={"child_id": CID, "date": "2099-01-01",
                                             "answer": "x", "no_llm": True})
    check("★ 从没跑过的那天 resume 也 409（否则会凭空跑一遍图）",
          r.status_code == 409, r.status_code)

    # =============================================== 7. other_notes 不再双写
    print("\n[7] ★ 「其他观察」只有一个写入口")
    fb = {"child_id": CID, "date": d, "no_llm": True,
          "feedbacks": [{"activity_id": "gro-007", "outcome": "refused",
                         "prompt_level": 0, "steps_done": 0, "note": ""}],
          "other_notes": "这段话现在不该再被抽一遍"}
    r = c.post("/api/feedbacks", json=fb)
    check("/api/feedbacks 200", r.status_code == 200, r.text[:200])
    body = r.json() if r.status_code == 200 else {}
    check("★ other_notes 不再产生 observation（图 publish 才是那个入口）",
          body.get("n_observations", 0) == 0, body.get("n_observations"))

    # =============================================== 8. ★ 澄清往返（走 HTTP）
    print("\n[8] ★ 澄清 interrupt 的 HTTP 往返")
    check_clarify_roundtrip(c, check)

    print("\n" + ("✓ HTTP 层自检全绿" if ok else "✗ 有失败项，见上"))
    return 0 if ok else 1


def check_clarify_roundtrip(c, check) -> None:
    """★ 澄清分支在 `--no-llm` 下**永远进不去** ——
    `validate_observation` 在降级时恒判 false（问也只会得到「我不知道」）。

    所以这一组不指望 `/api/observe/run` 自然走到 interrupt，而是**注入**
    一份已抽好的、带 needs_clarification 的 observation，再让图跑起来。
    被测的是 HTTP 那三层（status 认出在等人 / resume 接上 / 重复 resume 被拒），
    注入只是把图推到那个位置的手段 —— 图自己怎么判的由 graph/selfcheck.py 守。
    """
    import observe as observe_mod
    import redflag
    import schema
    from graph.runtime import get_graph
    from graph.state import thread_config

    d = "2026-10-29"
    cfg = thread_config(CID, d)
    g = get_graph()
    text = "他今天不太配合"
    question = "是哪个环节不配合？当时在做什么？"

    ev = schema.Observation(
        scene="indoor_play", behavior="refusal", antecedent="unclear",
        consequence="none", intensity=2, duration_sec=None,
        people_present=["mother"], raw_quote=text, spans=[], confidence=0.5,
        needs_clarification=True, clarifying_question=question)
    seeded = observe_mod.ObservationOutcome(
        source_text=text, date=d, events=[ev], redflags=redflag.CLEAN).model_dump(mode="json")

    # 注入：observed_text == parent_text 让 extract 走「复用」分支，
    # 于是这份带 needs_clarification 的抽取结果会被 validate 看到。
    g.update_state(cfg, {"child_id": CID, "date": d, "parent_text": text,
                          "observation": seeded, "observed_text": text,
                          "clarifying_question": question, "no_llm": True},
                   as_node="record_intake")
    g.invoke({"child_id": CID, "date": d, "parent_text": text, "no_llm": True}, cfg)

    st = c.get("/api/observe/status", params={"child_id": CID, "date": d}).json()
    check("★ status 认出图停在 interrupt 上（phase=clarify）",
          st.get("phase") == "clarify", st.get("phase"))
    check("★ 追问文案透传到前端", (st.get("interrupt") or {}).get("question") == question,
          (st.get("interrupt") or {}).get("question"))
    check("停在 interrupt 时还没有 observation 落库", st.get("observation_ids") == [],
          st.get("observation_ids"))

    r = c.post("/api/observe/resume", json={"child_id": CID, "date": d,
                                             "answer": "其实是我让他先洗手，他就不玩了",
                                             "no_llm": True})
    check("resume 返回 job_id", r.status_code == 200 and "job_id" in r.json(), r.status_code)
    res = _await(c, r.json()["job_id"])
    check("resume 的 job 跑完", res is not None and res.get("status") == "done",
          res.get("error") if res else "超时")
    out = (res or {}).get("result") or {}
    check("★ resume 后跑完（phase=done）", out.get("phase") == "done", out.get("phase"))
    check("回答并进了 state（语料是追加不是替换）",
          "先洗手" in json.dumps(out, ensure_ascii=False), out.get("trace"))

    # ★ 答完之后再 resume 一次必须 409 —— 否则会把整条图重跑一遍
    r2 = c.post("/api/observe/resume", json={"child_id": CID, "date": d,
                                              "answer": "再答一次", "no_llm": True})
    check("★ 答完后再 resume 被拒（409），不会重跑整条图", r2.status_code == 409, r2.status_code)


def _await(c, job_id: str, timeout: float = 30.0) -> dict | None:
    """轮询 job 直到终态。

    ★ 用轮询而不是读 SSE：`/api/jobs/{id}` 是同一个 job 的另一个出口，
      断言走它更稳（SSE 是 async 生成器，TestClient 里要单独 drain）。
      两条出口读的是同一个 `_Job`，所以覆盖的是同一段代码。
    """
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/jobs/{job_id}")
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(0.05)
    return None


if __name__ == "__main__":
    sys.exit(main())
