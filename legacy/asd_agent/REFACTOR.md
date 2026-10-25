# 案例级重构（case-centric）—— 分阶段执行文档

> **这份文档是本次重构的唯一权威计划。**
> 每个新 session 开工前先读：`asd_agent/REFACTOR.md`（本文）→ `asd_agent/PROGRESS.md`（全局进度与踩坑）→
> `asd_agent/LANGGRAPH.md`（编排层的实测结论）。
>
> 需求输入：`refract.md`《基于现有基建的家庭干预智能体重构方案》（2026-10-21，最新版）。
> 旧的设计文档已归档到 `docs/legacy/`，**不要照着它们实现**。

**阶段按「家长真实使用流程」排序**（描述问题 → 系统评估 → 出方案 → 家长选 → 执行 → 反馈 → 调整），
外加一条**地基阶段排在最前**：先把"方案必须来自活动库"这条约束松开，后面每一层才不用返工。

**状态**：⬜ 未开始　🟡 进行中　✅ 已完成

| 阶段 | 内容 | 状态 |
|---|---|---|
| **1** | **放宽 activity_id**：方案项统一抽象，计划能装不在活动库里的项（**地基，先做**） | ⬜ |
| **2** | **观察链一次性做完**：证据模型 + 多轮追问 + 评估版本 + case 图 + clarify interrupt + API + 前端 | ⬜ |
| **3** | 生成候选方案 + 家长选择（LLM 出多候选 → 校验 → 选 → 落 `plans`） | ⬜ |
| **4** | 执行 + 反馈 + 下一步决策（反馈三层分离 + `decide_next_action` + 旧状态机降级） | ⬜ |
| **5** | 历史回放与分支实验（time travel + `workflow_audit_events`） | ⬜ |

> 阶段 2 与阶段 3 之间有依赖（候选生成要吃评估结果），阶段 1 与谁都不依赖 → 可以先做、也可以并行。

---

## 0. 开工前必读

### 0.1 三条铁律（`asd_agent/graph/__init__.py:15-25`）

1. **checkpoint ≠ 业务表** —— checkpoint 是追加的 resume + provenance 层，不是业务数据的替代品。
2. **模型给证据，Python 做判定** —— 节点体默认写纯函数；引入 LLM 节点必须写清楚它为什么不能是判定函数。
3. **interrupt 恢复会重跑整个节点** —— 节点在 `interrupt()` 之前不得有任何不可重复的副作用。

### 0.2 本次重构**不许**破坏的九条边界

| # | 边界 | 位置 | 为什么 |
|---|---|---|---|
| 1 | 每条链**末尾唯一写库节点**，且排在所有 interrupt 之后 | `graph/nodes.py:313`、`plan_nodes.py:324`、`run_nodes.py:352` | interrupt 恢复会重跑整个节点（PostgresSaver 下实测，InMemorySaver 验不出来）→ `LANGGRAPH.md` 第二节⑤ |
| 2 | 业务表 append-only；只有 `red_flags.status` / `plans.status` /（新）`cases.status` 可改 | `store.py` | 真实发生的事不能被改写；`refract.md` §9.4 |
| 3 | 落库用唯一索引 + `ON CONFLICT`，**不做** SELECT-then-INSERT | `store.py:450-455` | 每线程一连接，并发下两个线程都能"看到没有这行" |
| 4 | 红旗 11 条规则 + `store.plan_paused` 唯一口径**一字不改** | `redflag.py:70-180`、`store.py:973` | 安全层；模型挂了红旗照样能报 |
| 5 | `check_quote()` 空串放行**只对"自称推断"成立** | `llm.py:188` | "自称直述却无引文"必须在**调用方**按不变量处理 |
| 6 | 降级路径**不许编数据** | `observe.py:385`、`profile.py:253` | 编一次会污染一个可判定的通道 |
| 7 | 幂等两层（落库唯一键 + checkpoint 输入指纹）缺一不可 | `feedback.py:387`、`server.py:1149` | 只做一层：数据对但白付 LLM，或白跑图 |
| 8 | 恢复后判定读 `snap.next` / `snap.interrupts`，**不读** state 里的 `needs_*` 字段 | `server.py:750-753`、`server.py:896-900` | resume 跑完后那字段是上一次的值 |
| 9 | checkpoint 里只存**纯 dict**（不存 dataclass / Pydantic 实例） | `plan_nodes.py:75-84` | msgpack 序列化 unregistered type |

### 0.3 总体架构（本次重构的形状）

```
┌─ HTTP 编排层（server.py + asd_agent/case/router.py，Python 判定）──────────┐
│  按 case 状态决定：跑哪条链 / 只跑反馈 / 重跑评估                          │
└──┬──────────────────────┬──────────────────────┬────────────────────────┘
   │ 观察链 {child}:{date} │ 计划链 plan:{child}:{date} │ 反馈链 run:{child}:{date}
   │ （阶段 2 扩）         │ （阶段 1+3 扩）        │ （阶段 4 扩）
   │ publish 写库         │ publish_plan 写库     │ apply_transitions 写库
└──┬──────────────────────┴──────────────────────┴────────────────────────┘
   │
   │ ★ 第四张图：case:{case_id}（长期 thread，一个案例一条）—— 阶段 2 落地
   │   load_case_context → extract_new_evidence(LLM) → update_assessment
   │   → safety_check → detect_unknowns → ⚡clarify_with_parent →（回抽取）
   │   → publish_case_events（★ 唯一写库节点）
   └─ 业务表：cases / assessment_versions / case_evidence / plan_candidates / decision_log
```

### 0.4 为什么**不**把现有三张图嵌成子图（refract.md §3.1 的字面画法）

否掉的理由是两条**实测**得来的硬约束会破掉：

1. 嵌成子图后父图会有 3 个写库节点 → 边界 #1 的**结构性**保证退化成"记得判重"；
2. thread 命名空间隔离（`{child}:{date}` / `plan:` / `run:` / `draft:`）由 LangGraph 统一接管后，
   四个前缀各自防的问题（同 thread `invoke` 接着上次跑）要重写一遍规则。

代价：跨图路由落在 HTTP 层（`case/router.py`）而不是图内。
**这个代价值得** —— `refract.md` §3.2 的主流程图本质是一张**路由表**，用 Python 判定更好测。

### 0.5 现有代码底座（不用重新探查）

**三张图 + 草稿图**（`asd_agent/graph/`）：

| 图 | thread | 节点链 | 写库节点 |
|---|---|---|---|
| 观察链 | `{child}:{date}` | record_intake → extract_observation(LLM `observe_extract`) → validate_observation → ⚡clarify → safety_gate → escalate? → publish | `nodes.py:313`（observations + red_flags） |
| 计划链 | `plan:{child}:{date}` | select_goal → retrieve_activities → generate_plan → validate_plan → ⚡approve_plan → freeze_plan → publish_plan | `plan_nodes.py:324`（plans） |
| 反馈链 | `run:{child}:{date}` | record_execution → analyze_outcome(LLM `summary_day`) → next_action(纯 Python 四选一) → apply_transitions | `run_nodes.py:352`（sessions/skill_state/daily_summary/red_flags） |
| 草稿图 | `draft:{child}:{date}` | save（永不 invoke） | 无 |

**表**（`store.py`，PostgreSQL，schema 按 `ASD_DB_PATH` 隔离，append-only 为主）：
`children` / `ability_profile` / `skill_state` / `day_cards` / `plans` / `sessions` / `observations` /
`materials_log` / `daily_summary` / `red_flags`（+ 4 张 checkpoint 表）。
迁移机制：`_init()` 里 `CREATE TABLE IF NOT EXISTS` + `_migrate()` 里 `ALTER TABLE ADD COLUMN IF NOT EXISTS`，
**索引必须跟着补列走**（`store.py:257-263`）；老库有合法重复行 → 建唯一索引前必须先回填（`store.py:458-490`）。

**LLM 出口只有一个物理位置**：`llm.py:266` `client(timeout_s).chat.completions.create(...)`。
现有 stage：`profile_extract`(90s) / `report_summary`(60s) / `observe_extract`(60s) / `summary_day`(40s) /
`coach_analysis`(40s) / `coach_card`(60s) / `planner_plan`(40s，仅 CLI 不在 HTTP)。
每次调用都有降级路径；有 replay（读本地文件，不发网络）与 capture（只存 response，**改 prompt 会让全部 fixture 失效**）模式。

**关键组件**：
`activities.py`（`Activity` 30+ 字段；50 条活动；6 条硬过滤；8 项权重 `WEIGHTS`：
`state_match .30` `skill_gap .15` `prereq_ready .14` `preference .10` `diversity .10` `material_fit .08`
`method_fit .08` `parent_pref .05` + `negative_penalty` 扣分不硬剔除）、
`capabilities.py`（`SkillAssessment` 词表硬校验 / inferred 置信度钳 0.35）、
`statemachine.py`（5 状态 + `RULES` 阈值表 `statemachine.py:106` + `upgrade_min_confidence 0.5`）、
`redflag.py`（11 条规则 + `referral{who,do,avoid,emergency}`）、
`observe.py`（`ObservationBatch` 四层 + `verify()` 三种降级）、
`feedback.py`（`ActivityFeedback` 构造即校验 + `quality_for()` 纯函数）、
`summary.py`（`DailySummary`）、`coach.py`（诊断 + 卡片）、`child_age.py`（出生日期→月龄唯一口径）。

### 0.6 通用工程约定

- 每阶段**一个可 revert 的 commit**；commit message 写"为什么"。
- 改完必须跑：图级自检 + HTTP 自检 + `activities --validate` + `vite build` + **真起服务打接口**。
  ⚠️ `server.py` 在仓库根目录且**不在任何回归集里** → 改它必须单独打接口（`PROGRESS.md` 记着这个坑）。
- 注释写**为什么**不写做了什么（中文）；每个降级路径都要写清楚"降级后系统还能干什么"。
- LLM 的输出用 Pydantic 模型约束（`extra="forbid"`），schema 直接当 `response_format` 的 `json_schema`。
  ⚠️ **带 `default` 的字段不在 `required` 里 → 模型可以不给或给空串**（实测 `evidence_quote` 全空就是这个原因）。
- 7B 对「照例子写」比「讲规则」敏感（`LANGGRAPH.md` 第四节之一）。

---

## 1. 阶段 1：放宽 activity_id（地基）⬜ ← **下一步从这里开始**

### 1.0 目标与验收

**目标**：让"方案"不再等于"一串活动库 ID"。计划里可以出现**不在活动库里的项**（由模型生成的策略），
而活动库退回到它该有的位置：**参考知识库**（`refract.md` §5.1）。

**验收**（全部可自动判定）：
1. 一个**不在活动库里**的方案项能被装箱进当天计划，和库内项**平权**。
2. 生成项**同样过**难度闸门、材料检查、时间预算 —— 放宽不等于绕过约束。
3. 生成项的语义变化能进指纹（该打断家长时打断），而**仅仅换了 ID 不打断**。
4. 生成项能出卡片、能被家长反馈、能被状态机归属到正确的技能细项。
5. 老 `plans.payload` / 老 checkpoint / 老 session 行**仍能读**。

### 1.1 本阶段的图结构：**不变**

阶段 1 **不加图、不改边、不动 interrupt 位置**（第四张 `case:` 图是阶段 2 的事）。计划链保持：

```
START → select_goal → retrieve_activities → generate_plan → validate_plan
      → substantive_change? ──是──→ ⚡ approve_plan ─┐ approve ─┤
                                              reject ──┤→ freeze_plan
                                                      │
                                否 ───────────────────┘ → publish_plan → END
```

**为什么不用改图**（两条签名决定的）：

- `activities.retrieve(req, *, pool=None)`（`activities.py:449`）本来就接受外部 `pool`，
  默认才 `load_activities()` —— 生成项塞进 `pool` 就自动走同一条 `_hard_filter` + `_score`。
- `planner.pack_day(cands, budget)`（`planner.py:420`）吃的是 `ScoredActivity`（里面包着一个 `Activity`），
  **不是 activity_id 字符串** —— 生成项只要是同形状的 `Activity`，装箱逻辑一行不用改。

**真正要改的只有四处**（其余是数据搬运）：

| 位置 | 改什么 |
|---|---|
| `graph/plan_graph.py::plan_inputs()` | 照 `paused` 的先例，把 `extra_items` 算好注入 `PlanRequest`（**唯一口径在入口，节点体不查库**） |
| `graph/plan_nodes.py:152` `retrieve_activities` | `retrieve(req)` → `retrieve(req, pool=load_activities() + req.extra_items)`；state 里 `candidate_ids` → `items`（带 `kind`，**纯 dict**） |
| `graph/plan_nodes.py:190` `generate_plan` | `_cands(req, top_k=12)` **也要带 pool** —— 它现在又检索了一遍；两处都注入，否则 `excluded` 里显示了、装箱时却不见了 |
| `planner.py:479` `plan_fingerprint` | 改成**语义签名**（见 1.3.4） |

`validate_plan` 与 `pack_day` **不用动** —— 它们只看 Activity 的字段，生成项字段齐全就自动受约束。

### 1.2 "方案要满足什么"流水线 = 九层，全是 0 token

生成项要过的是**同一套**，区别只有三处（见 1.5）。

| 层 | 逻辑 | 位置 |
|---|---|---|
| ① 装配 | `PlanRequest` 15 个字段（等级 / 目标状态 / 优先能区 / 时间预算 / 材料 / 抗拒 / 冷却 / 前置 / 最近 3 天 / 喜好…）+ `pool` | `activities.py:235` |
| ② **硬过滤 6 条** | 超时间预算 / 必需材料不在家 / 抗拒 ≥2 次进冷却 / 前置未完成 / **难度闸门**（画像 level<2 而难度>1）/ 最近 3 天重复 —— 每条不通过都带**给家长看的中文原因** | `activities.py:275-304` |
| ③ **打分 8 项** | `state_match .30`（状态机目标难度，最高项）`skill_gap .15` `prereq_ready .14` `preference .10` `diversity .10` `material_fit .08` `method_fit .08` `parent_pref .05` | `activities.py:324-334` |
| ④ 负分 | 家长标不喜欢的**扣分不硬剔除**（活动 -1.0 / 材料 -0.6 / 场景 -0.4）→ 界面上能解释"因为您说…" | `activities.py:425-446` |
| ⑤ **装箱** | 贪心占满预算；两条硬保证：活动不重复、**一天不重复练同一个技能细项**；成本 = `total_min + prep_min`（备料也是家长的时间） | `planner.py:420-475` |
| ⑥ 计划级校验 3 条 + 指纹 | 草稿内重复活动 / 一天重复同一细项 / 超预算 + `is_substantive_change` 指纹比对；**校验不过也必须让人看见** | `plan_nodes.py:232-246` |
| ⑦ 审批 interrupt | 只在**实质变更**时打断；`edit` 回检索重生成，最多 2 轮，且不再弹第二次 | `plan_nodes.py:252-294` |
| ⑧ 发布 | **唯一写库节点**；`status` / `version` / `active` | `plan_nodes.py:324` |
| ⑨ 执行侧 + 安全层 | 归属过滤（`evaluator.py:90`）→ 状态机（`statemachine.py:175`，含 `upgrade_min_confidence=0.5` 闸门）；红旗 → `store.plan_paused` → **不生成草稿** | — |

> ★ 第 ③ 层里的 `state_match` 权重最高，是"喜好不能推翻教学判断"的成文处。
> 生成项**同样按状态机目标难度打分** —— 它不是活动库里的东西，就更不该绕过难度。

### 1.3 本阶段专属的坑（先看，再写代码）

1. **★ 生成项会被静默丢出指纹。**
   `generate_plan:193` 是这样的：
   ```python
   acts = _acts_by_id({"plan_activity_ids": [p.activity.activity_id for p in picks]})
   "draft_fingerprint": plan_fingerprint([acts[i] for i in [...ids...] if i in acts])
   ```
   `_acts_by_id` 是**从活动库按 id 反查 Activity 对象**；生成项查不到 → 那个 `if i in acts`
   **把它静默丢掉**。后果：**生成项的指纹永远不参与审批判据，换什么生成方案都不会打断家长**，
   而且**不报错**。→ 指纹的输入必须改成"从 `items` 直接拿对象"，不能走活动库反查。
2. **★ 指纹会被 ID 污染。** 现在的指纹是
   `(activity_id, materials集合, difficulty, prompt_hierarchy[0], duration*sessions)`（`planner.py:495-505`）。
   生成项的 ID 每次重新生成都不同 → **指纹必然变** → 每次都打断家长 →
   "只在真变化时审批"这条判据被它自己吃掉（`LANGGRAPH.md` 第四节之四记着这个坑的成因）。
   → **指纹必须改成语义签名**（见 1.4.4）。
3. **★ 技能归属会查不到人。** `evaluator.apply_transitions` 用
   `activity.target_skill_items` 做归属过滤（`evaluator.py:90`），
   而 `store.sessions_for_skill` 靠**进程内活动库**反查 `activity_id → target_skill_items`
   （`PROGRESS.md` 第五节记着"技能/场景归属靠进程内活动库反查"这个老问题）。
   生成项不在活动库里 → 反查不到 → **它的 session 归属不到任何技能细项** → 状态机永远收不到这条证据。
   → 执行记录必须**自带** `target_skill_items`（见 1.4.5）。
4. **★ `generate_plan` 会再检索一遍。** `retrieve_activities` 已经全量检索过一次，
   `generate_plan` 又调 `activities.candidates(req, top_k=12)`（`plan_nodes.py:190`）。
   生成项注入**两处都要加**，只加一处就会出现"`excluded` 里说明了、装箱时却不见了"的不一致。
5. **`Activity.activity_id` 有正则约束** `^[a-z]{2,4}-\d{3}$`（`activities.py:73-113`），
   `gen:xxx` 过不了 → 要么放宽正则，要么把"项"和"活动"分成两层（见 1.4.2）。
6. **`PlannedActivity.activity_id` 的字段说明写着"必须逐字取自候选表里的 activity_id"**
   （`planner.py:55`）—— 这句话就是"方案 = 活动 ID 列表"这条约束的成文处，放宽时要一起改。
7. **`coach._pick_adjustment` 的 `ease`/`swap` 依赖候选池**（`coach.py:426-444` 拿库里活动换），
   生成项**换不动**（只能 keep / drop）→ 本阶段先记为已知限制，不为了它把活动库再拉回来。
8. **`Activity.target_skill_items` 现在是自由文本**（不校验词表），但 `activities.retrieve` 用
   `SKILL_ITEM_DOMAIN` 反查能区（`vocab.py`）→ 生成项若带**词表外**的技能细项，反查会失败 →
   要么生成项只用词表内细项，要么能区反查要能兜底（阶段 3 才需要，本阶段先只允许词表内）。

### 1.4 改动清单

**新增**

| 文件 | 职责 |
|---|---|
| `asd_agent/plan_item.py` | 方案项的**统一形状** + 库内项↔生成项的互转 + 语义签名（指纹用） |
| `asd_agent/plan_item_selfcheck.py` 或并入 `activities.py --validate` | 本阶段自检（见 1.7） |

**修改**

| 文件 | 改什么 |
|---|---|
| `asd_agent/activities.py` | `Activity` 增加 `kind: Literal['library','generated']`（默认 `'library'`）与 `origin: str`；`activity_id` 的约束放宽以容纳 `gen:` 前缀；`retrieve()` 返回的项一律 `kind='library'` |
| `asd_agent/planner.py` | `PlannedActivity` 的字段说明与模型放宽（不再要求"取自候选表"）；`plan_fingerprint` 改成**语义签名**；`pack_day` 接受库外项 |
| `asd_agent/graph/plan_nodes.py` | `retrieve_activities` / `generate_plan` / `validate_plan` / `freeze_plan` / `publish_plan` 存 `items`（含生成项）而不是纯 `activity_ids`；`state` 里的 `candidate_ids` → `items`（**纯 dict**） |
| `asd_agent/store.py` | `sessions` 加 `target_skills JSON`（执行记录自带归属）；`plans.payload` 兼容旧格式 |
| `asd_agent/evaluator.py` | 归属过滤优先读 session 自带的 `target_skills`，回退到活动库反查 |
| `asd_agent/coach.py` | 卡片能渲染生成项（形状相同即可）；`ease`/`swap` 对生成项的已知限制写进 docstring |
| `asd_agent/server.py` | `/api/card`、`/api/plan/{child}` 的 payload 兼容新 `items` 字段；`/api/feedbacks` 接受生成项的 `activity_id` |
| `frontend/src/components/CoachCard.vue` / `FeedbackItem.vue` | 显示生成项时用 `origin` 说明出处 |

### 1.5 详细设计

#### 1.5.1 统一抽象：方案项（PlanItem）

**核心决定：放宽的是「ID 必须来自活动库」这条约束，不是「Activity 这个形状」。**

理由：下游（planner / coach / evaluator / 卡片 / 反馈）读的都是 `Activity` 的**字段**，
不是它的**来源**。改形状 = 改六个下游；改来源 = 只改一处 + 加一个转换函数。

#### 1.5.2 `Activity` 的两处放宽

```python
class Activity(BaseModel):
    kind: Literal["library", "generated"] = "library"   # 来源：库里 / 模型生成
    origin: str = ""        # 生成项的出处说明（"LLM 候选 #3 · 基于 known_facts[2]"）
    activity_id: str        # 约束从 `^[a-z]{2,4}-\d{3}$` 放宽为接受 `gen:{candidate_id}:{index}`
    # 其余字段一律不动
```

- `gen:` 前缀的 ID 必须**稳定**：同一份候选方案的同一项，ID 在多次读之间不变
  （否则 session 唯一键 `(child,date,activity,run_id,seq)` 与状态机归属会漂）。
  建议 `gen:{candidate_id}:{item_index}`，其中 `candidate_id` 是 `plan_candidates.id`（落库后即稳定）。
- `origin` 是给人看的：家长在卡片上看到"这不是活动库里的现成做法"。

#### 1.5.3 生成项怎么进计划：与库内项**同一条流水线**

**关键约束：生成项不享有任何豁免。** 它必须：

1. 过**同一条**难度闸门（画像 level < 2 而难度 > 1 → 挡掉，`activities.py:296-299`）；
2. 过**同一条**材料检查（essential 材料不在家 → 挡；可给 substitute 才放行）；
3. 过**同一条**时间预算（`total_min + prep_min ≤ daily_minutes`）；
4. 拿**同一套权重**打分（`state_match .30` 仍是最高项 → **喜好不能推翻教学判断**这条不破）；
5. 被 `state_match` 按**同一个状态机目标难度**评判。

实现：`activities.py` 增加 `score_item(item: Activity, req: PlanRequest) -> ScoredActivity`，
让库内项与生成项走**同一个打分函数**（现在库内项在 `_score` 里直接拿 `a: Activity` → 天然可复用，
只要把"候选从哪来"与"怎么打分"分开）。

> 这是本阶段最重要的一条：**放宽的是"方案从哪来"，不是"方案要满足什么"。**

#### 1.5.4 指纹改成语义签名（本阶段的头号坑）

```
现在的指纹（planner.py:495-505）
  skills     = 所有 target_skill_items 的并集
  activities = sorted((activity_id, materials集合, difficulty, prompt_hierarchy[0], duration*sessions))

改成
  skills     = 同上（不变）
  items      = sorted((语义签名))      ← 不含 activity_id
  语义签名   = (tuple(sorted(target_skill_items)),
                 tuple(sorted(materials 名称)),
                 difficulty,
                 prompt_hierarchy[0],
                 duration*sessions,
                 steps 签名)            ← 新增：步骤变了就是实质变更
  steps 签名 = tuple((s.action, s.prompt_level) for s in steps)
```

- 库内项的语义签名**与现在等价**（`activity_id` 去掉后，同一活动仍然同签名）→ 老指纹行为不变。
- 生成项换了 ID 但语义相同 → 签名相同 → **不打断家长**。
- 生成项换了步骤/材料/难度/起点提示级别 → 签名变 → **打断**。
- ⚠️ 指纹是**存进 `plans.payload` 的**（`plan_nodes.py:342`），老行里存的是老格式指纹。
  → 比较时两个格式都要能读（老格式按 `(activity_id, ...)` 比，新格式按语义签名比）；
  **老行不重写**（append-only，边界 #2）。

#### 1.5.5 执行记录自带归属（第二个坑）

`sessions` 表加一列 `target_skills JSON`，写 `sessions` 时**同时落**该行的 `target_skill_items`。

- `evaluator.apply_transitions` 的归属过滤（`evaluator.py:90`）改为：
  **先读 session 自带的 `target_skills`**，没有再回退到活动库反查（老数据兼容）。
- `store.sessions_for_skill` 同样优先用该列。
- 顺带修掉 `PROGRESS.md` 第五节记的老问题：「技能/场景归属靠进程内活动库反查，
  全库没有指向 `activity_id` 的外键」—— 现在归属信息**跟着执行记录一起落库**了。

#### 1.5.6 向后兼容

| 老数据 | 怎么读 |
|---|---|
| `plans.payload.activities = [{activity_id, slot, minutes, why_this}]` | 读时按 `activity_id` 从活动库还原成完整项；新写入改成 `items = [...]`（含 `kind`） |
| 老 `plans.payload.fingerprint` | 比较时按老格式读（`(activity_id, materials, difficulty, prompt[0], dur)`） |
| 老 checkpoint 里的 `candidate_ids` | 读时当成库内项处理 |
| 老 `sessions` 行 | 没有 `target_skills` → 回退活动库反查 |

**不重写任何老行**（边界 #2）。

### 1.6 本阶段的已知限制（明确写出来，别到时候当成 bug）

1. 生成项**换不动**：教练的 `ease`/`swap` 只在库内候选池里挑，生成项只能 `keep` / `drop`。
2. 生成项**不参与检索排序**：它是被"装配"进来的，不是被"检索"出来的（检索仍只查活动库）。
3. 生成项的 `target_skill_items` 暂时**只允许词表内**（阶段 3 起放宽到开放标签，`refract.md` §4.3）。
4. 本阶段**不接 LLM**：生成项用**固定 fixture** 造（自检里手写一个），
   真正的 LLM 生成在阶段 3 —— 这样阶段 1 的每一条断言都不依赖网络。

### 1.7 自检断言（建议 9 组）

1. 库内项与生成项能被 `pack_day` 一起装箱；**生成项超预算时同样被砍掉**。
2. 生成项同样过难度闸门（level<2 且 difficulty>1 → 被挡）与材料检查（essential 缺失 → 被挡）。
3. 生成项能被打分，且 `state_match` 权重不因 `kind` 改变（喜好不能推翻教学判断这条不破）。
4. **指纹不被静默丢弃**：生成项必须出现在 `plan_fingerprint` 的输入里
   （现在 `_acts_by_id` 查不到就 `if i in acts` 丢掉 —— 这是坑 1，必须有断言守着）。
5. **指纹**：同一生成项换 ID 但语义相同 → 指纹不变（不打断家长）；改步骤/材料/难度/起点提示级别 → 指纹变（打断）。
6. 生成项能出卡片（`/api/card` 返回它，且带 `origin`）。
7. 生成项的执行反馈**能落到正确的技能细项**（`sessions_for_skill` 不再依赖活动库反查）。
8. 生成项能进状态机统计（`skill_state` 归属正确、`sessions` 唯一键不撞）。
9. **老 payload / 老 checkpoint / 老 session 行仍能读**（各造一条老格式 fixture 验证）。

### 1.8 验证命令

```bash
cd asd_agent
ASD_DB_PATH=/tmp/graph_selfcheck.db   ../.venv/bin/python -m graph.selfcheck    # 29 → 30+
ASD_DB_PATH=/tmp/graph_ckpt_check.db  ../.venv/bin/python -m graph.checkpointer
ASD_DB_PATH=/tmp/http_selfcheck.db    ../.venv/bin/python http_selfcheck.py     # 13 → 14+
../.venv/bin/python activities.py --validate
cd ../frontend && npx vite build
```

**真起服务打接口**：`/api/plan/run` → 看返回的 `items` 里有没有生成项；
`/api/card` → 卡片能渲染它；`/api/feedbacks` → 提交它的反馈 → `GET /api/state` 能看到归属。

### 1.9 提交要求

一个可 revert 的 commit，message 写"为什么"（例：`放宽 activity_id：方案项统一抽象 + 指纹改语义签名 + 执行记录自带归属`）。
提交前更新 `asd_agent/PROGRESS.md`（自检组数、验证结果、踩坑）与本文（勾状态 + 记录实际决策）。

### 1.10 交接

在本文阶段 1 末尾追加 `### 1.9 本阶段实际发生的事`：哪些设计被推翻、最终改了哪些文件、
自检组数从多少到多少、遗留项、**下一阶段的第一件事**。

---

## 2. 阶段 2：观察链一次性做完 ⬜

对应家长使用流程的第 1–2 步：**家长描述 → 系统理解 → 追问 → 形成评估**。
本阶段**不生成方案**（那是阶段 3），终点是"评估版本更新完成"。

### 2.0 目标与验收

**目标**：家长在一个"案例"里多次自然描述 → 系统积累**带原文引用的证据** → 主动发现**未知项**并追问
（一次一个、有限轮、有新信息才问）→ 形成**可追溯的评估版本**。

**验收**：
1. 家长两次补充信息 → 两次评估版本，`known_facts` 累积、`unknowns` 收敛。
2. 缺信息时系统**主动追问**，且**不重复问已答过的**；连续一轮没有新证据就不许再问。
3. 事实 / 假设 / 未知 / 矛盾四类信息**分开存**，家长一眼能看出哪条是事实、哪条是系统的猜测。
4. 编造的引文、没引文的"直述"、`support=none` 的结论**都进不了 known_facts**。
5. 服务重启后，案例的阶段与待办仍在。

### 2.1 专属坑

1. **case 图也要满足边界 #1**：`publish_case_events` 是**唯一**写库节点，排在**所有 interrupt 之后**
   （照抄现有三张图的形状）。
2. **`Command(resume=)` 打在没有 pending interrupt 的 thread 上会让 LangGraph 从头重跑整条图** ——
   不报错、不抛异常，就是数据写了两遍。守卫必须在 `graph.invoke` **之前**，返回 **409**
   （现成写法：`server.py:827-829`）。
3. **多轮追问 vs "只许问一次"**：现有 `clarify_asked` 守卫是为了防"系统把自己没听懂的锅推给人"。
   本阶段要放开多轮，就得用**"这一轮有没有新证据"**来替代"问没问过"（见 2.3.5）。
4. **checkpoint 只存纯 dict**（边界 #9）。`CaseEvidence` / `AssessmentVersion` 是 Pydantic → 进 state 前转 dict。
5. **FastAPI 按声明顺序匹配路由**：`/api/cases/{case_id}/xxx` 必须声明在 `/api/cases/{case_id}` **之前**
   （`server.py:736-740` 记着 `/api/plan/status` 被参数路由吃掉的坑）。

### 2.2 改动清单

**新增**

| 文件 | 职责 |
|---|---|
| `asd_agent/case/__init__.py` / `models.py` | `Case` / `CaseEvidence` / `AssessmentVersion` / `CaseClarify` / `CaseStatus` |
| `asd_agent/case/lifecycle.py` | 生命周期纯函数：`ALLOWED_TRANSITIONS` / `can_transition()` / `next_status()` |
| `asd_agent/case/evidence.py` | 证据抽取（LLM）+ 回指校验 + 降级 |
| `asd_agent/case/router.py` | 案例编排：下一步该做什么（纯 Python） |
| `asd_agent/case/labels.py` | **家长措辞的唯一出处**（`label` + `hint`），见 2.3.8 |
| `asd_agent/case/selfcheck.py` | case 层自检 |
| `asd_agent/graph/case_graph.py` / `case_nodes.py` | 第四张图（见 2.3.3） |
| `frontend/src/pages/CasePage.vue` + `CaseStage.vue` + `CaseClarify.vue` + `EvidenceList.vue` | 案例页与三个组件 |

**修改**：`asd_agent/store.py`（3 张新表 + 读写函数）、`asd_agent/graph/state.py`（`CaseState` + 载荷 + `case_thread_id()`）、
`asd_agent/graph/runtime.py`（第五张图的单例）、`asd_agent/server.py`（新路由）、
`frontend/src/App.vue`（页签）、`frontend/src/api.js`（fetch 封装）。

### 2.3 详细设计

#### 2.3.1 数据库

| 表 | 列 | 唯一键 | 可变性 |
|---|---|---|---|
| `cases` | `case_id TEXT PK`, `child_id TEXT`, `thread_id TEXT`, `status TEXT`, `title TEXT`, `current_assessment_version INT`, `current_cycle_id TEXT`, `created_at`, `updated_at` | PK | **仅 `status` 可改** |
| `assessment_versions` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `version INT`, `known_facts JSON`, `hypotheses JSON`, `unknowns JSON`, `contradictions JSON`, `risk_flags JSON`, `evidence_snapshot JSON`, `created_at` | `uq_assess_version(case_id, version)` | 只追加 |
| `case_evidence` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `assessment_version INT`, `claim TEXT`, `quote TEXT`, `source TEXT`, `status TEXT`, `support TEXT`, `occurred_at TEXT`, `scene TEXT`, `created_at` | `uq_evidence_once(case_id, assessment_version, claim_hash, source_hash)` | 只追加 |

枚举：
- `cases.status`（`refract.md` §7.1）：`assessing` / `awaiting_parent_input` / `ready_to_execute` /
  `awaiting_feedback` / `reviewing_outcome` / `revising_plan` / `paused` / `closed`
- `case_evidence.source`：`parent_message` / `parent_report` / `clinical_report` / `observation` / `model_inference`
- `case_evidence.status`：`parent_reported` / `observed` / `inferred` / `confirmed`
- `case_evidence.support`：`direct` / `partial` / `none`

#### 2.3.2 证据模型（`refract.md` §4.1/§4.2）

**两类检查必须分开**：

| 检查 | 谁做 | 怎么判 |
|---|---|---|
| **引用真实性**（原文是否真含这段话） | Python，0 token | `llm.check_quote(source, quote)`（`llm.py:182`）—— 沿用现有实现 |
| **结论支持程度**（这段话够不够支撑这个结论） | **受限枚举字段 + Python 规则** | LLM 在抽取时自评 `support: direct/partial/none`；Python 据此**禁止**升级状态 |

**硬不变量**（写进 `case/models.py` 的 model_validator，降级而不是报错 —— 与 `capabilities.py`
"inferred 高置信静默钳到 0.35"同风格）：

1. `status == "parent_reported"`（或 `confirmed`）但 `quote` 为空 → **不许进 `known_facts`**，降级为 `inferred`。
   ★ 这条直接修掉已查出的破口：模型自称 `report_stated` + 空引文，被 `check_quote` 空串放行
   （`llm.py:188`），以 ×3 权重把整份画像撑到 0.80、打开状态机升级闸门（`statemachine.py:237`）。
2. `support == "none"` → 一律不许进 `known_facts`（`confirmed` 需要 `support == "direct"`）。
3. `support == "partial"` → 可以进 `known_facts`，但必须带 `partial` 标记，且**不能**是 `confirmed`。
4. `status == "inferred"` → 置信度钳到 `< 0.4`（沿用 `INFERRED_MAX_CONFIDENCE = 0.35`）。

**矛盾处理**：同一 case 内两条 claim 冲突时**并列保留**，记进 `AssessmentVersion.contradictions`，
**不自动删任何一条**（原始陈述不可被覆盖）。

#### 2.3.3 case 图

```
START → load_case_context → extract_new_evidence(LLM) → update_assessment
      → safety_check → urgent? ──是──→ escalate → publish_case_events → END
      → detect_unknowns → 信息不足? ──是──→ ⚡clarify_with_parent ──（回 extract_new_evidence）
      → 信息足够 → publish_case_events → END
```

#### 2.3.3.1 现有观察链（今天跑的那张，阶段 2 **不动它**）

```
START → record_intake → extract_observation → validate_observation
                                          │
                        needs_clarification? ├─是─→ ⚡ clarify ──→ 回 extract_observation
                                          └─否─┐
                              safety_gate ←──┘   │
                     highest=="urgent"? ├─是──→ escalate ──┐
                                  └─否────────────────┴──→ publish → END
```

| 节点 | 读 state | 写 state | 调什么 | 判定在哪 |
|---|---|---|---|---|
| `record_intake` `nodes.py:75` | child_id, date, parent_text | parent_text（归一化） | 无 | **无** —— 只归一化，不抽不判不写 |
| `extract_observation` `nodes.py:97` | parent_text, observation, observed_text | observation, observed_text, redflags, n_events, clarifying_question | `observe.extract_observations`（**唯一 LLM**，stage `observe_extract`） | 幂等判据在节点内：**语料没变就复用上次结果**（`observed_text == parent_text`） |
| `validate_observation` `nodes.py:154` | observation, clarifying_question, clarify_asked | needs_clarification | 无 | **纯 Python**：任一事件 `needs_clarification` 且**没问过** 且**非降级** → 就问 |
| `clarify` `nodes.py:190` ⚡ | clarifying_question, parent_text | clarification_answer, clarified_text, clarify_asked（+ 重抽结果） | `interrupt(..., response_schema=ClarifyAnswer)` → 再调 `extract_observations` | 回答**追加**不替换（回指校验比的是整段原话） |
| `safety_gate` `nodes.py:251` | clarified_text or parent_text | redflags, verdict, scanned_text | `redflag.scan`（11 条正则，**0 token**） | 判定在 `redflag.py` 里已经做完 —— **节点只搬运，不重判** |
| `escalate` `nodes.py:280` | verdict, redflags | escalation{paused, guidance, guidance_items, do_not, emergency} | `redflag.guidance_for` | 纯计算，**一个字节都不写** |
| `publish` `nodes.py:313` | 全部 | observation_ids, red_flag_ids | `observe.persist` + `store.append_red_flags` | 幂等按**「这段语料」**；红旗按**「语料 × 这一条 code」** |

**这张图的四条形状规则**（不是画着好看，每条都有代价）：

1. **落库只在最后一个节点 `publish`** —— interrupt 恢复会重跑整个节点，凡是在 interrupt 之前写过库的东西，
   resume 一次就写两遍。这条靠**节点位置**保证，不靠"记得判重"。
2. **`clarify` 之前零副作用** —— 它只负责"停一下等人"，抽取在上一个节点已经算完。
3. **答完回 `extract_observation`（抽取），不是回 `validate_observation`（判定）** ——
   回答已经拼进语料，不重抽一遍，补的信息就只是躺在 state 里没人用。
4. **条件边是"跳过"不是"进出"** —— `needs_clarification` 为假时 `clarify` 节点**根本不执行**
   （`builder.py:62-69`）。interrupt 节点每进出一次就是一个 checkpoint，而澄清一天最多一次。

**挂在四个 HTTP 路由上**：`POST /api/observe/run`（job+SSE）→ `POST /api/observe/resume`
（`Command(resume=)`，**没有 pending interrupt 必须 409**，否则 LangGraph 当成新输入从头跑 → 写两遍）→
`GET /api/observe/status`（纯读 checkpoint，刷新页面靠它发现"还有个问题等你答"）。
草稿另用 `draft:` thread，**永不 invoke**。

#### 2.3.3.2 新 case 图逐节点

| 节点 | 读 | 写 | 调什么 | 判定在哪 |
|---|---|---|---|---|
| `load_case_context` | case_id | case_row, known_facts, unknowns | `store` 读 `cases` + `assessment_versions` | 纯读 |
| `extract_new_evidence` (LLM) | parent_text, **已有 facts/unknowns** | evidence[], unknowns[], contradictions[], clarifying_question | 新 stage `case_evidence_extract` | **增量**：把已有事实一起进 prompt，不是全量重抽 |
| `update_assessment` | evidence[], unknowns[] | known_facts, hypotheses, risk_flags | Python | 纯 Python：2.3.2 的**四条硬不变量** |
| `safety_check` | 全段语料 | risk_flags, verdict | `redflag.scan` | **判定逻辑一字不改** |
| `detect_unknowns` | unknowns[], evidence[] | needs_clarification, clarifying_question | 无 | 纯 Python：未知项非空 **且 本轮有新证据** **且 轮数 < 3** |
| `clarify_with_parent` ⚡ | clarifying_question | answer, clarify_round+1, 合并语料 | `interrupt(CaseClarify)` | 一次只问一个 |
| `publish_case_events` | 全部 | evidence_ids, assessment_version, **cases.status** | `store` 写 3 张表 | **唯一写库节点** |

#### 2.3.3.3 两张观察链的分工（不是替代关系）

| 维度 | 现有观察链 | 新 case 图 |
|---|---|---|
| thread | `{child}:{date}`（按天） | `case:{case_id}`（**长期**） |
| 回答的问题 | "今天发生了什么"（ABC 事件） | "这个案例到底怎么回事"（证据 / 事实 / 未知 / 矛盾） |
| 追问次数 | **一次**（`clarify_asked` 守卫） | 最多 3 轮，且**没新证据就不问** |
| 抽取方式 | 语料变了就全量重抽 | 增量（带已有事实进 prompt） |
| 落库 | `observations` | `case_evidence` + `assessment_versions` + `cases.status` |
| 终点 | 今天记下了 | 评估版本更新了 |

两者的**写库节点各管各的表** → 现有观察链在阶段 2 **一行都不用改**：
新图是**并排长出来的第四张图**，不是把旧的改造。

- **thread_id = `case:{case_id}`**（长期稳定）→ 对应 `refract.md` §9.1。
- `publish_case_events` 是**唯一**写库节点：写 `case_evidence` + `assessment_versions` + 改 `cases.status`。
- `safety_check` 直接复用 `redflag.scan()`（`redflag.py:280`）与 `store.plan_paused()`（`store.py:973`），
  **判定逻辑一字不改**；命中 urgent 时与观察链同款：先给转诊指引（`redflag.guidance_for`），但**仍要 publish**
  （`LANGGRAPH.md` 第四节之二记着"urgent 那天最该被记下来"）。
- 节点体默认纯函数；`extract_new_evidence` 是本图唯一的 LLM 节点。

#### 2.3.4 新 LLM stage `case_evidence_extract`

- 输入：家长这一轮的原话 + 已有 `known_facts` / `unknowns`（要增量，不是重新抽一遍）。
- 输出：`ObservationBatch` 风格的证据列表 —— `claims[{claim, quote, source, status, support, occurred_at, scene}]` +
  `unknowns[]` + `contradictions[]` + `clarifying_question`（**一次一个**）。
- 超时 60s（同 `observe_extract`），重试 3 次，**降级路径交白卷**（`events=[]`、`degraded=True`）——
  照 `observe.py:385` 的做法：降级时**一条都不编**。
- 证据抽取的字段必须**给例子**（7B 跟例子走，不跟论述走 —— `LANGGRAPH.md` 第四节之一）。

#### 2.3.5 多轮追问的三条硬约束

1. **一次只问 1 个问题**（`refract.md` §8.1）。
2. **全局最多 3 轮**（`MAX_CLARIFY_ROUNDS`）。
3. **连续一轮没有新证据就不许再问** —— 判据：这一轮的回答**没有产生新的 `CaseEvidence`**。
   ★ 这是"系统把自己没听懂的锅推给人"那个坑的解药：现有 `clarify_asked` 守卫把它压成"只许问一次"，
   本阶段放开多轮，就得用"有没有新信息"来替代"问没问过"。

#### 2.3.6 API

| 路由 | 形态 | 说明 |
|---|---|---|
| `POST /api/cases` | 直接 JSON | `{child_id, title, initial_text}` → 建 `cases` + 跑 case 图第一轮 |
| `POST /api/cases/{case_id}/messages` | job+SSE | 家长补充描述 → 跑 case 图（可能返回 `phase="clarify"`） |
| `POST /api/cases/{case_id}/clarify` | job+SSE | 回答追问（`Command(resume=)`；**无 pending interrupt 必须 409**） |
| `GET /api/cases/{case_id}` | 直接 JSON | 当前评估、生命周期状态、待办、最近决策 |
| `GET /api/cases/{case_id}/history` | 直接 JSON | 评估版本 / 证据时间线 |

- 旧路由 `/api/profile` `/api/plan/run` `/api/feedbacks` **一字不改**，继续可用（渐进迁移）。

#### 2.3.7 前端

- 新增 hash 页签 `case`（`App.vue` 的 `TABS` + `VALID`）→ `pages/CasePage.vue`。
- `CaseStage.vue`（阶段徽标）/ `CaseClarify.vue`（追问卡，一次一个问题）/
  `EvidenceList.vue`（**事实 / 假设 / 未知 / 矛盾四栏**，事实与假设必须一眼能分开）。
- 所有 fetch 走 `api.js`（唯一出口）。
- **措辞不在前端硬编码** —— 家长看到的中文全部来自 API 返回的 `label` / `hint`（见 2.3.8）。

#### 2.3.8 变量字典：让家长看得懂

**三条原则**（这一节是"家长视角"与"代码视角"之间的那层翻译）：

1. **state 字段名是给代码和模型看的，家长看到的是措辞。**
   `known_facts` 这种名字出现在家长眼前是失败的 —— 它暴露了内部结构，还会让家长
   误以为这是一份"结论清单"。家长要看到的是「**我们已经知道的**」。
2. **措辞的唯一出处是一份常量表 `asd_agent/case/labels.py`**，
   API 返回时把 `label`（标题）+ `hint`（一句解释）一起带出来；
   前端**不硬编码措辞、不翻译、不猜**。
   （与项目既有约定同源：前端不算业务数、`api.js` 是唯一出口。）
3. **内部字段一律不发给家长** —— 把"模型置信度 0.35""support=none"甩给家长
   既没有意义，又会误导（家长会以为"0.35"是孩子的某种指标）。

**变量字典**

| 内部变量名 | 类型 | 代码含义 | 家长看到的标题（label） | 一句话解释（hint） | 界面栏目 |
|---|---|---|---|---|---|
| `known_facts` | list[str] | 已确认的事实 | 我们已经知道的 | 这些是**你亲口说过、而且原话能对得上**的事 | 事实栏 |
| `hypotheses` | list[str] | 系统的推测 | 我们的猜测（还没确认） | 这些是**我们根据已知的事推出来的**，可能不对 | 假设栏 |
| `unknowns` | list[str] | 还不清楚的 | 还不清楚的 | 弄清楚这些，才知道该怎么做 | 未知栏 |
| `contradictions` | list[{a,b}] | 两条说法对不上 | 两处说法对不上 | 你前后说的不一样，我们**两个都留着** | 矛盾栏 |
| `risk_flags` | list[{code,quote,referral}] | 安全信号 | 需要特别注意的 | 这些情况下建议先找专业人士 | 顶部横幅（沿用 `RedFlagBanner.vue`） |
| `clarifying_question` | str | 系统想问的那一句 | 想问你一句 | 只需要回答这一句就行 | 追问卡 |
| `evidence[].claim` | str | 一条事实陈述 | 你说过 | —— | 证据条目 |
| `evidence[].quote` | str | 家长原话 | 你的原话 | 这是你说的原话，我们没有改写 | 证据条目（引号样式） |
| `evidence[].support` | enum | 证据够不够 | **不展示** | —— | —— |
| `evidence[].status` | enum | 事实 / 推断 | **不展示**（但它决定这条进不进"事实栏"） | —— | —— |
| `cases.status` | enum | 生命周期 | 见下表八种措辞 | —— | 阶段徽标 |
| `assessment_version` | int | 第几版评估 | **不展示** | —— | —— |
| `claim_hash` / `source_hash` | str | 幂等键 | **不展示** | —— | —— |

**八个生命周期状态的家长措辞**（`cases.status` → 徽标上的字）

| code | 家长看到 | hint |
|---|---|---|
| `assessing` | 我们在了解情况 | 你说的我们正在整理 |
| `awaiting_parent_input` | 想请你补充一点 | 有一处还不太清楚 |
| `ready_to_execute` | 方案准备好了 | 等你选一个 |
| `awaiting_feedback` | 等你练完告诉我一声 | 练完回来说一句就行 |
| `reviewing_outcome` | 我们在看看这次的效果 | —— |
| `revising_plan` | 我们在改方案 | 上次的做法要调一下 |
| `paused` | 先停一下 | 出现需要先处理的情况 |
| `closed` | 这个案子结束了 | —— |

**明确不展示的字段清单**（写出来是为了防止实现时"顺手就带上了"）：
`support` / `evidence[].status` / `confidence` / `claim_hash` / `source_hash` /
`assessment_version` / `thread_id` / `run_id` / 内部错误码。

**一条验收用的规矩**：家长看到的每一句话，都必须能回答「**这是谁说的**」——
你说的 / 我们推的 / 还没确认。说不清是谁说的，就不该出现在家长眼前。

### 2.4 自检断言（建议 11 组）

1. 建案例 → `cases` 落库、`thread_id='case:{id}'`、`status='assessing'`
2. 补充信息 → `case_evidence` 追加行、`assessment_versions` 版本 +1
3. **编造的引文** → 降级为 `inferred`、不进 `known_facts`
4. **`status='parent_reported'` 但 `quote` 为空** → 不许进 `known_facts` ← **修破口的那条**
5. `support='none'` → 不许进 `known_facts`；`support='partial'` → 可进但不可 `confirmed`
6. 未知项非空 → `status='awaiting_parent_input'`，图停在 ⚡clarify
7. 回答后**没有新证据** → **不再问第二轮**
8. 两次补充信息 → 评估版本 +2，`known_facts` 累积且 `unknowns` 收敛
9. 矛盾的两条 claim **并列保留**、进 `contradictions`，不自动删
10. **无 pending interrupt 时 `Command(resume=)` → 409**（不重跑图、不多写一行）
11. **家长措辞**：`GET /api/cases/{id}` 的响应里，每个面向家长的字段都带 `label` + `hint`，
    且**内部字段一个都不许出现**（`support` / `evidence[].status` / `confidence` / `*_hash` /
    `assessment_version` / `thread_id`）—— 断言方式：把这些键名挨个在响应 JSON 里查一遍，
    查到一个就红。（这条守的是 2.3.8 的"不展示清单"。）

另加：`http_selfcheck.py` +1 组（建案例 → 补充 → 答追问 → 读回，**真起服务打接口**）；
`graph/selfcheck.py` +1 组（case 图 interrupt 往返 + 恢复不双写，**必须用 PostgresSaver**）。

### 2.5 验证 / 提交 / 交接

同阶段 1 的 1.6–1.8（验证命令 / 提交要求 / 交接位）。

---

## 3. 阶段 3：生成候选方案 + 家长选择 ⬜

对应使用流程的第 3–4 步：**信息足够 → 出多个候选 → 家长选 → 冻结方案版本**。
依赖阶段 1（能装生成项）与阶段 2（评估作为输入）。

### 3.1 内容

- **新 LLM stage `plan_candidates`**（`asd_agent/case/candidates.py`）：
  - 输入：当前 `AssessmentVersion`（known_facts / hypotheses / unknowns）+ 家长约束 + **参考实践块**。
  - **参考实践块怎么拼**（`refract.md` §5.1，活动库降为参考知识库）：
    **不上向量检索**（项目既有立场）→ 用现成的 `activities.retrieve()` 拿候选，把 top-N（8–12 条）的
    `title` / `steps` 摘要 / `materials` / `success_criteria` 拼进 prompt；
    **同时把被硬过滤掉的活动和排除原因一起喂进去**（"家里没有必需材料：…"、"当前水平不适合难度 3"），
    让模型据此**生成替代材料/替代难度**的方案，而不是只会说"没有合适的活动"。
  - 输出 `CandidatePlan`（`extra="forbid"`）：`title` / `problem_addressed` / `based_on_facts[]` /
    `assumptions[]` / `steps[{action, prompt_level, time_limit_sec}]` / `total_minutes` /
    `materials[{name, essential, substitute}]` / `observation_metrics[]` / `stop_conditions[]` /
    `risks[]` / `reference_activity_ids[]`（可空 = 全新策略）/ `differentiation`（**强制字段**）。
  - 超时 90s（同建档抽取），重试 2 次，**降级路径**：`_fallback_candidates()` → 从活动库挑 2 条
    结构完整的候选（**绝不返回空列表**）。
- **转成方案项**：`plan_item.py::from_candidate(cand) -> Activity`（`kind='generated'`），
  再走阶段 1 建好的**同一条**打分/闸门/装箱流水线。
- **Python 校验**（0 token，硬约束）：① 总时长 ≤ 预算；② essential 材料不可得但有 substitute 才放行；
  ③ 必须有 `observation_metrics` 与 `stop_conditions`；④ 候选之间**不能只是改措辞**
  （`differentiation` 非空且 `steps` 签名不重合）；⑤ 安全：不允许"强行/惩罚/压制"类步骤。
  校验不通过的候选落 `status='rejected'` 并写明原因，**不静默丢弃**（与 `plan_nodes.validate_plan` 的 `changes` 同一路数）。
- **家长选择**：复用计划链已有的 ⚡ `approve_plan` interrupt，载荷扩展 action `"select_candidate"` + `candidate_id`；
  选定后落 `plans`（`source='llm_candidate'`、`case_id`、`cycle_id`、`based_on_assessment_version`），
  **下游 `/api/plan/{child}` 与 `/api/card` 的读路径一行不用改**（它们只认 `status='active'`）。
- **新表** `plan_candidates`：`id` / `case_id` / `cycle_id` / `based_on_assessment_version` / `content JSON` /
  `reference_activity_ids JSON` / `validation JSON` / `status` / `created_at`。
- **API**：`GET /api/cases/{case_id}/plan-candidates`、`POST /api/cases/{case_id}/plan-selection`。
- **前端**：`CandidatePlanCard.vue` —— 展示"试图解决什么 / 依据哪些已知信息 / 建立了什么假设 / 家长要做什么 /
  观察什么 / 什么情况停止 / 预计投入"，**不显示总分**（`refract.md` §10.3）；多张卡并列。

---

## 4. 阶段 4：执行 + 反馈 + 下一步决策 ⬜

对应使用流程的第 5–7 步：**执行 → 反馈 → 更新评估 → 决定继续/调整/更换/重新评估/暂停**。

- `ExecutionFeedback` 三层分离（`refract.md` §6.2）：
  **执行情况**（是否尝试 / 实际几次 / 哪些步骤 / 是否因家长精力不足而没做）/
  **当次表现**（目标行为是否出现 / 反应变化 / 需要多少帮助 / 是否出现新困难）/
  **跨时间效果**（不同日期是否一致变化 / 不同场景下是否变化 / 需要的帮助是否减少 / 有无新的负面反应）。
- `quality_for()` **保留**为兼容统计字段，但不再单独驱动状态升级。
- 新 LLM stage `decide_next_action`（LLM 提议 `{action, reasoning, evidence_ids, uncertainties, proposed_changes}`，
  `action ∈ {continue, adjust, replace, reassess, pause}`）+ **Python 批准层** `case/decision.py::approve_action`：
  - **永不批准**：`pause`（除非 urgent 红旗 → `store.plan_paused` 判定）、
    `replace`（除非新候选已生成且通过校验）、`reassess`（除非至少 1 条新证据）；
  - `adjust` 的改动只允许落在白名单（步骤 / 材料 / 时长 / 提示级别）。
  - `decision_log` 同时记 `proposed_action` 与 `applied_action` —— **模型提议与实际执行分开记**。
- 旧状态机降级为「约束与统计模块」：继续算、继续落 `skill_state`，但不再单独决定明天推什么难度（加开关，渐进关）。
- 词汇校验分层（`refract.md` §4.3）：结构字段 / 安全字段 / 引用对应关系继续严格；
  自然语言描述与新能力标签放开（生成项的 `target_skill_items` 可用词表外标签）。
- `case/router.py::next_step()` 落地：按 `cases.status` 决定下一步跑哪条链。

---

## 5. 阶段 5：历史回放与分支实验 ⬜

对应 `refract.md` §9 / §11 阶段三，也是本仓库 LangGraph 的 **Phase 5**（`LANGGRAPH.md` 第五节列了三条优先项）。

- 新表 `workflow_audit_events`（来源：`StateSnapshot.metadata` 的 `step` / `source`）。
- `GET /api/cases/{case_id}/history` 的 checkpoint 版本（`graph.get_state_history`）+ 分叉视图 + before/after diff。
- 分叉必须**换 thread_id**、`run_id` 跟着 thread_id 走、**分叉不得写业务事实表**
  （`refract.md` §9.4；三条唯一索引是 Phase 4 的幂等承诺，分叉必须尊重）。
- 四类使用场景（`refract.md` §9.3）：评估修正 / 方案对比 / 提示词与模型回归 / 反馈解析纠错。

---

## 6. 全局验收清单（重构收尾时逐条过）

- [ ] 多轮追问：能问、能停、不重复问
- [ ] 多候选：≥2 个且实质不同；家长能选、能改、能要求重生成、能暂缓
- [ ] 反馈关联：反馈能回指案例 / 方案版本 / 执行记录
- [ ] 决策可追溯：每次继续 / 调整 / 更换 / 重新评估 / 暂停都有 `decision_log` 行（含模型提议与实际动作）
- [ ] 安全：红旗 → 转诊 + 暂停；拒绝不被解释为"不配合"；单次成功不被当长期证据
- [ ] 重启可恢复：进程重启后 case 的阶段与待办仍在
- [ ] 幂等：同一反馈重复提交不多写一行、不白付 LLM
- [ ] 历史：能从 checkpoint 复现指定状态下的方案生成，且分支不覆盖真实事实
- [ ] 旧能力零回退：`/api/profile` `/api/plan/run` `/api/feedbacks` `/api/card` 行为不变
- [ ] **放宽没有变成绕过**：生成项同样过难度闸门、材料检查、预算与同一套权重打分

---

## 7. 附录：ID 与命名约定

| 概念 | 形式 | 备注 |
|---|---|---|
| `case_id` | `case:{child}:{seq}` | 业务主键，长期稳定 |
| case thread | `case:{case_id}` | LangGraph thread_id，长期 |
| `cycle_id` | `cycle:{case_id}:{n}` | 一轮"方案 → 执行 → 反馈 → 决策" |
| `assessment_version` | 该 case 内自增整数 | 对应 `assessment_versions.version` |
| `plan_id` / `plan_version` | 复用现有 `plans` 表 | 版本作用域仍是 `(child_id, plan_date)` |
| `execution_id` | `exec:{cycle_id}:{n}` | 对应 `sessions.execution_id` |
| 库内方案项 id | `activity_id`（`^[a-z]{2,4}-\d{3}$`） | `kind='library'` |
| 生成方案项 id | `gen:{candidate_id}:{item_index}` | `kind='generated'`；**必须稳定**（指纹不依赖它，但幂等键依赖它） |
| `evidence_id` | `case_evidence.id` | `decision_log.evidence_ids` 引用它 |
| `checkpoint_id` | LangGraph 提供的 | **不代替任何业务 ID**（`refract.md` §9.1） |
