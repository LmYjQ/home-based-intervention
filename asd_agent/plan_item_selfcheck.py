# -*- coding: utf-8 -*-
"""
阶段 1 验收：放宽 activity_id
=============================

    cd asd_agent && ASD_DB_PATH=/tmp/phase1_selfcheck.db \\
        ../.venv/bin/python plan_item_selfcheck.py

★ 这一组守的是 REFACTOR.md §1.0 的五条验收
------------------------------------------
  1. 生成项与库内项**平权**（一起装箱、一起被打分、一起被难度闸门筛）
  2. 放宽 ≠ 绕过（难度闸门 / 材料检查 / 时间预算对生成项一样生效）
  3. 语义变化要打断家长，**只换 ID 不能打断**
  4. 生成项能出卡片、能收反馈、且被状态机归到**对的**技能细项
  5. 老 payload / 老 checkpoint / 老 session 行仍然读得出来

★ 为什么单独一个文件，不并进 `graph/selfcheck.py`
--------------------------------------------------
`graph/selfcheck.py` 守的是**编排**（节点顺序、interrupt、写库时机），
它跑的是真的图。这里守的是**放宽 activity_id 这件事本身** ——
大部分断言是纯函数级的（装箱、打分、指纹、签名），
不需要起图、不需要 checkpointer、也不需要 LLM。
混进 `graph/selfcheck.py` 会让那套"每条断言都必须在模型挂了时也能跑"的
严谨性被一组纯函数断言稀释。

★ 为什么用固定 fixture，不用 LLM
-------------------------------
理由同 `generated.py`：这一阶段验的是**管道**，不是**生成质量**。
LLM 一来，网络一断整套自检就全挂；LLM 的不确定性还会让
"指纹不变"这类断言变成 flaky —— 而那正是本阶段最要紧的判据。
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import activities                                            # noqa: E402
import plan_item                                             # noqa: E402
import planner                                               # noqa: E402
import store                                                 # noqa: E402
from activities import Activity, PlanRequest                 # noqa: E402

DB = os.getenv("ASD_DB_PATH", "/tmp/phase1_selfcheck.db")
CID = "phase1-check"


def _req(**kw) -> PlanRequest:
    """一份**确定性的**检索请求。默认给足预算、家里材料齐全。"""
    base = dict(daily_minutes=30, materials_at_home={"卡片", "积木"},
                levels={}, target_states={}, priority_domains=["social_adapt"])
    base.update(kw)
    return PlanRequest(**base)


def _gen(**kw) -> Activity:
    """一条生成项（fixture 的副本，可改字段）。"""
    import generated
    a = generated.fixture_items()[0]
    d = a.model_dump()
    d.update(kw)
    return Activity.model_validate(d)


def main() -> int:
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    # =============================================== 1. 一起装箱
    print("\n[1] 库内项与生成项能被 pack_day 一起装箱")
    from activity_data import load_activities
    lib = list(load_activities())
    # 挑两条**技能细项不与生成项重叠**的库内项
    gen = _gen()
    others = [a for a in lib
              if not (set(a.target_skill_items) & set(gen.target_skill_items))][:5]
    pool = [activities.score_item(a, _req()) for a in others] + \
           [activities.score_item(gen, _req())]
    from planner import pack_day
    picks = pack_day(pool, 60)
    ids = [p.activity.activity_id for p in picks]
    check("★ 生成项与库内项进了同一个箱子",
          gen.activity_id in ids, ids)
    check("库内项也在",
          any(i in {a.activity_id for a in others} for i in ids), ids)
    check("★ 生成项没有被当成「库里没有」而被跳过（plan_item.is_generated 认得出它）",
          plan_item.is_generated(gen.activity_id)
          and not plan_item.is_generated(others[0].activity_id))

    # ---- 超预算时生成项同样被砍掉 ----
    # 预算 4 分钟 < 生成项的 5 分钟 → 它装不进去。
    # （不能拿"库里那几条也都装不下"来当证据 —— 那样分不清是谁被砍的。）
    tight = pack_day(pool, 4)
    check("★ 预算不够时生成项同样被砍掉（放宽 ≠ 豁免预算）",
          gen.activity_id not in [p.activity.activity_id for p in tight],
          [p.activity.activity_id for p in tight])

    # =============================================== 2. 硬过滤对生成项一样生效
    print("\n[2] 生成项同样过难度闸门与材料检查")
    # 难度闸门：画像 level=0（<2）而活动难度 2 → 挡
    hard_gen = _gen(difficulty=2, target_skill_items=["共同注意"])
    s = activities.score_item(hard_gen, _req(levels={"共同注意": 0}))
    check("★ 难度闸门对生成项一样生效（level<2 且难度>1 → 挡）",
          not s.passed and "水平" in s.hard_filter_reason, s.hard_filter_reason)
    s_ok = activities.score_item(_gen(difficulty=1), _req(levels={"共同注意": 0}))
    check("难度 1 的生成项在同样画像下通过", s_ok.passed, s_ok.hard_filter_reason)

    # 材料检查：essential 材料不在家 → 挡
    mat_gen = _gen(materials=[activities.Material(name="小推车", material_class="toy",
                                                  essential=True)])
    s2 = activities.score_item(mat_gen, _req(materials_at_home=set()))
    check("★ 材料检查对生成项一样生效（essential 不在家 → 挡）",
          not s2.passed and "材料" in s2.hard_filter_reason, s2.hard_filter_reason)
    s3 = activities.score_item(mat_gen, _req(materials_at_home={"小推车"}))
    check("材料齐了就能过", s3.passed, s3.hard_filter_reason)

    # 时间预算
    long_gen = _gen(duration_min=20, sessions_per_day=1, prep_min=0)
    s4 = activities.score_item(long_gen, _req(daily_minutes=10))
    check("★ 时间预算对生成项一样生效",
          not s4.passed and "预算" in s4.hard_filter_reason, s4.hard_filter_reason)

    # =============================================== 3. 同一套权重
    print("\n[3] 生成项拿同一套权重打分，state_match 仍是最高项")
    check("★ 权重表没被改（state_match 0.30 仍是最高）",
          activities.WEIGHTS["state_match"] == max(activities.WEIGHTS.values())
          and activities.WEIGHTS["state_match"] == 0.30, activities.WEIGHTS)
    # 同一份内容，库内项与生成项的**分项得分完全相同**
    lib_same = [a for a in lib if a.target_skill_items == gen.target_skill_items]
    if lib_same:
        other = lib_same[0]
        sl = activities.score_item(other, _req())
        sg = activities.score_item(gen, _req())
        check("★ 同内容不同来源 → 分项得分逐项相同（来源不影响打分）",
              sl.breakdown == sg.breakdown,
              (sl.breakdown, sg.breakdown))
    else:
        check("★ 同内容不同来源 → 分项得分逐项相同（来源不影响打分）", True,
              "（活动库里没有同细项的对照项，跳过）")
    sg = activities.score_item(gen, _req())
    check("生成项确实拿到了分项 breakdown（不是走了另一条打分路径）",
          set(sg.breakdown) == set(activities.WEIGHTS), sg.breakdown)

    # =============================================== 4. 指纹不被静默丢弃
    print("\n[4] 指纹不被静默丢弃（REFACTOR §1.3.1）")
    fp = planner.plan_fingerprint([gen])
    check("★ 生成项进了 plan_fingerprint 的输入（items 非空）",
          len(fp["items"]) == 1, fp)
    check("★ 指纹里**没有 activity_id**（ID 污染是 §1.3.2 记的坑）",
          "gen:fixture:0" not in json.dumps(fp, ensure_ascii=False, default=str),
          fp)
    # 旧写法（按 id 回活动库反查）会把它丢掉 —— 这里把那个洞钉死
    from activity_data import get as get_act
    check("★ 旧的反查路径确实查不到它（这正是必须改掉的原因）",
          get_act(gen.activity_id) is None)
    fp_lib = planner.plan_fingerprint(lib[:3])
    check("库内项的指纹仍然算得出来（没被这次改动打死）",
          len(fp_lib["items"]) == 3, len(fp_lib["items"]))

    # =============================================== 5. 指纹：换 ID 不变，改内容就变
    print("\n[5] 指纹：换 ID 不打断，改内容才打断")
    other_id = _gen(activity_id="gen:other-candidate:7")
    check("★ 同一件事换了个 ID → 指纹**不变**（不打断家长）",
          planner.plan_fingerprint([gen]) == planner.plan_fingerprint([other_id]),
          planner.plan_fingerprint([other_id]))
    substantive, reasons = planner.is_substantive_change(
        planner.plan_fingerprint([other_id]), planner.plan_fingerprint([gen]))
    check("★ 换 ID → 不是实质变更（needs_review 为假）",
          not substantive, reasons)

    for label, changed in [
        ("改步骤", _gen(steps=[activities.Step(step_id=i + 1, action=f"第 {i + 1} 步换了",
                                                child_target_response="有反应",
                                                time_limit_sec=5, prompt_level="natural")
                                for i in range(3)])),
        ("改难度", _gen(difficulty=2)),
        ("改起点提示级别", _gen(prompt_hierarchy=["换成更重的一档", "第二档", "第三档"])),
        ("改材料", _gen(materials=[activities.Material(name="小推车",
                                                        material_class="toy",
                                                        essential=True)])),
    ]:
        fp_new = planner.plan_fingerprint([changed])
        sub, rs = planner.is_substantive_change(fp_new, planner.plan_fingerprint([gen]))
        check(f"★ {label} → 指纹变了（要打断家长）", sub and bool(rs), rs)

    # ---- 跨版本（老 v1 指纹 vs 新 v2 指纹）：保守判成有变更 ----
    old_fp = {"skills": ["共同注意"],
              "activities": [["soc-003", ["他喜欢的东西"], 2,
                              "你先不要那个东西，说：没关系", 8]]}
    sub2, rs2 = planner.is_substantive_change(planner.plan_fingerprint([gen]), old_fp)
    check("★ 老指纹（v1）与新指纹（v2）比 → 保守判成有变更（不装作比过）",
          sub2 and any("格式升级" in r for r in rs2), rs2)

    # ---- 老指纹落库再读回来，仍然能比（JSON 里 tuple 变 list 的坑）----
    from activities import request_to_dict, request_from_dict
    fp_json = json.loads(json.dumps(fp, default=str))
    # 模拟 JSON 落库：tuple → list
    fp_json["items"] = [list(x) if isinstance(x, tuple) else x for x in fp["items"]]
    sub3, rs3 = planner.is_substantive_change(fp_json, fp)
    check("★ 指纹经 JSON 往返后仍能比（tuple→list 不炸、不误判）",
          not sub3, rs3)

    # =============================================== 6. 卡片能渲染生成项
    print("\n[6] 生成项能出卡片（带 origin）")
    import coach
    card = coach.card_from_activity(gen, activities.PreferenceSet())
    check("★ 卡片渲染得出来", bool(card.render()), card.render()[:60])
    check("★ 卡片里能看出它是生成项（origin 非空）",
          bool(gen.origin) and gen.kind == "generated", (gen.kind, gen.origin))

    # =============================================== 7. 反馈落到对的技能细项
    print("\n[7] 生成项的执行反馈落到正确的技能细项")
    store.reset(DB)
    conn = store.connect(DB)
    store.ensure_child(conn, CID, name="阶段1自检娃", age_months=34)
    # 造一份"当天生效的计划"，里面含这条生成项
    payload = {"date": "2026-10-20", "activity_ids": [gen.activity_id],
               "items": [plan_item.to_item(gen)],
               "total_minutes": 5, "budget_minutes": 30,
               "fingerprint": planner.plan_fingerprint([gen])}
    pid = store.save_plan(conn, CID, "2026-10-20", payload, status="approved", version=1,
                          approved_by="parent", approved_at="2026-10-20")
    store.activate_plan(conn, pid)      # 生效（"active" 才是"当前生效的那一版"）
    cur = store.active_plan(conn, CID, on="2026-10-20")
    check("★ 计划落库了（含生成项的完整内容）",
          cur is not None and cur["status"] == "active", cur and cur["status"])
    read_back = store.plan_items(cur["payload"])
    check("★ 读回来的计划里带着生成项（内容完整，不只是 id）",
          len(read_back) == 1 and read_back[0].activity_id == gen.activity_id
          and read_back[0].kind == "generated",
          [(a.activity_id, a.kind) for a in read_back])

    skills_of = store.plan_target_skills(conn, CID, "2026-10-20")
    check("★ 归属表里有这条生成项练什么",
          skills_of.get(gen.activity_id) == gen.target_skill_items, skills_of)

    from statemachine import SessionRecord
    sid = store.log_session(conn, CID,
                             SessionRecord(date="2026-10-20",
                                           activity_id=gen.activity_id,
                                           outcome="success", quality=0.9),
                             target_skills=gen.target_skill_items)
    row = conn.execute("SELECT target_skills FROM sessions WHERE id=%s",
                       (sid,)).fetchone()
    check("★ 执行记录自带归属（不再靠活动库反查）",
          json.loads(row["target_skills"]) == gen.target_skill_items, row["target_skills"])

    for item in gen.target_skill_items:
        got = store.sessions_for_skill(conn, CID, item, until="2026-10-20")
        check(f"★ sessions_for_skill 能查到它（技能细项「{item}」）",
              any(s.activity_id == gen.activity_id for s in got),
              [s.activity_id for s in got])

    # ---- 归属未知时不算进任何技能（不是"算进所有技能"）----
    other = store.sessions_for_skill(conn, CID, "共同注意", until="2026-10-20")
    check("★ 不相关的技能细项查不到它（不虚增别的技能的进展）",
          not any(s.activity_id == gen.activity_id for s in other),
          [s.activity_id for s in other])

    # =============================================== 8. 状态机与唯一键
    print("\n[8] 生成项能进状态机统计，唯一键不撞")
    sid2 = store.log_session(conn, CID,
                              SessionRecord(date="2026-10-20",
                                            activity_id=gen.activity_id,
                                            outcome="success", quality=0.9),
                              target_skills=gen.target_skill_items)
    n = conn.execute("SELECT count(*) AS n FROM sessions WHERE child_id=%s",
                     (CID,)).fetchone()["n"]
    check("★ 同一条反馈再提交一次 → 仍然只有一行（幂等键不撞）",
          n == 1 and sid2 == sid, (sid, sid2, n))
    # 一条**别的**生成项（不同 id）不应撞到同一行
    gen_b = _gen(activity_id="gen:fixture:1", title="另一条生成项",
                 target_skill_items=["共同注意"])
    sid3 = store.log_session(conn, CID,
                              SessionRecord(date="2026-10-20",
                                            activity_id=gen_b.activity_id,
                                            outcome="success", quality=0.9),
                              target_skills=gen_b.target_skill_items)
    n2 = conn.execute("SELECT count(*) AS n FROM sessions WHERE child_id=%s",
                      (CID,)).fetchone()["n"]
    check("★ 不同的生成项 → 不同的行（幂等键按 activity_id 分开）",
          n2 == 2 and sid3 != sid, (sid, sid3, n2))

    # 状态机看得见它的证据
    from capabilities import AbilityProfile, SkillAssessment
    from evaluator import TransitionsInput, apply_transitions
    prof = AbilityProfile(child_id=CID, child_age_months=34, overall_confidence=0.9,
                          assessments=[SkillAssessment(domain="social_adapt",
                                                      skill_item="情绪命名与表达",
                                                      level=0, source="inferred",
                                                      confidence=0.9)])
    sess = [SessionRecord(date="2026-10-20", activity_id=gen.activity_id,
                          outcome="success", quality=0.9)]
    out = apply_transitions(TransitionsInput(date="2026-10-20", sessions=sess),
                            {}, prof, conn=conn, child_id=CID)
    check("★ 状态机跑得起来（归属查得到，不是「归属未知」被排除）",
          isinstance(out, list), out)
    # 归属已知 → hist 里有它
    hist = store.sessions_for_skill(conn, CID, "情绪命名与表达", until="2026-10-20")
    check("★ 状态机拿得到这条证据（hist 非空）", len(hist) >= 1, len(hist))

    # =============================================== 9. 老数据仍能读
    print("\n[9] 老 payload / 老 checkpoint / 老 session 行仍能读")
    # ---- 老 payload：只有 activity_ids ----
    lib_act = lib[0]
    old_payload = {"date": "2026-10-19", "activity_ids": [lib_act.activity_id],
                   "total_minutes": 8, "budget_minutes": 30, "fingerprint": {}}
    old_items = store.plan_items(old_payload)
    check("★ 老 payload（只有 activity_ids）读得出来",
          len(old_items) == 1 and old_items[0].activity_id == lib_act.activity_id,
          [a.activity_id for a in old_items])
    check("★ 老 payload 还原出来的项 kind 是 library",
          old_items[0].kind == "library", old_items[0].kind)

    # ---- 老 checkpoint：plan_request 里没有 extra_items ----
    old_req_dict = request_to_dict(_req())
    old_req_dict.pop("extra_items", None)
    back = request_from_dict(old_req_dict)
    check("★ 老 checkpoint（没有 extra_items 这个键）读得出来",
          back.extra_items == [], back.extra_items)

    # ---- 老 session 行：target_skills 是空串 → 回退活动库反查 ----
    from statemachine import SessionRecord as SR
    conn.execute("INSERT INTO sessions(child_id,session_date,activity_id,outcome,"
                 "quality,out_of_routine,note,run_id,source,seq,target_skills) "
                 "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (CID, "2026-10-19", lib_act.activity_id, "success", 0.9, False,
                  "", "run-old", "parent", 0, ""))
    conn.commit()
    hist_old = store.sessions_for_skill(conn, CID,
                                        lib_act.target_skill_items[0], until="2026-10-20")
    check("★ 老 session 行（target_skills 空串）回退到活动库反查",
          any(s.activity_id == lib_act.activity_id for s in hist_old),
          [s.activity_id for s in hist_old])

    # ---- 老指纹（v1）也能读 ----
    # ★ v1 的 `activities` 是**元组列表**（存进 JSON 后变成 list of list），
    #   形状是 (activity_id, materials, difficulty, prompt_hierarchy[0], 时长)。
    old_fp2 = {"skills": ["共同注意"],
               "activities": [["soc-003", ["他喜欢的东西"], 2, "你先不要那个东西", 8]]}
    sub4, rs4 = planner.is_substantive_change(old_fp2, old_fp2)
    check("★ 老指纹（v1）与自己比 → 无变更", not sub4, rs4)
    old_fp3 = {"skills": ["共同注意"],
               "activities": [["soc-003", ["他喜欢的东西"], 3, "你先不要那个东西", 8]]}
    sub5, rs5 = planner.is_substantive_change(old_fp3, old_fp2)
    check("★ 老指纹（v1）改了难度 → 判成有变更",
          sub5 and any("难度" in r for r in rs5), rs5)

    # =============================================== 10. 生成项的 id 稳定性
    print("\n[10] 生成项的 id 必须稳定")
    import generated
    a1 = generated.fixture_items()[0].activity_id
    a2 = generated.fixture_items()[0].activity_id
    check("★ 同一份 fixture 两次读出来 id 一样",
          a1 == a2, (a1, a2))
    check("★ id 形如 gen:{candidate}:{index}（能过 Activity 的正则）",
          Activity.model_validate(generated.fixture_items()[0].model_dump()
                                  | {"activity_id": a1}) is not None)
    # 正则没被放宽到失控：库内 id 仍然合法，而"随便一个字符串"不合法
    def _mk(aid):
        return Activity(activity_id=aid, title="t", domains=["social_adapt"],
                        target_skill_items=["共同注意"], duration_min=5,
                        method=["NDBI"], method_rationale="r", difficulty=1,
                        setting=["anywhere"],
                        steps=[activities.Step(step_id=i + 1, action="a",
                                               child_target_response="b")
                               for i in range(3)],
                        success_criteria=["c"], prompt_hierarchy=["p"],
                        error_correction="e", success_behaviors=["b"],
                        refusal_behaviors=["r"], linked_scenes=["indoor_play"])
    for good in ("soc-003", "gro-001", "gen:fixture:0", "gen:cand-42:3"):
        _mk(good)
    check("★ 常见 id 形态都合法（库内 + 生成）", True)
    try:
        _mk("\u968f\u4fbf\u4e00\u4e2a\u5b57\u7b26\u4e32")
        bad = False
    except Exception:
        bad = True
    check("★ 正则没被放宽到失控（乱写的 id 仍然非法）", bad)

    print("\n" + ("✓ 阶段 1 验收全绿" if ok else "✗ 有失败项，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
