# -*- coding: utf-8 -*-
"""
计划链节点体
============

    select_goal → retrieve_activities → generate_plan → validate_plan
                     → substantive_change? ─是→ ⚡ approve_plan → freeze_plan
                     → freeze_plan → publish_plan → END

★ 为什么计划链是**另一张图**，不是观察图后面接一段
--------------------------------------------------
技术方案 §5.1 把它画在同一张图里（安全检查之后接着排计划），
Phase 3 落地时改成独立一张，理由是三个具体的，不是审美：

1. **触发时机不同。** 观察链由「家长写了一段话」触发；
   计划链由「家长打开今天 / 重新生成」触发。把它们焊在一张图上，
   意味着家长写一句话就必然重排一次计划 —— 而家长写"今天他在超市
   捂耳朵"时，他要的不是重排，是被记下来。
2. **thread_id 会打架。** 两张图共用 `{child}:{date}` 的话，
   观察链的 checkpoint 和计划链的会互相续跑（实测 ③ 的教训：
   同 thread 的 invoke 会接着上一次跑）。计划链用 `plan:` 前缀，
   与 `draft:` 一样是**命名空间隔离，不是 if 判断**。
3. **写库节点会变成两个。** 观察图的 `publish` 写 observations + red_flags，
   计划链的 `publish_plan` 写 plans。两条链各有各的末尾写库节点，
   各自满足「所有 interrupt 之后才落库」——
   而焊在一张图上就得让 `publish` 同时写三张表，
   或者让 `freeze_plan` 自己写 `plans`（那正是 LANGGRAPH.md
   明令禁止的：第二个写库节点意味着 interrupt 恢复重跑时会双写）。

★ 「暂停计划生成」的口径只有一个
--------------------------------
`generate_plan` 调 `store.plan_paused(conn, child_id)`，
**不自己再发明一个判据**。理由见 LANGGRAPH.md 第四节之三：
那个函数是 Phase 2 定的唯一口径（存在 urgent 且未关闭的红旗，按孩子不按天），
这里要加过期策略就改那一条 SQL。
这条依赖是**注入的**（`state["paused"]` 由图入口的调用方算好传进来），
节点体自己不连库 —— 见下面 `generate_plan` 的注释。
"""

from __future__ import annotations

import os
import sys
from typing import Any

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from activities import (                                          # noqa: E402
    Activity,
    PlanRequest,
    ScoredActivity,
    request_from_dict,
    retrieve,
)
import plan_item                                                  # noqa: E402
from planner import (                                              # noqa: E402
    is_substantive_change,
    pack_day,
    plan_fingerprint,
)
from langgraph.types import interrupt                              # noqa: E402

from .state import PlanReview                                      # noqa: E402

#: 家长对"今天做哪几条"能说的话。**就这三个**——
#: 多一个"alternative"，前端就要多一套按钮，而后端拿它做的事完全一样。
PLAN_ACTIONS = ("approve", "edit", "reject")


def _mark(state, name: str) -> list[str]:
    return list(state.get("trace") or []) + [name]


def _req(state) -> PlanRequest:
    """从 state 取回 `PlanRequest`。

    ★ state 里存的是**纯 dict**（`activities.request_to_dict`），
      这里现还原 —— 与 `graph/state.py` 里 observation 存 dict 同一理由：
      checkpointer 用 msgpack 序列化，dataclass / Pydantic 对象属于
      "未注册类型"，实测已经打出 `will be blocked in a future version` 的警告，
      而 checkpoint 的寿命比代码长。今天能反序列化不代表明年能。
    """
    return request_from_dict(state.get("plan_request") or {})


def _acts_by_id(state) -> dict[str, Activity]:
    """state 里存的活动 id → `Activity` 对象。

    ★ 为什么 state 存的是 `id` 而不是对象：
      checkpoint 的寿命比代码长，而活动库会改（加活动、改难度）。
      存 id 意味着老 checkpoint 永远读得出来；存对象的话，
      活动库一改，历史 state 里的 Pydantic 对象就成了当时的快照，
      与现在的活动定义悄悄分叉 —— 而"当时那条难度是 2"没人会去查。

    ★ ★ 现在**多了一路来源**：装配进来的生成项不在活动库里，
      反查不到 —— 曾经的 `if i in acts` 会把它们**静默丢掉**，
      于是生成项的指纹永远不参与审批判据（换什么生成方案都不打断家长），
      而且**不报错**。所以这里要把 req.extra_items 一起并进来。
    """
    from activity_data import load_activities
    ids = set(state.get("plan_activity_ids") or [])
    acts = {a.activity_id: a for a in load_activities() if a.activity_id in ids}
    for it in (_req(state).extra_items or []):
        if it.activity_id in ids:
            acts[it.activity_id] = it
    return acts


# ================================================================== 1. 选目标
def select_goal(state) -> dict:
    """从状态机与画像里挑今天优先练什么。**纯读，不调模型。**

    ★ 为什么这个节点"什么都不决定"：
      候选活动的排序已经由 `activities.retrieve` 做完（0.30 状态匹配
      + 0.15 技能差距 + …），再在这里挑一遍就是**第二套排序口径**。
      它只做一件事：把"今天优先哪个能区"这个**给人看的说法**算出来，
      以及记下候选表 —— 排序的唯一权威始终是 retrieve。
      这个说法会进审批 interrupt 的文案，家长据此判断要不要改。
    """
    req = _req(state)
    focus_domain, focus_reason = _priority_domain(req)
    return {
        "focus_domain": focus_domain,
        "focus_reason": focus_reason,
        "trace": _mark(state, "select_goal"),
    }


def _priority_domain(req: PlanRequest) -> tuple[str, str]:
    """优先能区 = 家长关心 且 当前状态最靠后的那个。**纯函数，可单测。**

    判据只有"状态顺序"，没有第二样：家长标了优先的能区先看，
    在这些能区里挑状态**最靠后**的那个（最需要练）。

    ★ 顺序从 `vocab.ABILITY_STATE` **推导**，不另写一张表：
      `loop.py:80` 已经这么做过一次（`_STATE_ORDER`），
      这里如果再写一份字面量，两处将来会漂移 ——
      而漂移的后果是"状态机认为最靠后"与"计划认为最靠后"不一致，
      界面上不会报错，只会推出一条看起来莫名其妙的重点。
    """
    from vocab import (ABILITY_DOMAIN, ABILITY_STATE, ABILITY_STATE_CN)
    if not req.priority_domains:
        return "", ""
    order = {s: i for i, s in enumerate(ABILITY_STATE)}
    d = min(req.priority_domains,
            key=lambda x: (-order.get(req.state_of(x), 0), x))
    st = req.state_of(d)
    return d, (f"{ABILITY_DOMAIN.get(d, d)}目前是「{ABILITY_STATE_CN.get(st, st)}」，"
               f"是家长标了优先、但状态最靠后的能区")


# ================================================================== 2. 检索候选
def retrieve_activities(state) -> dict:
    """调 `activities.retrieve`（纯 Python，毫秒级，零 token）。

    ★ 走全量 `retrieve` 而不是 `candidates`：前者带 `hard_filter_reason`，
      审批文案里要能说清"今天为什么没有推那条"。

    ★ ★ 候选池 = 活动库 **+ 装配进来的生成项**（`activities.build_pool`）。
      这里是**唯一**一处装配点：生成项与库内项走同一条硬过滤、同一套权重，
      所以它出现在 `excluded` 里时带着**和库内项一模一样的原因**
      （"家里没有必需材料：…"、"当前水平不适合难度3"）。
    """
    req = _req(state)
    scored: list[ScoredActivity] = retrieve(req)
    passed = [s for s in scored if s.passed]
    excluded = [{"activity_id": s.activity.activity_id, "reason": s.hard_filter_reason}
                for s in scored if not s.passed]
    return {
        "candidate_ids": [s.activity.activity_id for s in passed[:12]],
        "excluded": excluded,
        "trace": _mark(state, "retrieve_activities"),
    }


# ================================================================== 3. 生成草稿
def generate_plan(state) -> dict:
    """装箱出今天的计划（`planner.pack_day`，纯 Python、零 LLM、可复现）。

    ★ **暂停判据不在本节点里算**，由 `state["paused"]` 带入
      （图入口用 `store.plan_paused()` 算好，见 plan_graph.py）。
      这样做的理由不是省一次查询，是让「暂停」这件事**只有一个口径**——
      LANGGRAPH.md 第四节之三定的就是 `store.plan_paused`，而节点体里
      再写一遍 `exists urgent ...` 就是第二处定义，两处迟早漂移。

    ★ `pack_day` 而不是 `plan_week`：周计划那条 LLM 链路在 HTTP 里
      从来没被调用过（死代码，见 LANGGRAPH.md 四之四），
      而每天的安排是**可判定的**（时间预算 + 技能细项不重复 + 活动不重复），
      让 7B 去"编排"只会引入不可复现的抖动 ——
      家长明天打开页面看到不同的推荐顺序，是这个产品最不能出的错。
    """
    if state.get("paused"):
        # ★ 暂停时**不生成草稿**，也不走审批。给一份"看起来正常"的计划，
        #   等于让红旗的暂停提示变成一句空话（Phase 2 的 fail-closed 被架空）。
        return {
            "plan_activity_ids": [],
            "plan_total_minutes": 0,
            "draft_fingerprint": {},
            "plan_paused": True,
            "trace": _mark(state, "generate_plan(paused)"),
        }

    req = _req(state)
    from activities import candidates as _cands
    # ★ 候选表来自**同一个池**（库 + 生成项），由 `activities.build_pool` 装配。
    #   这里如果自己再拼一次池，就会出现 REFACTOR.md §1.3.4 记的那个不一致：
    #   `excluded` 里说明了某条生成项被剔除，装箱时它却不见了。
    passed = _cands(req, top_k=12)
    picks = pack_day(passed, req.daily_minutes)
    # ★ ★ 指纹的输入**直接来自 picks 里的对象**，
    #   不再走"按 id 回活动库反查"。
    #   反查对生成项**查不到** → `if i in acts` 把它**静默丢掉** →
    #   生成项的指纹永远不参与审批判据（换什么生成方案都不打断家长），
    #   而且**不报错**。直接从对象拿就没有这个洞。
    return {
        "plan_activity_ids": [p.activity.activity_id for p in picks],
        # ★ 每项存完整的 dict（含 kind / origin / steps / materials）。
        #   生成项不在活动库里，只存 id 的话下游就再也拿不回它的内容了。
        "plan_items": [plan_item.to_item(p.activity) for p in picks],
        "plan_total_minutes": sum(p.activity.total_min + p.activity.prep_min
                                  for p in picks),
        "draft_fingerprint": plan_fingerprint([p.activity for p in picks]),
        "plan_paused": False,
        "trace": _mark(state, "generate_plan"),
    }


# ================================================================== 4. 比对
def validate_plan(state) -> dict:
    """★ 纯 Python：这份草稿与当前生效的那一版有没有实质区别。

    比的是 `planner.is_substantive_change`（指纹级判据），
    当前的指纹来自 `state["current_fingerprint"]` ——
    由图入口从 `plans` 表读出来算的，**这里不查库**。
    理由与 `generate_plan` 里 `paused` 那条一样：
    口径写在纯函数里，节点只搬结果。

    ★ 顺带校验草稿本身合法（预算 / 不重复 / 技能细项不重复）。
      这些是 `pack_day` 保证的，但审批前再确认一次 ——
      "审批通过"这一步之后计划就冻结了，而家长会照着它做。
      `assert` 那种写法在这里不合适：审批是 HTTP 路径，
      崩掉的话前端只看到一个 500，而家长不知道自己批的是一份非法计划。
    """
    if state.get("plan_paused"):
        return {"needs_review": False, "changes": [],
                "trace": _mark(state, "validate_plan(skipped)")}

    req = _req(state)
    ids = list(state.get("plan_activity_ids") or [])
    # ★ 校验的是**当天排出来的那几项**（`plan_items`，含生成项），
    #   不是"再检索一次筛出 id 相同的" ——
    #   后者对生成项查不到（它不在活动库里），会让生成项被当成"没排进计划"。
    picks = plan_item.items_to_activities(state.get("plan_items") or [])

    problems: list[str] = []
    if len({a.activity_id for a in picks}) != len(ids):
        problems.append("草稿里有重复的活动")
    skills = [it for a in picks for it in a.target_skill_items]
    if len(skills) != len(set(skills)):
        problems.append("同一天重复练同一个技能细项")
    spent = sum(a.total_min + a.prep_min for a in picks)
    if spent > req.daily_minutes:
        problems.append(f"总时长 {spent} 分钟超过家长给的 {req.daily_minutes} 分钟")

    substantive, reasons = is_substantive_change(
        state.get("draft_fingerprint") or {}, state.get("current_fingerprint"))
    return {
        "needs_review": bool(substantive or problems),
        "changes": reasons + problems,      # 校验不过 = 必须让人看见
        "trace": _mark(state, "validate_plan"),
    }


# ================================================================== 5. ⚡ 审批
def approve_plan(state) -> dict:
    """⚡ **审批 interrupt** —— 家长在这里第一次拿到"改"的话语权。

    ★ 为什么这个节点在 `interrupt()` 之前什么副作用都没有：
      它确实没有。恢复时整个节点会重跑（实测 LANGGRAPH.md ⑤），
      而它重跑出来的结果与第一次完全一样（纯读 state）。
      对比 `publish_plan` —— 它排在 interrupt **之后**，所以它写库是安全的。

    ★ 载荷里带的是**理由**，不只是"同意吗"：
      `changes` 是 `validate_plan` 算出来的人话，
      家长要知道"到底哪里变了"才能判断 —— 一个光秃秃的
      「同意 / 不同意」按钮，等于让人盲签。
    """
    review: PlanReview = interrupt(
        {"type": "plan_review",
         "child_id": state["child_id"],
         "date": state["date"],
         "activities": list(state.get("plan_activity_ids") or []),
         "total_minutes": state.get("plan_total_minutes", 0),
         "focus_domain": state.get("focus_domain", ""),
         "focus_reason": state.get("focus_reason", ""),
         "changes": list(state.get("changes") or []),
         "allowed_actions": list(PLAN_ACTIONS)},
        response_schema=PlanReview,
    )
    if isinstance(review, dict):
        review = PlanReview.model_validate(review)
    return {
        "review": review.model_dump(),
        # 循环出口守卫的计数。写在**节点**里而不是条件边里：
        # 条件边拿到的是 state 的只读视图，它算出来的新值不会写回，
        # 于是下一轮读到的还是旧计数 —— 守卫形同虚设，而家长点一次
        # "改一下"就能让图在 approve <-> retrieve 之间一直转圈。
        # 与观察链的 clarify_asked 同一个守卫、同一个理由。
        "edit_loop": int(state.get("edit_loop", 0))
                      + (1 if review.action == "edit" else 0),
        # ★ 家长已经在这个 thread 上表过态了。edit 之后重新生成的那一版
        #   不再弹第二次审批（见 plan_graph._route_needs_review 的注释）。
        #   **只由 approve_plan 自己写** —— 条件边拿到的是只读视图，
        #   在那里赋值看着生效、下一轮读到的还是旧值。
        "already_reviewed": True,
        "trace": _mark(state, "approve_plan"),
    }


# ================================================================== 6. 冻结
def freeze_plan(state) -> dict:
    """把审批结果折算成"这一版叫什么"。**纯计算，一个字节都不写。**

    ★ 为什么 `freeze_plan` 绝对不许自己写 `plans` 表：
      LANGGRAPH.md 第四节之三那条硬约束 —— 一旦有第二个写库节点，
      「interrupt 恢复会重跑所在节点」就会在某天咬人
      （实测：恢复后 `before_interrupt` 出现 2 次）。
      写库只在 `publish_plan`，而它在所有 interrupt 之后。

    ★ `approve` 与 `active` 是两个状态，不是一个：
      批准 ≠ 生效。`rejected` 的那一份**不落 active**，
      于是 `active_plan()` 查到的仍然是家长上一次认可的那版 ——
      这正是"拒绝之后页面不能还在执行被拒的那份"的落点。
    """
    rv: dict[str, Any] = state.get("review") or {}
    action = rv.get("action", "approve") if rv else "approve"
    status = {"approve": "approved", "reject": "rejected"}.get(action, "approved")
    return {
        "plan_status": status,
        "plan_action": action,
        "plan_note": rv.get("note", ""),
        "trace": _mark(state, "freeze_plan"),
    }


# ================================================================== 7. 落库
def publish_plan(state) -> dict:
    """★ 计划链**唯一**写业务表的地方，且排在 ⚡ 之后。

    写一行 `plans`：payload 是活动 id 列表 + 分钟数 + 指纹。
    指纹存进去是为了下一次审批能直接读回来比 ——
    重新从活动库算的话，"活动库改了一条活动的难度"会让
    半年前的计划凭空变成"有变更"，而那一次变更根本没人提过。
    """
    import store

    child_id, day = state["child_id"], state["date"]
    status = state.get("plan_status", "approved")
    payload = {
        "date": day,
        "profile_id": state.get("profile_id"),
        "activity_ids": list(state.get("plan_activity_ids") or []),
        # ★ ★ 每项的**完整内容**（含 kind / origin / steps / materials）。
        #   存这一份是因为生成项不在活动库里 —— 只存 id，
        #   下次读这份计划（出卡片 / 收反馈 / 算技能归属）就找不到它了。
        #   老行没有这个键 → 读时按 activity_id 从活动库还原（见 store.plan_items）。
        "items": list(state.get("plan_items") or []),
        "total_minutes": state.get("plan_total_minutes", 0),
        "budget_minutes": _req(state).daily_minutes,
        "fingerprint": state.get("draft_fingerprint") or {},
        "focus_domain": state.get("focus_domain", ""),
        "focus_reason": state.get("focus_reason", ""),
        "paused": bool(state.get("plan_paused")),
    }
    # ★ 暂停的那一份**不落成 active**。
    #   之前没区分，结果是 plans 里躺着一行 status='active' 且 activity_ids=[]，
    #   `/api/card` 读到它会把"今天什么都不做"当成一份合法计划端出去 ——
    #   而家长看到的是一个空白页，没有任何提示说"因为红旗停着"。
    #   pause 是 store.py 里已经定义好的状态（第四节点移表时定下的），
    #   这里只是第一次真正用上它。
    status = "paused" if state.get("plan_paused") else state.get("plan_status", "approved")

    conn = store.connect()
    version = store.next_plan_version(conn, child_id, day)
    pid = store.save_plan(conn, child_id, day, payload, status=status, version=version,
                          approved_by="parent" if status == "approved" else "",
                          approved_at="" if status == "approved" else "")
    if status == "approved":
        # ★ 批准之后立刻 activate：让上一版降级为 completed，
        #   使"这一天只有一个生效版本"在结构上成立（见 activate_plan 的注释）。
        store.activate_plan(conn, pid)
    return {
        "plan_id": pid,
        "plan_version": version,
        "trace": _mark(state, "publish_plan"),
    }
