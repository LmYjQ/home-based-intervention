# -*- coding: utf-8 -*-
"""
红旗扫描
========
**刻意做成独立规则，不做成 LLM schema 里的一个字段。**

这是 verify_schema.py 的 A/B 实测结论，不是设计偏好：把红旗塞进主 schema，
模型 12/12 输出都合规，但那条唯一含自伤的语料 C11
（"看到旋转玩具就走开，然后会去咬自己的袖子"）被标成了 repetitive，
自伤漏检。原因不难理解 —— schema 变宽后注意被摊薄，
而"这是刻板行为还是自伤"恰恰需要模型**全神贯注**去判断一次的事。

分成两个 Agent 的效果完全不同：
  - 观察抽取：把分类做细、每条都能给出多维标签（语义任务）
  - 红旗扫描：只问"这段话里有没有必须让人停下来说话的东西"（判定任务）

而且这样做还有一个工程上的好处：红旗**永远不会被漏掉**。
它不是某个 Agent 记起来要调的一步，而是 `profile.py` 和阶段 4 的
`/api/observations` 都在入口处无条件跑一遍的规则。

命中之后的行为：不是拒绝处理，而是**照常处理 + 打一个高优先级标记**。
一个真的在咬自己袖子的孩子，恰恰是最需要有人今天就给他一份干预计划的人。

★ Phase 2 加在这一层的三样东西（都在这里，不在图节点里）
-------------------------------------------------------
1. `referral` —— 转诊指引。**直接长在 `RULES` 每一项上**，而不是旁边另开一张
   表：规则与它的临床文案在同一次编辑里，"加了一条规则却忘了写指引"合不进来。
   之前那句硬编码在 `graph/nodes.py` 的通用文案有个具体后果 ——
   命中"异食"和命中"撞头"给家长的是同一句话，而这两件事该找的人不一样。

2. `guidance_for()` —— 多个 code 同时命中时的**逐条**聚合，不是揉成一段。
   家长面对的是"今天有两件事要处理"，不是一个混合句。

3. `can_transition()` —— 红旗状态流转判定。纯 Python、零 token、可重放，
   与 `statemachine.propose_transition` 同一套做法。
   **"已看到"不等于"已处理"** —— 只有 closed 解除计划暂停。

★ 判定归这里，落库不归这里
--------------------------
`scan()` 至今仍是纯正则、零依赖、离线可跑（它也是 12 个离线回归之一）。
红旗的**落库**是 `store.append_red_flags` + 图的 `publish` 节点的活，
原因写在 `store.py` 那张表的注释里 —— 判定的出处和它的存储位置要分开，
否则将来有人改规则时会顺手改掉写库逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ------------------------------------------------------------------ 规则表
# severity 分三档，决定前端的呈现方式，也决定**后续动作**：
#   urgent —— 立刻提示，建议当天联系专业人员；不得静默通过
#            → 落库 + 触发计划暂停（见 store.plan_paused）+ 给转诊指引
#   high   —— 需要追问细节，可能与已有判断冲突 → 落库 + 给追问，不暂停
#   watch  —— 记录下来，可能改变后续计划的权重 → 落库，不追问不暂停
#
# 每条规则五部分：正则（匹配原文）、级别、命中后要问家长的话、
# 以及**转诊指引**。正则全部写成"动词+对象"的形式而不是孤立的名词 ——
# 匹配"咬自己"而不匹配"咬"，匹配"撞头"而不匹配"碰头"（那是玩闹）。
#
# referral 的四个字段对应家长真正会问的四个问题，少一个就答不上来：
#   who       找谁   —— 必须具体到能直接照着做，"看医生"不是指引
#   do        做什么 —— 包含"记录"，红旗的临床价值大半在记录里
#   avoid     不要做 —— **防害字段，不允许为空**
#   emergency 急诊红线 —— 什么时候这件事不再是"明天打电话"而是"现在去急诊"
REFERRAL_SPEC = ("who", "do", "avoid", "emergency")

RULES: list[dict] = [
    # ---------------- 自伤：最高优先级 ----------------
    dict(code="self_injury", severity="urgent", group="自伤",
         pattern=r"(咬|啃|抓|掐|挠|撞|砸|撕)(自己|自已|自个)|自伤|伤自己|咬伤",
         ask="他咬/撞自己的时候，是什么时候开始的？当时手上有没有东西？",
         referral=dict(
             who="当天联系发育行为儿科（儿童保健科的行为门诊）或儿童精神科医生",
             do="记录发生时间、当时在做什么、留下了什么痕迹；拍下伤痕存档；"
                 "把这段原话和记录一起带给医生",
             avoid="不要用手强行按住或绑住他，也不要在情绪峰值时讲道理；"
                   "不要自行加大干预强度",
             emergency="出血不止、伤口较深、意识不清，或短时间内反复自伤 → 立即急诊")),
    dict(code="head_banging", severity="urgent", group="自伤",
         pattern=r"撞头|头撞|撞墙|撞门|用头(顶|撞)",
         ask="撞头的频率大概多久一次？有没有因此去过医院？",
         referral=dict(
             who="当天联系发育行为儿科或儿童精神科；频率高或伴受伤的直接挂急诊",
             do="记录每次撞击的部位与力度、当时在做什么；把头面部照片留存；"
                 "尽快做一次头外伤评估",
             avoid="不要在他撞击时把他往后拉（可能造成颈部损伤）；"
                   "不要只让他「冷静一下」就当这件事过去了",
             emergency="撞击后呕吐、嗜睡、意识不清、瞳孔不等大、鼻耳出血 → 立即急诊")),
    # ---------------- 对他人的攻击 ----------------
    dict(code="aggression", severity="high", group="攻击",
         # "打我的手"是家长叙述自己被打的最常见句式，只写"打人"会整条漏掉
         pattern=r"打(我|人|他|她|小孩|孩子)|抓伤|掐(人|我|他|她)|咬人|咬我|踢人|推倒|扔东西砸|摔门砸",
         ask="他打人主要是在被要求停下的时候，还是没有明显前因？",
         referral=dict(
             who="先记下触发场景；一周内反复出现或伤到别人，请咨询发育行为儿科",
             do="记录「什么时候因为什么指令被打」，把前因和指令原话一起写下来 —— "
                 "这正是判断「能不能用这个指令」的关键",
             avoid="不要在他打人时立刻用更强的指令压过去（会加重而不是收敛）",
             emergency="")),
    # ---------------- 走失 ----------------
    dict(code="elopement", severity="high", group="走失",
         pattern=r"走丢|走失|跑出去|冲出(家门|马路)|不见了|找不到孩子",
         ask="他跑出去过几次了？当时周围有没有车或者水？",
         referral=dict(
             who="有过走失史或反复冲出家门，请咨询发育行为科；同时联系当地派出所备案",
             do="随身带姓名与联系方式；高风险时段有人看守；记录每次走失的路线与时长",
             avoid="不要用「抓回来打一顿」来处理走失，也不要把他独自留在家里",
             emergency="走失且已超过半小时，或已接近马路/水域 → 立即报警并报警找人")),
    # ---------------- 感觉与身体 ----------------
    dict(code="pica", severity="urgent", group="进食",
         pattern=r"吃(泥|土|沙|石头|纸巾|卫生纸|玩具)|非食物|异食",
         ask="他吃的是哪些东西？有没有因此做过检查？",
         referral=dict(
             who="当天联系儿科或发育行为儿科，说明吞食过哪些东西",
             do="保留吞下的物品或照片；尽快做血常规、铁、铅等检查；"
                 "记录吞食的量与频次",
             avoid="不要用手去掏他的嘴强行抠出来（有窒息风险）",
             emergency="吞下尖锐物/化学品，或出现咳嗽、呕吐、呼吸费力 → 立即急诊")),
    dict(code="food_refusal", severity="high", group="进食",
         pattern=r"(完全)?拒食|不吃饭|滴水不进|只吃.{0,4}(几样|固定)",
         ask="现在一天大概吃多少？体重有没有变化？",
         referral=dict(
             who="持续数天只吃几样或完全不进食，请尽快咨询儿科或营养科",
             do="记录每日实际摄入与体重；带上生长曲线就诊",
             avoid="不要用「不吃饭就不许玩」来做条件交换 —— "
                   "对进食焦虑的孩子这会把它变成对抗",
             emergency="")),
    dict(code="seizure_sign", severity="urgent", group="身体",
         pattern=r"(发呆|失神|愣住|叫不应).{0,6}(几秒|一会)|抽搐|抽动|癫痫|翻白眼|口角流水",
         ask="这些情况持续多久？有没有发生过在车里或独自一人的时候？",
         referral=dict(
             who="尽快做脑电图评估；发作请挂神经内科。做过评估的带上既往报告",
             do="记录发作时长、当时在做什么、是否呼之不应；录像（家长知情前提下）；"
                 "记下最近一次发作时间",
             avoid="不要在发作时往他嘴里塞东西或按住四肢",
             emergency="发作超过 5 分钟、反复发作不恢复、首次发作或伴外伤 → 立即急诊")),
    dict(code="sleep_regression", severity="watch", group="睡眠",
         pattern=r"整夜|整宿|整晚.{0,4}(醒|哭|闹)|睡眠.{0,2}(倒退|变差|变差)|突然.{0,4}不睡",
         ask="是从什么时候开始的？之前一直是什么样的？",
         referral=dict(
             who="持续两周以上的睡眠变化可以带着记录咨询儿科；"
                 "同时留意是否与其他倒退同时出现",
             do="记录入睡时间、夜醒次数与时长、白天状态；把前一周的作息一起带去",
             avoid="不要用白天补觉来抵消夜醒，会把节律推得更乱",
             emergency="")),
    # ---------------- 行为倒退（提示要重查评估报告） ----------------
    dict(code="regression", severity="high", group="倒退",
         pattern=r"(突然|最近|这阵子).{0,6}(不会|退化|倒退|退步|变差)|原来会.{0,6}现在不会",
         ask="以前能做到、现在做不到的，具体是哪些事？大概是什么时候开始的？",
         referral=dict(
             who="★ 这一条最要紧：能力倒退要重查评估。"
                 "请带上旧报告到发育行为儿科或儿童精神科复评",
             do="把「原来会什么、现在什么不会」逐条写下来，"
                 "带上之前的评估报告与就诊记录；排查近期的身体不适与用药变化",
             avoid="不要按「练一练就好了」处理倒退 —— "
                   "它更常见的原因是疾病或环境变化，而不是技能退化",
             emergency="")),
    # ---------------- 疑似家暴 / 明显安全风险 ----------------
    dict(code="safety_risk", severity="urgent", group="安全",
         pattern=r"(打|骂|揍)他|掐他|关(在|进)(黑屋|厕所)|不给吃",
         ask="这些情况发生时，通常是谁在场？",
         referral=dict(
             who="当下先保证孩子安全；有暴力或虐待迹象请联系当地妇联反家暴工作站、"
                 "公安机关或未成年人保护热线",
             do="记录发生时间、在场的人、具体做了什么；保留照片与就医记录",
             avoid="★ 不要因为这是「家里的事」就继续把孩子留在原环境里",
             emergency="孩子受伤、或你现在无法保证他的安全 → 立即联系警方与未成年人保护热线")),
    # ---------------- 医疗信号 ----------------
    dict(code="pain_signal", severity="high", group="身体",
         pattern=r"(一直|老是|经常)(哭闹|哼哼)|不明原因.{0,4}(哭|发热)|肚子疼|耳朵疼",
         ask="这些情况有没有和生病的时间对上？",
         referral=dict(
             who="反复不明原因哭闹或发热，请带记录去儿科排查器质性原因",
             do="记录发作时间、时长、当天进食与精神状态；就诊时按时间顺序给出来",
             avoid="不要反复用「是不是装的」来解释持续的哭闹",
             emergency="")),
]

_COMPILED = [(r["code"], r["severity"], r["group"], r["pattern"], r["ask"], r["referral"],
              re.compile(r["pattern"])) for r in RULES]


@dataclass(frozen=True)
class Referral:
    """一条规则的转诊指引。**四个字段缺一不可**，`avoid` 是防害的那一个。"""

    who: str
    do: str
    avoid: str
    emergency: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in REFERRAL_SPEC}


def _referral_of(raw: dict) -> Referral:
    return Referral(**{k: (raw.get(k) or "") for k in REFERRAL_SPEC})


@dataclass(frozen=True)
class RedFlag:
    code: str
    severity: str      # urgent / high / watch
    group: str
    quote: str
    ask: str
    referral: Referral

    @property
    def triggered(self) -> bool:
        return True

    def render(self) -> str:
        tag = {"urgent": "【立即关注】", "high": "【需要追问】", "watch": "【记录观察】"}[self.severity]
        return f"{tag}{self.group}（{self.code}）：{self.quote}\n    建议追问：{self.ask}"

    def as_row(self) -> dict:
        """落库/落 state 用的一行。**必须是纯 dict** ——
        checkpoint 的寿命比代码长，存对象会让老历史在重构后读不出来。
        """
        r = self.as_dict_of_referral()
        return {"code": self.code, "severity": self.severity, "group": self.group,
                "quote": self.quote, "ask": self.ask, **r}

    def as_dict_of_referral(self) -> dict:
        return {f"referral_{k}": v for k, v in self.referral.as_dict().items()}

    def dump(self) -> dict:
        """给 checkpoint 用的形状（`as_row` 加上前端还要用的 referral 本体）。"""
        d = self.as_row()
        d["referral"] = self.referral.as_dict()
        return d

    @classmethod
    def from_dump(cls, d: dict) -> "RedFlag":
        return cls(code=d["code"], severity=d["severity"], group=d.get("group", ""),
                   quote=d.get("quote", ""), ask=d.get("ask", ""),
                   referral=_referral_of(d.get("referral") or d))


@dataclass(frozen=True)
class RedFlagReport:
    hits: tuple[RedFlag, ...] = ()

    @property
    def triggered(self) -> bool:
        return bool(self.hits)

    @property
    def highest(self) -> str:
        if not self.hits:
            return ""
        order = {"urgent": 0, "high": 1, "watch": 2}
        return min(self.hits, key=lambda h: order[h.severity]).severity

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        """urgent 级的 code。前端据此在页面上强制置顶展示。"""
        return tuple(h.code for h in self.hits if h.severity == "urgent")

    @property
    def urgent_hits(self) -> tuple[RedFlag, ...]:
        return tuple(h for h in self.hits if h.severity == "urgent")

    def render(self) -> str:
        if not self.hits:
            return "未发现红旗信号。"
        order = {"urgent": 0, "high": 1, "watch": 2}
        lines = ["发现以下红旗信号，请优先处理，不要跳过："]
        lines += [h.render() for h in sorted(self.hits, key=lambda h: order[h.severity])]
        return "\n".join(lines)


CLEAN = RedFlagReport()


def scan(text: str) -> RedFlagReport:
    """扫一段文本，返回命中的红旗。**纯正则，无 LLM，<1ms。**

    命中后取**最早出现**的匹配片段作为 quote —— 家长核对的是"你说我写了这句话"，
    给他一个中段片段不如给他开头的位置好找。
    """
    if not text or not text.strip():
        return CLEAN

    hits: list[RedFlag] = []
    seen: set[str] = set()
    for code, severity, group, pattern, ask, referral, rx in _COMPILED:
        m = rx.search(text)
        if not m:
            continue
        # 同一 code 只报一次；但 urgent 级允许多条同族规则各自命中
        # （"咬自己"和"撞头"是不同的信号，必须都报出来）
        if code in seen:
            continue
        seen.add(code)
        s = max(0, m.start() - 8)
        quote = text[s:m.end() + 12].strip()
        # 片段开头不要落在标点上，否则家长核对时看到的是「，打人」这种半截话
        quote = quote.lstrip("，。！？；、,.!?; \n")
        hits.append(RedFlag(code=code, severity=severity, group=group,
                            quote=quote, ask=ask, referral=_referral_of(referral)))

    order = {"urgent": 0, "high": 1, "watch": 2}
    hits.sort(key=lambda h: order[h.severity])
    return RedFlagReport(tuple(hits))


# ================================================================== Phase 2
# 下面三个纯函数把「判定」和「怎么呈现 / 怎么存」分开。
# 判定仍然只有 `scan` 一处；这三个都不做任何新的判断。
# ==================================================================

def rows_for(report: RedFlagReport) -> list[dict]:
    """一次扫描 → 待落库的行。**全部严重度都出**，不只是 urgent。

    只落 urgent 的话，"这孩子上个月出现过几次自伤"就答不出来了 ——
    而那正是红旗记录相对于 `observations.redflag_triggered` 那个布尔的全部价值。
    需不需要暂停由 `store.plan_paused` 按 severity 判，与这里无关。
    """
    return [h.as_row() for h in report.hits]


def dump_hit(h: RedFlag) -> dict:
    return h.dump()


def report_from_dump(dump: dict) -> RedFlagReport:
    """从 state / checkpoint 里的 dict 还原报告。

    ★ 老 checkpoint 里没有 `referral` 字段 —— `_referral_of` 用空串兜底，
      而**不是**让整条路径 raise。这与 LANGGRAPH.md「向后兼容」那条同源：
      新字段一旦 required，每一条旧记录都会被静默跳过。
    """
    hits = tuple(RedFlag.from_dump(d) for d in (dump.get("hits") or []))
    return RedFlagReport(hits)


def guidance_for(report: RedFlagReport) -> dict:
    """多个 urgent code 命中时的**逐条**聚合。纯函数，零 token。

    ★ 为什么结构化而不是一段话：家长面对的是"今天有两件事要处理"，
      揉成一句的结果是其中一件被略过 —— 而略掉的那一件可能正是更急的那件。

    ★ 为什么只有 urgent 进这里：high/watch 本来就是另外两种行为
      （追问细节 / 记下来），`redflag.py` 顶上那三档的定义就是这么写的。
      混进同一个 blob 会稀释 urgent 该有的力度。

    `summary` 仍然是一句 str —— 老 checkpoint 与老前端读的就是这个键，
    换成结构会打断它们。
    """
    items = []
    do_not: list[str] = []
    emergency = False
    for h in report.urgent_hits:
        r = h.referral
        items.append({"code": h.code, "group": h.group, "severity": h.severity,
                      "quote": h.quote, "ask": h.ask, **r.as_dict()})
        if r.avoid and r.avoid not in do_not:
            do_not.append(r.avoid)
        emergency = emergency or bool(r.emergency)

    if not items:
        summary = ""
    elif len(items) == 1:
        summary = f"检测到 1 项需要当天处理的信号：{items[0]['group']}。"
        if emergency:
            summary += items[0]["emergency"]
    else:
        groups = "、".join(i["group"] for i in items)
        summary = f"检测到 {len(items)} 项需要当天处理的信号：{groups}。"
        if emergency:
            summary += "其中含急诊红线，请见下方说明。"

    return {"summary": summary, "items": items, "do_not": do_not,
            "emergency": emergency}


# ================================================================== 状态流转
RedFlagStatus = Literal["open", "acknowledged", "closed"]

#: 允许的转移。**只有"已关闭→已看到"是禁的**：没处理过就"已看到"，
#: 语义上不成立，而且它会让家长以为自己已经处理过了。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"acknowledged", "closed"},
    "acknowledged": {"closed", "open"},
    "closed": {"open"},
}


def can_transition(frm: str, to: str) -> bool:
    """状态转移判定。**纯 Python，零 token，可重放**（与 statemachine 同一做法）。

    同状态 → True（幂等：前端双击不该炸）。未知状态 → False。
    """
    if frm == to:
        return frm in ALLOWED_TRANSITIONS
    return to in ALLOWED_TRANSITIONS.get(frm, set())


def _cli() -> None:
    cases = [
        # 取自 verify_schema.CASES 的 C11 —— 这条是本文件存在的理由
        ("C11 自伤", "他最近好像不太喜欢那个旋转的玩具，看到就走开，然后会去咬自己的袖子。"),
        ("C3 攻击", "早上出门前他不肯穿鞋，说要自己穿，我帮他他就打我的手，只好由着他光脚。"),
        ("C4 感官", "超市里人太多了，他突然蹲下来捂住耳朵，怎么哄都不行，最后我们只能回家。"),
        ("纯正常语料", "今天挺好的，他在客厅玩的时候主动跑过来指着我手里的书给我看。"),
        ("发育倒退", "他原来会自己吃饭，最近突然不会了，叫他也不理。"),
        ("自伤+撞头", "他生气时会咬自己手腕，还会撞墙。"),
        ("空文本", "   "),
    ]
    for name, text in cases:
        r = scan(text)
        mark = "★" if r.triggered else " "
        print(f"{mark} {name:12s} triggered={str(r.triggered):5s} "
              f"highest={r.highest or '-':6s} codes={[h.code for h in r.hits]}")
        for h in r.hits:
            print(f"     · [{h.severity}] {h.group} — 命中片段「{h.quote}」")

    print("\n--- 家长可见文案示例 ---")
    print(scan("他会咬自己的手，打人，最近老是发呆几秒叫不应。").render())
    print("\n--- 正常语料 ---")
    print(scan("今天挺好的，他主动跑过来指给我看。").render())

    # ---------------- Phase 2 自检 ----------------
    # 本文件是 12 个离线回归之一，所以这些断言在这里也要成立。
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")

    print("\n--- Phase 2：转诊指引 ---")
    missing = [r["code"] for r in RULES
               if not all((r["referral"].get(k) or "").strip() for k in ("who", "do", "avoid"))]
    check(f"★ 全部 {len(RULES)} 条规则都有 who/do/avoid", not missing, missing)

    g1 = guidance_for(scan("他会咬自己的手，最近老是发呆几秒叫不应。"))
    check("guidance 逐 code 给（自伤 + 身体两项 urgent）",
          len(g1["items"]) == 2, [i["code"] for i in g1["items"]])
    check("★ summary 仍是一句 str（老 checkpoint / 老前端读的就是这个键）",
          isinstance(g1["summary"], str) and g1["summary"], g1["summary"])
    check("emergency 是布尔不是字符串（前端要靠它决定要不要整条变红）",
          g1["emergency"] is True)
    check("每项都带 ask（家长要答的问题）", all(i["ask"] for i in g1["items"]))

    g2 = guidance_for(scan("他今天不太配合。"))
    check("没有 urgent 时 guidance 为空", g2["items"] == [] and g2["summary"] == "")

    print("\n--- Phase 2：落库行与往返 ---")
    rep = scan("他生气时会咬自己手腕，还会撞墙。")
    rows = rows_for(rep)
    check("rows_for 出的是纯 dict（checkpoint 只存 dict）",
          all(isinstance(r, dict) for r in rows) and len(rows) == 2, len(rows))
    back = report_from_dump({"hits": [h.dump() for h in rep.hits]})
    check("dump → from_dump 往返等价", back.hits == rep.hits)
    old = report_from_dump({"hits": [{"code": "self_injury", "severity": "urgent",
                                      "group": "自伤", "quote": "q", "ask": "a"}]})
    check("★ Phase 2 之前的老 dump 仍能还原（没有 referral 字段）",
          old.urgent_hits and old.urgent_hits[0].code == "self_injury")

    print("\n--- Phase 2：状态流转 ---")
    for a, b in [("open", "acknowledged"), ("open", "closed"),
                 ("acknowledged", "closed"), ("acknowledged", "open"), ("closed", "open")]:
        check(f"{a} → {b} 允许", can_transition(a, b))
    check("★ closed → acknowledged 拒绝（没处理过就不算「已看到」）",
          not can_transition("closed", "acknowledged"))
    check("同状态幂等（前端双击不炸）", can_transition("open", "open"))
    check("未知状态拒绝", not can_transition("open", "done") and not can_transition("x", "open"))

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
