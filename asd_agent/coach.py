# -*- coding: utf-8 -*-
"""
教练 Agent —— 整个 demo 里最有戏的一个
=========================================
它做两件事，**不是 ReAct，是两步 pipeline**：

    Step 1  诊断：读最近的家长观察记录，找出抗拒/进步的信号，给出 impact 调整建议
    Step 2  产出：把选定活动写成一张家长能照着做的卡片

为什么这个 Agent 值得单独讲
---------------------------
**教练的价值不在于生成活动（那是检索的活），而在于生成"退路"。**
家长最痛的不是"今天做什么"，而是"孩子不配合了怎么办"——
`if_child_refuses` 这一节是整个 demo 的情感落点。
讲清楚"为什么不做 RAG 也得有一个 Agent"，比讲 RAG 有力得多。

承重字段 vs 装饰字段
-------------------
Step 1 的 `CoachAnalysis` 本身就是**可见推理链**：信号 → 解释 → 影响，
每条信号都要能指回原始家长输入的连续片段（`evidence_quote`，可回指校验）。
`free_reasoning` 是**装饰字段**：不参与决策、不校验、7B 写得不好也不影响任何逻辑。
把 LLM 输出分成"承重"和"装饰"两类，这样 7B 的短板就不会污染系统正确性 —— 这条值得在面试时讲。

★ 诊断结论不直接生效
--------------------
模型只给 `impact: ease/drop/swap`，**真正的调整由 Python 执行**：
`ease` 换成同能区低一档难度的活动、`drop` 今天不出卡片、`swap` 换候选表里的另一条。
理由和状态机一样：模型说"应该简单一点"，至于"简单一点"具体是哪一条活动，
那是可判定的，让 7B 自己去候选表里挑就等于把正确性交给了它。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import llm
from activities import (
    Activity,
    PreferenceSet,
    PlanRequest,
    ScoredActivity,
    preferred_hooks,
    render_preferences,
)
from vocab import (ABILITY_DOMAIN, ANTECEDENT, BEHAVIOR, CONSEQUENCE, FUNCTION_CN,
                   SCENE, function_of)

Impact = Literal["keep", "ease", "drop", "swap"]


# ================================================================== Step 1 输出
class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["refusal", "progress", "schedule_conflict", "sensory", "none"]
    obs_id: str = Field(
        default="", max_length=8,
        description="这条信号依据的是上面哪一条观察记录（填它的编号，如 o2）。"
                    "填了才能拿它去核对功能分类是否与 ABC 一致",
    )
    function: Literal["tangible", "attention", "escape", "sensory", "unclear"] = Field(
        default="unclear",
        description="这个行为的功能：得到实物／得到注意／逃离任务／得到感觉刺激。"
                    "系统会拿它和你引用那条观察的 consequence 核对，对不上以 ABC 为准",
    )
    evidence_behavior: str = Field(
        default="", description=f"BEHAVIOR 词表 code；确实指不出具体行为就填空串"
    )
    evidence_quote: str = Field(
        default="",
        description="**原始家长输入里的连续片段，逐字复制**。这是可校验的推理链，"
                    "编造会在下面被逐字匹配拦下 —— 那时这条信号会被作废。",
    )
    interpretation: str = Field(max_length=50, description="≤50字，这条观察说明什么")
    impact: Impact = Field(
        description="对今天计划的调整：keep=照常 / ease=降低难度 / "
                    "swap=换一条活动 / drop=今天先不做"
    )

    @model_validator(mode="after")
    def _behavior_in_vocab(self) -> "Signal":
        if self.evidence_behavior and self.evidence_behavior not in BEHAVIOR:
            raise ValueError(
                f"evidence_behavior {self.evidence_behavior!r} 不在 BEHAVIOR 词表里"
            )
        return self


class CoachAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant_obs_ids: list[str] = Field(default_factory=list, max_length=8)
    signals: list[Signal] = Field(min_length=1, max_length=4)
    # ★ 装饰字段：不参与任何决策、不校验、不回灌到下游。
    free_reasoning: str = Field(default="", max_length=300)


# ================================================================== Step 2 输出
class CoachStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(ge=1, le=6)
    do: str = Field(description="家长做什么，祈使句")
    say: str | None = Field(default=None, description="家长说的话，原样给出；不需要说就填 null")
    wait_sec: int = Field(ge=1, le=60, description="说完之后等几秒。ESDM 的关键参数")
    if_ok: str = Field(max_length=40, description="孩子做到了就怎么奖励")
    if_not: str = Field(max_length=40, description="没做到怎么补救，一句")


class RefusalScript(BaseModel):
    """孩子不配合时的退路。**整个 demo 的情感落点。**"""

    model_config = ConfigDict(extra="forbid")

    recognize: str = Field(
        description="先认出他的情绪，不要直接下指令。"
                    "孩子抗拒时最没用的一句话是『别哭了』",
    )
    reduce_demand: str = Field(description="把要求降到几乎不可能失败的那一步")
    choices: list[str] = Field(min_length=2, max_length=3, description="给他选，最多 3 个")
    abandon_signal: str = Field(
        description="什么情况下今天就到此为止。**明确放弃比硬扛更能保住长期坚持**",
    )


class CoachCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str
    title: str
    total_minutes: int = Field(ge=2, le=40)
    why_today: str = Field(max_length=80, description="为什么今天给他做这个，家长视角")
    steps: list[CoachStep] = Field(min_length=3, max_length=6)
    prompt_ladder: list[str] = Field(max_length=4, description="从轻到重的提示阶梯")
    if_child_refuses: RefusalScript
    success_check: list[str] = Field(min_length=1, max_length=3)
    record_prompt: str = Field(max_length=60, description="结束后请家长记录什么，一句话")

    def render(self) -> str:
        L = [f"【今天做】{self.activity_id} {self.title}（{self.total_minutes} 分钟）",
             f"  为什么：{self.why_today}", "  步骤："]
        for s in self.steps:
            say = f'说“{s.say}”' if s.say else "不说话"
            L.append(f"    {s.step_id}. {s.do} → {say}，等 {s.wait_sec} 秒")
            L.append(f"       做到了：{s.if_ok}")
            L.append(f"       没做到：{s.if_not}")
        if self.prompt_ladder:
            L.append("  提示阶梯（从轻到重）：")
            L += [f"    {i}. {p}" for i, p in enumerate(self.prompt_ladder, 1)]
        r = self.if_child_refuses
        L += ["  ★ 如果他不配合：",
              f"    先认：{r.recognize}",
              f"    降要求：{r.reduce_demand}",
              f"    给他选：{' / '.join(r.choices)}",
              f"    什么时候放弃：{r.abandon_signal}"]
        L.append(f"  怎么算成功：{'、'.join(self.success_check)}")
        L.append(f"  做完记一下：{self.record_prompt}")
        return "\n".join(L)


# ================================================================== 输入
class ObservationInput(BaseModel):
    """喂给诊断步骤的一条家长观察。

    ★ `raw` 必须是**整段家长原话**（同一次输入产生的多条 ObservationInput 共用同一段），
    不是单条事件的引文 —— 见 `observe.to_coach_inputs()` 的说明。

    ABC 字段全部带默认值，所以现存的 SAMPLE_OBS / PARENT_TODAY 一行都不用改。
    这不是偷懒：诊断在没有 ABC 时（手写输入、旧数据）必须仍能跑，
    否则加字段就会把老调用点全变成非法参数。
    """

    model_config = ConfigDict(extra="forbid")

    obs_id: str
    date: str = ""
    raw: str = ""                  # ★ 家长原话，回指校验的基准
    scene: str = ""
    behavior: str = ""
    # ---- ABC + 量化（接上 observe.py 之后才有值）----
    antecedent: str = ""
    consequence: str = ""
    intensity: int | None = None
    duration_sec: int | None = None
    confidence: float = 0.5
    function: str = "unclear"      # ★ Python 从 consequence 算的，不给模型说
    note: str = ""

    def abc_line(self) -> str:
        """渲成一行中文给 prompt 看。code 不直接给模型，先翻成人话。"""
        if not (self.antecedent or self.consequence):
            return ""
        a = ANTECEDENT.get(self.antecedent, self.antecedent) or "未记录"
        c = CONSEQUENCE.get(self.consequence, self.consequence) or "未记录"
        return f"前因：{a}  →  后果：{c}（这个行为替他解决的问题：{FUNCTION_CN.get(self.function, '说不清')}）"


class CoachOutcome(BaseModel):
    """一次教练调用的完整产物。"""

    model_config = ConfigDict(extra="forbid")

    card: CoachCard
    analysis: CoachAnalysis | None = None
    activity: Activity | None = None
    degraded: bool = False
    adjustment: str = ""            # 实际做出的调整（keep/ease/drop/swap 的落地说明）
    traces: list[llm.LLMTrace] = Field(default_factory=list)
    unverified_quotes: list[str] = Field(default_factory=list)

    def render_trace(self) -> str:
        return " | ".join(f"[{i + 1}/{len(self.traces)}] {t.brief()}"
                          for i, t in enumerate(self.traces))


# ================================================================== prompts
# 行为词表渲染成给模型对齐用的清单。**必须在 f-string 之前定义** ——
# 否则 f-string 里拿到的是空串，而且不会报错，只是提示词悄悄少了一块。
BEHAVIOR_CN_BLOCK = "\n".join(f"- {k} = {v}" for k, v in BEHAVIOR.items())

_ANALYSIS_SYSTEM = f"""你是一名自闭症儿童干预经验丰富的行为分析师。
你在读家长最近记下的一些事情，找出**会影响今天怎么安排**的信号。

你可以用的行为分类（evidence_behavior 必须是其中之一，或者留空）：
{BEHAVIOR_CN_BLOCK}

【四条规则】
1. **evidence_quote 必须逐字复制家长原话里的连续片段。**
   系统会拿它去原文里做逐字匹配，对不上这条信号就作废了。
   宁可留空字符串，也不要自己"总结"一句看起来合理的话。

2. **只报真正会影响今天安排的信号，最多 4 条。**
   "今天他吃了半碗饭"不是信号 —— 它不改变今天的干预安排。
   "他一看到蜡笔就把脸转过去"才是。

3. **impact 要克制。**
   - keep：绝大多数情况都是这个。观察只是背景，不影响安排
   - ease：他今天状态明显不好（累、生病、刚发生过冲突），今天降到简单一档
   - swap：这条活动明显踩到了他的雷，换一条
   - drop：今天真的不适合做（比如刚闹过一场、情绪还没平）
   把"孩子今天不太高兴"直接判成 drop 是过度反应，会让计划天天取消。

4. **每条信号都要指出它依据的是哪一条观察（填它的 obs_id）。**
   不填 obs_id 的信号无法被系统核对，等于凭空推断。

5. **function 填"这个行为替他解决了什么"，必须与该观察的 ABC 一致。**
   记录里已经给了每条观察的"这个行为替他解决的问题"，你照着填就行 ——
   系统会拿它和你引用的那条观察做核对，**对不上时以 ABC 为准并改掉你的说法**。
   你自由发挥在这里只会白费：7B 很爱在自由文本上把任何事情都说成"逃离任务"。

6. **不要诊断，不要评价家长。**
   你在描述观察说明了什么，不是在评判谁做得不对。"""


def _analysis_user(observations: list[ObservationInput], cands: list[ScoredActivity],
                   recent_summary: list[dict] | None = None) -> str:
    lines = ["【家长最近的观察记录（系统已结构化，你不必自己从头读一遍）】"]
    for o in observations:
        lines.append(f"[{o.obs_id}] 场景:{SCENE.get(o.scene, o.scene) or '未记录'}"
                     f" 行为:{BEHAVIOR.get(o.behavior, o.behavior) or '未记录'}"
                     + (f" 强度{o.intensity}/5" if o.intensity else "")
                     + (f" {o.duration_sec}秒" if o.duration_sec else ""))
        abc = o.abc_line()
        if abc:
            lines.append(f"  {abc}")
        if o.raw:
            lines.append(f"  原话：{o.raw}")
    if not observations:
        lines.append("（今天没有新的观察记录 —— 这本身就是一条信息：他可能还没开始练，"
                     "或者上一条记录还在。impact 就选 keep。）")
    if recent_summary:
        # ★ 这是教练 Agent 第一次能读到"方向"，而不只是计数。
        #   之前记忆切片只有 refusal_counts / recent_scenes / target_states
        #   三个数字，教练只能看见"他昨天抗拒了 gro-007"，
        #   看不见"精细动作连续三天没进展"这种整体判断。
        lines += ["", "【最近几天的日结（这是系统对上几天的整体判断）】"]
        for s in recent_summary[-3:]:
            for d in s.get("domain_updates", []):
                cn = ABILITY_DOMAIN.get(d.get("domain", ""), d.get("domain", ""))
                arrow = {"up": "在往上走", "flat": "和平时差不多",
                         "down": "在往下走"}.get(d.get("direction", ""), "")
                lines.append(f"- {s.get('date','')} {cn} {arrow}"
                             f"：{d.get('rationale','')}")
            for f in s.get("flags", []):
                lines.append(f"- {s.get('date','')} ⚠ {f}")
            if s.get("parent_note"):
                lines.append(f"- {s.get('date','')} 给家长的上一天总结：{s['parent_note']}")
    lines += ["", "【今天原本打算做的活动】"]
    for s in cands[:5]:
        lines.append(f"- {s.activity.brief()}  || {s.activity.title}")
    lines += ["", "只从上面的观察记录里找信号。逐条判断它对今天的影响。"]
    return "\n".join(lines)


_CARD_SYSTEM = """你是一名自闭症儿童干预教练。你在给一位家长写一张**他今天照着就能做**的卡片。

【这张卡片真正的重点】
家长最痛的不是"做什么"，而是**"孩子不配合了怎么办"**。
所以 if_child_refuses 这一节要写得好 —— 它比步骤更重要。

【步骤怎么写】
- do：家长做什么，祈使句。"把车放在他看得见够不到的地方"
- say：家长**原样说的话**。不需要说话就填 null。
  ★ 不要写"告诉他该怎么做"这种没有台词的说明，要写出那句台词本身
- wait_sec：说完之后等几秒。**这是 ESDM 的核心，不要写成 0。**
  等待时间越短越有效 —— 大部分家长失败是因为等不够久就替他做了
- if_ok / if_not：一句话。做到了怎么收，没做到怎么补

【不配合退路怎么写】
- recognize：**先认出他的情绪**，不要直接下指令。
  孩子正在崩溃时，一句"别哭了"会让事情更糟。先说出来："你还想玩是不是"
- reduce_demand：把要求降到**他几乎不可能失败**的那一步。
  不是"小声一点再试一次"，而是"这次只要他看一眼车就行"
- choices：给他 2-3 个可以选的东西。给选择权能显著减少对抗
- abandon_signal：**明确说出什么情况下今天就到此为止**。
  家长需要知道"什么时候可以合法地放弃"。
  明确放弃比硬扛更能保住长期坚持 —— 写清楚这一点很重要

【不要做的事】
- 不要写"根据评估报告显示……"这类专业话术，家长不是他的治疗师
- 不要承诺效果，不要说"这能提高他的语言能力"
- 步骤不要超过 6 条，家长没耐心读更多"""


def _card_user(activity: Activity, analysis: CoachAnalysis | None,
               preferences: PreferenceSet, signals_note: str) -> str:
    a = activity
    hooks = preferred_hooks(preferences, a)
    lines = [
        f"【今天要做的活动】{a.activity_id} {a.title}",
        f"  能区：{'/'.join(ABILITY_DOMAIN[d] for d in a.domains)}",
        f"  目标技能：{'、'.join(a.target_skill_items)}",
        f"  时长：{a.total_min} 分钟",
        f"  准备（{a.prep_min} 分钟）：{'；'.join(a.setup) or '不需要准备'}",
        f"  材料：{'、'.join(m.name for m in a.materials) or '不需要任何材料'}",
        f"  成功判据：{'、'.join(a.success_criteria)}",
        f"  提示阶梯（供你参考，可以改写得更口语）：{' → '.join(a.prompt_hierarchy)}",
        f"  出错处理：{a.error_correction}",
    ]
    if hooks:
        lines += ["", "★ 这条活动上可以挂的「动机钩子」（孩子喜欢的东西，用它当诱饵）：",
                  *[f"    - {h}" for h in hooks]]
    if signals_note:
        lines += ["", "【诊断结果，会影响你写步骤的方式】", signals_note]
    if preferences.free_notes or preferences.free_texts():
        lines += ["", render_preferences(preferences)]
    return "\n".join(lines)


# ================================================================== 后置校验
def _verify_quotes(analysis: CoachAnalysis, observations: list[ObservationInput]
                   ) -> tuple[CoachAnalysis, list[str]]:
    """回指校验。**作废而不是丢弃。**

    校验不过的信号降级成 `kind="none", impact="keep"` ——
    它不再影响今天的安排，但仍出现在推理链里（让人看得见模型"想说什么"）。
    丢弃整条信号会让人以为模型没看到那条观察。
    """
    corpus = "\n".join(o.raw for o in observations)
    obs_by_id = {o.obs_id: o for o in observations}
    unverified: list[str] = []
    fixed: list[Signal] = []
    for s in analysis.signals:
        # ---- 功能一致性：与它引用的那条观察的 ABC 核对 ----
        # ★ 自动纠正而不是报错。ABC 是结构化且回指校验过的，
        #   模型的功能判断是自由文本 —— 冲突时以 ABC 为准是确定性的，
        #   不需要问模型"你确定吗"。同一理由见 capabilities.py 的钳制先例。
        s = _align_function(s, obs_by_id)

        if llm.check_quote(corpus, s.evidence_quote):
            fixed.append(s)
            continue
        unverified.append(s.evidence_quote)
        fixed.append(s.model_copy(update={
            "kind": "none", "impact": "keep", "evidence_quote": "",
            "interpretation": (s.interpretation + "（引文未通过原文核对，已作废）")[:50],
        }))
    return analysis.model_copy(update={"signals": fixed}), unverified


def _align_function(sig: Signal, obs_by_id: dict[str, ObservationInput]) -> Signal:
    """把 `Signal.function` 纠正成它引用的观察的 ABC 功能。

    这条校验是接线后最值钱的一段 Python：没有它，`Observation.consequence`
    只是一个被渲进 prompt 的装饰字段，模型该怎么说还怎么说；
    有了它，ABC 才真正**承重** —— 系统能挡住模型在自由文本上的过度发挥。
    """
    o = obs_by_id.get(sig.obs_id) if sig.obs_id else None
    if o is None:
        return sig
    truth = function_of(o.consequence) if o.consequence else "unclear"
    if truth == "unclear" or sig.function in ("unclear", truth):
        return sig
    return sig.model_copy(update={
        "function": truth,
        "interpretation": (sig.interpretation + f"（功能已按 ABC 纠正为"
                           f"{FUNCTION_CN.get(truth, truth)}）")[:50],
    })


def _pick_adjustment(analysis: CoachAnalysis | None, cands: list[ScoredActivity],
                     today_id: str) -> tuple[ScoredActivity | None, str]:
    """**把 impact 落成具体的活动选择。这段是 Python，不是模型。**

    模型说"应该简单一点"，但"简单一点"到底是哪一条活动，是可判定的：
    在候选表里找同能区、难度更低的那条。

    ★ 返回的是**选好的那一条**，不是候选表。
    早先返回整个列表、由调用方再按 today_id 去取，结果 swap 根本没换 ——
    调用方拿着原封不动的列表按原 id 去取，拿到的还是原来那条，
    而日志里却打印着"已换成 fin-004"，**看起来换成功了**。
    "返回菜单让调用方挑"在这里是错的抽象：换活动这件事必须在这里就换完。
    """
    if not cands:
        return None, ""
    cur = next((s for s in cands if s.activity.activity_id == today_id), cands[0])
    a = cur.activity
    impact = max((s.impact for s in analysis.signals),
                 key=lambda i: _IMPACT_RANK[i], default="keep") if analysis else "keep"

    if impact == "drop":
        return None, "诊断判定今天不适合做（drop）——已跳出发卡环节"

    if impact == "ease":
        easier = [s for s in cands
                  if s.activity.activity_id != a.activity_id
                  and s.activity.difficulty < a.difficulty
                  and set(s.activity.domains) & set(a.domains)]
        if easier:
            pick = min(easier, key=lambda s: (a.difficulty - s.activity.difficulty, -s.score))
            return pick, (
                f"诊断判定今天状态不佳（ease）——{a.activity_id} 难度{a.difficulty} "
                f"换成同能区更简单的 {pick.activity.activity_id}")
        return cur, ("诊断判定今天状态不佳（ease）——但候选表里没有难度更低的活动，"
                     f"保持 {a.activity_id}（它已经是难度{a.difficulty}）")

    if impact == "swap":
        alts = [s for s in cands if s.activity.activity_id != a.activity_id]
        if alts:
            return alts[0], (f"诊断判定这条活动踩雷（swap）——"
                             f"{a.activity_id} 换成 {alts[0].activity.activity_id}")
        return cur, f"诊断判定需要换（swap）——但候选表里只有 {a.activity_id} 一条，保持原样"

    # ---- 功能驱动的加强规则（接线后的核心收益）----
    # 症状驱动的不足：ease 只看"他今天状态不好"，swap 只看"踩雷了"。
    # 有了 ABC 才能问出**为什么**抗拒 —— 而答案直接指向该换什么。
    #
    # ★ 只能收紧，不能放松：模型已经判了 drop 的分支在上面就 return 了，
    #   到这里的 impact 必然是 keep，所以只会把它往更保守的方向推。
    funcs = {sg.function for sg in (analysis.signals if analysis else [])
             if sg.function != "unclear"}

    if "escape" in funcs:
        # 行为的功能是**逃离任务** → 要求越难越会启动这个行为。
        # 这是教科书答案，但现在系统自己能给。
        easier = [x for x in cands
                  if x.activity.activity_id != a.activity_id
                  and x.activity.difficulty < a.difficulty
                  and set(x.activity.domains) & set(a.domains)]
        if easier:
            pick = min(easier, key=lambda x: (a.difficulty - x.activity.difficulty, -x.score))
            return pick, (f"ABC 显示孩子是在用这个行为**逃离任务**"
                          f"（{FUNCTION_CN['escape']}）——难度{a.difficulty}越高越会触发它，"
                          f"换成更简单的 {pick.activity.activity_id}")
        return cur, (f"ABC 显示孩子是在**逃离任务**——但候选表里没有难度更低的活动，"
                     f"保持 {a.activity_id}，改在卡片里把要求降下来")

    if "sensory" in funcs and a.sensory_load == "high":
        alts = [x for x in cands if x.activity.activity_id != a.activity_id
                and x.activity.sensory_load != "high"]
        if alts:
            return alts[0], (f"ABC 显示孩子是在**获取感觉刺激**"
                              f"（{FUNCTION_CN['sensory']}）——而 {a.activity_id} "
                              f"是高感官负荷，再做就是火上浇油，换成 {alts[0].activity.activity_id}")

    if "attention" in funcs:
        # 不改 impact：错误不在"做什么"，而在"家长怎么执行"。
        # 交给 Step 2 的一句话提醒，那才是它该出现的地方。
        return cur, (f"ABC 显示他在**获取注意**（{FUNCTION_CN['attention']}）——"
                     f"今天照常做，但完成后**不要用表扬和关注做奖励**，"
                     f"用他喜欢的东西")

    return cur, "诊断判定照常（keep）"


_IMPACT_RANK = {"keep": 0, "ease": 1, "swap": 2, "drop": 3}


# ================================================================== 无 LLM 兜底
def card_from_activity(activity: Activity, prefs: PreferenceSet = PreferenceSet(),
                       total_minutes: int | None = None) -> CoachCard:
    """纯数据兜底卡片。**活动定义里本来就带 steps / success_criteria /
    prompt_hierarchy / error_correction，所以"没有教练 Agent"的卡片是完整的。**

    这不是残次品 —— 它只是没有把"不配合怎么办"讲透。
    这一点值得在面试时讲：**降级路径的质量决定了这个系统在模型挂掉时
    到底还是不是个有用的工具**，而不只是"报了个错"。
    """
    hooks = preferred_hooks(prefs, activity)
    hook_note = f"（用他喜欢的{hooks[0][3:-1]}当诱饵）" if hooks else ""
    steps = []
    for s in activity.steps[:6]:
        steps.append(CoachStep(
            step_id=s.step_id, do=s.action, say=None,
            wait_sec=s.time_limit_sec or 5,
            if_ok="立刻给他想要的东西，然后结束这一轮",
            if_not=activity.error_correction[:40],
        ))
    return CoachCard(
        activity_id=activity.activity_id, title=activity.title,
        total_minutes=total_minutes or activity.total_min,
        why_today=activity.method_rationale[:80],
        steps=steps,
        prompt_ladder=activity.prompt_hierarchy[:4],
        if_child_refuses=RefusalScript(
            recognize="先说出他想要什么，不要下指令",
            reduce_demand=f"把要求降到最小{f'，比如只看他一眼{hook_note}' if hook_note else ''}",
            choices=["先做这个", "先不做了", f"换个地方{hook_note}" if hook_note else "换个地方"],
            abandon_signal="哭了超过 2 分钟、或连续两次都不理你，今天就到此为止，"
                           "这不算失败",
        ),
        success_check=activity.success_criteria[:3],
        record_prompt=record_prompt_for(activity, len(steps)),
    )


def record_prompt_for(activity: Activity, n_steps: int) -> str:
    """这张卡片结束后请家长填什么。**由活动定义生成，不再是一句通用话。**

    原先是一句静态的 `"做完记一下：他做到了几步、有没有不配合"`，
    它对 50 条活动全都一样，所以家长面对"翻书找东西"和"踢罐子进门"
    被问的是同一个问题。逐条反馈之后，家长端的输入是**结构化的**，
    问题就必须具体到这条活动的判据 —— 否则家长得自己把"做到几步"
    翻译成"这条活动的几步是什么意思"。

    三个字段对应 `feedback.ActivityFeedback`：
      结果    → activity.success_criteria[0]
      提示层级 → len(activity.prompt_hierarchy)，取自活动自己的阶梯
      做到几步 → n_steps（卡片上实际印了几步）

    ★ 判据只出现一次。早先的写法把它在整句里重复了两遍
      （"填「X」做没做到、X用了几级提示、几/N 步"），
      一条判据 20 字就撑爆了 CoachCard 的 max_length=60 ——
      "能在等待 5 秒内主动停下的比例明显提高"这种长判据直接 Pydantic 报错。
      现在判据截断到 18 字且只说一次，剩下的用字段名表达。
    """
    crit = (activity.success_criteria or ["完成"])[0]
    crit = crit if len(crit) <= 18 else crit[:17] + "…"
    n_lvl = len(activity.prompt_hierarchy[:4]) or 4
    return f"「{crit}」做没做到 · 用了几级提示(共{n_lvl}级) · 几步/{n_steps}"


def render_feedback_form(activity: Activity, n_steps: int) -> str:
    """家长端这一条活动的输入框。**CLI 版长这样，产品上它是一个卡片组件。**

    ★ 为什么"结果"是四个按钮而不是一个输入框：
      outcome 是状态机的直接输入（refused/skipped 都不进能力计算，
      success/partial 进），让它可枚举就消除了"把不配合说成没做完"这一整类错误。
      而"做到几步 / 用了几级提示"才是需要模型从自由文本里判断的部分 ——
      所以它们分成「结构化选项」+「选填补充」两栏，而不是全塞进一个文本框。
    """
    crit = (activity.success_criteria or ["完成这条活动"])[0]
    ladder = activity.prompt_hierarchy[:4] or ["不用提示", "手势提示", "示范", "动手辅助"]
    L = [f"  活动：{activity.activity_id} {activity.title}",
         f"  判断标准：{crit}",
         "",
         "  结果（点一个）：",
         "    [独立做到]  [提示一下能做到]  [只做到一部分]  [不配合]",
         "    [没做成 · 生病/太晚了]",
         "",
         "  用到第几级提示："]
    L += [f"    {i}. {p}" for i, p in enumerate(ladder, 1)]
    L += ["",
          f"  做到几步：____ / {n_steps}",
          "",
          "  补充（选填，可以只写一句）：",
          "    ________________________________",
          "",
          "  [跳过这条]"]
    return "\n".join(L)


# ================================================================== 主入口
def coach_today(
    cands: list[ScoredActivity],
    *,
    today_id: str | None = None,
    observations: list[ObservationInput] | None = None,
    preferences: PreferenceSet | None = None,
    req: PlanRequest | None = None,
    recent_summary: list[dict] | None = None,
    on_step=None,
) -> CoachOutcome:
    """今天的家长卡片。**两步 LLM 调用；任一步失败都返回一张完整可用的卡片。**

    recent_summary 是最近几天的日结（store.latest_summary）。
    ★ 它只喂给 Step 1 诊断，不喂给 Step 2 出卡片 —— 出卡片要的是
      "这条活动怎么做"，方向判断与它无关，混进去只会让 7B 写出
      "因为他社交在进步所以今天做这个"这种倒因为果的话。
    """
    observations = observations or []
    prefs = preferences or (req.preferences if req else PreferenceSet())

    if not cands:
        return CoachOutcome(
            card=CoachCard(
                activity_id="-", title="今天没有合适的活动",
                total_minutes=2, why_today="候选表是空的，说明时间、材料或难度的限制太紧",
                steps=[CoachStep(step_id=i, do=d, wait_sec=5,
                                 if_ok="继续", if_not="跳过")
                       for i, d in enumerate(
                           ["确认每天的时间预算至少 15 分钟",
                            "在材料清单里加上 2-3 样家里常有的东西",
                            "明天再试一次"], 1)],
                prompt_ladder=[],
                if_child_refuses=RefusalScript(
                    recognize="先不用管这个错误页", reduce_demand="今天什么都不做也可以",
                    choices=["明天再说", "先问问系统"], abandon_signal="随时"),
                success_check=["看到了这条提示"], record_prompt="什么都不用记"),
            degraded=True, adjustment="候选表为空")

    today_id = today_id or cands[0].activity.activity_id
    traces: list[llm.LLMTrace] = []
    analysis: CoachAnalysis | None = None
    unverified: list[str] = []

    # ---- Step 1 诊断 ----
    if observations:
        try:
            draft, t = llm.chat_json(
                system=_ANALYSIS_SYSTEM,
                user=_analysis_user(observations, cands, recent_summary),
                model_cls=CoachAnalysis, name="coach_analysis",
                stage="coach_analysis", max_tokens=1500,
                timeout_s=llm.TIMEOUT_S * 2, on_step=on_step,
            )
            traces.append(t)
            analysis, unverified = _verify_quotes(draft, observations)
        except llm.LLMError:
            analysis = None      # 诊断失败不阻断，直接按 keep 走

    # ---- impact 落地（Python 决定换哪条，返回的就是选好的那一条）----
    chosen, adjustment = _pick_adjustment(analysis, cands, today_id)
    if chosen is None:      # drop
        return CoachOutcome(card=card_from_activity(cands[0].activity, prefs),
                            analysis=analysis, degraded=True, adjustment=adjustment,
                            traces=traces, unverified_quotes=unverified)
    act = chosen.activity

    # ---- Step 2 卡片 ----
    note = ""
    if analysis:
        note = "\n".join(f"  · {s.kind}｜{s.interpretation} → 建议 {s.impact}"
                         for s in analysis.signals)
    try:
        draft2, t2 = llm.chat_json(
            system=_CARD_SYSTEM,
            user=_card_user(act, analysis, prefs, note),
            model_cls=CoachCard, name="coach_card",
            stage="coach_card", max_tokens=3000,
            temperature=0.3,       # 这步允许一点创造性，但仍以可判定为主
            timeout_s=llm.TIMEOUT_S * 3, on_step=on_step,
        )
        traces.append(t2)
        # 活动身份以检索为准，模型改写标题/时长一律不认
        card = draft2.model_copy(update={
            "activity_id": act.activity_id, "title": act.title,
            "total_minutes": act.total_min,
        })
        return CoachOutcome(card=card, analysis=analysis, activity=act,
                            adjustment=adjustment, traces=traces,
                            unverified_quotes=unverified)
    except llm.LLMError:
        return CoachOutcome(card=card_from_activity(act, prefs),
                            analysis=analysis, activity=act, degraded=True,
                            adjustment=adjustment, traces=traces,
                            unverified_quotes=unverified)


# ================================================================== 自检
SAMPLE_OBS = [
    ObservationInput(obs_id="o1", date="2026-10-16", scene="indoor_play",
                     behavior="refusal", antecedent="wants_item",
                     consequence="got_item", intensity=3, confidence=0.75,
                     raw="今天拿出小汽车他就哭，扔到地上不让碰，我自己玩了一会儿就好了。"),
    ObservationInput(obs_id="o2", date="2026-10-16", scene="mealtime",
                     behavior="joint_attention", antecedent="unclear",
                     consequence="unclear", intensity=1, confidence=0.7,
                     raw="吃饭的时候他突然指着窗户叫了一声，我看他指的方向是鸟。"),
]
for _o in SAMPLE_OBS:
    _o.function = function_of(_o.consequence)   # 真实链路里由 observe.py 填


def _cli() -> None:
    import os
    import store
    from activities import PlanRequest, candidates as retrieve_candidates

    conn = store.reset("/tmp/coach_demo.db")
    store._seed_demo(conn, "demo-001", confidence=0.85)
    prof = store.latest_profile(conn, "demo-001")
    pc = prof.parent_constraints
    st = store.current_states(conn, "demo-001")
    req = PlanRequest(
        levels=prof.levels(),
        target_states={a.domain: st.get(a.skill_item, {}).get("state", "not_started")
                       for a in prof.assessments},
        priority_domains=prof.domains_of_interest(),
        daily_minutes=pc.daily_minutes_available,
        materials_at_home=set(pc.materials_at_home),
        avoid_load_sensory=pc.avoid_load_sensory,
        preferences=pc.preferences,
    )
    cands = retrieve_candidates(req, top_k=5)

    print("=== 无 LLM 的兜底卡片（模型挂掉时家长看到的东西）===")
    out = CoachOutcome(card=card_from_activity(cands[0].activity, pc.preferences),
                       activity=cands[0].activity, degraded=True)
    print(out.card.render())
    print("\n  ★ 注意：这张卡片是完整的，缺的只是退路没讲透。")

    print("\n=== impact 落地逻辑（纯 Python，不调模型）===")
    from coach import CoachAnalysis, Signal
    for impact in ("keep", "ease", "swap", "drop"):
        an = CoachAnalysis(signals=[Signal(kind="none", interpretation="测试", impact=impact)])
        pick_s, adj = _pick_adjustment(an, cands, cands[0].activity.activity_id)
        pick = pick_s.activity.activity_id if pick_s else "（不出卡片）"
        print(f"  impact={impact:5s} → 选中 {pick}   {adj[:64]}")

    print("\n=== 回指校验：喂一条编造的引文 ===")
    an = CoachAnalysis(signals=[
        Signal(kind="refusal", evidence_behavior="refusal",
               evidence_quote="他明确说过讨厌这辆车",  # 原文里没有
               interpretation="他自己表达过反感", impact="drop"),
        Signal(kind="progress", evidence_behavior="joint_attention",
               evidence_quote="他突然指着窗户叫了一声",  # 原文里真实存在
               interpretation="他开始主动分享他看到的东西", impact="keep"),
    ])
    fixed, unv = _verify_quotes(an, SAMPLE_OBS)
    for s in fixed.signals:
        mark = "已作废" if s.impact == "keep" and s.kind == "none" else "保留"
        print(f"  [{mark}] kind={s.kind:8s} impact={s.impact:5s} {s.interpretation}")
    print(f"  未通过核对：{unv}")

    print("\n=== ★ 功能一致性：模型说的功能与 ABC 冲突时以 ABC 为准 ===")
    shared_raw = SAMPLE_OBS[0].raw       # 两条共用同一段原话（同一次输入）
    obs2 = [
        ObservationInput(obs_id="o1", scene="mealtime", behavior="tantrum",
                         antecedent="adult_demand", consequence="left_scene",
                         intensity=4, raw=shared_raw),
        ObservationInput(obs_id="o2", scene="mealtime", behavior="meltdown",
                         antecedent="adult_demand", consequence="got_extra_attention",
                         intensity=5, raw=shared_raw),
    ]
    an = CoachAnalysis(signals=[
        # 声称"功能是得到实物"，但它引用的 o1 记的是 left_scene → 功能是 escape
        Signal(kind="refusal", obs_id="o1", function="tangible",
               evidence_behavior="tantrum", evidence_quote="扔到地上不让碰",
               interpretation="他想要那个东西", impact="keep"),
        # 声称"功能是逃离"，但 o2 记的是 got_extra_attention → 功能是 attention
        Signal(kind="refusal", obs_id="o2", function="escape",
               evidence_behavior="meltdown", evidence_quote="扔到地上不让碰",
               interpretation="他被要求停下就崩", impact="keep"),
    ])
    fixed2, _ = _verify_quotes(an, obs2)
    for sg in fixed2.signals:
        print(f"  [{sg.obs_id}] 模型说 tangible/escape → 纠正为 {sg.function}")
        print(f"        {sg.interpretation}")
    assert fixed2.signals[0].function == "escape", "应按 o1 的 left_scene 纠正"
    assert fixed2.signals[1].function == "attention", "应按 o2 的 got_extra_attention 纠正"
    print("  ✓ 两条都按 ABC 纠正了 —— ABC 是承重字段，不是装饰")

    print("\n=== ★ 共享 corpus：多条观察共用整段原话时回指仍成立 ===")
    print(f"  两条 raw 长度 {len(obs2[0].raw)} / {len(obs2[1].raw)}，"
          f"内容相同={obs2[0].raw == obs2[1].raw}")
    print("  若 raw 填的是单条事件的引文，降级过的那条会被清空 → corpus 缺段")
    print("  → 教练的正常信号会被误判为编造")

    print("\n=== 功能驱动的加强规则 ===")
    print(f"  候选表里有 {len([x for x in cands if x.activity.sensory_load == 'high'])}"
          f" 条高感官活动")
    for fn in ("escape", "sensory", "attention"):
        an2 = CoachAnalysis(signals=[Signal(kind="none", obs_id="o1", function=fn,
                                            interpretation="测试", impact="keep")])
        pick, why = _pick_adjustment(an2, cands, cands[0].activity.activity_id)
        print(f"  function={fn:10s} → {pick.activity.activity_id if pick else '（无）'}")
        print(f"      {why[:78]}")
    print("  ★ 只收紧不放松：模型判 drop 的分支在函数规则之前就 return 了")

    conn.close()


if __name__ == "__main__":
    _cli()
