"""命令行闭环演示（计划 §8.4 "先做 CLI"）。

用法：

    # 真人对话，边打边回
    python -m rdi_agent.cli

    # 跑固定演示输入，不交互
    python -m rdi_agent.cli --demo

固定演示输入是计划里那条："孩子 4 岁，最近不愿意去幼儿园，早上出门就哭"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from rdi_agent.db.engine import dispose_engine
from rdi_agent.graph.runtime import setup_graph, shutdown_graph

# 固定演示输入：开场白 + **一轮**追问的回答。
#
# 为什么是一轮：只允许追问一轮（MAX_FOLLOWUP_ROUNDS = 1），
# 家长答完这一轮就直接出方案。所以演示只需要一条回答。
#
# 这条回答把 check_sufficiency 关心的四件事一次补齐：
# 场景（几点、在哪、持续多久）、孩子的具体行为、试过什么效果如何、家长的目标。
DEMO_ANSWER_1 = (
    "早上从家门口走到楼下大概五分钟，一直抱着我腿不说话。到幼儿园门口就大哭，"
    "抱起来能好。送进去以后老师说她今天自己吃了早饭。"
    "在家不这样，只有去幼儿园才哭。试过提前十分钟出门，也试过抱着她走过去，都没什么用。"
    "目标是能正常入园、别在门口哭半小时，晚十分钟到也接受。"
)

DEMO_MESSAGES = [
    "孩子 4 岁，最近不愿意去幼儿园，早上出门就哭。",
    DEMO_ANSWER_1,
]


def _show_plan(plan: dict[str, Any]) -> None:
    print(f"\n--- [{plan['plan_id']}] {plan['title']}"
          f"{'  ★推荐' if plan.get('recommended') else ''} ---")
    print(f"依据事实：{'; '.join(plan.get('observed_facts', [])) or '（无）'}")
    print(f"可能解释：{'; '.join(plan.get('tentative_interpretations', [])) or '（无）'}")
    print(f"说明：{plan.get('explanation', '')}")
    for action in plan.get("parent_actions", []):
        print(f"  · {action['action']}（情境：{action.get('context', '')}）")
        for step in action.get("steps", []):
            print(f"      - {step}")
        for obs in action.get("observe", []):
            print(f"      观察：{obs}")
        for stop in action.get("reduce_or_stop_if", []):
            print(f"      停止：{stop}")
    print(f"  观察指标：{'; '.join(plan.get('success_indicators', []))}")
    print(f"  停止条件：{'; '.join(plan.get('stop_conditions', []))}")
    if plan.get("uncertainty_notes"):
        print(f"  不确定：{'; '.join(plan['uncertainty_notes'])}")
    citations = plan.get("source_citations") or []
    if citations:
        print(f"  标签：{plan.get('application_label')}")
        for c in citations:
            pages = ""
            if c.get("pdf_page_start") is not None:
                pages = f"，PDF 第 {c['pdf_page_start']}-{c['pdf_page_end']} 页"
            print(f"    - {c.get('chapter') or c.get('title')}{pages}")
            print(f"      支撑：{c.get('claim_supported')}")
    else:
        print(f"  标签：{plan.get('application_label')}（无指南引用）")


def _show_evidence(items: list[dict[str, Any]]) -> None:
    """证据面板：这次依据的是指南里哪几段。"""
    if not items:
        print("\n（这次没有检索到可引用的指南内容）")
        return
    print(f"\n【指南依据 {len(items)} 条】")
    for i, item in enumerate(items, start=1):
        pages = ""
        if item.get("pdf_page_start") is not None:
            pages = f"，PDF 第 {item['pdf_page_start']}-{item['pdf_page_end']} 页"
        print(f"  {i}. {item.get('section_title') or item.get('title')}{pages}")
        print(f"     {item.get('summary')}")
        print(f"     来源：{item.get('source_type')}　审核：{item.get('review_status')}")


async def _demo() -> None:
    """跑固定输入，走完 追问 → 方案 → 选择 → 反馈。"""
    from rdi_agent.api import conversations as api
    from rdi_agent.schemas.feedback import FeedbackSubmission

    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label="4 岁男孩", age_band="3-4")
    )
    cid = conv["conversation_id"]
    print(f"会话：{cid}")

    turn = 0
    status, plans = "", []
    for msg in DEMO_MESSAGES:
        turn += 1
        res = await api.send_message(
            cid, api.SendMessageRequest(message=msg, turn_id=f"demo_t{turn}")
        )
        status = res["status"]
        print(f"\n【家长】{msg}")
        if status == "awaiting_followup":
            print(f"\n【追问 {len(res['questions'])} 个】")
            for q in res["questions"]:
                # questions 里是 dict（带选项），老数据可能是裸字符串。
                if isinstance(q, dict):
                    print(f"  ? {q.get('question', '')}")
                    for opt in q.get("options") or []:
                        print(f"      · {opt}")
                else:
                    print(f"  ? {q}")
            continue
        if status == "awaiting_plan_selection":
            plans = res["plans"]
            _show_evidence(res.get("evidence") or [])
            for p in plans:
                _show_plan(p)
            break
        print(f"\n【助手】{res['message']}")
        _show_evidence(res.get("evidence") or [])
        if res.get("evidence_gaps"):
            print(f"\n（证据缺口：{'; '.join(res['evidence_gaps'])}）")
        break

    if status != "awaiting_plan_selection":
        print(f"\n演示输入没走到方案选择就结束了（status={status}），这和预期不符。")
        print("如果是 limited_answer，说明证据门控没放行 —— 看上面的证据缺口。")
        return

    pick = next(p["plan_id"] for p in plans if p.get("recommended"))
    sel = await api.select_plan(cid, api.SelectPlanRequest(plan_id=pick))
    print(f"\n【已选】{sel['message']}（trial={sel['trial_id']}）")

    fb = await api.submit_feedback(
        sel["trial_id"],
        FeedbackSubmission(
            trial_id=sel["trial_id"],
            turn_id="demo_fb1",
            attempted=True,
            implementation_notes="试了三天，早上提前十分钟出门，不催他。",
            child_response={"behavior": "第三天自己走进去了", "affect": "还是有点闷", "words": ""},
            parent_observations=["哭的时间变短了", "进园后在门口回头看我一次"],
        ),
    )
    print(f"\n【反馈分析】{json.dumps(fb['analysis'], ensure_ascii=False, indent=2)}")
    print(f"\n【下一步】{fb['message']}")


async def _interactive() -> None:
    """真人对话。"""
    from rdi_agent.api import conversations as api

    conv = await api.create_conversation(
        api.CreateConversationRequest(display_label=input("孩子（昵称即可）: ").strip() or "孩子")
    )
    cid = conv["conversation_id"]
    print(f"会话：{cid}\n（Ctrl-C 结束；输入 :quit 退出）\n")

    turn = 0
    while True:
        try:
            msg = input("【家长】").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not msg or msg == ":quit":
            return

        turn += 1
        res = await api.send_message(
            cid, api.SendMessageRequest(message=msg, turn_id=f"cli_t{turn}")
        )
        if res["status"] == "awaiting_followup":
            for q in res["questions"]:
                # questions 里是 dict（带选项），老数据可能是裸字符串。
                if isinstance(q, dict):
                    print(f"  ? {q.get('question', '')}")
                    for opt in q.get("options") or []:
                        print(f"      · {opt}")
                else:
                    print(f"  ? {q}")
            continue
        if res["status"] == "awaiting_plan_selection":
            _show_evidence(res.get("evidence") or [])
            for p in res["plans"]:
                _show_plan(p)
            try:
                pick = input("\n选哪一张（输入 plan_id）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            sel = await api.select_plan(
                cid, api.SelectPlanRequest(plan_id=pick, turn_id=f"cli_t{turn}_sel")
            )
            print(f"\n【助手】{sel['message']}")
            print(f"trial_id = {sel['trial_id']}")
            print("（下一轮 :feedback 会让你录入这次尝试的结果）")
            continue
        print(f"\n【助手】{res['message']}")
        _show_evidence(res.get("evidence") or [])


async def _feedback() -> None:
    """反馈录入。"""
    from rdi_agent.api import conversations as api
    from rdi_agent.schemas.feedback import ChildResponse, FeedbackSubmission

    trial_id = input("trial_id: ").strip()
    print("\n可以空着不填。")
    attempted = input("有没有尝试过？y/n: ").strip().lower().startswith("y")
    notes = input("实际怎么做的: ").strip()
    behavior = input("孩子的行为: ").strip()
    affect = input("孩子的情绪: ").strip()
    words = input("孩子说的话: ").strip()
    observations = [x for x in input("你观察到的变化（逗号分隔）: ").split("，") if x.strip()]
    difficulties = [x for x in input("执行中卡住的地方（逗号分隔）: ").split("，") if x.strip()]

    res = await api.submit_feedback(
        trial_id,
        FeedbackSubmission(
            trial_id=trial_id,
            turn_id=f"fb_{trial_id}",
            attempted=attempted,
            implementation_notes=notes,
            child_response=ChildResponse(behavior=behavior, affect=affect, words=words),
            parent_observations=observations,
            difficulties=difficulties,
        ),
    )
    print(f"\n【分析】{json.dumps(res['analysis'], ensure_ascii=False, indent=2)}")
    print(f"\n【下一步】{res['message']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="RDI 家长助手 CLI")
    parser.add_argument("--demo", action="store_true", help="跑固定演示输入")
    parser.add_argument("--feedback", action="store_true", help="录入一次尝试的反馈")
    args = parser.parse_args()

    await setup_graph()
    try:
        if args.feedback:
            await _feedback()
        elif args.demo:
            await _demo()
        else:
            await _interactive()
    finally:
        await shutdown_graph()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())