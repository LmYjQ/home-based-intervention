# -*- coding: utf-8 -*-
"""
Phase 0 验收
============

图级断言。跑法：

    cd asd_agent && ASD_DB_PATH=/tmp/graph_selfcheck.db \\
        ../.venv/bin/python -m graph.selfcheck

★ 写断言前先读一遍被断言的节点（PROGRESS.md 阶段 4 坑 2 / 阶段 4b 坑 1-2，
  两次都是"按直觉写断言，第一次跑就误报 3 条，挡住的全是正确行为"）。
  下面每条断言的依据写在注释里，凭据是节点源码或实测输出，
  不是"应该就是这样"。

★ 关于 interrupt 的验证方式
----------------------------
澄清分支在 `--no-llm` 下**永远进不去**（`validate_observation` 里
`degraded` 为真时恒判 false —— 降级时打断家长只会得到"我不知道"）。
所以要验 interrupt 往返，就得让图**带着一份已抽好的 observation 进去**。

那不是 mock，是真数据：`ObservationBatch` 的字段全部来自 `schema.py`
的真实约束，`model_dump` 出来就是 `ObservationOutcome.model_dump` 的形状。
这里手工构造它，是因为 Phase 0 不该依赖 LLM 存活才能验编排 ——
编排的断言必须能在**模型挂了**的情况下也跑出来，这正是它的价值所在。
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import observe                                          # noqa: E402
import redflag                                          # noqa: E402
import schema                                           # noqa: E402
import store                                            # noqa: E402
from langgraph.types import Command                     # noqa: E402

from .builder import build_graph                        # noqa: E402
from .checkpointer import saver_for, close_all          # noqa: E402
from .state import thread_config, fork_config           # noqa: E402

DB = os.getenv("ASD_DB_PATH", "/tmp/graph_selfcheck.db")
CID = "graph-check"


def _counts(conn, cid: str) -> dict:
    q = lambda t: conn.execute(          # noqa: E731
        f"SELECT count(*) AS n FROM {t} WHERE child_id=%s", (cid,)).fetchone()["n"]
    return {"observations": q("observations"),
            "sessions": q("sessions"),
            "skill_state": q("skill_state")}


def _seed_observation(*, needs_clarification: bool,
                      question: str, source: str) -> dict:
    """构造一份**真实 schema 约束下**的 observation dump（非降级）。"""
    ev = schema.Observation(
        scene="indoor_play", behavior="refusal", antecedent="unclear",
        consequence="none", intensity=2, duration_sec=None,
        people_present=["mother"], raw_quote="他今天不太配合",
        spans=[], confidence=0.5,
        needs_clarification=needs_clarification,
        clarifying_question=question if needs_clarification else None)
    out = observe.ObservationOutcome(
        source_text=source, date="", events=[ev],
        redflags=redflag.CLEAN,
        degraded=False)          # ★ 非降级 —— 这是澄清分支能进的前提
    return out.model_dump(mode="json")


def main() -> int:
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    store.reset(DB)
    conn = store.connect(DB)
    store.ensure_child(conn, CID)
    g = build_graph(saver_for(DB))

    # =============================================== 1. 干净输入直通
    print("\n[1] --no-llm：降级路径一路走到 publish")
    d1 = "2026-10-20"
    cfg1 = thread_config(CID, d1)
    text = "今天在客厅玩积木，他自己拿了两块叠起来，然后拿给我看。"
    out = g.invoke({"child_id": CID, "date": d1, "parent_text": text,
                    "no_llm": True}, cfg1)
    tr = out.get("trace", [])
    check("没停在 interrupt", g.get_state(cfg1).next == (),
          g.get_state(cfg1).next)
    check("走完 extract → safety → publish",
          tr == ["record_intake", "extract_observation", "validate_observation",
                 "safety_gate", "publish"], tr)
    check("降级时恒不打断（问也只会得到「我不知道」）",
          out.get("needs_clarification") is False)
    check("红旗干净", (out.get("verdict") or {}).get("highest") == "",
          (out.get("verdict") or {}).get("reasons"))
    check("★ 降级时一条 observation 都不编造",
          out.get("observation_ids") == [], out.get("observation_ids"))
    c1 = _counts(conn, CID)
    check("业务表零写入", c1 == {"observations": 0, "sessions": 0, "skill_state": 0}, c1)

    # =============================================== 2. interrupt 能停
    print("\n[2] ⚡ 澄清 interrupt 能停住")
    d2 = "2026-10-21"
    cfg2 = thread_config(CID, d2)
    obs2 = _seed_observation(needs_clarification=True,
                             question="是哪个环节不配合？当时在做什么？",
                             source="他今天不太配合")
    stop = g.invoke({"child_id": CID, "date": d2, "no_llm": True,
                     "parent_text": "他今天不太配合",
                     "observation": obs2, "observed_text": "他今天不太配合",
                     "clarifying_question": "是哪个环节不配合？当时在做什么？"},
                    cfg2)
    check("首次调用停在 interrupt（不返回）",
          g.get_state(cfg2).next == ("clarify",),
          f"next={g.get_state(cfg2).next}")
    snap2 = g.get_state(cfg2)
    check("★ 图确实停在 clarify 节点上", snap2.next == ("clarify",), snap2.next)
    check("interrupt 前节点都跑过了",
          any(t.startswith("extract_observation")
              for t in (snap2.values or {}).get("trace", [])),
          (snap2.values or {}).get("trace"))
    check("interrupt 后节点没跑（safety_gate 还没到）",
          not any(t.startswith("safety_gate")
                  for t in (snap2.values or {}).get("trace", [])),
          (snap2.values or {}).get("trace"))
    check("interrupt 前业务表仍是空的",
          _counts(conn, CID) == c1, _counts(conn, CID))

    # =============================================== 3. resume 接上
    print("\n[3] resume 接上，且节点被重跑")
    final = g.invoke(Command(resume={"answer": "其实是我让他先洗手，他就不玩了",
                                     "skip": False}), cfg2)
    tr2 = final.get("trace", [])
    print(f"     trace = {tr2}")
    check("resume 后走完了后续节点", tr2[-1] == "publish", tr2[-1:])
    check("澄清回答已并入语料（是追加，不是替换）",
          "不太配合" in final.get("clarified_text", "")
          and "先洗手" in final.get("clarified_text", ""),
          repr(final.get("clarified_text")))
    check("answer 落到 state", "先洗手" in final.get("clarification_answer", ""))
    check("澄清只发生一次（循环出口守卫生效，没有反复追问）",
          tr2.count("clarify") == 1, f"clarify × {tr2.count('clarify')}")

    # --- 重跑这件事本身，用一个专门的探针图验 ---
    check("★ interrupt 恢复会重跑所在节点（PG checkpointer 下实测）",
          _probe_rerun(), "见 _probe_rerun 的 docstring：这条是整个副作用约束的依据")

    # =============================================== 4. 重复 resume 不重复写库
    print("\n[4] ★ 副作用只发生一次")
    n_obs = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s",
        (CID,)).fetchone()["n"]
    check("★ 降级路径一条都没落库（publish 里 degraded 为真就不写）", n_obs == 0, n_obs)

    # =============================================== 5. time travel
    print("\n[5] time travel")
    snaps = list(g.get_state_history(cfg2))
    check("★ 能读出多个 checkpoint", len(snaps) >= 4, f"{len(snaps)} 个")
    check("snapshot 有 values / next / config / metadata",
          all(hasattr(s, "values") for s in snaps) and all(hasattr(s, "metadata") for s in snaps))
    last = g.get_state(cfg2)
    check("跑完后图已结束", last.next == (), last.next)
    check("metadata 带 step（Phase 5 audit 的现成来源）",
          last.metadata.get("step") is not None,
          {k: last.metadata.get(k) for k in ("step", "source")})
    mid = next((s for s in snaps if (s.values or {}).get("parent_text")), None)
    check("找得到可 fork 的中间态", mid is not None)

    # =============================================== 6. 分叉不污染
    print("\n[6] 分叉隔离（方案 §5.5 的技术前提）")
    hist_before = len(list(g.get_state_history(cfg2)))
    counts_before = _counts(conn, CID)
    if mid is not None:
        edited = {**(mid.values or {}),
                  "parent_text": "他今天不太配合（其实做了）"}
        fcfg = fork_config(CID, d2, "manual")
        forked = g.invoke(edited, fcfg)
        check("分叉跑通", "trace" in forked, forked.get("trace"))
        # ★ 分叉的语义是"从某个历史态另起一条"，
        #   所以它**继承**历史 state（含澄清回答），但写在**另一个 thread** 上。
        #   之前我断言"新 thread 不含澄清回答"，那是在断言一个从未被要求过的性质 ——
        #   而且它恰好把方案 §5.5 要的能力（把昨天的输入改掉再看今天怎么变）
        #   断言成了"不该继承"。
        check("分叉带进了编辑内容",
              "其实做了" in (forked.get("parent_text") or ""),
              forked.get("parent_text"))
        check("★ 分叉落在与原 thread 不同的 thread_id 上",
              fcfg["configurable"]["thread_id"]
              != cfg2["configurable"]["thread_id"],
              fcfg["configurable"]["thread_id"])

    check("★ 原 thread 的 checkpoint 数不变",
          len(list(g.get_state_history(cfg2))) == hist_before,
          f"{hist_before} → {len(list(g.get_state_history(cfg2)))}")
    check("★ 原 thread 的 state 不变（编辑没串过去）",
          "其实做了" not in json_str(g.get_state(cfg2).values))
    check("★ 业务表行数不变", _counts(conn, CID) == counts_before,
          f"{counts_before} → {_counts(conn, CID)}")

    # =============================================== 7. thread_id 不可复用
    print("\n[7] thread_id 不可复用（同名 run 的 checkpoint 会累积）")
    n_a = len(list(g.get_state_history(cfg2)))
    g.invoke({"child_id": CID, "date": d2, "no_llm": True,
              "parent_text": "又跑一次同样的日期"}, cfg2)
    n_b = len(list(g.get_state_history(cfg2)))
    check("★ 同名 thread 第二次跑会追加 checkpoint（所以生产上用日期做后缀）",
          n_b > n_a, f"{n_a} → {n_b}")

    # =============================================== 8. 安全闸门
    print("\n[8] 安全闸门（纯正则，零 token）")
    d8 = "2026-10-24"
    cfg8 = thread_config(CID, d8)
    # redflag.scan 的已知真阳性：verify_schema 的 C11
    out8 = g.invoke({"child_id": CID, "date": d8, "no_llm": True,
                     "parent_text": "他最近不太喜欢那个旋转的玩具，看到就走开，"
                                    "然后会去咬自己的袖子。"}, cfg8)
    v8 = out8.get("verdict") or {}
    check("C11 命中 urgent", v8.get("highest") == "urgent", v8.get("highest"))
    check("走 escalate 而非 publish",
          (out8.get("trace") or [])[-1] == "escalate", (out8.get("trace") or [])[-1:])
    check("★ escalate 暂停计划生成", (out8.get("escalation") or {}).get("paused") is True)
    check("escalate 给了转诊指引",
          bool((out8.get("escalation") or {}).get("guidance")))

    # =============================================== 9. 四层字段向后兼容
    print("\n[9] 四层字段：新字段不能把旧数据打死")
    check_layers_backcompat(check)

    # =============================================== 10. 降级时四层全空
    print("\n[10] 降级时四层全空（一条都不编）")
    check_layers_degraded(g, check)

    # =============================================== 11. publish 的幂等键是语料
    print("\n[11] ★ publish 幂等键是「本次语料」而不是「这一天」")
    check_publish_idempotency(g, conn, check)

    # =============================================== 12. 规则对齐
    print("\n[12] ★ 规则 1-6 仍与 verify_schema 逐字对齐")
    check_rules_alignment(check)

    # =============================================== 13. 草稿
    print("\n[13] 草稿：存 checkpoint，且一个业务表都不碰")
    check_draft(conn, check)

    close_all()
    print("\n" + ("✓ Phase 0/1 图级验收全绿" if ok else "✗ 有失败项，见上"))
    return 0 if ok else 1


# ================================================================== [13] 草稿
def check_draft(conn, check) -> None:
    """草稿走独立的 thread 命名空间，且**只写 checkpoint 表**。

    「只写 checkpoint」这件事不是靠自觉 —— 它靠的是草稿图从不 invoke：
    `update_state` 只落 checkpoint，没有任何节点被执行，
    所以「interrupt 恢复重跑节点」那条硬约束在这里**结构上不适用**。
    断言守的是「草稿不能因为将来有人改成 invoke 而开始写业务表」。
    """
    from . import draft
    from .runtime import get_draft_graph

    before = _counts(conn, CID)
    dg = get_draft_graph(DB)

    check("空草稿读出来是 None（不是空壳）",
          draft.load_draft(dg, child_id=CID, date="2026-10-28") is None)

    draft.save_draft(dg, child_id=CID, date="2026-10-28",
                     rows=[{"activity_id": "gro-007", "outcome": "",
                            "prompt_level": 0, "note": "试了两次"}],
                     other_notes="超市里他捂耳朵", saved_at="12:00:00")
    d = draft.load_draft(dg, child_id=CID, date="2026-10-28")
    check("半填的行能存能取（outcome 为空也存得下）",
          bool(d) and d["rows"][0]["outcome"] == "", d)
    check("自由文本存取一致", bool(d) and d["other_notes"] == "超市里他捂耳朵")
    check("★ 草稿 thread 与 run thread 是不同命名空间",
          draft.draft_thread_id(CID, "2026-10-28")
          != thread_config(CID, "2026-10-28")["configurable"]["thread_id"],
          draft.draft_thread_id(CID, "2026-10-28"))

    # 覆盖式：家长改的是同一份草稿，不是记历史
    draft.save_draft(dg, child_id=CID, date="2026-10-28",
                     rows=[], other_notes="改了", saved_at="12:01:00")
    d2 = draft.load_draft(dg, child_id=CID, date="2026-10-28")
    check("保存是覆盖而不是追加", d2["rows"] == [] and d2["other_notes"] == "改了")

    draft.clear_draft(dg.checkpointer, child_id=CID, date="2026-10-28")
    check("clear 后读回 None",
          draft.load_draft(dg, child_id=CID, date="2026-10-28") is None)
    check("★ 草稿全程一个业务表都没碰",
          _counts(conn, CID) == before, f"{before} → {_counts(conn, CID)}")


# ================================================================== [9] 向后兼容
#: 一条**Phase 1 之前**落库的 payload 字面量（没有 fact_fields）。
#: 留着它不是为了好看 —— 它是 server.py 教练诊断那条读路径的替身：
#: 那里是 `Observation.model_validate(json.loads(r["payload"]))`
#: 且外面包着裸 `except: continue`。字段一旦改成 required，
#: 每一条历史 observation 都会校验失败并被**静默跳过** —— 没有报错，
#: 只是家长和教练突然看不到几个月前的观察了。这条断言守的就是那个静默。
OLD_PAYLOAD = {
    "scene": "indoor_play", "behavior": "refusal", "antecedent": "unclear",
    "consequence": "none", "intensity": 2, "duration_sec": None,
    "people_present": ["mother"], "raw_quote": "他今天不太配合",
    "spans": [], "confidence": 0.5, "needs_clarification": False,
    "clarifying_question": None, "note": None,
}


def check_layers_backcompat(check) -> None:
    """★ 四个新字段全部可选 —— 逐个证明它们不会打死已有数据。"""
    ev = schema.Observation.model_validate(OLD_PAYLOAD)
    check("旧 payload（无 fact_fields）仍能校验", ev.behavior == "refusal")
    check("fact_fields 缺省为空而不是报错", ev.fact_fields == [], ev.fact_fields)

    b = observe.ObservationBatch.model_validate({"events": [OLD_PAYLOAD]})
    check("旧 batch（无后三层）仍能校验", len(b.events) == 1)
    check("后三层缺省为空",
          (b.parent_interpretation, b.hypotheses, b.missing_information)
          == (None, [], []),
          (b.parent_interpretation, b.hypotheses, b.missing_information))

    # graph/nodes.py 的 publish 那条重校验路径：dump → validate
    dumped = observe.ObservationOutcome(
        source_text="x", date="2026-10-20", events=[ev]).model_dump(mode="json")
    back = observe.ObservationOutcome.model_validate(dumped)
    check("旧 checkpoint 的 state 形状仍能重校验（publish 那条路径）",
          back.source_text == "x")


# ================================================================== [10] 降级
def check_layers_degraded(g, check) -> None:
    """降级时四层必须全空。**空不是「坏了」，是没得说。**

    这与 observe._fallback_batch「一条事件都不编」是同一条原则：
    编造的解读/假设会被家长当成对孩子行为的判断，而那正是这个系统
    最不该给的东西 —— 它给的是证据与追问，不是结论。
    """
    fb = observe._fallback_batch("他今天不太配合", "2026-10-20", redflag.CLEAN)
    check("降级时零事件", fb.events == [] and fb.degraded)
    check("★ 降级时后三层全空（不编解读、不编假设、不编缺失）",
          (fb.parent_interpretation, fb.hypotheses, fb.missing_information)
          == (None, [], []),
          (fb.parent_interpretation, fb.hypotheses, fb.missing_information))

    d = "2026-10-26"
    out = g.invoke({"child_id": CID, "date": d, "no_llm": True,
                    "parent_text": "今天没什么事"}, thread_config(CID, d))
    obs = out.get("observation") or {}
    check("图级降级跑完后四层仍全空",
          (obs.get("parent_interpretation"), obs.get("hypotheses"),
           obs.get("missing_information")) == (None, [], []),
          (obs.get("parent_interpretation"), obs.get("hypotheses"),
           obs.get("missing_information")))


# ================================================================== [11] 幂等键
def check_publish_idempotency(g, conn, check) -> None:
    """★ 这一组是 Phase 1 改 publish 的**全部理由**。

    原判据是 `_today_has_observations(child, date)` ——「这一天有没有任何一行」。
    Phase 0 成立是因为当时只有图一个写入口。图一接进 HTTP，
    `/api/feedbacks` → `apply_feedbacks` 同一天也会从**逐条活动的 note**
    写 observations 行，于是那个粗判据会把「其他观察」误判成
    「今天已落库」而静默跳过 —— 没有报错，家长那段话就是不落库。

    同一天出现两批来源不同的语料，是 Phase 1 之后**正常**的形态，
    所以幂等判据必须细到「这一段语料」。
    """
    d = "2026-10-27"
    date_s = d
    # 先手写一条「逐条活动 note」风格的行：同日期、不同 source。
    note_source = "玩木头人他不肯听，我说你停他就站在那儿不动"
    ev = schema.Observation.model_validate({**OLD_PAYLOAD, "raw_quote": "他不肯听"})
    note_out = observe.ObservationOutcome(source_text=note_source, date=date_s,
                                          events=[ev])
    note_ids = observe.persist(conn, CID, note_out)
    n_after_note = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s AND observed_at=%s",
        (CID, date_s)).fetchone()["n"]
    check("前置：同日期已有逐条 note 写的行", n_after_note == len(note_ids), n_after_note)

    other = "超市里他蹲下来捂住耳朵了，我没理他，过了一会儿自己好了。"
    obs_other = _seed_observation(needs_clarification=False, question="",
                                  source=other)
    cfg = thread_config(CID, d)
    out = g.invoke({"child_id": CID, "date": d, "no_llm": True,
                    "parent_text": other, "observation": obs_other,
                    "observed_text": other}, cfg)

    check("★ 没被同日期的 note 行误判成「已落库」（旧的日期级判据这里返回 []）",
          bool(out.get("observation_ids")), out.get("observation_ids"))

    srcs = [r["source"] for r in conn.execute(
        "SELECT DISTINCT source FROM observations WHERE child_id=%s AND observed_at=%s",
        (CID, date_s)).fetchall()]
    check("★ 两种来源的语料都在，互不吞",
          set(srcs) == {note_source, other}, srcs)

    n_before = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s AND observed_at=%s",
        (CID, date_s)).fetchone()["n"]

    # 同一段语料再跑一次 → 幂等，不重复写
    out2 = g.invoke({"child_id": CID, "date": d, "no_llm": True,
                     "parent_text": other, "observation": obs_other,
                     "observed_text": other}, thread_config(CID, d + "#again"))
    n_after = conn.execute(
        "SELECT count(*) AS n FROM observations WHERE child_id=%s AND observed_at=%s",
        (CID, date_s)).fetchone()["n"]
    check("同一段语料再跑一次不重复落库",
          n_after == n_before, f"{n_before} → {n_after}")
    check("★ 逐条 note 那行没被波及",
          conn.execute(
              "SELECT count(*) AS n FROM observations "
              "WHERE child_id=%s AND observed_at=%s AND source=%s",
              (CID, date_s, note_source)).fetchone()["n"] == len(note_ids))


# ================================================================== [12] 规则对齐
def check_rules_alignment(check) -> None:
    """★ `_RULES_1_6` 与 verify_schema 的提示词必须逐字一致。

    observe.py 的模块注释声明了这件事，但注释不是断言 ——
    一次「顺手整理提示词」就能把它悄悄作废，而后果是
    verify_schema 那份 12/12 的 A/B 结论**不再代表生产链路**。
    """
    import verify_schema
    prompt = verify_schema.build_prompt()
    check("★ observe._RULES_1_6 逐字出现在 verify_schema 的提示词里",
          observe._RULES_1_6 in prompt)
    check("★ 新规则没有混进 1-6（在 _RULES_LAYERS 里）",
          "fact_fields" not in observe._RULES_1_6
          and "fact_fields" in observe._RULES_LAYERS)
    check("两套 SYSTEM 都接上了新规则",
          "fact_fields" in observe.SYSTEM and "fact_fields" in observe.SYSTEM_SINGLE)


def _probe_rerun() -> bool:
    """★ 单独验「`interrupt()` 恢复时会重跑所在节点」。

    为什么不在真图上验：真图的生产节点**在 interrupt 之前不做任何副作用**
    （那正是本项目的硬约束），所以真图里根本没有可观测的重跑痕迹 ——
    这恰恰说明约束被遵守了，而不是说明重跑不存在。

    所以这里用一个**故意违规**的探针节点：它在 interrupt 之前原地改 state。
    若重跑发生，那一行会出现两次。

    ★★ 一个必须记住的实测结果（2026-10-20）
    --------------------------------------
    **这个现象只在 PostgresSaver 下出现，用 InMemorySaver 验不出来。**
    实测同一个探针：
        InMemorySaver → ['before_interrupt', 'after']              （重跑没留下痕迹）
        PostgresSaver → ['before_interrupt', 'before_interrupt', 'after']

    后果很具体：谁要是拿 InMemorySaver 写个单测，看到"只执行了一次"，
    就会推断"恢复不会重跑节点"，然后放心地在 interrupt 之前写库 ——
    然后在生产（用 PG）上双写。所以这个探针**必须用生产 saver**，
    且这条结论要跟着代码一起走，别只在测试里成立。
    """
    from typing import TypedDict
    from langgraph.graph import START, END, StateGraph
    from langgraph.types import interrupt

    class P(TypedDict, total=False):
        hits: list[str]

    def probe(state) -> dict:
        hits = state.get("hits", [])
        hits.append("before_interrupt")       # ← 故意的副作用，用来暴露重跑
        ans = interrupt("q")
        hits.append("after")
        return {"hits": hits}

    pg = StateGraph(P)
    pg.add_node("probe", probe)
    pg.add_edge(START, "probe")
    pg.add_edge("probe", END)
    gr = pg.compile(checkpointer=saver_for(DB))

    cfg = {"configurable": {"thread_id": "probe-rerun"}}
    gr.invoke({"hits": []}, cfg)
    out = gr.invoke(Command(resume="ok"), cfg)
    hits = out.get("hits", [])
    print(f"     探针 hits = {hits}")
    return hits.count("before_interrupt") == 2


def json_str(v) -> str:
    import json
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        return str(v)


if __name__ == "__main__":
    sys.exit(main())
