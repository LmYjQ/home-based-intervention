# -*- coding: utf-8 -*-
"""
生成项的来源（阶段 1 = 固定 fixture）
====================================

**这一份方案从哪来** —— 活动库里没有的那几条，是谁放进来的。

★ 为什么单独一个模块
--------------------
"候选从哪来"与"候选怎么打分"必须是两件事：
  · 活动库里的那些 → **检索**出来的（`activities.retrieve`）
  · 不在活动库里的那些 → **装配**进来的（本模块）
但装配进来的那些**不享有任何豁免** —— 同一套硬过滤、同一套权重、同一条
装箱流水线（见 `activities.score_item` / `activities.build_pool`）。
**放宽的是"方案从哪来"，不是"方案要满足什么"。**

★ 阶段 1 为什么不接 LLM
-----------------------
阶段 1 要验的是**管道**（生成项能不能一路走到卡片与反馈），
不是**生成质量**。接 LLM 的话：
  · 每条自检断言都要网络（本地断网 = 整套自检全挂）
  · LLM 不确定性会让"指纹不变"这类断言变成 flaky
所以本阶段用**固定 fixture**（一条手写的生成项），
真正的 LLM 生成在阶段 3 —— 届时只换本模块的实现，
下游一行都不用改（这正是把"来源"单独收成一个模块的用意）。

★ 开关
------
默认**关**（`ASD_GENERATED_FIXTURE` 不为 1 时不装配任何生成项）。
理由：这一条 fixture 是**手写**的，它不是"模型针对这个孩子生成的做法"，
无条件装配进真实用户的计划里就是在骗家长 ——
家长看到"这是针对你家孩子生成的做法"，而它其实是写死的。
自检与手工验证时显式打开它。
"""

from __future__ import annotations

import os

from activities import Activity, Material, Step

#: 打开开关的环境变量名。
ENV_FLAG = "ASD_GENERATED_FIXTURE"

#: 生成项的 id 前缀与稳定来源。**必须稳定** ——
#: sessions 的幂等键 `(child, date, activity_id, run_id, seq)` 与状态机归属
#: 都按 activity_id 走，ID 每次重生成都变的话，那两处就会漂。
#: 阶段 3 起 `candidate_id` 换成落库后的候选 id（`plan_candidates.id`），
#: 它一旦落库就不再变。
CANDIDATE_ID = "fixture"


def enabled() -> bool:
    """本进程是否装配生成项。"""
    return os.getenv(ENV_FLAG, "") in ("1", "true", "yes", "on")


def _steps() -> list[Step]:
    return [
        Step(step_id=1, action="你把两张一样的卡片并排放在他面前，说：看看，这两个一样",
             child_target_response="看着两张卡片", time_limit_sec=5, prompt_level="natural"),
        Step(step_id=2, action="用手指依次点左边那张、右边那张，说：一样的",
             child_target_response="目光跟着手指移动", time_limit_sec=8, prompt_level="natural"),
        Step(step_id=3, action="停手，等 5 秒",
             child_target_response="自己伸手去指其中一张", time_limit_sec=5, prompt_level="natural"),
        Step(step_id=4, action="他指了就跟着他说：对，两张一样",
             child_target_response="有回应或有动作", time_limit_sec=None, prompt_level="natural"),
    ]


def fixture_items() -> list[Activity]:
    """那条固定的生成项。

    ★ 挑"情绪命名与表达"这个技能细项是有讲究的：
      它在活动库里只被一条活动覆盖（`soc-007`，而那条难度 2，
      在入门水平的孩子身上会被难度闸门挡掉），
      所以它**不与库内项抢同一个细项** ——
      而"同一天不重复练同一个技能细项"是装箱的硬保证，
      抢了的话这条生成项会被挤掉（不报错，就是不出现）。
    """
    return [
        Activity(
            activity_id=f"gen:{CANDIDATE_ID}:0",
            title="找出一模一样的两张卡片",
            kind="generated",
            origin="固定 fixture（阶段 1 未接 LLM）· 针对「情绪命名与表达」的做法",
            domains=["social_adapt"],
            target_skill_items=["情绪命名与表达"],
            duration_min=5,
            sessions_per_day=1,
            materials=[],
            setting=["anywhere"],
            sensory_load="low",
            prep_min=0,
            method=["NDBI", "PRT"],
            method_rationale="先让孩子指出「一样的」，再把「一样」这个词挂到他的动作上，"
                              "比直接要求他说出来更容易开口",
            difficulty=1,
            setup=["两张完全一样的卡片（没有就撕两片一样的纸）"],
            steps=_steps(),
            success_criteria=["能连续两次指到其中一张"],
            prompt_hierarchy=["你先不要那两张，说：我们来找一样的",
                              "手带着他的手指到其中一张",
                              "手扶着他的手把两张并到一起"],
            error_correction="他走开就收起来，过一会儿再来。**硬掰只会让他记住这件事很烦。**",
            generalization_cues=["换成两个一样的积木", "换到户外用树叶"],
            success_behaviors=["pointing", "attending", "imitation"],
            refusal_behaviors=["refusal", "withdrawal", "tantrum"],
            linked_scenes=["indoor_play", "mealtime"],
        )
    ]


def items_for(conn=None, child_id: str = "", day: str = "") -> list[Activity]:
    """给这一天装配生成项。**关着开关就是空列表。**

    ★ 签名留了 `conn` / `child_id` / `day` ——
      阶段 3 的 LLM 生成要读这个孩子的画像与当天观察，
      而**调用点的形状不该变**（调用方不该知道来源换了实现）。
    """
    if not enabled():
        return []
    return fixture_items()
