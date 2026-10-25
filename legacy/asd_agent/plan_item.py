# -*- coding: utf-8 -*-
"""
方案项（plan item）的统一抽象
==============================
**这一份方案里的一项** —— 可能是活动库里现成的，也可能是模型生成的新做法。

★ 放宽的是「ID 必须来自活动库」这条约束，**不是「Activity 这个形状」**
-----------------------------------------------------------------------
下游（planner / coach / evaluator / 卡片 / 反馈）读的都是 `Activity` 的**字段**，
不是它的**来源**。所以：
  · 改形状 = 改六个下游
  · 改来源 = 只改这一处 + 一个转换函数
于是"统一形状"就是 `Activity` 本身，本模块只提供三件事：
  1. **来源的标记**（`kind` / `origin`）与互转
  2. **纯 dict 互转**（state / checkpoint 里只存 dict，见边界 #9）
  3. **语义签名**（指纹的比较单位）

★ 为什么语义签名里不含 activity_id
-----------------------------------
生成项的 ID 每次重新生成都不同（`gen:{candidate_id}:{index}` 里的 candidate_id
是新的一版）。指纹里含 ID 的话，"换了 ID 但做的事完全一样"会被判成实质变更
—— 于是每次重新生成都打断家长，**"只在真变化时审批"这条判据被它自己吃掉**
（LANGGRAPH.md 第四节之四记着这个坑的成因）。

★ "换 ID 不打断"与"换了内容要打断"怎么同时成立
-------------------------------------------------
签名收的是**会改变孩子要做的事**的那些字段：练什么 / 用什么 / 难度 /
从第几级提示 / 做多久 / **具体怎么做**。
  · 同一件事换了 ID → 签名相同 → 不打断 ✓
  · 步骤/材料/难度变了 → 签名不同 → 打断 ✓
最后一条（steps）是本阶段新增的：生成项之间最容易差的就是步骤，
而步骤变了，家长要做的动作就真的变了。
"""

from __future__ import annotations

from typing import Any, Literal

from activities import Activity

#: 方案项的两种来源。**刻意是一份可枚举的元组**：
#: 它同时给 `Activity.kind` 用、给分支判断用、给前端的角标用 ——
#: 三处各写一遍字面量，迟早有一处落后，而落后的那一处不报错。
KIND_LIBRARY = "library"
KIND_GENERATED = "generated"

ItemKind = Literal["library", "generated"]


def is_generated(activity_id: str) -> bool:
    """这个 id 是不是生成项的 id（`gen:` 前缀）。

    ★ 判定看**前缀**而不是查表 —— 生成项不在活动库里，
      而"在不在库里"这件事在某些时刻是反的（活动库被换掉），
      前缀是那件事的**来源**，比"能不能查到"更稳。
    """
    return activity_id.startswith("gen:")


def make_generated_id(candidate_id: str, index: int) -> str:
    """生成项的 id：`gen:{candidate_id}:{index}`。

    ★ 必须**稳定**：同一份候选方案的同一项，ID 在多次读之间不变。
      否则 sessions 的幂等键 `(child, date, activity_id, run_id, seq)`
      与状态机归属会跟着漂 —— 家长填了两次反馈会落两行。
      `candidate_id` 用落库后的候选 id（`plan_candidates.id`），
      它一旦落库就不再变。
    """
    return f"gen:{candidate_id}:{index}"


def to_item(a: Activity) -> dict[str, Any]:
    """`Activity` → **纯 dict**（state / checkpoint / plans.payload 里存的就是它）。

    ★ 为什么要转：checkpointer 用 msgpack 序列化，Pydantic 实例属于
      "未注册类型"（实测会打出 "will be blocked in a future version" 的警告），
      而 **checkpoint 的寿命比代码长** —— 今天能反序列化不代表明年能。
      同一个理由，`graph/state.py` 里的 observation 也存 dict。
    """
    return a.model_dump(mode="json")


def from_item(d: dict[str, Any]) -> Activity:
    """**纯 dict** → `Activity`。`to_item` 的逆。

    老数据缺字段时用默认值（`kind` / `origin` 都是选填），
    所以老行读得出来。
    """
    return Activity.model_validate(d)


def items_to_activities(items: list[dict[str, Any]]) -> list[Activity]:
    """state 里的一组方案项 dict → `Activity` 列表。"""
    return [from_item(d) for d in (items or [])]


def as_generated(a: Activity, *, activity_id: str, origin: str = "") -> Activity:
    """把一条活动**转成生成项**：换 id、标来源、记出处。

    ★ 这就是"库内项 ↔ 生成项的互转"里唯一需要的那一格：
      形状一个字没动，只改了**来源**与**身份**。
      转换之后它走的仍然是同一条流水线（硬过滤 / 权重 / 装箱）。
    """
    d = a.model_dump()
    d["activity_id"] = activity_id
    d["kind"] = KIND_GENERATED
    d["origin"] = origin or a.origin
    return Activity.model_validate(d)


def signature(a: Activity) -> tuple:
    """**一个方案项的语义签名** —— 指纹的比较单位。

    ★ 收哪些字段：只看**会改变孩子要做的事**的那些。
      · target_skill_items —— 练什么变了 = 实质变更
      · materials        —— 用什么变了 = 实质变更（材料换了，准备方式就变了）
      · difficulty       —— 难度变了 = 实质变更
      · prompt_hierarchy[0] —— 从第几级提示开始 = 实质变更
      · duration×sessions —— 做多久变了 = 实质变更
      · steps            —— **具体怎么做**变了 = 实质变更
    """
    return (
        tuple(sorted(a.target_skill_items)),
        tuple(sorted({m.name for m in a.materials})),
        a.difficulty,
        (a.prompt_hierarchy[0] if a.prompt_hierarchy else ""),
        a.duration_min * a.sessions_per_day,
        tuple((s.action, s.prompt_level) for s in a.steps),
    )
