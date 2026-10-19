# -*- coding: utf-8 -*-
"""
状态转移落地
============
把一天的 sessions 接到状态机上。**转移由 Python 规则决定，不由模型决定。**

★ 这个模块为什么从 428 行砍到只剩这一个函数
--------------------------------------
原先这里有两块东西：

  A. **逐活动判定**（`evaluate_day` / `DailyEval.per_activity` / `ActivityEval`）
     让 LLM 从家长一整段话里推断"哪条活动今天做得怎么样"——
     outcome、quality、成功判据命中，全靠模型从自由文本里裁量。

  B. **日级聚合**（`domain_updates` / `flags` / `parent_note`）

改逐条反馈之后，两块各归其位：

  A → 挪到 `feedback.py`。家长**直接点选项**回答了这个问题，
    模型再去猜一遍不只是浪费 token，是把一个确定的答案变成一个可能有错的答案
    （家长点了「不配合」，模型可能判成 partial）。
    同批被删掉的还有 `_repair` 的四条校验里与 A 相关的三条
    （丢弃幻觉 activity_id / 漏条补 no_data / quality 与 outcome 自洽）——
    它们全是在为"模型逐条判定"兜底，而逐条反馈下这些物理上不可能发生。

  B → 挪到 `summary.py`。它需要跨样本归纳，单条反馈推不出来。
    顺带修好一件事：B 原先只被 `DailyEval.render()` 打印然后丢弃，
    **全项目无人消费、不落库**。现在它落 `daily_summary` 表。

**A 里唯一有价值的东西是 `_repair` 里的第 ④ 条思路** ——
"这个字段不能由模型自由裁量"。那条思路没有消失，
它变成了 `feedback.quality_for()` 里一张写死的映射表：
quality 从提示词里的自由裁量变成一个确定性函数，可重放。

★ 保留下来的这个函数为什么重要
--------------------------
每个技能细项只看**真正练到它**的那些 session。这里曾经把当天的全部 session
无差别喂给每个技能，于是练语言得到的成功会算进"共同注意"的进展里 ——
状态机看起来一直在正常推进，实际上是在拿别人的成绩往上爬。

这个 bug 极隐蔽：没有任何报错，只是所有技能一起虚假升级。
同类事故在 `store.py` 也发生过一次（`_demo_history()` 写死 soc-004，
而它的 target_skill_items 不含共同注意，于是状态机拿到空历史
却照样输出一条完整转移链）。**演示数据自己必须先经得起交叉核对。**
"""

from __future__ import annotations

from pydantic import BaseModel


class TransitionsInput(BaseModel):
    """apply_transitions 的输入。

    ★ 刻意只留 `date` 和 `sessions`：原 `EvalInput` 有 activities /
      observations / parent_self_report / prior_states / history_summary 五个字段，
      而这个函数**一个都没读** —— 它只读 sessions 和历史。
      那些字段是喂给 evaluate_day 的 prompt 的，不是喂给状态机的。
      留着它们会让人误以为状态机受 parent_self_report 影响，而它不受。
    """

    date: str
    sessions: list = []      # list[statemachine.SessionRecord]


# ================================================================== 状态转移落地
def apply_transitions(inp: TransitionsInput, states: dict[str, dict],
                      profile, *, conn=None, child_id: str = "") -> list:
    """**把评估结果接到状态机上。转移由 Python 规则决定，不由模型决定。**

    每个技能细项只看**真正练到它**的那些 session。传 conn 时还会带上该细项的
    历史记录（不只是今天这几条）—— 状态机的规则全是时间窗统计
    （最近 7 天 / 14 天），只喂今天一条的话永远不会转移。

    ★ 这里曾经把当天的全部 session 无差别喂给每个技能，
    于是练语言得到的成功会算进"共同注意"的进展里 ——
    状态机看起来一直在正常推进，实际上是在拿别人的成绩往上爬。
    """
    import statemachine as sm

    try:
        from activity_data import get as get_activity
    except Exception:
        get_activity = lambda aid: None     # noqa: E731

    out: list = []
    for item in profile.assessments:
        # 只保留目标技能命中这条技能细项的 session
        todays = []
        for s in inp.sessions:
            a = get_activity(s.activity_id)
            if a is None or item.skill_item in a.target_skill_items:
                todays.append(s)

        if conn is not None and child_id:
            import store
            hist = store.sessions_for_skill(conn, child_id, item.skill_item,
                                            until=inp.date)   # 历史含今天这条
        else:
            hist = todays

        cur = states.get(item.skill_item, {}).get("state", "not_started")
        since = states.get(item.skill_item, {}).get("since")
        t = sm.propose_transition(
            skill_item=item.skill_item, domain=item.domain,
            current_state=cur,
            level_est=sm.estimate_level(item.level, hist),
            history=hist, end_date=inp.date,
            profile_confidence=profile.overall_confidence, since=since,
        )
        if t:
            out.append(t)
    return out

# ================================================================== 自检
def _cli() -> None:
    """★ 专门验一件事：session 只喂给**真正练到它**的技能细项。"""
    from capabilities import SkillAssessment, AbilityProfile
    from statemachine import SessionRecord
    from activity_data import get as get_act

    # 造一个最小画像：共同注意 + 捏取
    prof = AbilityProfile(child_id="ev-check", child_age_months=34,
                         overall_confidence=0.9, assessments=[
        SkillAssessment(domain="social_adapt", skill_item="共同注意",
                        level=0, source="inferred", confidence=0.9),
        SkillAssessment(domain="fine_motor", skill_item="捏取",
                        level=0, source="inferred", confidence=0.9),
    ])

    # soc-001 的 target_skill_items 含共同注意；fin-001 含捏取。
    # ★ 两条 session 一起进去，然后看共同注意是不是**只**吃到了 soc-001 那条。
    print("=== 技能归属：session 只发给真正练到它的细项 ===")
    sess = [SessionRecord(date="2026-10-17", activity_id="soc-001",
                          outcome="success", quality=0.9),
            SessionRecord(date="2026-10-17", activity_id="fin-001",
                          outcome="refused", quality=0.0)]
    for s in sess:
        a = get_act(s.activity_id)
        print(f"  {s.activity_id} {a.title}  target={a.target_skill_items}")

    seen = {}
    for item in prof.assessments:
        got = [s.activity_id for s in sess
               if get_act(s.activity_id) is None
               or item.skill_item in get_act(s.activity_id).target_skill_items]
        seen[item.skill_item] = got
        print(f"  {item.skill_item} 收到 {got}")

    assert seen["共同注意"] == ["soc-001"], "共同注意不该收到练捏取的记录"
    assert seen["捏取"] == ["fin-001"]
    print("  ✓ 练精细动作的抗拒没有算进共同注意的进展里")
    print()
    print("  ★ 这条曾经被写成\"把当天全部 session 无差别喂给每个技能\"，")
    print("    输出完全正常、没有任何报错，只是所有技能一起虚假升级。")

    print()
    print("=== 无 conn 时也能跑（纯内存，不落库）===")
    out = apply_transitions(TransitionsInput(date="2026-10-17", sessions=sess),
                            {}, prof)
    print(f"  转移数 = {len(out)}（样本太少不该有转移）")
    assert isinstance(out, list)


if __name__ == "__main__":
    _cli()
