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
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------ 规则表
# severity 分三档，决定前端的呈现方式：
#   urgent —— 立刻提示，建议当天联系专业人员；不得静默通过
#   high   —— 需要追问细节，可能与已有判断冲突
#   watch  —— 记录下来，可能改变后续计划的权重
#
# 每条规则三部分：正则（匹配原文）、级别、以及命中后要问家长的话。
# 正则全部写成"动词+对象"的形式而不是孤立的名词 ——
# 匹配"咬自己"而不匹配"咬"，匹配"撞头"而不匹配"碰头"（那是玩闹）。
RULES: list[dict] = [
    # ---------------- 自伤：最高优先级 ----------------
    dict(code="self_injury", severity="urgent", group="自伤",
         pattern=r"(咬|啃|抓|掐|挠|撞|砸|撕)(自己|自已|自个)|自伤|伤自己|咬伤",
         ask="他咬/撞自己的时候，是什么时候开始的？当时手上有没有东西？"),
    dict(code="head_banging", severity="urgent", group="自伤",
         pattern=r"撞头|头撞|撞墙|撞门|用头(顶|撞)",
         ask="撞头的频率大概多久一次？有没有因此去过医院？"),
    # ---------------- 对他人的攻击 ----------------
    dict(code="aggression", severity="high", group="攻击",
         # "打我的手"是家长叙述自己被打的最常见句式，只写"打人"会整条漏掉
         pattern=r"打(我|人|他|她|小孩|孩子)|抓伤|掐(人|我|他|她)|咬人|咬我|踢人|推倒|扔东西砸|摔门砸",
         ask="他打人主要是在被要求停下的时候，还是没有明显前因？"),
    # ---------------- 走失 ----------------
    dict(code="elopement", severity="high", group="走失",
         pattern=r"走丢|走失|跑出去|冲出(家门|马路)|不见了|找不到孩子",
         ask="他跑出去过几次了？当时周围有没有车或者水？"),
    # ---------------- 感觉与身体 ----------------
    dict(code="pica", severity="urgent", group="进食",
         pattern=r"吃(泥|土|沙|石头|纸巾|卫生纸|玩具)|非食物|异食",
         ask="他吃的是哪些东西？有没有因此做过检查？"),
    dict(code="food_refusal", severity="high", group="进食",
         pattern=r"(完全)?拒食|不吃饭|滴水不进|只吃.{0,4}(几样|固定)",
         ask="现在一天大概吃多少？体重有没有变化？"),
    dict(code="seizure_sign", severity="urgent", group="身体",
         pattern=r"(发呆|失神|愣住|叫不应).{0,6}(几秒|一会)|抽搐|抽动|癫痫|翻白眼|口角流水",
         ask="这些情况持续多久？有没有发生过在车里或独自一人的时候？"),
    dict(code="sleep_regression", severity="watch", group="睡眠",
         pattern=r"整夜|整宿|整晚.{0,4}(醒|哭|闹)|睡眠.{0,2}(倒退|变差|变差)|突然.{0,4}不睡",
         ask="是从什么时候开始的？之前一直是什么样的？"),
    # ---------------- 行为倒退（提示要重查评估报告） ----------------
    dict(code="regression", severity="high", group="倒退",
         pattern=r"(突然|最近|这阵子).{0,6}(不会|退化|倒退|退步|变差)|原来会.{0,6}现在不会",
         ask="以前能做到、现在做不到的，具体是哪些事？大概是什么时候开始的？"),
    # ---------------- 疑似家暴 / 明显安全风险 ----------------
    dict(code="safety_risk", severity="urgent", group="安全",
         pattern=r"(打|骂|揍)他|掐他|关(在|进)(黑屋|厕所)|不给吃",
         ask="这些情况发生时，通常是谁在场？"),
    # ---------------- 医疗信号 ----------------
    dict(code="pain_signal", severity="high", group="身体",
         pattern=r"(一直|老是|经常)(哭闹|哼哼)|不明原因.{0,4}(哭|发热)|肚子疼|耳朵疼",
         ask="这些情况有没有和生病的时间对上？"),
]

_COMPILED = [(r["code"], r["severity"], r["group"], r["pattern"], r["ask"], re.compile(r["pattern"]))
             for r in RULES]


@dataclass(frozen=True)
class RedFlag:
    code: str
    severity: str      # urgent / high / watch
    group: str
    quote: str
    ask: str

    @property
    def triggered(self) -> bool:
        return True

    def render(self) -> str:
        tag = {"urgent": "【立即关注】", "high": "【需要追问】", "watch": "【记录观察】"}[self.severity]
        return f"{tag}{self.group}（{self.code}）：{self.quote}\n    建议追问：{self.ask}"


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
    for code, severity, group, pattern, ask, rx in _COMPILED:
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
                            quote=quote, ask=ask))

    order = {"urgent": 0, "high": 1, "watch": 2}
    hits.sort(key=lambda h: order[h.severity])
    return RedFlagReport(tuple(hits))


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


if __name__ == "__main__":
    _cli()
