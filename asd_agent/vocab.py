# -*- coding: utf-8 -*-
"""
受控词表（Controlled Vocabulary）设计
====================================

设计原则
--------
1. **可观察优先**：只记录家长看得见、听得到的外部行为，不推测孩子的内在动机。
   "想要电视"是可推断的，"拒绝被要求关电视"是可观察的 —— 选后者。
2. **MECE + 保留出口**：每个事件尽量只归一类；每张表都保留 other / unclear，
   强制穷尽会逼出错误标签，而错误标签会污染后续所有统计。
3. **扁平单层**：家长 3 秒内要能选出来，不做多级嵌套。
4. **前因/后果按"功能"组织**：这是与教科书式 ABC 记录法的关键区别。
   功能性分类直接服务于功能行为评估（FBA），而 FBA 决定干预方案。
   记录下来的不是"后果是什么"，而是"这个行为替他/她解决了什么"。
"""

# ---------------------------------------------------------------- 场景
# 正在进行的事。transition 在这里和前因里都出现是有意的：
# 转换确实是一个"场景"，且转换失败可以由多种前因引起（需求受阻/过度兴奋/环境嘈杂…）
SCENE = {
    "mealtime":      "正餐／喂饭",
    "snack":         "零食时间",
    "bedtime":       "睡前程序",
    "nap":           "午睡",
    "bath":          "洗澡",
    "dressing":      "穿脱衣服",
    "toilet":        "如厕／换尿布",
    "leaving":       "出门前",
    "transit":       "乘车／外出途中",
    "restaurant":    "外出就餐",
    "shopping":      "购物",
    "playground":    "户外活动",
    "indoor_play":   "室内玩耍",
    "screen":        "看屏幕",
    "guest":         "家里来客人",
    "party":         "聚会／多人场合",
    "sibling":       "与手足互动",
    "parent_busy":   "家长忙／注意力被占用",
    "learning":      "作业／学习",
    "clinic":        "医院／机构",
    "waiting":       "排队／等待",
    "transition":    "换活动／换场景",
    "other":         "其他",
}

# ---------------------------------------------------------------- 行为
# 问题行为与积极行为放在同一张表 —— 因为 PRT / ESDM 的干预目标恰恰是积极行为，
# 记录系统必须能装下"他今天第一次主动指给我看"这类事件。
BEHAVIOR = {
    # —— 情绪爆发类 ——
    "meltdown":         "情绪崩溃（完全失控、无法用言语安抚）",
    "tantrum":          "发脾气（有明确诉求、仍能被安抚）",
    "screaming":        "尖叫",
    "prolonged_crying": "持续哭闹",
    # —— 自我伤害／攻击类（高危，见 redflag.py）——
    "self_injury":      "自伤（咬自己、撞头等）",
    "hit_people":       "打人",
    "push_people":      "推人",
    "property_damage":  "破坏物品",
    "throwing":         "摔／扔东西",
    "elopement":        "跑开／走丢",
    # —— 退缩回避类 ——
    "withdrawal":       "退缩／封闭自己",
    "hiding":           "躲藏",
    "refusal":          "拒绝指令",
    "hiding_hands":     "藏手（不想被看见／被要求）",
    # —— 重复刻板类 ——
    "repetitive":       "重复刻板动作（摇、敲、转）",
    "echolalia":        "仿说",
    "scripting":        "照本宣科式复述",
    "transition_stuck": "转场卡住（走不动）",
    "fixation":         "固着于某件事／某物",
    # —— 感觉类 ——
    "sensory_seeking":  "感觉寻求（找强刺激）",
    "sensory_avoid":    "感觉回避（捂耳、躲避接触）",
    # —— 进食／自理类 ——
    "food_refusal":     "完全拒食",
    "food_selective":   "挑食（只吃固定的几种）",
    "pica":             "吃非食物物品",
    "toileting_accident": "尿湿／弄脏",
    # —— 积极／目标行为类 ——
    "spontaneous_request": "主动提出需求（手势、拉手、用语）",
    "verbal_request":      "用语言提出要求",
    "joint_attention":     "共同注意（指给你看／分享他的兴趣）",
    "imitation":           "模仿你",
    "symbolic_play":       "象征性游戏（过家家、扮演）",
    "sharing":             "分享",
    "turn_taking":         "等待轮流",
    "task_completion":     "完成了一项任务",
    "self_regulation":     "自己平静下来",
    "persisting":          "坚持完成一件困难的事",
    "accepted_no":         "能接受『不行』",
    "flexible_switch":     "能接受计划改变",
    "other":               "其他",
}

# ---------------------------------------------------------------- 前因
# 组织为六类：需求受阻 / 转换与时间 / 生理状态 / 感官环境 / 社交与成人的行为 / 情绪状态
ANTECEDENT = {
    # 需求与自主受阻（对应"自主性需求被阻断"这一自闭症核心特征）
    "demand_unmet":      "需求或要求没被满足",
    "wants_choice":      "想自己做选择，被代替了",
    "no_consent":        "正在做的事被要求停下",
    "wants_item":        "想要某样东西拿不到",
    "wants_continued":   "想继续某活动但被结束了",
    # 转换与时间
    "transition":        "活动／场景要转换",
    "waiting":           "需要等待或排队",
    "schedule_change":   "原本的计划被打乱",
    "activity_ending":   "活动快结束了",
    # 生理状态（illness 走红旗通道，见 redflag.py）
    "fatigue":           "累或困了",
    "hunger":            "饿了",
    "toileting_need":    "想上厕所",
    # 感官与环境
    "noise":             "噪音大、人多、场所嘈杂",
    "crowd":             "人多拥挤",
    "touch":             "身体接触／被碰到",
    "unfamiliar_place":  "不熟悉的环境",
    "sensory_overload":  "感官过载（声音、光、味道同时来）",
    # 社交与成人的行为
    "ignored":           "叫了没被回应、被忽视",
    "peer_presence":     "有同伴在场",
    "sibling_conflict":  "与手足发生冲突",
    "adult_demand":      "成人要求过多或反复催促",
    "too_many_choices":  "可选项太多",
    # 情绪状态
    "hyperarousal":      "过度兴奋",
    "boredom":           "无聊",
    "prior_upset":       "刚经历了不开心的事",
    "unclear":           "说不清／没观察到明确前因",
    "other":             "其他",
}

# ---------------------------------------------------------------- 后果
# ★ 按功能（function）分类，不是按表面现象分类。
# 记录的是"这个行为替他解决了什么"，而不是"他做了什么"。
# 这是 ABC 记录法的工程化关键：拿到这张表才能做 FBA，进而才能选干预方案。
#   tangible  → 他得到了实物/活动        → 干预：教他用可接受的方式提出要求
#   social    → 他得到了注意/掌控感      → 干预：不可用注意作为奖励
#   escape    → 他逃离了任务/场景         → 干预：任务分级 + 视觉日程预告
#   sensory   → 他得到了感觉刺激         → 干预：提供替代性感觉输入
CONSEQUENCE = {
    # 获得物
    "got_item":            "得到了想要的东西",
    "got_choice":          "获得了选择权",
    "got_activity_extended": "活动被延长了",
    # 获得注意／掌控
    "got_extra_attention": "获得了额外注意",
    "adult_relented":      "成人让步／降低了要求",
    # 逃避与终止
    "activity_ended":      "活动结束了",
    "left_scene":          "离开了那个场景",
    "task_stopped":        "任务停止了",
    "got_break":           "获得了短暂休息",
    "successfully_transitioned": "成功完成了转换",
    # 感觉刺激
    "got_sensory":         "获得了感觉刺激（摇晃／旋转／敲击等）",
    # 成人的其他反应
    "adult_approached":    "成人靠近并安抚了",
    "adult_left":          "成人离开了他",
    "adult_intensified":   "成人加强指令或制止了",
    "adult_ignored":       "成人没有反应",
    # 无明显后果
    "continued_unchanged": "活动照常继续，没有明显变化",
    "unclear":             "说不清／没观察到明显后果",
    "none":                "明确没有任何后果",
    "other":               "其他",
}

# ---------------------------------------------------------------- 在场者
# 用于分析"不同在场者带来的行为差异" —— 家长最容易自己发现却最难解释的现象
PEOPLE_PRESENT = {
    "mother":    "妈妈",
    "father":    "爸爸",
    "grandparent": "祖辈",
    "sibling":   "兄弟姐妹",
    "peer":      "同伴／其他孩子",
    "teacher":   "老师",
    "therapist": "治疗师",
    "stranger":  "陌生人",
    "alone":     "当时没有其他人",
}

# ================================================================= 教练 Agent 的新增词表
#
# 下面这些服务于"评估报告 → 能力画像 → 干预计划"这条链路。
# 与前面五张表同源的动机：**消灭标签漂移**，区别在于作用域 ——
# 前面五张管"单次抽取"，下面这些管"跨天聚合"。
# 能力画像每天被读改写一次，如果模型能自创能力名（"社交主动性" vs "共同注意"），
# 同一项技能会在不同天变成两个 key，状态机永远推不上去。

# ---------------------------------------------------------------- 五能区
# 儿心量表的五个能区。叫"能区"而非"领域"，是为了和报告上的说法对齐。
ABILITY_DOMAIN = {
    "gross_motor":  "大运动",
    "fine_motor":   "精细动作",
    "language":     "语言",
    "cognition":    "认知",
    "social_adapt": "社会适应",
}

# ---------------------------------------------------------------- 技能细项
# 每个能区下的细项，作为抽取时的受控词表（防止模型自创能力名）。
# 刻意选"家长看得见、报告上会出现"的粗颗粒行为，而不是"社交主动性"这类构念式
# 命名 —— 构念无法被直接观察，也无法被设计成一个具体活动去练。
SKILL_ITEMS = {
    "gross_motor": [
        "独走稳", "跑", "上下楼梯", "双脚跳", "单脚站", "踢球", "投掷", "骑滑步车",
    ],
    "fine_motor": [
        "抓握", "捏取", "涂鸦", "串珠", "翻书页", "用剪刀", "扣纽扣", "搭积木塔",
    ],
    # ★ 原先这里有"两步指令"，与"听懂双指令"语义几乎重复。
    # 实测 7B 会把同一句证据（"能记住两步指令"）同时分给两个细项，
    # 于是状态机把一个构念拆成两个 key 各推各的，历史被劈成两半。
    # 重复的词表项比缺失的更危险：缺失只是"这项没进展"，重复会让统计口径失真。
    # —— 合并为"听懂双指令"（activity_data 里 4 处引用已一并改指）。
    "language": [
        "听懂单词", "听懂双指令", "主动表达需要",
        "命名物体", "扩展句子", "回答是非问",
    ],
    # "记住三步指令"已删：零条活动覆盖 → 状态机永远推不动它，
    # 只会作为一条死 key 混进画像和统计。词表里躺着一个推不动的项是纯负债。
    "cognition": [
        "配对", "分类", "排序",
        "解决简单问题", "因果理解", "时间顺序",
    ],
    "social_adapt": [
        "眼神回应", "共同注意", "主动分享", "轮流等待",
        # "适应新环境"同样因零活动覆盖而删除
        "接受帮助", "情绪命名与表达",
    ],
}

# 扁平的 code -> 中文 映射。**刻意不并进 VOCABS**：细项有 36 项，
# 塞进去会让任何调用 vocab_block 的 prompt 都平白多出几十行。
# 需要渲染时用 skill_items_block()，按能区分组，更像量表本身的样子。
SKILL_ITEM_CN: dict[str, str] = {
    item: item for items in SKILL_ITEMS.values() for item in items
}

# 每个技能细项归属哪个能区 —— 检索时用来从"目标细项"反查能区。
SKILL_ITEM_DOMAIN: dict[str, str] = {
    item: domain for domain, items in SKILL_ITEMS.items() for item in items
}

# ---------------------------------------------------------------- 能力状态机
# 五态。只允许相邻转移（外加回退），不允许跳级 —— 详见 statemachine.py。
ABILITY_STATE = [
    "not_started",   # 未开始
    "entry",         # 入门
    "practicing",    # 练习中
    "mastered",      # 已掌握
    "generalizing",  # 泛化中
]

ABILITY_STATE_CN = {
    "not_started": "未开始",
    "entry":       "入门",
    "practicing":  "练习中",
    "mastered":    "已掌握",
    "generalizing": "泛化中",
}

# 每个状态对应的目标活动难度。**这是状态机与检索器的接缝** ——
# 状态不是只用来显示的，它直接改变候选活动的打分（w_state_match）。
STATE_TARGET_DIFFICULTY = {
    "not_started": 1,
    "entry":       1,
    "practicing":  2,
    "mastered":    3,
    "generalizing": 3,
}

# ---------------------------------------------------------------- FBA 功能分类
# 把上面 19 个 consequence 归成 5 类**功能**。这是功能性行为评估（FBA）的核心一步，
# 也是"观察到行为"变成"决定怎么干预"的分界：
#     got_item      → 他要实物   → 教他用可接受的方式提要求
#     left_scene    → 他在逃离   → 任务分级 + 视觉日程预告
#     got_sensory   → 他要感觉   → 提供替代性感觉输入
#     got_extra_... → 他要关注   → 不可用注意作为奖励
#
# 放在 vocab.py 而不是 coach.py 里，是因为它**是词表的派生关系**：
# 教练对一条观察说"这个行为的功能是逃离"，系统要能拿它引用的那条观察的
# consequence 去核对。派生关系住在使用方，将来加一个 consequence code 就会漏掉归类。
#
# 注意 class 名沿用上面 CONSEQUENCE 注释里的 "social"；这里叫 attention 是因为
# 那组里既有"得到注意"也有"成人让步＝得到掌控"，attention 更贴近日常说法。
CONSEQUENCE_FUNCTION = {
    "tangible":  ("got_item", "got_choice", "got_activity_extended"),
    "attention": ("got_extra_attention", "adult_relented", "adult_approached",
                  "adult_intensified", "adult_ignored"),
    "escape":    ("activity_ended", "left_scene", "task_stopped", "got_break",
                  "adult_left"),
    "sensory":   ("got_sensory",),
    # 这一组是"不构成功能"的四种情况：没变化、成功了、说不清、没有。
    # successfully_transitioned 归这里是有意的 —— 它描述的是**目标达成**，
    # 不是某个问题行为靠它换来的好处，不该和 left_scene 一起当成"功能"。
    "unclear":   ("continued_unchanged", "successfully_transitioned",
                  "unclear", "none", "other"),
}

FUNCTION_CN = {
    "tangible":  "得到实物或选择",
    "attention": "得到注意或掌控",
    "escape":    "逃离任务或场景",
    "sensory":   "得到感觉刺激",
    "unclear":   "说不清／不构成功能",
}


def function_of(consequence: str) -> str:
    """consequence code → FBA 功能。**纯派生函数，可重放。**

    词表里没有的 code 归 `unclear` 而不是抛错 —— 因为 consequence 已经被
    `schema.Observation` 的 Literal 拦过了，走到这里必然是合法 code；
    而"这个合法后果不属于任何一类功能"在语义上就是 unclear，不是错误。
    抛错会让调用方为了防御一个不可能发生的分支而写 try/except。
    """
    for fn, codes in CONSEQUENCE_FUNCTION.items():
        if consequence in codes:
            return fn
    return "unclear"


def validate_function_coverage() -> list[str]:
    """每个 consequence code 必须恰好归一类。`python vocab.py` 会调用。

    这条自检挡的是一个很隐蔽的漂移：将来往 CONSEQUENCE 里加一个新 code
    而忘了归类，`function_of` 会**静默**返回 unclear —— 不报错，
    只是那条行为的功能从"逃离任务"悄悄变成"说不清"，于是教练不再降低难度，
    家长拿到一条明显不合适的建议。这正是本项目反复出现的那类 bug：
    引用了存在但放错位置的 ID。
    """
    errs: list[str] = []
    grouped = [c for codes in CONSEQUENCE_FUNCTION.values() for c in codes]
    for code in CONSEQUENCE:
        n = grouped.count(code)
        if n == 0:
            errs.append(f"consequence {code!r} 没有归入任何功能类别")
        elif n > 1:
            errs.append(f"consequence {code!r} 被归入了 {n} 个功能类别")
    for c in grouped:
        if c not in CONSEQUENCE:
            errs.append(f"功能分类里引用了词表中不存在的 consequence：{c!r}")
    return errs


# ---------------------------------------------------------------- 材料类别
# 用途：把"只用家里常见物品"这条硬约束变成可判定的分类判断。
# app / printable 单独一类，是为了在前端能打"需额外准备"的角标。
MATERIAL_CLASSES = (
    "common_household",  # 家里一定有：碗、杯子、袜子、钥匙
    "toy",               # 玩具（可能没有）
    "food",              # 食物
    "paper",             # 纸笔类
    "body_parts",        # 身体部位（拍手、指鼻子）
    "wearable",          # 可穿戴：帽子、袜子、手套
    "app",               # 需要 App
    "printable",         # 需要打印或提前自制卡片
)

# 不需要额外采购的类别 —— 检索时若 essential 材料全落在这些类别里，视为"家里一定有"
ALWAYS_AT_HOME = ("common_household", "food", "paper", "body_parts", "wearable")


VOCABS = {
    "scene":       SCENE,
    "behavior":    BEHAVIOR,
    "antecedent": ANTECEDENT,
    "consequence": CONSEQUENCE,
    "people_present": PEOPLE_PRESENT,
    "ability_domain": ABILITY_DOMAIN,
}


def vocab_block(name: str) -> str:
    """把词表渲染成带编号的文本，供塞进 prompt 帮助模型对齐中文表述。"""
    lines = []
    for i, (code, cn) in enumerate(VOCABS[name].items(), 1):
        lines.append(f"{i:2d}. {code} = {cn}")
    return "\n".join(lines)


def skill_items_block() -> str:
    """技能细项按能区分组渲染。

    单独写一个函数而不是并进 vocab_block：细项的 code 和中文是同一个字符串，
    编号渲染成 "1. 独走稳 = 独走稳" 很怪，按能区分组反而更像量表本身的样子，
    7B 对齐效果更好。
    """
    out = []
    for domain, cn in ABILITY_DOMAIN.items():
        out.append(f"[{cn} / {domain}]")
        for i, item in enumerate(SKILL_ITEMS[domain], 1):
            out.append(f"  {i}. {item}")
        out.append("")
    return "\n".join(out).rstrip()


def vocab_summary() -> str:
    parts = [f"{k}({len(v)})" for k, v in VOCABS.items()]
    parts.append(f"ability_state({len(ABILITY_STATE)})")
    parts.append(f"skill_items({len(SKILL_ITEM_CN)})")
    parts.append(f"material_classes({len(MATERIAL_CLASSES)})")
    return "  |  ".join(parts)


if __name__ == "__main__":
    import sys

    print("词表规模：", vocab_summary())
    for k in VOCABS:
        print(f"\n===== {k} =====")
        print(vocab_block(k))
    print("\n===== skill_items (按能区) =====")
    print(skill_items_block())
    print("\n===== ability_state =====")
    for code, cn in ABILITY_STATE_CN.items():
        print(f"  {code:14s} {cn:6s} -> 目标难度 {STATE_TARGET_DIFFICULTY[code]}")
    print("\n===== FBA 功能分类 =====")
    for fn, codes in CONSEQUENCE_FUNCTION.items():
        print(f"  {fn:10s} {FUNCTION_CN[fn]}")
        print(f"             {'、'.join(codes)}")

    errs = validate_function_coverage()
    if errs:
        print(f"\n✗ 功能分类有 {len(errs)} 处问题：")
        for e in errs:
            print("   " + e)
        sys.exit(1)
    print(f"\n✓ {len(CONSEQUENCE)} 个 consequence code 全部恰好归入一个功能类别")

    print("\n===== material_classes =====")
    for c in MATERIAL_CLASSES:
        print(f"  {c:20s} {'家里一定有' if c in ALWAYS_AT_HOME else '需确认'}")
