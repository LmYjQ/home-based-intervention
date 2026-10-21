# 案例级重构（case-centric）—— 分阶段执行文档

> **这份文档是本次重构的唯一权威计划。**
> 每个新 session 开工前先读：`asd_agent/REFACTOR.md`（本文）→ `asd_agent/PROGRESS.md`（全局进度与踩坑）→
> `asd_agent/LANGGRAPH.md`（编排层的实测结论）。
>
> 需求输入：`refract.md`《基于现有基建的家庭干预智能体重构方案》（2026-10-21，最新版）。
> 旧的设计文档已归档到 `docs/legacy/`，**不要照着它们实现**。

**状态**：⬜ 未开始　🟡 进行中　✅ 已完成

| 阶段 | 内容 | 状态 |
|---|---|---|
| A | 最小闭环：多轮追问 → 多候选方案 → 家长选择 → 反馈关联 → 重启可恢复 | ⬜ |
| B | 反馈与决策重构：反馈三层分离 + `decide_next_action`（LLM 提议 / Python 批准） | ⬜ |
| C | 历史回放与分支实验：time travel + `workflow_audit_events` | ⬜ |
| D | 逐步放宽固定规则：活动库降为参考库、硬过滤分层、评分降为辅助 | ⬜ |

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
   │ （阶段 A 不动）       │ （阶段 A 扩展）        │ （阶段 B 扩展）
   │ publish 写库         │ publish_plan 写库     │ apply_transitions 写库
└──┬──────────────────────┴──────────────────────┴────────────────────────┘
   │
   │ ★ 新增第四张图：case:{case_id}（长期 thread，一个案例一条）
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
`activities.py`（50 条活动 / 6 条硬过滤 / 8 项权重 `WEIGHTS`：`state_match .30` `skill_gap .15` `prereq_ready .14`
`preference .10` `diversity .10` `material_fit .08` `method_fit .08` `parent_pref .05` + `negative_penalty` 扣分不硬剔除）、
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

## 1. 阶段 A：最小闭环 ⬜ ← **下一步从这里开始**

### A.0 目标与验收（可判定的）

**目标**：家长可以在一个"案例"里多轮描述问题 → 系统发现缺口并追问 → 信息足够后给出**多个实质不同的候选方案** →
家长选一个 → 真实执行反馈能关联到这个案例与这一版方案 → **服务重启后流程能恢复**。

**验收**（全部可自动判定）：
1. 家长两次补充信息，系统两次更新评估并**只问真正缺的**（不是重复问已答过的）。
2. 候选方案 ≥ 2 个，且彼此在执行步骤上**实质不同**（不是改措辞）。
3. 家长选定的那一版落到 `plans` 且 `status='active'`；`/api/plan/{child}` 与 `/api/card` 读到的就是它。
4. 反馈能关联到 `case_id` / `cycle_id` / `execution_id`。
5. 进程重启后，`GET /api/cases/{id}` 仍能显示当前处于哪个阶段、待办是什么。
6. 任何一步的 LLM 不可用时，系统**仍能走完**（走降级路径，不编数据、不返回空）。

### A.1 本阶段专属的坑（先看，再写代码）

1. **第四张图也要满足边界 #1**：case 图的 `publish_case_events` 是**唯一**写库节点，排在**所有 interrupt 之后**。
   现有三张图都是这个形状（`nodes.py:313` / `plan_nodes.py:324` / `run_nodes.py:352`），照抄。
2. **`Command(resume=)` 打在没有 pending interrupt 的 thread 上会让 LangGraph 从头重跑整条图** ——
   不报错、不抛异常，就是数据写了两遍。守卫必须在 `graph.invoke` **之前**，返回 **409**。
   现成写法：`server.py:827-829`（计划链审批）与 `server.py:993-997`（观察链澄清）。
3. **FastAPI 按声明顺序匹配路由**：`/api/cases/{case_id}/xxx` 必须声明在 `/api/cases/{case_id}` **之前**，
   否则会被参数路由吃掉（`server.py:736-740` 记着 `/api/plan/status` 的坑）。
4. **多轮追问 vs "只许问一次"**：现有 `clarify_asked` 守卫是为了防"系统把自己没听懂的锅推给人"（`LANGGRAPH.md` 第四节）。
   本阶段要放开多轮，但必须补上**三条硬约束**（见 A.3.5），否则那个坑会回来。
5. **checkpoint 只存纯 dict**（边界 #9）。`CandidatePlan` / `CaseEvidence` 是 Pydantic → 进 state 前转 dict。
6. **`plans` 表加列要走 `_migrate()`**，且索引跟着补列走；老库有重复行 → 建唯一索引前先回填。
7. **`store.plan_paused` 是唯一「暂停计划生成」口径** —— case 层要暂停就调它，不许自己写一遍 `exists urgent ...`。

### A.2 改动清单（按文件）

**新增**

| 文件 | 职责 | 关键函数 / 类 |
|---|---|---|
| `asd_agent/case/__init__.py` | 包入口 | 导出下列符号 |
| `asd_agent/case/models.py` | Pydantic 模型（`extra="forbid"`，字段带中文 description） | `Case` / `CaseEvidence` / `AssessmentVersion` / `CandidatePlan` / `CasePlanSelection` / `CaseClarify` / `CaseStatus` |
| `asd_agent/case/lifecycle.py` | 生命周期纯函数（0 token） | `ALLOWED_TRANSITIONS` / `can_transition(a, b) -> bool` / `next_status(current, *, has_unknowns, has_selected_plan, has_feedback, paused) -> str` |
| `asd_agent/case/evidence.py` | 证据抽取（LLM）+ 校验 + 降级 | `extract_evidence(...)` / `verify_evidence(draft, source) -> (draft, unverified)` / `_fallback_evidence()` |
| `asd_agent/case/candidates.py` | 候选方案生成（LLM）+ 校验 + 参考活动拼装 | `build_reference_block(req) -> str` / `generate_candidates(...)` / `validate_candidate(cand, req) -> (ok, problems)` / `_fallback_candidates()` |
| `asd_agent/case/router.py` | case 编排：下一步该做什么 | `next_step(case_row) -> Literal[...]` |
| `asd_agent/case/selfcheck.py` | case 层自检 | `_cli()` |
| `asd_agent/graph/case_graph.py` | case 图定义 + `case_inputs()` | `build_case_graph()` / `case_inputs()` |
| `asd_agent/graph/case_nodes.py` | case 图节点体 | `load_case_context` / `extract_new_evidence` / `update_assessment` / `safety_check` / `detect_unknowns` / `clarify_with_parent` / `publish_case_events` |
| `frontend/src/pages/CasePage.vue` | 案例页（新增页签） | — |
| `frontend/src/components/CaseStage.vue` | 生命周期徽标 | — |
| `frontend/src/components/CaseClarify.vue` | 追问卡片 | — |
| `frontend/src/components/CandidatePlanCard.vue` | 候选方案卡 | — |
| `frontend/src/components/EvidenceList.vue` | 事实/假设/未知三栏 | — |

**修改**

| 文件 | 改什么 |
|---|---|
| `asd_agent/store.py` | `_init()` 加 5 张新表；`_migrate()` 加 `plans` 4 列 + `sessions` 3 列；新增读写函数（`cases` / `assessment_versions` / `case_evidence` / `plan_candidates` / `decision_log`） |
| `asd_agent/graph/state.py` | 新增 `CaseState` TypedDict + `CaseClarify` / `CasePlanSelection` 载荷 + `case_thread_id()` / `case_config()`（前缀 `case:`） |
| `asd_agent/graph/runtime.py` | 新增第五张图的单例缓存与 getter（照抄现有四个 getter 的形状） |
| `asd_agent/graph/plan_nodes.py` | `approve_plan` 的 interrupt 载荷扩展：新增 action `"select_candidate"` + `candidate_id`（**不改旧枚举值**） |
| `asd_agent/server.py` | 新增 7 个 `/api/cases/...` 路由；旧路由**一字不改** |
| `frontend/src/App.vue` | `TABS` 加 `case` + `VALID` 白名单 + 路由声明顺序 |
| `frontend/src/api.js` | 新增 7 个 fetch 封装（唯一出口，URL 字面量不进组件） |
| `asd_agent/PROGRESS.md` | 每阶段完成后更新进度与自检组数 |
| `asd_agent/REFACTOR.md`（本文） | 勾状态、记录该阶段的实际决策与踩坑 |

### A.3 详细设计

#### A.3.1 数据库

**新增 5 张表**（DDL 放 `store.py` 的 `_init()`，列进 `_migrate()`，索引跟着补列走）：

| 表 | 列 | 唯一键 | 可变性 |
|---|---|---|---|
| `cases` | `case_id TEXT PK`, `child_id TEXT`, `thread_id TEXT`, `status TEXT`, `title TEXT`, `current_assessment_version INT`, `current_cycle_id TEXT`, `created_at`, `updated_at` | PK | **仅 `status` 可改** |
| `assessment_versions` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `version INT`, `known_facts JSON`, `hypotheses JSON`, `unknowns JSON`, `contradictions JSON`, `risk_flags JSON`, `evidence_snapshot JSON`, `created_at` | `uq_assess_version(case_id, version)` | 只追加 |
| `case_evidence` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `assessment_version INT`, `claim TEXT`, `quote TEXT`, `source TEXT`, `status TEXT`, `support TEXT`, `occurred_at TEXT`, `scene TEXT`, `created_at` | `uq_evidence_once(case_id, assessment_version, claim_hash, source_hash)` | 只追加 |
| `plan_candidates` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `cycle_id TEXT`, `based_on_assessment_version INT`, `content JSON`, `reference_activity_ids JSON`, `validation JSON`, `status TEXT`, `created_at` | `idx_cand_case(case_id, cycle_id, id DESC)` | **仅 `status` 可改**（selected / rejected） |
| `decision_log` | `id BIGINT IDENTITY PK`, `case_id TEXT`, `cycle_id TEXT`, `based_on_assessment_version INT`, `proposed_action TEXT`, `applied_action TEXT`, `reasoning TEXT`, `evidence_ids JSON`, `parent_decision TEXT`, `created_at` | `idx_decision_case(case_id, id DESC)` | 只追加 |

枚举值：

- `cases.status`（`refract.md` §7.1）：
  `assessing` / `awaiting_parent_input` / `ready_to_execute` / `awaiting_feedback` / `reviewing_outcome` /
  `revising_plan` / `paused` / `closed`
- `case_evidence.source`：`parent_message` / `parent_report` / `clinical_report` / `observation` / `model_inference`
- `case_evidence.status`：`parent_reported` / `observed` / `inferred` / `confirmed`
- `case_evidence.support`：`direct` / `partial` / `none`
- `plan_candidates.status`：`proposed` / `selected` / `rejected` / `superseded`

**改 2 张现有表**（只加列，不改语义，老数据不动）：

| 表 | 加什么 | 为什么 |
|---|---|---|
| `plans` | `case_id TEXT`, `cycle_id TEXT`, `based_on_assessment_version INT`, `source TEXT DEF 'activity_library'` | 让"生效方案"能回指案例与评估版本；`source` 让家长选择页知道它是模型生成还是活动库拼的 |
| `sessions` | `case_id TEXT`, `cycle_id TEXT`, `execution_id TEXT` | 反馈要能关联到案例与一次执行（`plan_id` / `plan_version` 已有） |

#### A.3.2 证据模型（refract.md §4.1/§4.2）

**两类检查必须分开**：

| 检查 | 谁做 | 怎么判 |
|---|---|---|
| **引用真实性**（原文是否真含这段话） | Python，0 token | `llm.check_quote(source, quote)`（`llm.py:182`）—— 沿用现有实现 |
| **结论支持程度**（这段话够不够支撑这个结论） | **受限枚举字段 + Python 规则** | LLM 在抽取时自评 `support: direct/partial/none`；Python 据此**禁止**升级状态 |

**硬不变量**（写进 `case/models.py` 的 model_validator，降级而不是报错 —— 与 `capabilities.py` 的
"inferred 高置信静默钳到 0.35"同风格）：

1. `status == "parent_reported"`（或 `confirmed`）但 `quote` 为空 → **不许进 `known_facts`**，降级为 `inferred`。
   ★ 这条直接修掉已查出的破口：模型自称 `report_stated` + 空引文，被 `check_quote` 空串放行
   （`llm.py:188`），以 ×3 权重把整份画像撑到 0.80、打开状态机升级闸门（`statemachine.py:237`）。
2. `support == "none"` → 一律不许进 `known_facts`（`confirmed` 需要 `support == "direct"`）。
3. `support == "partial"` → 可以进 `known_facts`，但必须带 `partial` 标记，且**不能**是 `confirmed`。
4. `status == "inferred"` → 置信度钳到 `< 0.4`（沿用 `INFERRED_MAX_CONFIDENCE = 0.35`）。

**矛盾处理**（refract.md §4.2 / FR-08）：同一 case 内两条 claim 冲突时**并列保留**，
在 `AssessmentVersion.contradictions` 里记下来，**不自动删任何一条**（原始陈述不可被覆盖）。

#### A.3.3 case 图

```
START → load_case_context → extract_new_evidence(LLM) → update_assessment
      → safety_check → urgent? ──是──→ escalate → publish_case_events → END
      → detect_unknowns → 信息不足? ──是──→ ⚡clarify_with_parent ──（回 extract_new_evidence）
      → 信息足够 → publish_case_events → END
```

- **thread_id = `case:{case_id}`**（长期稳定，一个案例一条）→ 对应 `refract.md` §9.1。
- `publish_case_events` 是**唯一**写库节点：写 `case_evidence` + `assessment_versions` + 改 `cases.status`。
- `safety_check` 直接复用 `redflag.scan()`（`redflag.py:280`）与 `store.plan_paused()`（`store.py:973`），
  **判定逻辑一字不改**；命中 urgent 时与观察链同款：先给转诊指引（`redflag.guidance_for`），但**仍要 publish**
  （`LANGGRAPH.md` 第四节之二记着"urgent 那天最该被记下来"）。
- 节点体默认纯函数；`extract_new_evidence` 是本图唯一的 LLM 节点。

#### A.3.4 候选方案（LLM 生成 + Python 校验 + 家长选）

**新 LLM stage `plan_candidates`**（`case/candidates.py`）：

- **输入**：当前 `AssessmentVersion`（known_facts / hypotheses / unknowns）+ 家长约束
  （`daily_minutes` / `materials_at_home` / `alone_only`）+ **参考实践块**。
- **参考实践块怎么拼**（`refract.md` §5.1，活动库降为参考知识库）：
  **不上向量检索**（项目既有立场）→ 用现成的 `activities.retrieve()` 拿候选，把 top-N（8–12 条）的
  `title` / `steps` 摘要 / `materials` / `success_criteria` 拼进 prompt；
  **同时把被硬过滤掉的活动和排除原因一起喂进去**（"家里没有必需材料：…"、"当前水平不适合难度 3"），
  让模型据此**生成替代材料/替代难度**的方案，而不是只会说"没有合适的活动"。
- **输出模型 `CandidatePlan`**（Pydantic，`extra="forbid"`）：
  `title` / `problem_addressed` / `based_on_facts[]` / `assumptions[]` /
  `steps[{action, prompt_level, time_limit_sec}]` / `total_minutes` /
  `materials[{name, essential, substitute}]` / `observation_metrics[]` / `stop_conditions[]` /
  `risks[]` / `reference_activity_ids[]`（可空 = 全新策略）/ `differentiation`（**强制字段**，与其他候选的实质区别）
- **Python 校验**（`validate_candidate`，0 token，硬约束）：
  ① 总时长 ≤ 家长预算；
  ② essential 材料不可得**但有 substitute** 才放行，否则剔除；
  ③ 必须有 `observation_metrics` 与 `stop_conditions`（`refract.md` §5.3）；
  ④ 候选之间**不能只是改措辞** —— 校验 `differentiation` 非空且与其它候选的 `steps` 签名不重合；
  ⑤ 安全：不允许出现"强行/惩罚/压制"类步骤（与红旗规则的 `avoid` 口径一致）。
  校验不通过的候选落 `status='rejected'` 并写明原因，**不静默丢弃**（与 `plan_nodes.validate_plan` 的 `changes` 同一路数）。
- **家长选择**：复用计划链已有的 ⚡ `approve_plan` interrupt，载荷扩展 action `"select_candidate"` + `candidate_id`；
  选定后 `freeze_selected_plan` 落 `plans` 表（`source='llm_candidate'`、`case_id`、`cycle_id`、`based_on_assessment_version`）。
  → **`/api/plan/{child}` 与 `/api/card` 的读路径一行不用改**（它们只认 `status='active'`）。
- **降级路径**（LLM 挂了必须还有路）：`_fallback_candidates()` → 从活动库按难度/材料挑 2 条，
  用活动字段填成结构完整的 `CandidatePlan` → 家长照样能选、能执行。**绝不返回空列表。**

#### A.3.5 多轮追问的三条硬约束

1. **一次只问 1 个问题**（`refract.md` §8.1："生成具体、易回答的问题"）。
2. **全局最多 3 轮**（`MAX_CLARIFY_ROUNDS`）。
3. **连续一轮没有新证据就不许再问** —— 判据：这一轮的回答**没有产生新的 `CaseEvidence`**。
   ★ 这条是"系统把自己没听懂的锅推给人"那个坑的解药：现有 `clarify_asked` 守卫把它压成"只许问一次"，
   本阶段放开多轮，就得用"有没有新信息"来替代"问没问过"。

#### A.3.6 路由（case 编排）

`case/router.py::next_step(case_row)` 返回下一步动作，**纯 Python 0 token**：

| `cases.status` | next_step | 触发什么 |
|---|---|---|
| `assessing` | `assess` | 跑 case 图（抽证据 → 更新评估） |
| `awaiting_parent_input` | `await_reply` | 等家长回答追问（不跑图） |
| `ready_to_execute` | `await_feedback` | 等真实执行（不跑图） |
| `awaiting_feedback` | `review` | 收到反馈 → 反馈链（阶段 B 再扩） |
| `reviewing_outcome` | `revise` | 需要出新候选 → 计划链 |
| `paused` | `await_reply` | 红旗未关闭（`store.plan_paused` 判定）→ 提示转诊 |
| `closed` | `none` | 归档 |

#### A.3.7 API

| 路由 | 形态 | 说明 |
|---|---|---|
| `POST /api/cases` | 直接 JSON | `{child_id, title, initial_text}` → 建 `cases` + 跑 case 图第一轮 |
| `POST /api/cases/{case_id}/messages` | job+SSE | 家长补充描述 → 跑 case 图（可能返回 `phase="clarify"`） |
| `POST /api/cases/{case_id}/clarify` | job+SSE | 回答追问（`Command(resume=)`；**无 pending interrupt 必须 409**） |
| `GET /api/cases/{case_id}` | 直接 JSON | 当前评估、生命周期状态、待办、最近决策 |
| `GET /api/cases/{case_id}/history` | 直接 JSON | 评估版本 / 证据 / 候选 / 决策时间线 |
| `GET /api/cases/{case_id}/plan-candidates` | 直接 JSON | 候选方案列表 |
| `POST /api/cases/{case_id}/plan-selection` | 直接 JSON | 选候选 / 要求重生成 / 暂不执行 |

- 旧路由 `/api/profile` `/api/plan/run` `/api/feedbacks` **一字不改**，继续可用（渐进迁移）。
- **路由声明顺序**：`/api/cases/{case_id}/xxx` 全部在 `/api/cases/{case_id}` 之前。
- **409 守卫**：照抄 `server.py:827-829`（计划链）的写法。

#### A.3.8 前端

- 新增 hash 页签 `case`（`App.vue` 的 `TABS` + `VALID`）→ `pages/CasePage.vue`。
- 组件：`CaseStage.vue`（阶段徽标）/ `CaseClarify.vue`（追问卡，一次一个问题）/ `CandidatePlanCard.vue`
  （展示"试图解决什么 / 依据哪些已知信息 / 建立了什么假设 / 家长要做什么 / 观察什么 / 什么情况停止 / 预计投入"，
  **不显示总分** —— `refract.md` §10.3）/ `EvidenceList.vue`（事实与假设必须一眼能分开）。
- 候选多张卡并列，每张底部「选这个」+「都不合适，重新生成」。
- 所有 fetch 走 `api.js`（唯一出口）。

### A.4 自检断言（`asd_agent/case/selfcheck.py`，建议 10 组）

1. 建案例 → `cases` 落库、`thread_id='case:{id}'`、`status='assessing'`
2. 补充信息 → `case_evidence` 追加行、`assessment_versions` 版本 +1
3. **编造的引文** → 降级为 `inferred`、不进 `known_facts`
4. **`status='parent_reported'` 但 `quote` 为空** → 不许进 `known_facts` ← **修破口的那条**
5. `support='none'` → 不许进 `known_facts`；`support='partial'` → 可进但不可 `confirmed`
6. 未知项非空 → `status='awaiting_parent_input'`，图停在 ⚡clarify
7. 回答后**没有新证据** → **不再问第二轮**
8. 信息足够 → 候选 ≥ 2 且 `steps` 签名互不重合
9. 家长选一个 → `plans` 落一行 `status='active'`、`source='llm_candidate'`、`cases.status='ready_to_execute'`
10. **无 pending interrupt 时 `Command(resume=)` → 409**（不重跑图、不多写一行）

另加：
- `http_selfcheck.py` +1 组（建案例 → 补充 → 选方案 → 反馈 → 读回，**真起服务打接口**）
- `graph/selfcheck.py` +1 组（case 图 interrupt 往返 + 恢复不双写，**必须用 PostgresSaver**）

### A.5 验证命令

```bash
cd asd_agent
ASD_DB_PATH=/tmp/graph_selfcheck.db   ../.venv/bin/python -m graph.selfcheck    # 29 → 30+
ASD_DB_PATH=/tmp/graph_ckpt_check.db  ../.venv/bin/python -m graph.checkpointer
ASD_DB_PATH=/tmp/http_selfcheck.db    ../.venv/bin/python http_selfcheck.py     # 13 → 14+
ASD_DB_PATH=/tmp/case_selfcheck.db    ../.venv/bin/python -m case.selfcheck     # 新增
../.venv/bin/python activities.py --validate
cd ../frontend && npx vite build
```

**真起服务打接口**（`server.py` 不在回归集里，这步不能省）：

```bash
cd /Users/qiyusama/Documents/code/ganyu
ASD_DB_PATH=/tmp/case_live.db ASD_NO_LLM=1 PYTHONPATH=.:./asd_agent \
  ./.venv/bin/python -c "import uvicorn,server; uvicorn.run(server.app,host='127.0.0.1',port=8123,log_level='warning')"
# 建案例 → 补充 → 答追问 → 生成候选 → 选方案 → 读 /api/cases/{id} → 提交反馈 → 再读
```

每一步都要能说清"这一请求付了几次 LLM、写了哪几张表"。

### A.6 提交要求

- 一个 commit，可 revert；message 写"为什么"（例：`案例级重构 A1：cases/case_evidence 表 + case 图（末尾唯一写库）`）。
- 提交前更新 `asd_agent/PROGRESS.md`（自检组数、验证结果、踩坑）与本文（勾状态 + 记录实际决策）。
- 涉及 `store.py` 表结构的改动 → **必须重启后端**（`store._init` 只在某个 schema 首次创建时跑，
  跑着的服务不会凭空多出新表，新路由会 500 `relation "cases" does not exist`）。

### A.7 交接给下一个 session 的话

阶段 A 完成后，在本文「阶段 A」末尾追加一段 `### A.8 本阶段实际发生的事`：
- 哪些设计在实现时被推翻、为什么；
- 新增/改动的文件清单（最终版）；
- 自检组数从多少到多少；
- 遗留项与已知妥协；
- **下一阶段的第一件事**是什么。

---

## 2. 阶段 B：反馈与决策重构 ⬜

对应 `refract.md` §6 / §11 阶段二。

- `ExecutionFeedback` 三层分离：**执行情况**（是否尝试 / 实际几次 / 哪些步骤 / 是否因家长精力不足而没做）/
  **当次表现**（目标行为是否出现 / 反应变化 / 需要多少帮助 / 是否出现新困难）/ **跨时间效果**（不同日期是否一致变化 /
  不同场景下是否变化 / 需要的帮助是否减少 / 有无新的负面反应）。
- `quality_for()` **保留**为兼容统计字段（照 `refract.md` §6.2），但不再单独驱动状态升级。
- 新 LLM stage `decide_next_action`（LLM 提议 `{action, reasoning, evidence_ids, uncertainties, proposed_changes}`，
  `action ∈ {continue, adjust, replace, reassess, pause}`）+ **Python 批准层** `case/decision.py::approve_action`：
  - **永不批准**：`pause`（除非 urgent 红旗 → `store.plan_paused` 判定）、
    `replace`（除非新候选已生成且通过校验）、`reassess`（除非至少 1 条新证据）；
  - `adjust` 的改动只允许落在白名单（步骤 / 材料 / 时长 / 提示级别）。
  - `decision_log` 同时记 `proposed_action` 与 `applied_action` —— **模型提议与实际执行分开记**。
- 旧状态机降级为「约束与统计模块」：继续算、继续落 `skill_state`，但不再单独决定明天推什么难度（加开关，渐进关）。
- 词汇校验分层（`refract.md` §4.3）：结构字段 / 安全字段 / 引用对应关系继续严格；自然语言描述与新能力标签放开。

## 3. 阶段 C：历史回放与分支实验 ⬜

对应 `refract.md` §9 / §11 阶段三，也是本仓库 LangGraph 的 **Phase 5**（`LANGGRAPH.md` 第五节列了三条优先项）。

- 新表 `workflow_audit_events`（来源：`StateSnapshot.metadata` 的 `step` / `source`）。
- `GET /api/cases/{case_id}/history` 的 checkpoint 版本（`graph.get_state_history`）+ 分叉视图 + before/after diff。
- 分叉必须**换 thread_id**、`run_id` 跟着 thread_id 走、**分叉不得写业务事实表**
  （`refract.md` §9.4；三条唯一索引是 Phase 4 的幂等承诺，分叉必须尊重）。
- 四类使用场景（`refract.md` §9.3）：评估修正 / 方案对比 / 提示词与模型回归 / 反馈解析纠错。

## 4. 阶段 D：逐步放宽固定规则 ⬜

对应 `refract.md` §5 / §11 阶段四。

- 活动库彻底降为参考知识库；最终方案**可以不对应任何 activity_id**。
- 硬过滤分层（`refract.md` §5.2）：预算=硬约束；材料不可用→优先生成替代方案；抗拒冷却→提醒与调整信号
  （不自动等同于"孩子不配合"）；前置未完成→改成检查该方案的真实前提；难度门槛→改成对具体方案的适配性与风险检查；
  最近三天重复→保留提醒但允许有理由地继续。
- 8 项评分从"选唯一方案"改为辅助评估。
- 每条规则改动都要有**明确的作用域、触发原因和日志**（否则重构后无法解释某个方案为何被过滤）。

---

## 5. 全局验收清单（重构收尾时逐条过）

- [ ] 多轮追问：能问、能停、不重复问
- [ ] 多候选：≥2 个且实质不同；家长能选、能改、能要求重生成、能暂缓
- [ ] 反馈关联：反馈能回指案例 / 方案版本 / 执行记录
- [ ] 决策可追溯：每次继续 / 调整 / 更换 / 重新评估 / 暂停都有 `decision_log` 行（含模型提议与实际动作）
- [ ] 安全：红旗 → 转诊 + 暂停；拒绝不被解释为"不配合"；单次成功不被当长期证据
- [ ] 重启可恢复：进程重启后 case 的阶段与待办仍在
- [ ] 幂等：同一反馈重复提交不多写一行、不白付 LLM
- [ ] 历史：能从 checkpoint 复现指定状态下的方案生成，且分支不覆盖真实事实
- [ ] 旧能力零回退：`/api/profile` `/api/plan/run` `/api/feedbacks` `/api/card` 行为不变

---

## 6. 附录：ID 与命名约定

| 概念 | 形式 | 备注 |
|---|---|---|
| `case_id` | `case:{child}:{seq}` | 业务主键，长期稳定 |
| case thread | `case:{case_id}` | LangGraph thread_id，长期 |
| `cycle_id` | `cycle:{case_id}:{n}` | 一轮"方案 → 执行 → 反馈 → 决策" |
| `assessment_version` | 该 case 内自增整数 | 对应 `assessment_versions.version` |
| `plan_id` / `plan_version` | 复用现有 `plans` 表 | 版本作用域仍是 `(child_id, plan_date)` |
| `execution_id` | `exec:{cycle_id}:{n}` | 对应 `sessions.execution_id` |
| `evidence_id` | `case_evidence.id` | `decision_log.evidence_ids` 引用它 |
| `checkpoint_id` | LangGraph 提供的 | **不代替任何业务 ID**（`refract.md` §9.1） |
