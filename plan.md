自闭症儿童「家长干预教练」Agent — 面试 Demo 实现计划

 Context

 为什么做这个。 目标是 agent 开发岗的面试 demo，不是产品。核心叙事：把医生的评估报告转成家长每天在家能执行的干预计划，并根据每天反馈动态调整——展示个性化规划、RAG、长期记忆、多 Agent 协作这四件事。

 现状。 /Users/qiyusama/Documents/code/ganyu/asd_agent/ 已有 3 文件 774 行：
 - vocab.py (211行) — 五张受控词表 SCENE(24)/BEHAVIOR(45)/ANTECEDENT(30)/CONSEQUENCE(19)/PEOPLE_PRESENT(9)，有 vocab_block() 渲染进 prompt
 - schema.py (106行) — Observation Pydantic，用 Literal[tuple(VOCABS[...])] 把词表变成 enum 约束
 - verify_schema.py (457行) — A/B 实验，12 条真实家长语句。已得结论：json_schema 12/12 合规，json_object 0/12；且红旗检测必须做成独立规则前置扫描，塞进主 schema 反而削弱检出率。

 技术栈 Python 3.12 venv + OpenAI SDK → SiliconFlow + Qwen/Qwen2.5-7B-Instruct + pydantic。已有 .env。

 已确认的四个方向（用户选定）：
 1. 在 asd_agent 上扩展，不新建项目——每日 Observation 直接喂给教练 Agent，构成双向闭环
 2. 自建结构化活动库（~50 条，基于 NDBI/ESDM/ABA 公开教学元素）+ 本地结构化检索，不做 embedding
 3. 沿用 SiliconFlow + Qwen，所有结构化输出一律 response_format={"type":"json_schema"}
 4. FastAPI + 前端

 三条贯穿全局的决策：
 - 不引入 ReAct / tool-calling 循环。 7B 在 SiliconFlow 上多轮工具调用不可靠，且每次 5-15s，面试现场必崩。全部 Agent 是「一次受限解码调用 + Python 确定性编排」。
 - 计数交给 Python，语义交给 LLM。 这是已有红旗结论的推广：凡是可判定的（状态转移、时长预算、材料是否齐、activity_id 是否合法）全部硬规则。
 - 每条 LLM 路径都要有降级路径 + 延迟可见。 可靠性是 demo 的说服力来源。

 ---
 一、文件划分

 保持现有平铺风格（from schema import Observation 同级导入），不建包——demo 里 python server.py 一条命令跑起来最省事。

 asd_agent/
 ├── vocab.py            现有 211 行，不动。在末尾追加 4 张新词表
 ├── schema.py           现有 106 行，不动
 ├── verify_schema.py    现有 457 行，不动（它是 A/B 证据 + 回归集）
 ├── llm.py              新增 ~120  ★ 唯一的 LLM 出口
 ├── capabilities.py     新增 ~150  ★ 五能区词表 + AbilityProfile + 状态常量
 ├── profile.py          新增 ~200  ★ 评估报告 → AbilityProfile
 ├── activities.py       新增 ~180  ★ Activity Pydantic + retrieve()
 ├── activity_data.py    新增 ~600  ★ 50 条活动数据 + --validate 自检
 ├── planner.py          新增 ~180  ★ 规划 Agent
 ├── coach.py            新增 ~220  ★ 教练 Agent（唯一多步，最有戏）
 ├── evaluator.py        新增 ~170  ★ 评估 Agent
 ├── statemachine.py     新增 ~120  ★ 状态转移规则（纯函数）
 ├── redflag.py          新增 ~90   ★ 独立红旗扫描（沿用已有结论）
 ├── store.py            新增 ~230  ★ SQLite + 记忆查询
 ├── fixtures.py         新增 ~250  ★ 预置回放（现场救命）
 ├── server.py           新增 ~260  ★ FastAPI + SSE
 └── static/index.html   新增 ~450  ★ 单页前端（原生 JS，不构建）

 必须抽 llm.py。 现在 verify_schema.py:419 自己建 client、自带超时和计时；三个 Agent 各写一遍就是三份重试逻辑。

 def chat_json(*, system, user, model_cls: type[BaseModel], name: str,
               max_tokens=2000, temperature=0.0, retries=1) -> tuple[BaseModel, LLMTrace]
 LLMTrace = dataclass(latency_ms, model, prompt_tokens, completion_tokens, finish_reason, raw_content, attempt)。trace 一路冒泡到 API 响应，前端把它显示成"第 2 次重试 / 7.3s"——把延迟摆在 UI 上比说"我们做了缓存"强。另加
 warmup()（启动时预热连接）和 lru_cache 版的 chat_json_cached（规划 Agent 输入每天几乎不变）。

 verify_schema.py 里 quote_grounded 的子串校验逻辑抽成 check_quote(text, quote) -> bool，A/B 脚本和 demo 共用。

 ---
 二、能力画像

 追加到 vocab.py：
 ABILITY_DOMAIN = {"gross_motor":"大运动","fine_motor":"精细动作","language":"语言",
                   "cognition":"认知","social_adapt":"社会适应"}
 SKILL_ITEMS = {  # 每个能区 6-8 项细项
   "gross_motor":["独走稳","跑","上下楼梯","双脚跳","单脚站","踢球","骑滑步车"],
   "fine_motor":["抓握","捏取","涂鸦","串珠","翻书页","用剪刀","扣纽扣","搭积木塔"],
   "language":["听懂单词","听懂双指令","主动表达需要","两步指令","命名物体","扩展句子","回答是非问"],
   "cognition":["配对","分类","排序","记住三步指令","解决简单问题","因果理解","时间顺序"],
   "social_adapt":["眼神回应","共同注意","主动分享","轮流等待","接受帮助","适应新环境","情绪命名与表达"],
 }
 ABILITY_STATE = ["not_started","entry","practicing","mastered","generalizing"]
 MATERIAL_CLASSES = ("common_household","toy","food","paper","body_parts","wearable","app","printable")

 为什么 SKILL_ITEMS 必须是受控词表：画像是跨天 read-modify-write 的长期状态。模型自创"社交主动性"vs"共同注意"会让同一项在不同天变成两个 key，状态永远推不上去。与 schema.py 里消灭标签漂移
 同动机，只是从"单次抽取"升级到"跨天聚合"。

 capabilities.py 关键模型：
 class SkillAssessment(BaseModel):
     domain: Literal[tuple(ABILITY_DOMAIN)]
     skill_item: str
     level: int = Field(ge=0, le=5)   # 0不会 1全身协助 2部分协助 3能完成但慢 4独立 5泛化
     age_equivalent_months: int | None
     evidence_quote: str               # 报告原文连续子串；推断则空串
     source: Literal["report_stated","inferred"]
     confidence: float = Field(ge=0, le=1)

 class ParentConstraints(BaseModel):
     daily_minutes_available: int = Field(ge=5, le=180, default=30)
     materials_at_home: list[str]
     alone_only: bool = True
     known_refusals: list[str]
     avoid_load_sensory: bool = False

 class AbilityProfile(BaseModel):
     child_id: str; child_age_months: int; report_date: str | None
     assessments: list[SkillAssessment] = Field(min_length=1, max_length=40)
     priority_domains: list[...] = Field(max_length=3)
     parent_constraints: ParentConstraints
     overall_confidence: float
     needs_clarification: bool = False
     clarifying_question: str | None = None

 source 字段是关键。 报告常只写"语言发育落后"不给细项。正确行为是标 inferred + confidence < 0.4，而不是把"能听懂单词"当成报告事实。该字段直接作为状态机初始置信度的折扣项。

 profile.py 流程： ① redflag.py 前置扫报告 → ② 一次 LLM 调用，system 里塞 vocab_block('skill_items') → ③ check_quote 校验每条 evidence_quote → ④ 失败则 needs_clarification=True 且不给该 skill_item 分配 level。报告 >4000
 字先摘要再抽取，并在 UI 标明"已分段抽取"。半结构化表格不走第二条路径，同一 schema 即可，只在 prompt 里加"输入可能包含表格，注意跨行对齐"。

 ---
 三、活动库与检索

 activities.py 的 Activity 定义：
 class Material(BaseModel):
     name: str
     material_class: Literal[tuple(MATERIAL_CLASSES)]
     essential: bool = True     # False = 有更好，没有也能做

 class Step(BaseModel):
     step_id: int
     action: str                     # 家长做什么（祈使句）
     child_target_response: str
     time_limit_sec: int | None      # ESDM 关键：等待时间
     prompt_level: Literal["natural","gesture","verbal","model","physical"]

 class Activity(BaseModel):
     activity_id: str = Field(pattern=r"^[a-z]{2,4}-\d{3}$")   # "soc-014"
     title: str
     domains: list[...] = Field(min_length=1, max_length=2)
     target_skill_items: list[str] = Field(min_length=1)
     # 可执行性约束（硬过滤用）
     duration_min: int = Field(ge=2, le=20)
     sessions_per_day: int = Field(ge=1, le=3, default=1)
     materials: list[Material] = Field(min_length=1)
     adult_count: Literal[1] = 1        # 单一家长可执行，模板级保证
     setting: list[Literal["indoor_home","outdoor","anywhere"]]
     sensory_load: Literal["low","medium","high"] = "low"
     prep_min: int = Field(default=0)   # 准备时间，不计入 duration
     # 教学元数据
     method: Literal["NDBI","ESDM","ABA","PRT","RBT"]
     method_rationale: str
     difficulty: Literal[1,2,3]         # 1入门 2练习 3泛化
     prerequisites: list[str]           # 其他 activity_id
     prerequisite_skills: list[str]
     # 教练 Agent 的原料
     setup: list[str] = Field(min_length=1, max_length=3)
     steps: list[Step] = Field(min_length=3, max_length=8)
     success_criteria: list[str] = Field(min_length=1, max_length=3)
     prompt_hierarchy: list[str]         # 提示层级从少到多（ESDM 核心）
     error_correction: str               # 最少提示原则
     generalization_cues: list[str]
     # 与行为观察的接口（双向闭环关键）
     success_behaviors: list[str]        # BEHAVIOR 词表 code
     refusal_behaviors: list[str]        # 出现即判为抗拒
     linked_scenes: list[str]            # SCENE 词表 code

 success_behaviors / refusal_behaviors / linked_scenes 直接存 vocab.py 现有词表的 code——这是"计划 ↔ 行为记录"闭环的接缝，活动库和 Observation 共享同一套受控词表。

 检索：结构化硬过滤 + 加权打分。不是 embedding。
 @dataclass
 class PlanRequest:
     ability_profile; target_states: dict[str,str]; daily_minutes: int
     recent_activity_ids: list[str]; refusal_counts: dict[str,int]
     success_counts: dict[str,int]; materials_at_home: set[str]
     avoid_load_sensory: bool

 @dataclass
 class ScoredActivity:
     activity: Activity; score: float
     hard_filter_reason: str | None   # 被剔除时保留原因，供 UI 解释
     breakdown: dict[str, float]      # 分项得分，供"为什么推荐这个"

 硬过滤（剔除但保留原因字符串）：

 ┌─────────────────────────────────────────────────────────────┐
 │                            条件                             │
 ├─────────────────────────────────────────────────────────────┤
 │ duration_min * sessions_per_day + prep_min <= daily_minutes │
 ├─────────────────────────────────────────────────────────────┤
 │ 所有 essential=True 材料都在 materials_at_home 中           │
 ├─────────────────────────────────────────────────────────────┤
 │ refusal_counts.get(id,0) >= 2（连续抗拒 2 次进冷却期）      │
 ├─────────────────────────────────────────────────────────────┤
 │ prerequisites 全部已成功过                                  │
 ├─────────────────────────────────────────────────────────────┤
 │ 技能 level < 2 时要求 difficulty == 1                       │
 ├─────────────────────────────────────────────────────────────┤
 │ avoid_load_sensory → 剔除 sensory_load == "high"            │
 └─────────────────────────────────────────────────────────────┘

 打分公式（归一到 1.0，breakdown 保留原始分）：
 score = 0.30*w_state_match + 0.20*w_skill_gap + 0.15*w_prereq_ready
       + 0.10*w_material_fit + 0.10*w_diversity + 0.10*w_method_fit + 0.05*w_parent_pref
 - w_state_match：not_started/entry→difficulty 1，practicing→2，mastered/generalizing→3，匹配 1.0 否则 0.4。状态机直接改变检索结果，而不是只改变显示。
 - w_skill_gap：min(1.0, Σ clamp(1 - level/5 + 0.3, 0, 1.5) / 3.0)
 - w_diversity：1 - min(1, success_counts.get(id,0)/4)，防腻
 - w_parent_pref：linked_scenes 与家长最近高频 Observation.scene 有交集 → 1.0（来自记忆库）
 - known_refusals 命中 title 或材料名 → 直接 -1.0（负分，保留在榜上用于解释"因为您说抗拒XX所以没推"）

 为什么不上 embedding——这是面试加分点：50 条、字段高度结构化、检索维度全是可判定的。embedding 只会带来无谓的模型下载，且覆盖不了 essential=False 这种硬约束。真正的检索价值在结构化过滤的组合爆炸。"我知道什么时候不该上
 RAG"本身就是能力展示。

 另提供 explain_retrieval(req, activity) -> str，每张活动卡下方显示"因为：社会适应/共同注意 当前入门级 · 材料全部在家 · 上周未练过"。这是整个 demo 最打动人的一行字。

 activity_data.py 用 ACTIVITIES: list[dict] 字面量（便于单独 review），load_activities() 校验并缓存。配 python activities.py --validate：检查 prerequisites 引用存在、target_skill_items 在 SKILL_ITEMS 内、success/refusal
 behaviors 和 linked_scenes 在词表内、无自环依赖。这条自检挡掉 90% 数据手误。

 ---
 四、三个 Agent

 ┌───────────┬────────────────────────────────────────────┬───────────────────┬────────────────────────────────────┐
 │   Agent   │                    职责                    │     LLM 调用      │                工具                │
 ├───────────┼────────────────────────────────────────────┼───────────────────┼────────────────────────────────────┤
 │ Planner   │ 画像+状态+记忆 → 一周结构化日程            │ 1 次              │ retrieve()                         │
 ├───────────┼────────────────────────────────────────────┼───────────────────┼────────────────────────────────────┤
 │ Coach     │ "今天做什么" → 家长照做的话术卡片 + 日调整 │ 2 次（诊断→产出） │ retrieve(), store.get_recent_obs() │
 ├───────────┼────────────────────────────────────────────┼───────────────────┼────────────────────────────────────┤
 │ Evaluator │ Observation + 成功判据 → 日评估 + 转移建议 │ 1 次              │ propose_transition()               │
 └───────────┴────────────────────────────────────────────┴───────────────────┴────────────────────────────────────┘

 关键边界：状态转移不由 Evaluator 决定。 Evaluator 只产出"证据+建议"，Python 规则落地。

 Planner

 输入 PlanInput(ability_profile, current_states, weekly_recap, refusal_counts, days=7)
 输出 WeeklyPlan(plan_id, days: list[DailyBlock], excluded_activity_ids, recap)，其中
 class PlannedActivity(BaseModel):
     activity_id: str          # 必须来自候选列表
     slot: Literal["morning","afternoon","evening","anytime"]
     minutes: int = Field(ge=2, le=20)
     why_this: str             # ★ 一句中文家长视角，≤40字

 候选由 Python 检索，LLM 只做编排+解释。 生成后 Python 校验 activity_id 必须在候选表内，不在则替换为检索第一名并记入 excluded_activity_ids。失败时降级为 greedy_fallback_plan(req) 贪心装箱，永远能返回一份合法计划。

 Coach（最有戏的一个）

 两步 pipeline，不是 ReAct。

 Step 1 诊断——读 recent_observations，找抗拒/进步信号：
 class Signal(BaseModel):
     kind: Literal["refusal","progress","schedule_conflict","sensory","none"]
     evidence_behavior: str      # BEHAVIOR code
     evidence_quote: str         # 必须是原始家长输入的连续子串 ← 可校验的推理链
     interpretation: str         # ≤50字
     impact: Literal["keep","ease","drop","swap"]
 class CoachAnalysis(BaseModel):
     relevant_obs_ids: list[str]
     signals: list[Signal] = Field(min_length=1, max_length=4)
     free_reasoning: str = ""    # ★ 装饰字段，不参与决策、不校验

 Step 2 产出家长卡片：
 class CoachStep(BaseModel):
     step_id: int; do: str; say: str | None
     wait_sec: int; if_ok: str; if_not: str
 class RefusalScript(BaseModel):
     recognize: str; reduce_demand: str
     choices: list[str]; abandon_signal: str
 class CoachCard(BaseModel):
     activity_id: str; title: str; total_minutes: int
     why_today: str; steps: list[CoachStep] = Field(min_length=3, max_length=6)
     prompt_ladder: list[str] = Field(max_length=4)
     if_child_refuses: RefusalScript
     success_check: list[str]; record_prompt: str

 if_child_refuses 是整个 demo 的情感落点。 家长最痛的不是"做什么"而是"孩子不配合怎么办"。教练 Agent 的价值不在于生成活动（那是检索），而在于生成"退路"。 面试时讲这一条比讲 RAG 有力得多。

 CoT 可见性（混合式）：
 - Step 1 的 CoachAnalysis 本身就是可见推理链——结构化（信号→解释→影响），每步能指回 evidence_quote，可校验。比自由文本 reasoning 字段强。
 - pipeline 每阶段之间把上一阶段产物作为下一阶段 prompt 的显式输入，前端渲染成折叠面板：[1/3] 检索候选 12→硬过滤 7 · 0.03s / [2/3] 教练诊断 引用3条观察 · 8.2s / [3/3] 生成卡片 5步+1退路 · 6.1s
 - free_reasoning 作为装饰字段（可选、不校验、不参与决策）——把 LLM 输出分成"承重字段"和"装饰字段"，7B 写得不好也不影响任何逻辑。这条值得在面试时讲。

 Evaluator

 输入 EvalInput(date, planned, activity_details, observations, parent_self_report, prior_states, history)
 输出：
 class ActivityEval(BaseModel):
     activity_id: str
     outcome: Literal["success","partial","refused","skipped","no_data"]
     success_criteria_met: list[str]; refusal_behavior: str | None
     quality: float = Field(ge=0, le=1); evidence_obs_ids: list[str]; note: str
 class DomainEvidence(BaseModel):     # ← 只给证据和建议，判定留给 Python
     domain: Literal[tuple(ABILITY_DOMAIN)]
     direction: Literal["up","flat","down"]
     magnitude: Literal["none","small","medium","large"]
     rationale: str; evidence_obs_ids: list[str]
 class DailyEval(BaseModel):
     date: str; per_activity: list[ActivityEval]; domain_updates: list[DomainEvidence]
     flags: list[str]; parent_note: str

 ---
 五、状态机 — Python 硬规则，LLM 只给证据

 # statemachine.py
 @dataclass
 class StateTransition:
     domain: str; skill_item: str
     frm: str; to: str; trigger: str      # 命中的规则标识，如 "3consec_success@0.8"
     evidence: dict                         # 供 UI 的证据快照

 def propose_transition(domain, skill_item, cur, level_est, history,
                        profile_confidence) -> StateTransition | None

 ┌─────────────────────────────────┬──────────────────────────────────────────────────────────────────┐
 │              转移               │                             触发条件                             │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ not_started → entry             │ ≥1 次 success/partial，或 level ≥1                               │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ entry → practicing              │ 最近 7 天 success ≥3 且 mean quality ≥0.5                        │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ practicing → mastered           │ 最近 14 天 success ≥8 且 mean quality ≥0.75 且 refusal_rate ≤0.2 │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ mastered → generalizing         │ success ≥5 且其中 ≥3 次在 linked_scenes 外的场景成功             │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ practicing → entry（回退）      │ 最近 7 天 refusal_rate ≥0.5 或 success == 0                      │
 ├─────────────────────────────────┼──────────────────────────────────────────────────────────────────┤
 │ generalizing → mastered（回退） │ 最近 14 天 refusal_rate ≥0.4                                     │
 └─────────────────────────────────┴──────────────────────────────────────────────────────────────────┘

 外加 guard：profile_confidence < 0.5 的 skill_item 禁止升级（报告没写清的不能靠练习次数硬推上去）。

 level_est 不由 LLM 给，由 sessions 表的历史计数算：
 level_est = clamp(初始level + 0.15*success - 0.20*refused, 0, 5)
 这样状态机完全可重放——删库重建、灌 fixture，14 天状态时间线一比一复现。对 demo 可靠性价值极高。

 为什么不让 LLM 判状态：7B 在"连续 8 次 vs 3 次"这种计数判断上不可靠，而这恰好是状态机的全部。且状态判错 → w_state_match 把检索带偏 → 错误级联放大。

 状态不单独存，是 assessments 历史 + skill_state 表的物化视图，每次读取走 SQL 聚合，杜绝"状态字段和实际记录不一致"的经典 bug。

 ---
 六、长期记忆 — SQLite（stdlib sqlite3，零依赖，不用 ORM，直接写 SQL 字符串）

 data/coach.db：children / ability_profile / skill_state / plans / sessions / observations / materials_log

 skill_state 只追加、永不 UPDATE——状态历史即审计日志，面试时可直接说"每次状态变更可追溯，升级和回退对称"。

 三个必需查询（返回 Pydantic 对象直接进 prompt，不是拼字符串）：
 def what_last_week(child_id, end_date, days=7) -> WeeklyRecap
 def what_child_refuses(child_id, lookback_days=30) -> RefusalReport
     # {patterns:[{refusal_behavior,count,activity_ids,last_date,dominant_scene}],
     #  avoided_materials, recommended_cooldown_ids}
 def what_progressed(child_id, lookback_days=30) -> ProgressReport
     # {improvements:[{domain,skill_item,frm,to,delta_level,evidence}],
     #  consistent_successes, stagnant_domains}
 what_progressed 是面试时当场演示的最佳素材：一条 SQL + 状态机聚合，不需要 LLM，秒回。
