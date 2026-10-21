# 家长干预教练 Agent — 进度交接

面试 demo。完整设计方案见 `~/.claude/plans/app-mvp-jolly-kazoo.md`（已批准）。
本文件记录**实际做到哪、验证结果如何、下一步做什么**，供换 session 继续。

---

## 🔖 换 session 先看这一屏（2026-10-19 晚更新）

> ## ⚡ 2026-10-20 新增：LangGraph 接入（**新 session 优先看这份**）
>
> **`asd_agent/LANGGRAPH.md`** ← 换 session 动手前先读那份，里面有：
> - 已装依赖与 PG 启动（`langgraph` **本来就在 1.2.16**；新装 `langgraph-checkpoint-postgres` 3.2.0）
> - **LangGraph 全部关键行为的实测结论**（interrupt 恢复会重跑节点、分叉必须换 `thread_id`、
>   `thread_id` 不可复用、`PostgresSaver` 没有 `schema=` 参数…）—— **别抄文档，1.x 与网上示例有出入**
> - **Phase 0 / 1 / 2 / 3 / 4 均已完成**（2026-10-21 复核全绿）。图级验收 9 → **14 → 23 → 29 组**断言，
>   `http_selfcheck.py` **9 → 10 → 12 组**。Phase 1 的接线约束见第四节之二；
>   **Phase 4 开工必读 = 第四节之五**（图长什么样、幂等的两层、run_id 是什么、
>   next_action 的判据、六个坑；带 ⚠️ 的没重新验证过）。
> - **★ 下一步 = Phase 5（time travel）**。**开工前必读 LANGGRAPH.md 顶部的
>   「🎯 Phase 5 开工前必须知道的四件事」**（run_id 必须跟着 thread_id 走、
>   三条唯一索引是 Phase 4 的幂等承诺、分叉必须重新过安全闸门与审批、
>   `workflow_audit_events` 还没有）。
>
> **★ 一条会静默咬人的实测结果**：`interrupt()` 恢复会重跑所在节点这件事
> **只在 `PostgresSaver` 下可观测**，用 `InMemorySaver` 写单测会看到"只跑了一次"，
> 于是推断"恢复不重跑"，再把写库放到 interrupt 之前 —— 上生产就双写。
> 探针在 `graph/selfcheck.py::_probe_rerun`，别删。
>
> **`store.py` 已改：连接加 `autocommit=True`（`store.py:100`）** —— 这不是风格调整。
> 原长连接非 autocommit + 全项目零 `rollback()` ⇒ 每次读都留下永不结束的事务 ⇒ 连接长期停在
> `idle in transaction` ⇒ 把 `PostgresSaver.setup()` 的 `CREATE INDEX CONCURRENTLY` 卡死
> （CONCURRENTLY 等的是**整个 database** 的 virtualxid，与 schema 无关）。
> 语义零变化：11 个 `commit()` 全在单个写函数结尾，无一处多语句事务。
> 那些 `commit()` **保留**，别去「清理」。
> 验证：12/12 离线回归 + 180 并发 0 失败 + HTTP 全链路 + 跑完无 idle-in-transaction。
>
> **Phase 1 做完了四件事**：
> 1. **四层字段**（技术方案 §4）：`fact_fields` 在 `schema.Observation`（逐事件）；
>    另三层在 `observe.ObservationBatch`（跨事件）。**全部可选带默认值** ——
>    required 会让 `server.py` 那条裸 `except: continue` 的重校验**静默丢掉所有历史观察**。
> 2. **澄清 interrupt 接进 HTTP**：`/api/observe/run`（可能返回 `phase:"clarify"`）→
>    `/api/observe/resume` → `/api/observe/status`（刷新后重新发现待答的追问）。
> 3. **`TodayPage.vue` 的 `canSubmit` 修了**：原来完全不看 `otherNotes`，
>    家长只写自然语言观察时按钮点不动 —— 技术方案 §1.1 第一步在界面上走不通。
> 4. **草稿存 checkpoint**：前端此前零持久化，填一半刷新即丢。
>
> **★ 三个会静默咬人的结论**（细节见 LANGGRAPH.md 第四之二节）：
> - `Command(resume=...)` 打在没有 pending interrupt 的 thread 上，
>   **会被当成新输入从头跑一遍整条图**（重新抽取、重新落库）。守卫 = 409，挡在 invoke 之前。
> - 中断判定要用 `snap.next` / `snap.interrupts`，**不能用** state 里的 `needs_clarification`
>   —— resume 跑完之后那是上一次的值，会把「已答过」误报成「还在等」。
> - 写库的 `observed_at` 与幂等判据查的日期**必须同一个键**。
>   曾不一致，于是每次跑都重写一遍、行还落到空日期上（新 selfcheck [11] 抓到的）。
>
> **★ prompt 的一条经验**：7B 对四层的抽象描述**三层全空**（合规输出 `null`/`[]`
> 但没内容，schema 标不标 required 都一样）；改成「照这个例子写」才抽得出来。
> 小模型跟例子，不跟论述。新规则单独放 `_RULES_LAYERS`，**绝不能动与 verify_schema
> 逐字对齐的 `_RULES_1_6`**（selfcheck [12] 把这件事变成机器可验的不变量）。
>
> **`asd_agent/http_selfcheck.py` 是新增的**（**9 → 10 组**）—— `server.py` **不在任何回归集里**，
> 而 Phase 1 往里面接了图。不补这一层就等于把刚做对的东西放在没人守的地方。
>
> **Phase 2 ✅（2026-10-20，安全链：红旗落库 + 转诊指引）**。细节见 LANGGRAPH.md 第四节之三：
> - **`escalate → END` 改成了 `escalate → publish → END`。** 判定一行没动，
>   改的是"判完之后这一天还写不写" —— 原来 urgent 那天的家长原话**从不进
>   `observations`**，最该记下来的那天什么都没留下。
>   放在 `publish` 而不是 `escalate` 的真正理由：Phase 3 要在 `safety_gate` 之后
>   插 `⚡ approve_plan`，写在 `escalate` 就变成"interrupt 之前写库"。
> - **新表 `red_flags`**，唯一索引 `(child_id, observed_at, code, source_hash)`
>   + `ON CONFLICT DO NOTHING`。键里**不含 `thread_id`**（分叉重跑不污染真实历史）。
>   `status` 是**全项目唯一可变的业务字段**，行的身份仍 append-only。
> - **转诊指引长在 `redflag.RULES` 每一项上**（`who`/`do`/`avoid`/`emergency`，
>   `avoid` 是防害字段不允许为空）。多个 urgent 逐条给，不揉成一段；
>   `escalation.guidance` **仍是一句 str**（老 checkpoint 与老前端读的就是它）。
> - **★ 给 Phase 3 的合同**：`store.plan_paused(child_id)` 是「暂停计划生成」的
>   **唯一口径**（存在 urgent 且未关闭；按孩子不按天）。`generate_plan` 必须调它，
>   不许自己再发明一个。前端也只读后端返回的 `paused`，不自己从列表推。
> - 前端新建 `RedFlagBanner.vue`（不复用 `RedFlagList.vue`：一个吃扫描结果无按钮，
>   一个吃已落库的行要按状态给动作），**置顶**在"今天的安排"之上。
>
> **Phase 3 ✅（2026-10-21，计划链：审批 interrupt + 计划版本化）**。
> 细节见 LANGGRAPH.md 第四节之四，**Phase 4 必读那一节**：
> - **"系统排的计划"第一次要家长点头。** `plans` 表从 4 列变 8 列
>   （`status`/`version`/`approved_by`/`approved_at`）—— 之前"最后写入即生效"，
>   而"生效"这件事没有任何记录。`status` 沿用 `red_flags` 同一套可变字段约定。
> - **计划链是独立的一张图**（`graph/plan_graph.py`，thread 前缀 `plan:`），
>   不是观察图后面接一段。**它与观察图不共用 thread** —— 同 thread 的 invoke
>   会接着上一次跑，两条链共用的话家长写一段话就会把计划链的 run 续下去。
> - **实质性变更判据** = `planner.is_substantive_change()`（纯函数、零 token），
>   比的是**指纹**不是逐字段 diff。逐字段比会把每次重新生成都变成一次打断，
>   把"只在真变化时审批"这条判据自己吃掉。
> - **`/api/card` 的卡片缓存搬到新表 `day_cards`**。原先它写 `plans` 表，
>   于是 `active_plan()` 把一张卡片当成一份已批准的计划端出去 ——
>   卡片 payload 没有 fingerprint，下次审批拿它比指纹判成"什么都没变"，
>   **任何真变化都不再打断家长，审批从此静默失效。**
> - **暂停时落 `status='paused'`**，否则 `/api/card` 会把"今天什么都不做"
>   当成一份合法计划端出去。
> - **`planner.plan_week` 的去留已定**：保留作 CLI demo 的周计划，HTTP 走 `pack_day`。
>   接进 `generate_plan` 是错的（一个是周、一个是天，会每天都判成有变更）。
>
> **下一步 = Phase 5（time travel）**：`GET /api/threads/{id}/history` →
> `graph.get_state_history(config)`；分叉视图 + 前后 diff；
> `workflow_audit_events` 表落库。
> **开工顺序**：先读 `LANGGRAPH.md` 的「🎯 Phase 5 开工前必须知道的四件事」
> 与「四之五、Phase 4 ✅ 已完成」那一节 —— 里面有本轮踩到的六个坑。
> **别只读这一屏就开工**：Phase 5 要做分叉，而分叉必须尊重 Phase 4 的
> 三条唯一索引（键里都有 `run_id`）—— 踩坏了是**静默的状态抖动**
> （重跑一次多一行转移）。
>
> **★ Phase 3 已为反馈链铺好路**（LANGGRAPH.md 顶部第 4 条）：
> `sessions` 有 `plan_id`/`plan_version`，`store.log_session` 会**自动**把练习
> 挂到那天生效的那一版计划下（查不到存 NULL，不乱挂）——
> "这次执行属于哪一版安排"因此有了落点，
> 而 `apply_transitions` 的幂等键可以用
> `(child_id, skill_item, run_id, from_state, to_state)`，
> 其中 `run_id` 由 thread_id + checkpoint_id 派生，分叉天然不撞。

> **Phase 4 ✅（2026-10-21，反馈链：幂等键 + 「接下来怎么办」）**。
> 细节见 LANGGRAPH.md 第四节之五，**Phase 5 必读那一节**：
> - **第三张图**（`graph/run_graph.py`，thread 前缀 `run:`）：
>   `record_execution → analyze_outcome → next_action → apply_transitions`，
>   直线，没有 interrupt。**写库只在最后一个节点** ——
>   这条用「跑一遍去掉写库节点的图」验（selfcheck [28]），
>   而不是用 trace 顺序糊弄过去。
> - **★ 修掉一条真 bug**：`/api/feedbacks` 以前没有幂等保护，
>   重复提交 → sessions 里两条 refused → `refusal_counts` 翻倍到 2
>   → **当天误触冷却阈值** → 那条活动被踢出明天的候选，
>   而界面上看不出为什么。现在**两层**保证：
>   落库那半（三个唯一索引 + upsert）+ checkpoint 那半（输入指纹 →
>   直接返回上次结果，**不重跑图、不重付 LLM**）。
> - **`run_id` = 「一天的执行」的身份**（= thread_id = `run:{child}:{date}`），
>   进 `sessions` / `skill_state` / `daily_summary` 三条唯一键。
>   分叉出来的 thread 不同 → run_id 不同 → **分叉不撞真实历史**。
> - **`next_action`：`continue` / `adjust` / `pause` / `escalate`**，
>   **纯 Python 判**（红旗 / `store.plan_paused` / 回退型转移），
>   随响应给前端。
> - **★ 顺带修掉一个安全洞**：家长的原话有**两个入口** ——
>   「其他观察」框（走观察链）与逐条活动的补充描述（走反馈链）。
>   以前只有前者被红旗扫过，于是"他玩着玩着就去咬自己的袖子"
>   **从来没被扫过** —— 不是扫了没命中，是压根没扫。
> - **`/api/card` 读已生效的那一版计划**（遗留项 3）：
>   教练的 `ease`/`swap` 不再生效（批准过的那一版不许被换掉），
>   缓存的键加上计划版本（批了第 2 版 → 旧卡片自动作废）。
> - `sessions` 补 `prompt_level` / `steps_done` 两列 ——
>   它们以前只以中文形式塞在 `note` 里，结构化数据取不回来。
>
> **★ 本轮踩到的两个坑**（细节见 LANGGRAPH.md 第四节之五）：
> - **老库里合法存在的重复行会让 `CREATE UNIQUE INDEX` 直接失败**，
>   而它失败在迁移函数里 → 整个库连不上，症状是"服务起不来"，
>   真正的原因一个字都没提。→ 先回填再建索引（已用模拟老库实测）。
> - **`loop.py` 的 live 路径在第一次出卡片时就崩**
>   （`out_prev = []` 把历史扔掉 + 类型传错）。
>   12 个离线回归一个都不碰 live 路径，所以没人发现。
>   **已修，但 live 路径本轮未实跑**（没有 replay fixture）。

**✅ 工作区已提交**（2026-10-21，分支 `pg-langgraph-baseline`）：
```
aa70b6d Phase 0：LangGraph 编排层地基
8a2b221 Phase 1 步 1-2：四层字段 + publish 幂等键重挂
13d74ab Phase 1 步 3-4：图接进 HTTP（两阶段澄清 + 草稿）
adbf9aa Phase 1 步 5：前端接上观察链 + 草稿持久化
3f653cc PROGRESS：交接屏更新到 Phase 1
a25acc5 Phase 2 步 1：红旗落库能力（规则 + 表 + 节点体）
14e1f38 Phase 2 步 2：改边 + HTTP 面（escalate 之后仍然走 publish）
b30c28c Phase 2 步 3：置顶红旗横幅（前端）
d0dccbf PROGRESS/LANGGRAPH：交接屏更新到 Phase 2
7d74d2a Phase 3 步 1：计划链底座（plans 版本化 + 实质性变更判据）
ba68932 Phase 3 步 2：计划链图 + 审批 interrupt
d082899 Phase 3 步 3：图级验收 14 → 23 组
78362f4 Phase 3 步 4：审批 interrupt 接进 HTTP（HTTP 自检 9 → 10 组）
8bf40ec Phase 3 步 5：前端接上计划审批面板
8a9008f Phase 4 步 1：run_id 与幂等键（存储层底座）
ad0edba Phase 4 步 2-3：反馈链图（第三张图）+ 图级验收 23 → 29 组
abc5521 Phase 4 步 4：反馈链接进 HTTP（HTTP 自检 10 → 11 组）
49bca92 Phase 4 步 5：/api/card 读已生效的那一版计划（遗留项 3）
```
每一步都是可 revert 的独立 commit，改既有行为的那步单独成步。

> Phase 3 沿用 Phase 2 的拆法：步 1 只加能力（表 + 判据 + 迁移）不动图的边；
> 步 2 才建图；步 3 补断言（与建图分开是因为这一轮**没有改既有断言**——
> 与 Phase 2 不同，Phase 3 没动观察图的边，所以不存在"旧断言把 bug 写成规范"的问题）；
> 步 4 接 HTTP；步 5 是前端（依赖步 4 的 API）。

**⚠️ 提交时未纳入版本库**：`p3-review-panel.png` / `p3-after-approve.png`
（Phase 3 手测截图，纯留档）。

> Phase 2 刻意拆成三步而不是一个大 commit：
> 步 1 只加能力（规则 + 表 + 节点体），**不改图的边**；
> 步 2 才改边，并**同时**改它牵动的断言 ——
> 旧断言 `trace[-1] == "escalate"` 把 bug 写成了规范，
> 改边与改断言必须同一个 commit，否则中间那个 commit 是不绿的。
> 步 3 是前端（依赖步 2 的 API）。

**✅ 存储层已换成 PostgreSQL，并发 500 随之消失**（180 请求 0 失败，迁移前失败 8 个）。
详见「✅ 迁移到 PostgreSQL 已完成」一节 —— 那里有改动清单、
sqlite→postgres 对照表、三个设计决定、以及踩到的坑。

**⚠️ 跑任何东西之前先起 PG 容器**（**端口绑 127.0.0.1**，不要绑 0.0.0.0）：
```bash
docker run -d --rm --name ganyu_pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ganyu -p 127.0.0.1:55432:5432 \
  -v ganyu_pgdata:/var/lib/postgresql/data 81bd698b4594
```

**⚠️ 本机 8000 端口被别的程序占着**（实测：另一个 python 进程，HTTP 404）。
起后端请用 **8123**，并把 `frontend/vite.config.js` 的代理 target 同步改成 8123 ——
不改的话前端会打到别人的服务上，满屏 404。

**前端手测**：`frontend/MANUAL_TEST.md` —— 逐步写了输入什么、预期看到什么，
核心那条断言是 `refusal_counts = {"gro-007": 1}`（只有家长亲手填「不配合」的那条）。

**动手前必读的两节**：
1. 「✅ 迁移到 PostgreSQL 已完成」—— 尤其坑 1（**批量替换漏了 server.py，
   12 个离线回归全绿但 `/api/card` 500**；跨进程接线必须单独打接口）
2. 「🔜 待定：daily 的 direction 要改成 weekly」—— 有两个问题待用户确认

**当前入口**：
```bash
# CLI（12/12 全绿，0.4s）
cd asd_agent && ../.venv/bin/python loop.py --no-llm --script hardcoded
# 前后端（ASD_DB_PATH 只决定 schema 名，连接串是 ASD_PG_DSN）
ASD_DB_PATH=/tmp/ganyu_dev.db ASD_NO_LLM=1 PYTHONPATH=.:./asd_agent \
  ./.venv/bin/python -c "import uvicorn,server; uvicorn.run(server.app,host='127.0.0.1',port=8123,log_level='warning')"
cd frontend && npm run dev        # ⚠️ 5173 被占时自动退到 5174
```

**三个自检入口**（改图 / 改 server.py 之后必须跑，全绿才算数）：
```bash
cd asd_agent
ASD_DB_PATH=/tmp/graph_selfcheck.db  ../.venv/bin/python -m graph.selfcheck    # 29 组
ASD_DB_PATH=/tmp/graph_ckpt_check.db ../.venv/bin/python -m graph.checkpointer
ASD_DB_PATH=/tmp/http_selfcheck.db   ../.venv/bin/python http_selfcheck.py     # 12 组
```

**⚠️ 本项目 venv 是 uv 建的，没有 pip** —— 装包用
`uv pip install --python .venv/bin/python <pkg>`。

**三条最容易被回退的正确设计**（下面每条都有专门章节，别顺手改回去）：

1. **家长端是逐条填的，不是一段话**（阶段 4b）。`refusal_sessions` 收单个
   `activity_id: str | None`，不做「事件 × 活动」的笛卡尔积。
   改回 `set[str]` 会让没做过的活动白得抗拒记录。
2. **outcome/quality 由家长点，不让模型猜**（阶段 4b）。`quality_for()` 是
   确定性函数。改回提示词里的自由裁量，状态机闸门就形同虚设。
3. **模型给证据，Python 做判定**（贯穿全项目）。状态转移只由
   `statemachine.propose_transition()` 的计数规则决定，日结的方向判断
   只是证据、不是结论。

**最容易踩的坑**（写断言/接线前必读）：
- 写断言前先读一遍被断言的函数 —— 阶段 4 的三条断言全是按直觉写的，
  第一次跑就误报 3 条，挡住的全是正确行为（阶段 4 坑 2）
- `skip 了 build_request 的 before=` → 今天的反馈会回过头来解释今天
- `card.record_prompt` 里判据说两遍 → 超 60 字，Pydantic 直接报错

---

## 一句话定位

把医生的评估报告转成家长每天在家能执行的干预计划，并根据每天反馈动态调整。
四个展示点：**多 Agent 协作 / RAG / 长期记忆 / 状态机**。

---

## 项目结构

```
ganyu/
├── .env                      SILICONFLOW_API_KEY / BASE_URL / LLM_PROVIDER / LLM_TIMEOUT_S
├── asd_agent/                平铺，不建包（现有风格）
├── asd_agent/PROGRESS.md     ← 换 session 动手前先读这份
├── asd_agent/sources.md      方法级溯源（文献/指南/证据边界）
└── asd_agent/results/2026-10-16/   verify_schema 的 A/B 实验产物
```

### 已有（阶段 0，原有，未改动）
| 文件 | 行数 | 说明 |
|---|---|---|
| `vocab.py` | ~290 | 五张受控词表 + **新增五能区/技能细项/状态机/材料类别** |
| `schema.py` | 106 | `Observation` Pydantic，`Literal` 约束 |
| `verify_schema.py` | 457 | A/B 实验脚本，**不要改，它是立论基础和回归集** |

### 阶段 1 已完成 ✅
| 文件 | 行数 | 说明 |
|---|---|---|
| `llm.py` | ~300 | **唯一的 LLM 出口**：`chat_json()` + `LLMTrace` + live/replay/capture + `check_quote()` |
| `activities.py` | ~640 | `Activity`/`Step`/`Material` + `PreferenceSet` 喜好模型 + `PlanRequest` + `retrieve()` 硬过滤与打分 + `preferred_hooks()` + `validate_all()` |
| `activity_data.py` | ~1290 | 50 条活动数据（10 大运动 / 8 精细 / 10 语言 / 10 认知 / 10 社会适应 + 8 条跨能区） |

### 阶段 2 已完成 ✅
| 文件 | 行数 | 说明 |
|---|---|---|
| `capabilities.py` | 314 | `SkillAssessment`/`ParentConstraints`/`AbilityProfile` + 两条不变量 |
| `profile.py` | 424 | 报告 → 画像，一次 `chat_json` + 红旗前置 + 回指校验 + 降级 |
| `statemachine.py` | 416 | 五态机，**纯函数、零 token、可重放** |
| `store.py` | 696 | 7 张表，`skill_state` 只追加 + 三个必需查询 |
| `redflag.py` | 194 | 独立规则扫描，11 条规则 × 三档严重度 |

### 阶段 3 已完成 ✅
| 文件 | 行数 | 说明 |
|---|---|---|
| `planner.py` | 448 | 规划 Agent：候选由 Python 检索，LLM 只编排 + 写 why_this |
| `coach.py` | 553 | 教练 Agent：诊断（可校验推理链）→ 家长卡片 |
| `evaluator.py` | 428 | 评估 Agent：只给证据，判定留给状态机 |
| `sources.md` | 130 | **方法级溯源**（用户补充）：文献出处、指南锚点、证据边界 |

### 阶段 4 已完成 ✅（闭环接线）
| 文件 | 行数 | 说明 |
|---|---|---|
| `observe.py` | ~752 | 家长一段话 → 多条结构化事件 + 红旗 + 回指校验 + 降级。**（阶段 4b 改：加逐条模式的 `SYSTEM_SINGLE`，`refusal_sessions` 收敛到单条）** |
| `loop.py` | ~801 | **唯一 demo 入口**：端到端链路 + **五日**闭环 + `assert_loop_closed()` 交叉核对 |
| `vocab.py`（改） | +60 | FBA 功能分类 `CONSEQUENCE_FUNCTION` / `function_of()` |
| `store.py`（改） | +15 | session 补 note 列；红旗计数改 `COUNT(DISTINCT observed_at)` |
| `coach.py`（改） | +130 | 接上 ABC；功能一致性校验；功能驱动的加强规则。**（阶段 4b 加 `render_feedback_form()`）** |

> 阶段 4b 又新增了 `feedback.py` / `summary.py`，并把 `evaluator.py` 从 428 行砍到 166 行。
> 详见下方「阶段 4b」一节。


---

## 阶段 1 验证结果（全部通过）

```bash
cd asd_agent && ../.venv/bin/python activities.py --validate   # ✓ 全部通过
```

```
活动库：50 条
  能区分布：大运动=10  精细动作=13  语言=14  认知=21  社会适应=16
  方法分布：NDBI=33  ESDM=45  ABA=13  PRT=9  RBT=2
✓ 全部通过：技能细项/材料类别/行为词表/场景词表/依赖图 均无问题
```

检索器在三种约束下的实测：

| 场景 | 通过/总数 | 验证点 |
|---|---|---|
| 语言/社会落后，30分钟，避开高感官，材料只有杯子和绘本 | 11/50 | 材料硬过滤剔除 15 条；难度门槛剔除 1 条 |
| 只有 15 分钟，零道具 | 10/50 | 时间预算剔除 5 条；仍返回非空 |
| soc-001 冷却中、soc-002 成功 4 次、soc-006 最近排过 | 13/50 | 冷却期/前置/新鲜度/近期排重全部按预期生效 |

时间预算扫描（10/15/20/30/45 分钟）：全部返回非空候选，单条最长时长 8/12/16/16/16 分钟。

喜好功能的验证结果：
```
喜欢（soc-004 活动 + 小汽车材料 + 洗澡场景）
  0.880 soc-004 看他的眼睛     pref=1.0   ← 从基线 0.692 升到榜首
  0.855 lan-004 指认身体部位   pref=0.7   ← 间接命中（洗澡场景）
  0.830 gro-007 一二三木头人   pref=0.0

★ 安全性质实测
  喜欢 soc-009 但前置 soc-008 未完成 → passed=False
  原因：前置活动尚未完成：soc-008    ← 喜好无法复活被硬过滤的活动
```

`llm.py` 健康检查 `python llm.py`：`llm_ok: true`，超时 20s，模型 `Qwen/Qwen2.5-7B-Instruct`。

---

## 喜好配置环节（阶段 1 追加，已完成）

家长可以配置孩子近期的喜好，它作用在**两处**：

### ① 检索排序（`activities.py`）
```python
class PreferenceItem(BaseModel):    # kind: activity | material | scene | free_text
    kind: Literal[...]              # value + sentiment: like | dislike
class PreferenceSet(BaseModel):
    items: list[PreferenceItem] = []     # max 30
    free_notes: str = ""                 # 家长原话，只进教练 prompt
```
`PlanRequest.preferences: PreferenceSet` → 打分新增 `preference` 项（权重 0.10）。

**权重重配后（合计仍为 1.00）：**
```
0.30 状态匹配 ← 最高项，不可动摇
0.15 技能差距 / 0.14 前置就绪 / 0.10 喜好命中 / 0.10 新鲜度
0.08 材料契合 / 0.08 方法契合 / 0.05 场景贴合
```

**★ 核心安全性质：喜好只能改变"同等适配度"活动之间的排序，永远不能推翻教学判断。**
三条保证，面试时可以直接讲：
1. `state_match` 仍是最高权重项（0.30 > 0.10），家长喜欢推不动难度不匹配的活动
2. 喜好**只在 `_hard_filter` 通过之后**参与打分，**不能复活被剔除的活动**
   （实测：喜欢 soc-009 但前置未完成 → 依然被剔除）
3. 不喜欢走 `negative_penalty()` 扣分**而不硬剔除**，保留在榜上可解释 ——
   家长需要知道"我听到了你的偏好，以及为什么这条还是出现了"

`free_text` **刻意不参与检索**：让 LLM 猜"他喜欢玩水"对应哪个活动，
等于把排序权交给不可复现的黑箱。只喂给教练 Agent。

### ② 教练卡片（阶段 3 的 `coach.py` 要接）
```python
preferred_hooks(pref, activity) -> list[str]   # 这条活动上能挂的动机钩子
render_preferences(pref) -> str               # 渲染成进 prompt 的片段
```
**这才是喜好真正的价值。** 检索排序只决定"今天做哪条"，但家长能不能坚持，
取决于孩子看到那条活动时有没有熟悉且喜欢的东西垫在里面。教练拿这个列表去写
`why_today` 和步骤措辞 —— "用他最喜欢的小汽车当诱饵做共同注意"。
**面试时讲这一条比讲"喜好参与了打分"有力得多。**

### 阶段 2 的接线点
`ParentConstraints` 里加一个字段即可：
```python
class ParentConstraints(BaseModel):
    ...
    preferences: PreferenceSet = Field(default_factory=PreferenceSet)
```
不要把喜好塞进 `AbilityProfile` —— 那是"医生评估出来的能力"，
喜好是"家长这周观察到的偏好"，两者置信度和来源都不同，混在一起会让
`overall_confidence` 失去意义。

### 材料匹配三层（`material_available()`）
1. 完全相等
2. 双向子串（先剥量词：`一辆小车` ↔ `小车`、`一本绘本` ↔ `绘本`）
3. 字符重叠兜底（剥量词后仍不相交时）—— 共有实义词 ≥2 且重叠率 ≥0.6

第 3 层**刻意让它漏而不是猜**：`水杯` vs `杯子` 只共有"杯"字，不算匹配。
材料匹配错了会让家长白准备一堆没用的东西，比少匹配一条活动糟糕得多。
11 条用例全部符合预期，见下。

---

## 阶段 1 踩到的坑（已修，勿回退）

1. **材料匹配不能用精确字符串相等。** 家长写"小车"，活动写的是"一辆小车"，
   相等比较会让"只用家里常见物品"这条最关键的约束在真实输入下失效。
   子串匹配也不够 —— 家长写"小汽车"、活动写"一辆小车"，加个量词就断了。
   最终是三层匹配（相等 → 剥量词子串 → 字符重叠），见上文。

2. **零道具活动必须允许 materials/setup 为空。** 拍手、数数、上下楼这类活动
   本来就不需要道具，而"零准备成本"恰恰是最容易被家长坚持的活动。
   不要为了满足 `min_length=1` 给它们硬塞道具。

3. **修数据别用全局字符串替换。** 我用全局 replace 修 `setting` 字段的非法值时，
   把 `linked_scenes` 里的 `playground` 也替成了 `outdoor` —— 而 `outdoor`
   根本不在 SCENE 词表里。50 条手工数据上很容易踩，定向改。

4. **Pydantic 校验在 `load_activities()` 里，是故意的。** 厂商的 schema 保证是
   服务端契约，这里是我们自己的数据契约。任何一条引用错了词表都该在这里炸掉。

5. **写单元测试时先确认测试用例本身是对的。** 我一度把「绘本 vs 图画书」
   期望成匹配（以为共有"画"字）—— 其实绘≠画，代码是对的、测试是错的。
   测试失败先怀疑测试，再怀疑代码。

### `material_available()` 的 11 条用例（全部符合预期）
```
✓ 一辆小车 vs 小汽车   -> True   量词+同义词
✓ 一辆小车 vs 小车     -> True   剥量词
✓ 一本绘本 vs 绘本     -> True   剥量词
✓ 小车   vs 大车       -> False  不该把小车当大车
✓ 杯子   vs 水杯       -> False  共有1字，刻意漏
✓ 绘本   vs 图画书     -> False  绘≠画，只共有"书"，刻意漏
✓ 绘本   vs 图画绘本   -> True   共有"绘""书"
✓ 袜子   vs 袜子       -> True   完全相等
✓ 两个纸箱 vs 纸箱     -> True   剥量词
✓ 袜子团 vs 小毛巾团   -> False  共有"团"，刻意漏
✓ 袜子团 vs 袜子       -> True   双向子串
```

---

## 阶段 2 已完成 ✅（能力画像 + 状态机 + 记忆存储 + 红旗）

5 个文件全部落地，真实模型 + 真实 sqlite 跑通。

| 文件 | 行数 | 说明 |
|---|---|---|
| `capabilities.py` | ~270 | `SkillAssessment` / `ParentConstraints` / `AbilityProfile` + 两条不变量 |
| `profile.py` | ~400 | 报告 → 画像。**一次** `chat_json` + 红旗前置 + 回指校验 + 确定性降级 |
| `statemachine.py` | ~330 | 五态机，**纯函数、零 token、可重放** |
| `store.py` | ~470 | 7 张表，`skill_state` 只追加 + 三个必需查询 |
| `redflag.py` | ~200 | 独立规则扫描，11 条规则 × 三档严重度 |

### 阶段 2 验证结果（全部实测通过）

```bash
cd asd_agent
../.venv/bin/python statemachine.py   # 状态机
../.venv/bin/python store.py          # 存储 + 三个必需查询
../.venv/bin/python redflag.py        # 红旗
../.venv/bin/python profile.py        # 端到端抽取（真调模型，约 18-47s）
```

**状态机（一条 35 条记录的时间线走完整条链）**
```
social_adapt/共同注意：未开始 ↑ 入门   —— 已经有部分成功记录
                 入门 ↑ 练习中   —— 最近 7 天成功 5 次、平均质量 0.50
                 练习中 ↑ 已掌握  —— 最近 14 天成功 13 次、平均质量 0.77、抗拒率 7%
                 已掌握 ↑ 泛化中  —— 当前状态以来成功 8 次，其中 3 次在常用场景之外
                 泛化中 ↓ 已掌握  —— 最近 14 天新场景抗拒率 45%，泛化还不稳

同一份数据 profile_confidence=0.35 → 0 次转移（闸门全关）
打乱输入顺序后结果一致 → True（可重放）
```

**升级闸门在存储层的对照**（`store.py` 自检，`demo-001` vs `demo-002`）
```
同一份 35 条练习记录，只改画像可信度：
  0.85 → 5 次转移（entry→practicing→mastered→generalizing→mastered）
  0.30 → 0 次转移
what_progressed(demo-002) 同时报出：
  ⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足，升级闸门拦下了
```
**这是面试时最值得演示的一屏**：数据一字不差，轨迹完全不同。

**红旗**：`verify_schema` 的 C11（"会去咬自己的袖子"）→ urgent/self_injury ✓
C3（"我帮他他就打我的手"）→ high/aggression ✓（"打人"匹配不到"打我的手"，规则已补）
纯正常语料 → 不误报 ✓

**端到端接缝**：画像 levels + 状态机 target_states + 记忆切片 → `PlanRequest`
→ `retrieve()` 14/50 通过，前 3 条都带可读理由，剔除项带原因
（大球/气球缺材料、gro-001 前置未完成）。

### 阶段 2 踩到的坑（已修，勿回退）

1. **报告抽取要 45-47s，20s 的全局超时上限会直接把它打进降级路径。**
   7B 出 ~1100 token 就是这个量级，而 20s 是**每日链路**（教练卡片、行为观察）
   的 SLA。用同一个上限去卡建档链路，结果不是"慢一点"而是整份画像拿不到。
   → `llm.py` 加了 `chat_json(timeout_s=)` + `client(timeout_s)`，按超时复用连接池。
   `profile.py` 用 `EXTRACT_TIMEOUT_S=90`。**默认仍是 20s，不要动。**
   报告抽取是一次性建档操作，阶段 4 挂后台 job + SSE。

2. **把"inferred 却给了高 confidence"做成硬报错，会让降级路径变成常态路径。**
   原设计是 `raise`，实测 7B 几乎必然踩中（它倾向给所有条目同样的 0.95），
   连续两次不合规 → 整份画像作废。为了一个软数据质量问题丢掉全部数据，
   量级完全搞反了。
   → 改成**静默钳制到 0.35**。该规则要保证的只是"推断条目不得充当升级依据"，
   钳制完全满足，而失败率归零。prompt 里仍然要求 <0.4，钳制只是兜底。
   反而保留硬报错的那条：**skill_item 不在词表 → 硬报错**（真编造，必须拦）。
   "细项合法但能区填错"→ 自动按 `SKILL_ITEM_DOMAIN` 纠正。

   ★ 教训：**校验规则的严重度要和它的失败代价匹配**。软问题钳制，硬问题报错。

3. **0 级的门槛必须写死。** 报告写"有短暂的眼神回应，但无法持续"，
   7B 判成 L0（不会）。而检索器有 `lvl < 2 → 挡掉 difficulty>1`，
   于是这条技能的所有进阶活动被全部挡死，系统退化成永远只推最简单的。
   → `LEVEL_HINT` 与 profile 的 prompt 都加了校准锚点：
   "偶有/有时/短暂/不稳定"一律给 1 或 2，只有明确说"从不/完全没有"才给 0。
   改完实测：`眼神回应` 从 L0 变 L1，正确。

4. **回退必须让旧证据失效。** 曾出现
   「泛化中 →(外出连续抗拒)→ 已掌握 → 隔一天又 → 泛化中」的抖动：
   累计成功次数不随回退清零，退回去第二天旧证据又把它推上来。
   → `propose_transition(..., since=)` —— 进入当前状态的日期之后的记录才计入累计证据。
   回退只是延迟一天反而制造状态抖动，而抖动通过 `w_state_match`
   直接表现为"今天难度3、明天难度2、后天难度3"，家长看到的是系统在乱改计划。

5. **演示数据必须自己先经得起交叉核对。** `_demo_history()` 原先写死
   `soc-004 看他的眼睛`，而那条活动的 `target_skill_items` 是眼神回应/轮流等待，
   **不含共同注意**。于是 `sessions_for_skill('共同注意')` 过滤掉全部 35 条、
   状态机拿到空历史却照样输出一条完整转移链 —— 一切看着正常，实则全是幻觉。
   改成 `soc-001 指给你看他喜欢的`（真的以共同注意为目标技能），
   并在 `statemachine.py` 的自检里加了一条 assert 挡住这类错误。

6. **`what_progressed` 最初比的是"窗口内第一行 vs 最后一行"。**
   窗口内先升到泛化、又因连续抗拒退回来，会被报成 `↑ 泛化中 → 已掌握` ——
   **方向正好反了**，而且掩盖了先升后降的事实。
   → 改成比较**窗口初状态 vs 当前状态**（窗口口径），这才是家长关心的
   "这一个月他总体在进步还是退步"。中间过程交给 `state_history()` 讲。

7. **闸门对照的候选不能只来自 `skill_state`。**
   闸门从第一天就拦住全部升级时，`skill_state` 里一行都没有，
   只遍历已记录状态的话这条对照永远看不到自己 —— 恰恰是最该被看见的情况。
   → 候选改为「skill_state 的项 ∪ sessions 里活动覆盖到的细项」。

8. **`StateTransition` 自己填 `at_date`。**
   原本让调用方传日期，于是 `store._seed_demo` 里出现了"先全写首日、
   再删掉重写"的荒唐代码。审计线的时间戳错了整条历史就废了，
   所以这个字段由 `propose_transition` 填，不留给调用方猜。

### 阶段 2 的定夺（已落地）

**① 词表重叠已合并**（实测问题，已修）

`语言` 能区的 `两步指令` 与 `听懂双指令` 语义几乎重复，7B 会把同一句证据分给两项，
状态机于是把一个构念拆成两个 key 各推各的。
→ **删 `两步指令`，保留 `听懂双指令`**，`activity_data.py` 4 处引用一并改指：
`lan-001` 只留 `主动表达需要`（它本来就是在逼主动表达，两步指令本就不搭）、
`lan-003` 去重后只留 `听懂双指令`、`lan-008` 改指、`cog-006` 只留 `时间顺序`。
标题与 success_criteria 里的中文描述保留原样（那是文案，不是词表 key）。

另删两个**零活动覆盖**的细项：`记住三步指令`、`适应新环境`。
理由：零覆盖 = 状态机永远推不动它 = 一条混进画像和统计的死 key，纯负债。

**② 阶段 4 的建档接口走后台 job + SSE**（已定，阶段 4 实现）
`POST /api/profile` 返回 `{job_id}`，`GET /api/jobs/{id}/stream` 推
「正在读语言能区… 已 8s → 正在回指校验 10 条证据… 已 31s → done」。
每日链路（教练卡片、行为观察）仍走 20s 硬上限不变。

**③ 补了 `validate_all` 一条漏掉的检查，顺手抓到第二个真 bug**

原先只验证"细项在自己的能区里"，不验证"细项的能区被这条活动声明了"。
补上后立刻抓到 `fin-008 涂鸦画车`：步骤 3 明确在练"指着问：这是什么？→ 说出名字或指认"
（= `命名物体`，语言），却只声明了 `["fine_motor","cognition"]`，
而 cognition 是虚的（target 里没有任何认知细项）。
后果：`state_match` 会拿精细动作/认知的目标难度去评一条语言技能，**打分完全错位**。
→ 改为 `["fine_motor","language"]`。50 条现已全过。

★ 这条检查值得单独说一句：**"引用了不存在的 ID"容易查，
"引用了存在但放错位置的 ID"要靠交叉验证才发现。**
同一个构念被两套 key 分别推进，和能力落在没声明的能区里，
都是后一类 —— 都不会报错，只会让推荐慢慢变得不合理。

---

---

## 阶段 3 已完成 ✅（三个 Agent + 端到端 CLI）

| 文件 | 行数 | LLM 调用 | 说明 |
|---|---|---|---|
| `planner.py` | 448 | 1 次 | 检索候选由 Python 算，LLM 只做编排 + 写 `why_this` |
| `coach.py` | 553 | 2 次 | 诊断（可校验推理链）→ 家长卡片 |
| `evaluator.py` | ~~428~~ → 166 | — | 日评估。**阶段 4b：只留 `apply_transitions`，逐活动判定挪到 `feedback.py`、日级聚合挪到 `summary.py`** |
| ~~`pipeline_demo.py`~~ | — | 全链路 7 阶段 CLI。**阶段 4 已合并进 `loop.py` 并删除** |

**核心边界（本项目最值得讲的一条工程原则）：**
模型只回答语义判断（"今天这次成功算不算泛化"、"这活动踩雷了吗"），
凡是可判定的（时长够不够、换哪条活动、该不该升级）全部交给 Python。
同一条原则落在四处：planner 的时长与预算校验、coach 的 impact 落地、
evaluator→statemachine 的 state 转移、profile 的整体可信度、
summary 的方向判断（它也只给 direction + rationale，不产出"所以该升级"）。

### 阶段 3 实测结果（**全部离线，0.0s，9 个入口全绿**）

```bash
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator; do
  ../.venv/bin/python $f.py; done
../.venv/bin/python loop.py --no-llm --script hardcoded   # ★ 阶段 4 后统一用这个
```
> 当年这一节验的是 `pipeline_demo.py`。它后来被 `loop.py` 覆盖并删除，
> 上面的命令已换成 `loop.py`（单日链路部分完全一致）。

端到端 demo 的输出直接印证了两条设计（命令已换成 `loop.py`）：

```
[6] 状态机 → 转移判定
    没有转移：画像可信度 0.10 低于升级闸门 0.5，全部升级被拦下
[7] 记忆库 → 周回顾（纯 SQL）
    最近 30 天：⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足（数据不够，不猜）
```

**⚠️ 未实测：三个 Agent 的真实模型路径。**
权限分类器以"数据外发"为由拦下了跑 demo 脚本的调用（`pipeline_demo.py`，已删）。
待验三件事，**阶段 4 之前必须补**：
1. 四个 schema 在 `json_schema` 约束解码下的**实际合规率**
2. **`CoachCard` 会不会截断** —— 设计文档自标"最可能现场翻车的单点"
   （6 步 + prompt_ladder + 4 段退路，max_tokens=3000）。
   `llm.py` 已有 `finish_reason=="length"` → ×1.5 自动重试 → 仍失败则落兜底卡片
3. **每步真实耗时** —— 教练两次串行，按 7B 实测速度预计 **30–45s**，
   超出 PROGRESS 原先假设的 20s。已定：阶段 4 一律走 job_id + SSE，不在前端同步等。

自己跑：
```
! ../.venv/bin/python loop.py --trace     # 当年这条是 pipeline_demo.py
```
（本项目无任何真实患者数据，全是合成 fixture，发外部 API 无隐私风险。）

### 阶段 3 踩到的坑（已修，勿回退）

1. **贪心兜底排成了"两天一循环"。** 只避开"昨天"，而候选池 14 条要填 20 个位置，
   于是第 1-2 天、第 3-4 天重复。**看起来排满了，实质家长第三天就在重做第一天。**
   → 改用 `last_used` 按"最久未排"轮转：20 个位置用满 14 条不同活动。

2. **`swap` 根本没换活动，而日志说换了。** `_pick_adjustment` 返回整个候选表，
   调用方又按原 `today_id` 取，拿回的还是原来那条，但打印着"已换成 fin-004"。
   → **换活动这件事必须在函数里换完**，返回选好的那一条而不是菜单。

3. **`apply_transitions` 把当天所有记录喂给每个技能。**
   练语言的成功会算进"共同注意"的进展 —— 状态机看起来一直在正常推进，
   实际是在拿别人的成绩往上爬。
   → 按 `activity.target_skill_items` 过滤，且接上 `store.sessions_for_skill()`
   的完整历史（状态机规则全是时间窗统计，只喂今天一条永远不动）。

4. **`excluded_activity_ids` 自相矛盾。** 去重时丢掉的 id 与保留下来的同一个 id
   混进同一张清单，前端会说"xxx 被剔除了"而它明明在计划里。
   → 末尾按"是否真的不在计划里"筛。

5. **两套 `outcome` 词表不一致是故意的，要收敛到一处。**
   `ActivityEval` 有 `no_data`、`SessionRecord` 没有 —— 因为**"没信息"不是观察结果，
   是没有观察**。写成 session 等于凭空造一次练习记录，会实实在在扣一分能力。
   → 收敛为 `evaluator.to_session_records()`，调用方不会再踩。

6. **`_OUTCOME_ALIASES` 是我写的假兜底。** 曾加 `{"成功": "success"}` 映射，
   但 `outcome` 是 Literal，Pydantic 构造时就拒，根本走不到归一化。
   留着它是"看起来在兜底、实际永不执行"，比没有更糟。已删。

7. **降级模式要敢于交白卷。** 家长原话提到"小汽车"，当天活动都不用小汽车，
   `--no-llm` 下阶段 5 一条 session 都不写。
   **没有**让兜底模式假装"今天成功了"来凑满链路，而是改去灌 35 条**真实练习历史**
   让状态机在真数据上跑。副作用反而更好：闸门可见地被触发。

### 阶段 3 的设计取舍

- **教练的 `impact` 不直接生效。** 模型只给 `keep/ease/drop/swap`，
  换成哪条活动由 Python 决定（`ease` 找同能区难度更低的那条）。
  让 7B 自己去候选表里挑"简单一点的"等于把正确性交给它。
- **`free_reasoning` 是装饰字段**：不参与决策、不校验、不回灌。
  把 LLM 输出分成"承重"和"装饰"，7B 的短板就不会污染系统正确性。
- **降级卡片不是残次品**：活动定义里本来就带 `steps` / `success_criteria` /
  `prompt_hierarchy` / `error_correction`，所以"没有教练 Agent"的卡片是完整的，
  缺的只是退路没讲透。**降级路径的质量决定模型挂掉时它还是不是个有用的工具。**

---

## 方法溯源与 RBT 撤除（2026-10-18）

`sources.md` 是用户补充的方法级溯源文件（文献出处、国内指南锚点、证据强度边界）。
**改 `method` 标签前先读它。**

### RBT 标签已撤除 ✅
`sources.md §5` 的判断成立，已改代码：
- 中文语境 RBT = 强化疗法 = **离散单元教学法 DTT 的别称**，要求回合结构 + 固定强化协议
- 英文语境 RBT = Registered Behavior Technician —— **那是岗位名称，根本不是方法**
- 库内仅 `gro-001 推球走直线`、`gro-003 气球不落地` 挂着，都属无回合结构的自由游戏，
  两种定义下都不成立，归 NDBI 才对

改动 4 处：
1. `activity_data.py` 两条活动 `"method": ["NDBI","RBT"]` → `["NDBI"]`
2. `activities.py` 的 `Literal` **枚举里删掉 `"RBT"`** —— 留着会让"看起来严谨"的
   标签继续被塞进新数据
3. `activities.py --validate` 的方法分布清单改为**从数据推导**（原来写死 5 个名字，
   撤掉 RBT 之后就少统计一列而不报错，那种"少了一列"没人会注意到）
4. `activity_data.py` 模块 docstring 写明撤除理由 + 指向 `sources.md`

**对检索零影响**：`method` 只用于 `brief()` 展示与 CLI 统计，不进任何打分项。
撤除后方法分布 = `ESDM=45 NDBI=33 ABA=13 PRT=9`，与 `sources.md` 建议一致。

### 证据边界（主动声明，别等被问）
`Project AIM(2020)` 三级收紧后**无任何干预类型达到统计显著**。
本项目的口径：**我验证的不是疗效，是"能否把干预动作稳定落成家长可执行的日程"。**
方法有效性有文献支持；我的贡献是把文献里的教学元素转成家长可执行、可追溯、
可动态调整的日常动作，**不是重新验证疗效**。主动说出边界比被问出来强。

---

---

## 阶段 4：闭环接线 ✅（计划执行 → 反馈 → 更新计划）

**要解决的问题**：系统能跑通「报告 → 画像 → 一周计划 → 卡片」，但闭环是断的。
三处断点由代码确认，不是推测：
1. `store.log_observation()` **零调用点** —— observations 表从未被写入
2. `Observation` 的 14 个字段里，除 `scene` 和红旗计数外**从未被任何代码读取**；
   `antecedent`/`consequence`/`intensity` 是死数据
3. 教练诊断**完全看不到 ABC** —— 拿着 raw 原话重做一次无约束推断

后果：`refusal_counts` 只来自评估 Agent 的每天一条，**"连续抗拒两次进冷却期"
这条规则在真实链路里永远触发不了**。系统看起来会调整，实际上不会。

### 已定决策
- **A：每日"更新计划"= 只重算候选 + 卡片，不重跑 LLM 排期。**
  状态转移 / 抗拒冷却 / 场景变化已全部流进 `retrieve()`（纯 Python 毫秒级）。
  一周计划降级为"候选池与顺序参考"。**`planner.py` 未改动。**
- **B：一次家长输入抽多条事件。**「今天拒绝了两次」单条只能记 1 次，冷却触发不了。

### 改动清单
| 文件 | 改动 | 规模 |
|---|---|---|
| `observe.py` | **新增**：多事件抽取 + 红旗 + 回指校验 + 降级 + session 桥接 + 转换 | ~480 |
| `loop.py` | **新增**：3 天循环 CLI + `assert_loop_closed()` 交叉核对 | ~450 |
| `vocab.py` | 加 `CONSEQUENCE_FUNCTION`/`FUNCTION_CN`/`function_of()` + 覆盖率自检 | +60 |
| `store.py` | `log_session` 补 note 列；红旗计数改 `COUNT(DISTINCT observed_at)` | ~15 |
| `coach.py` | `ObservationInput` 扩 ABC；`Signal` 加 `obs_id`/`function`；prompt 渲 ABC；功能一致性纠正；功能驱动规则 | +130 |
| `planner.py` `schema.py` `activities.py` `statemachine.py` `evaluator.py` `redflag.py` `activity_data.py` `verify_schema.py` | **不动** | 0 |

### ★ 两个 demo 已合并（`pipeline_demo.py` 已删）

`loop.py` 一度与 `pipeline_demo.py` 重复 —— 后者是单日 7 阶段链路，前者多了三日闭环。
核实后确认 **`pipeline_demo` 只有一屏是独有的**：
```
⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足，升级闸门拦下了
```
升级闸门的那一屏对照需要**历史数据**才出得来，而空库的 loop 只能打出
"没有任何技能状态变化"，于是整个"升级闸门"卖点看不见。
→ 搬成 `loop.py --seed-history`（语义上就是"从第五周开始演"，真实用户本来就有历史），
同时把报告红旗与引文未核对的展示也补进 loop。
→ **`pipeline_demo.py` 已删**，`loop.py` 成为唯一 demo 入口。

删除前项目**没有版本控制**，所以先 `git init` 并提交了一次基线 ——
现在每次改动都可 diff、可回滚，面试时也能直接展示阶段 1→4 的设计演进。

### 验证（10 个离线入口全绿，0.0s）
```bash
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator observe; do
  ../.venv/bin/python $f.py; done
../.venv/bin/python loop.py --no-llm --script hardcoded              # ★ 闭环演示
../.venv/bin/python loop.py --no-llm --script hardcoded --seed-history  # + 升级闸门对照
```

`loop.py --no-llm --script hardcoded` 的收尾一屏：
```
Day1 候选前 5：gro-007  fin-001  gro-006  fin-004  fin-008
Day2 候选前 5：fin-004  fin-008  cog-001  lan-002  lan-004
★ 因家长 Day1 的反馈退出：gro-007
      gro-007 —— 已连续抗拒 2 次，进入冷却期
因其他硬过滤退出（与反馈无关）：fin-001、gro-006
      fin-001 —— 最近 3 天刚练过，避免重复
新进候选：cog-001、lan-002、lan-004
```
**家长一段话 → observations → 抗拒计数 → 检索硬过滤，第二天候选真的变了，
而这条链上没有任何一步需要重新跑排期 LLM。**

### 阶段 4 踩到的坑（已修，勿回退）

1. **★ 抗拒桥接做全局匹配会产出 58 条 session。**
   初版按「behavior ∈ refusal_behaviors 且 scene ∈ linked_scenes」全局搜索，
   实测一次输入产出 **58 条** refused session —— 库里有 48 条活动同时满足这两个条件。
   那不是精确，那是把整个活动库冷掉了。
   根本问题是**信息不足**：家长说"他不肯听"并没有说他在玩哪条活动。
   → 归属的前提改成"当天确实练过这条"（`attempted_ids` 必填参数）。
   不属于任何当天活动的抗拒（"晚饭他把盘子摔了"）**不计入 refusal_counts**，
   但仍是一条 Observation，照常影响 recent_scenes 和教练诊断。
   **它扣不到任何具体活动的头上，就不该扣。**

2. **★ `assert_loop_closed` 的三条断言是按直觉写的，不是按代码行为写的。**
   第一次跑就误报 3 条，挡住的全是正确行为：
   · "Day2 候选必须与 Day1 不同" → Day3 没有新反馈，**候选本来就应该稳定**。
     该保证的是"变化时可归因"，不是"每天都变"
   · "recent_scenes 必须在当天事件里" → 它查的是 **14 天滚动窗口**，
     Day2 出现 Day1 的 `indoor_play` 是对的。该查"存在于库里某一天"
   **写断言前先读一遍被断言的函数。**

3. **★ 剔除原因要从全量 `retrieve()` 取，不能从 `candidates()` 取。**
   `candidates()` 返回的全是 `passed=True` 的对象，而它们按定义没有
   `hard_filter_reason` —— 于是打印出来是空的「没推荐：」。
   更糟的是 `assert_loop_closed` 因为找不到原因，判定"变化不可解释"，
   差点让人以为闭环没生效。

4. **★ `_show_diff` 的索引差一天，整个演示最关键的两行消失了。**
   Day2 的候选是由 **Day1** 的反馈淘汰出来的，所以原因要去 Day1 取。
   写成 `b.cooled_out`（Day2 自己）拿到空列表，输出只剩"新进候选"，
   看起来像系统自己换了一批活动。

5. **顺序：先出卡片，再收反馈。**
   反过来做会让 Day1 出现荒谬一幕：周计划刚建议 gro-007，同一天的反馈
   立刻把它冷却掉，"今天的建议"当场失效。用户要的是
   「计划执行 → 反馈 → 更新计划」，那反馈影响的是**明天**。

6. **同一天同一活动只允许一个 Agent 给 verdict。**
   观察桥接已经产出 refused 的活动，评估 Agent 就只补空 ——
   否则 `refusal_rate` 的分母翻倍，状态机的回退阈值全部失真。

7. **红旗计数用 `COUNT(DISTINCT observed_at)`。**
   一次输入落成 N 条 observation 共享同一个 `observed_at`，
   `COUNT(*)` 会数成"N 天遇到了 N 次红旗"，而家长关心的是**几天**。
   这个 bug 在 observations 恒为空时一直藏着。

### 设计要点

- **`to_coach_inputs()` 的 `raw` 必须是整段原话**，不是单条事件的引文。
  `coach._verify_quotes()` 用 `"
".join(o.raw)` 做回指校验；
  若填单条引文，降级过的事件（引文被清空）会让 corpus 缺段，
  **教练的正常信号会被误判为编造**。事件靠 `obs_id` 区分，不靠 `raw`。
- **多事件回指校验用整段 corpus，不分段。** 中文口语没有可靠事件边界，
  切错的代价（真事件被判编造 = 丢数据）远高于不切（跨事件误挂 = 标注不准）。
- **`Signal.function` 与 `Observation.consequence` 的交叉校验**是接线后最值钱的一段：
  模型说"功能是逃避"，但它引用的观察记的是 `got_extra_attention` → 以 ABC 为准。
  没有它，ABC 只是被渲进 prompt 的装饰字段。
- **功能驱动的加强规则**（只能收紧不能放松）：`escape`+高难度 → 强制 ease；
  `sensory`+高感官活动 → 强制 swap；`attention` → 不改 impact，
  但给卡片加"不要用注意做奖励"。

### ⚠️ 未实测
三个新路径的真实模型行为全部未验证（权限分类器拦截外发）：
1. `ObservationBatch` 在约束解码下的合规率（离线回归集用 `verify_schema` 的 12 条 CASES，
   **但不改 verify_schema.py** —— 它是立论基础）
2. 多事件会不会截断（`max_tokens=4000`，不是单事件的 2000）
3. 真实耗时：3 天 = 1 planner + 3 observe + 3×2 coach + 3 eval = **16 次 LLM ≈ 5–7 分钟**。
   **现场必须 `DEMO_MODE=replay`**

### 已知局限（主动写明）
`activities.py` 的冷却判定 `refusal_counts >= 2` 是 30 天滚动计数、
**没有冷却期结束条件** —— 抗拒两次，这条活动一个月不再出现。
对 demo 是好事（变化可见），对产品是缺陷。引入 `cooldown_until` 列
属于改检索器语义，本阶段不做。

---

## 阶段 4b：家长反馈改成逐任务 + 日结接活 ✅（2026-10-19）

### 为什么改（四个问题，同一个根因）

家长原来用**一整段话**描述整天，由此暴露：

1. **抗拒归属是无中生有的。** `observe.refusal_sessions(out, attempted_ids)`
   收的是当天**计划**的全集，在其中做「事件 × 活动」的笛卡尔积匹配。
   家长明说"今天玩一二三木头人"（只玩了 gro-007），fin-001 / gro-006 却各白得
   1 次 refused 记录 —— 只因为它们恰好也含 `refusal` 且都在 `indoor_play`。
   实测 4 条 refused session 里 **2 条是无中生有**。
   当年（`observe.py:391`）把 `attempted_ids` 从可选改成必填是对的，
   但**粒度选错了 —— 用"计划"而不是"执行"**。
2. **质量信息被丢弃。** "后来我拉着他才停下"（成人的物理辅助 = 提示阶梯第 3 级）
   会大幅降低 quality，"我一看都六点了就没再玩了"其实是 `skipped` 不是 `refused`
   —— 两段都埋在长句子里被丢掉。
3. **评估 Agent 在猜家长可以直接回答的问题。** `EvalActivityInput.materials` 的
   注释写"降级模式靠它把家长的话对上活动"，而 `_fallback_eval` 实际只匹配
   `title`/`activity_id` —— **这个字段从来没被读过**，注释与代码脱节。
4. **日结结果对明天的计划零影响。** `DailyEval.domain_updates` / `.flags` /
   `.parent_note` grep 全项目：**只被 `render()` 打印，无人消费、不落库**，
   `store.py` 里没有 `daily_eval` 表。

### 改成了什么

家长端：**当天排了几条就逐条填几条**，每条是
`结果（5 个按钮）+ 用到第几级提示 + 做到几步 + 选填自由补充`，
外加一个当天的「其他观察」框。

```
【2026-10-17 的家长反馈】
  gro-007 → refused（质量 0.00）      note=家长选「不配合」
  fin-001 → skipped（质量 0.00）      note=家长选「跳过这条」
  gro-006 → skipped（质量 0.00）
```

`refusal_counts = {'gro-007': 1}` —— **fin-001 / gro-006 是 0**。
改动前这三者都会各记至少 1 次。

### 评估 Agent 被拆成两半

| 部分 | 原位置 | 现在 | 为什么 |
|---|---|---|---|
| **A. 逐活动判定**<br>outcome / quality / 判据命中 | `evaluator.py` | `feedback.py` | 家长**直接点选项**回答了这个问题。模型再猜一遍不只是浪费 token，是**把一个确定的答案变成一个可能有错的答案**（家长点了「不配合」，模型可能判成 partial） |
| **B. 日级聚合**<br>能区方向 / 跨天 flags / 给家长一句话 | `evaluator.py`（**死代码**） | `summary.py` + **`daily_summary` 表** | 需要跨样本归纳，单条反馈推不出来。而且它原先只被 print 后丢弃 —— 现在真落库、真被读 |

`evaluator.py` **428 行 → 184 行**，只留 `apply_transitions`
（按 `target_skill_items` 反查技能归属的那套逻辑，一行没改）。
被删掉的 `_repair` 四条校验里有三条是为 A 兜底的，逐条反馈下**物理上不可能发生**
（不存在幻觉 id、不存在漏条、quality 由函数算）——
它们那条"这个字段不能由模型自由裁量"的核心思路变成了 `feedback.quality_for()`
里一张写死的映射表。**quality 从提示词里的自由裁量变成了确定性函数，可重放** ——
原先同一档位不同天能差 0.15，而状态机的升级闸门要拿平均 quality 比 0.5 / 0.75。

### 这次改动最值钱的一屏

```
★ 日结：这三天的方向判断落库了（改动前它们只被打印然后丢掉）
  2026-10-15  大运动 ↓  今天1条都不配合
  2026-10-16  精细动作 ↑  今天2条都做出来了
  ...
  ★ 明天 build_request 会读这些（recent_summary），
    教练 Agent 第一次能拿到「方向」，而不只是三个数字。
```

`PlanRequest.recent_summary` 刻意**不参与打分**，只进教练 Step 1 诊断的 prompt ——
它是 LLM 写的一段话，混进 0.30/0.15/0.14 那套权重就等于把检索的正确性
交给一个不可复现的字符串。位置对了才有价值。

### 演示从 3 天改成 5 天（冷却阈值的语义变了）

逐条反馈下一条反馈**最多产生 1 条** refused session（家长已点「不配合」时
`apply_feedbacks` 会跳过桥接，否则一次练习就够 `MAX_REFUSALS_PER_DAY=2`）。
所以 `refusal_counts >= 2` 从"同一天两次"变成"**两天都拒**" ——
临床上更合理（隔两天各拒一次才是真该换掉它的信号），
代价是演示要多跑两天。冷却是这条链上最有说服力的一屏，值得。

Day 4 第二次拒 gro-007 → Day 5：
```
【7】明天的候选（重新检索）
  ★ 因家长反馈而退出候选：
    ✗ gro-007 —— 没推荐：已连续抗拒 2 次，进入冷却期
```

### 新增/改动文件

| 文件 | 改动 |
|---|---|
| `feedback.py` **新** | `ActivityFeedback` + `apply_feedbacks` + `quality_for`（纯 Python，零 token） |
| `summary.py` **新** | 日结 Agent，`DomainEvidence` 从 evaluator 搬来；`_repair` 三条校验 |
| `store.py` | 新增 `daily_summary` 表 + `save_summary` / `latest_summary(before=)` |
| `observe.py` | `extract_observations(activity=...)` + `SYSTEM_SINGLE`（规则 7/8 分支）；`refusal_sessions(activity_id: str \| None)` 去掉笛卡尔积 |
| `evaluator.py` | 428 → 184 行，只留 `apply_transitions` + `TransitionsInput` |
| `coach.py` | `record_prompt_for()` 按活动判据动态生成 + `render_feedback_form()`；诊断 prompt 读 `recent_summary` |
| `activities.py` | `PlanRequest.recent_summary`（不参与打分） |
| `loop.py` | `DayInput.feedbacks` 重构、5 天剧本、`run_day` 七步重排、`_hardcoded_outcome` 删除、两条新断言 |

### 验证结果（全部通过）

```bash
cd asd_agent
for f in activities observe store statemachine coach evaluator feedback summary planner; do
  ../.venv/bin/python $f.py; done          # 12/12 OK
../.venv/bin/python activities.py --validate   # ✓ 50 条
../.venv/bin/python loop.py --no-llm --script hardcoded   # 0.0s
```

```
✓ 0 天出现「重算结果与实际取用不一致」（检索是纯函数，正常应为 0）
★ 3 天候选因家长反馈被剔除，其中 1 天进了冷却期
★ 抗拒只记在家长亲手填过的那条活动上，「其他观察」框贡献 0 条
★ 日结落库 5 行，且报的每个能区今天都真的练过
```

`record_prompt_for()` 对全部 50 条活动实测，最长 45 字，无一超 `max_length=60`。

### 本阶段踩到的坑（已修，勿回退）

1. **交叉核对的索引跨了两次重算。** 原断言比 `days[i-1].cands_in`（第 i 天开头）
   与 `days[i].cands_tomorrow`（第 i+1 天结尾）—— 中间隔着两次重算，
   于是三处该有的原因"全都查不到"。正确是 `days[i-1].cands_tomorrow` 对
   `days[i].cands_in`：检索是纯函数，两者**本该相同**，
   不同的部分必须能被 `days[i-1]` 的剔除清单解释。
   （而它们恒等这件事本身是个**好消息**：证明第 7 步重算与第 1 步取用同一套逻辑。）

2. **反查"冷却有没有依据"的对象换人了。** 原断言问"当天有没有事件归属到它" ——
   那在旧形态下对，因为抗拒只从事件桥接来。逐条反馈下**主要来源是家长点的
   「不配合」**，而那条恰好会让桥接被跳过（防重复计数），于是 `d.hits` 是空的，
   断言误报"冷却了但查不到事件"。正确的反查对象是 sessions 表里那天那条
   `outcome='refused'` 的记录 —— 两个来源都包含。

3. **`record_prompt` 会被长判据撑爆。** 初版把判据在句子里说了两遍，
   "能在等待 5 秒内主动停下的比例明显提高"（21 字）直接超 `CoachCard.max_length=60`，
   Pydantic 当场报错。改成判据截断到 18 字且**只说一次**，
   其余用字段名表达。**已对 50 条活动全量验证。**

4. **写 `apply_feedbacks` 时差点用 zip 配对。** 早先把 `(activity_id, outcome)`
   存进一个列表、过滤出另一个列表再 `zip` 回来，靠"两个列表过滤条件相同"配对 ——
   改一处过滤就静默错位，而错位的后果是**抗拒记到错误的活动头上**，
   正是本次要修的那个 bug 的另一个版本。改成直接存 `(feedback, outcome)` 对象。

5. **`EvalActivityInput.materials` 的注释是假的。** 写它的时候注释说
   "降级模式靠它把家长的话对上活动，因为家长说的是「小汽车」不会说活动标题"，
   但 `_fallback_eval` 的 `corpus` 匹配写的是 `a.title in corpus`，
   **materials 从头到尾没被读过**。删掉时才发现 —— 这是"注释描述意图、
   代码做别的事"的典型。逐条反馈后断点自然消失（每条反馈自带 activity_id）。

### 尚未解决 / 下一步

- **`refusal_counts >= 2` 没有冷却期结束条件**（老问题，语义变了但问题还在）：
  抗两天，这条活动一个月不再出现。对 demo 是好事，对产品是缺陷。
- **抽取质量仍未验证。** 这次改动完全绕开了它（outcome 由家长点），
  但"从家长补充描述里抽事件"这条路的准确率依然只有 live/capture 才测得到。
- **`--no-llm` 下状态机仍被闸门冻结**（降级画像 `overall_confidence=0.1`）。
  这是对的 —— 闸门该关的时候就得关。要看真的转移需要真实画像（18-47s）。
- **阶段 5-6（API 与前端包装层）** 的 `server.py` 路由建议需要按新形态改：
  `POST /api/observations` 应该变成"逐条反馈"+"其他观察"两个入口。

---
### 阶段 5 进行中（2026-10-19 晚）：Vue 前端 + FastAPI 已落地，**未提交**

**状态：代码写完并通过浏览器实测，但工作区未提交。** `git status`：
```
 M asd_agent/observe.py     + source 列
 M asd_agent/store.py       + source 列 / + connect_thread_local
 M asd_agent/summary.py     + domain_updates 兜底与形状校验
?? server.py                新
?? frontend/                新（Vue 3 + Vite，8 个组件）
?? .playwright-mcp/         ← 浏览器测试产物，建议加进 .gitignore
```

#### 已落地的东西

| 文件 | 说明 |
|---|---|
| `server.py` ~650 | 10 条路由，**零业务判断**，全部转调 `asd_agent/` |
| `frontend/src/api.js` | 唯一 fetch 出口 + `streamJob()`（两条慢路径共用 SSE） |
| `frontend/src/App.vue` | 一天的流程：建档 → 卡片 → 逐条反馈 →（自动）收尾 |
| 8 个组件 | ProfileForm / PlanView / CoachCard / FeedbackItem / OtherNotes / ProgressView / AgentTrace / RedFlagList + JobProgress |

**实测通过**（curl + 真浏览器）：
```
✓ /api/profile + SSE   进度逐条吐出；C11 红旗命中 urgent/self_injury
✓ /api/plan           排序与 CLI 基线一致；36 条带原因剔除
✓ /api/card            record_prompt 43 字（<60 上限）
✓ /api/feedbacks       2ms 秒回，refusal_counts={'gro-007':1} 核心断言通过
✓ npx vite build       通过；浏览器加载无 JS 错误
```

#### ★ 本轮修掉的两个真 bug（都是"纸面代码"暴露的）

**① `observations` 表没有整段原话 —— 跨进程喂不动教练。**
长期记忆要"把攒下的观察喂给明天的教练"，而表里只有单条事件的 `raw_quote`，
`to_coach_inputs` 的契约却是 `raw` 必须是**整段原话** —— 用碎片拼 corpus 会让
教练的引文回指校验把正常信号全判成编造（阶段 4 警告过）。
→ 加 `source` 列，`observe.persist` 一并写入。**这是阶段 5 之前就存在的缺口。**

**② `DailySummary.domain_updates` 的默认值是 `(FieldInfo,)` 元组而不是 `[]`。**
模型省略这个选填字段时，`_repair` 里 `d.domain` 直接 AttributeError → 日结 500。
**只在 live 路径触发**，所以 `--no-llm` 的 12 个离线回归一个都没报。
→ `_repair` 里 `isinstance(d, DomainEvidence)` 挡形状，
   且"今天有记录却一条方向都没给"时用 `_fallback_summary` 顶上（纯 Python、零 token）。
★ **根因仍未定位**（见下方待办）。修的是后果，不是因。

#### 本轮做过的两个设计决定

**日结改成自动触发。** `/api/feedbacks` 提交后直接收尾，前端不再点第二个按钮。
理由不是省一次点击，是**那条顺序不变量（记录先落库 → 再转移 → 再日结）
本就该由后端保证**，拆成两个端点等于让前端记着。
加幂等保护：`apply_transitions` **不幂等**（`append_state` 只追加，重跑会状态抖动），
所以当天已收尾就直接返回落库的那份，`force=true` 才重算。

**LLM 与 Python 的分工（用户定）**：日结的 `direction` 由 Python 按计数判，
LLM 只写 `flags` 和 `parent_note` —— 那些才是真需要归纳的。

---

## ✅ 迁移到 PostgreSQL 已完成（2026-10-19 晚）

**动机**：sqlite 的并发 500（见下）试过两种修法都没成功，
而需求上本来就要换成 PG —— 于是直接换，不再在 sqlite 上修第三个版本。

**环境**：`psycopg 3.3.6`（`uv pip install --python .venv/bin/python -i https://pypi.tuna.tsinghua.edu.cn/simple "psycopg[binary]"`），
容器 `postgres:16-alpine`，端口绑 **127.0.0.1:55432**（不要绑 0.0.0.0，会把开发库暴露到局域网）：

```bash
docker run -d --rm --name ganyu_pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ganyu -p 127.0.0.1:55432:5432 81bd698b4594
```

### ★ 并发 500 已随迁移消失（这才是换 PG 的唯一理由）

| 测试 | sqlite（迁移前） | psycopg（迁移后） |
|---|---|---|
| HTTP 15 轮 × 12 并发 = 180 请求 | **失败 8** | **0 失败** |
| 进程内 12 线程 × 15 轮 × 4 个只读查询 = 720 | 偶发 `InterfaceError` | **0 失败** |
| 12 线程 × 8 轮 × 3 种写 = 288 条 | 需全局锁 | **0 失败、0 丢失** |

**根因回顾**：`sqlite3` 的**游标**不是线程安全的。`check_same_thread=False`
只是「不检查线程归属」，**不等于线程安全** —— 两个线程同时 `execute()`
同一个连接即抛 `InterfaceError: bad parameter or other API misuse`。

⚠️ **这个 bug 串行测不出来。** 当年连打 15 次串行全绿、5 轮小并发也只炸 2 轮，
差点据此否定它。迁移后又特意用**同样的形状**压测，就是为了确认它真的没了。

### 改了什么（34 条 SQL，5 个文件）

| 位置 | sqlite | postgres |
|---|---|---|
| 7 张表主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| 占位符 | `?` | `%s` |
| 布尔列 | `INTEGER NOT NULL DEFAULT 0` | `BOOLEAN NOT NULL DEFAULT FALSE` |
| 建表 | `executescript()` | 多语句单次 `execute()`（psycopg3 支持，已实测） |
| 取自增 id | `cursor.lastrowid` | `INSERT ... RETURNING id` |
| 临时库 | 删 `/tmp/x.db` + `-wal` + `-shm` | `store.reset()` → `DROP SCHEMA ... CASCADE` |
| 列清单 | `sqlite_master` | `information_schema.tables` + `current_schema()` |

**可以保留的**：`row["col"]`（`conn.row_factory = dict_row` 保住）、
`COUNT(DISTINCT …)`、`BETWEEN`、`ORDER BY …, id`、`ON CONFLICT … DO UPDATE`。
**改动集中在 DDL 与占位符，查询逻辑基本没动。**

### ★ 三个值得单独说的决定

**① 「路径」翻译成 schema，不是 database。**
调用点传 `/tmp/xxx.db` 的目的是「拿一个干净互不干扰的环境」。
`CREATE DATABASE` 不能在事务里跑、不能被连接池复用、删库还得先断连接 ——
三处都比 schema 麻烦，而 `DROP SCHEMA ... CASCADE` 一条语句正好等于「删掉这个 db 文件」。
→ 34 个调用点的形状原样保留，`ASD_DB_PATH` 沿用旧名（只决定 schema 名），
新的连接串是 `ASD_PG_DSN`。

**② `connect_thread_local()` 与 `connect()` 合流成一个入口。**
迁移前它们是两个入口、两套缓存，于是「某个调用点忘了用 thread_local 版」
成了一种可能 —— 而当年手写 `with _IO_LOCK:` 漏了 6 处端点正是同一类错误。
现在只有一个入口，这种错误在结构上不再可能。

**③ `_IO_LOCK` 连同它保护的顺序不变量一起处理。**
删锁之前逐处看过：它锁的三处都是「先写记录 → 再转移 → 再日结」这类**顺序不变量**。
这些不变量是真的（`apply_transitions` 读的是 append 之前的 `current_states`，
边算边写会让后一条看到前一条的结果），所以**保留不变量、只删锁**，
并在原地写下它到底在保证什么。留一把不再需要的锁不只是冗余 ——
它会让下一个人以为「这里还有个必须加锁的坑」。

### 本轮踩到的坑（已修，勿回退）

1. **★ 批量替换漏了 `server.py`，而它不在回归集里。**
   `asd_agent/` 六个文件的 `?`→`%s` 一次替换全中，
   12 个离线入口全绿 —— 但 `server.py` 里那处 `WHERE child_id=?` 没被处理，
   `/api/card` 直接 500（`ProgrammingError: the query has 0 placeholders but 2 parameters were passed`）。
   **它只在真发 HTTP 时才暴露，而 12 个回归入口一个都不碰 server.py。**
   → 「改完离线回归全绿」不等于改完了，凡是跨进程的接线都要单独打一次接口。

2. **`SUM(outcome='success')` 在 PG 里直接报错**，不是静默算错：
   `function sum(boolean) does not exist`（sqlite 把比较结果当 0/1 累加，PG 不会）。
   → `COUNT(*) FILTER (WHERE outcome='success')`。**这类差异好在它是硬报错。**

3. **`cursor.lastrowid` 不存在**，7 个写入函数全部要改 `RETURNING id`。
   psycopg3 也没有 `Connection.row_factory` 的**类属性**，
   但**实例属性可设** —— 设了之后 `row["col"]` 与 `dict(row)` 都能照旧用。

### 验证（全部实测通过）

```bash
docker run -d --rm --name ganyu_pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ganyu -p 127.0.0.1:55432:5432 81bd698b4594
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator observe feedback summary planner; do
  ../.venv/bin/python $f.py; done          # 12/12 全绿
../.venv/bin/python activities.py --validate
../.venv/bin/python loop.py --no-llm --script hardcoded   # 五日闭环 + 冷却 + 日结落库 5 行
```

HTTP 全链路（`ASD_NO_LLM=1`，13 条路由）：
```
✓ /api/profile + SSE    进度逐条吐出
✓ /api/plan             排序与 CLI 基线一致；36 条带原因剔除
✓ /api/card             record_prompt 43 字（<60 上限），3 步 / 3 级提示阶梯
✓ /api/feedbacks        refusal_counts={'gro-007':1}  ★核心断言通过；其他观察框贡献 0 条
✓ /api/close            日结自动落库（进 close 子对象）
✓ 并发 180 请求         0 失败（迁移前 8 失败）
```

**注意**：`/api/summaries` 不带 `before=` 时返回空，是**既有设计**不是回归 ——
`latest_summary` 用 `on_date<?`（严格小于 today），教练读的是「之前」。
与 sqlite 口径完全一致，已核对。

### 数据在哪

PG 里每个「临时库」对应一个 schema，可直接查：
```bash
docker exec ganyu_pg psql -U postgres -d ganyu -c "\dt loop_loop_child.*"
```

---

## 🔜 待定：daily 的 direction 要改成 weekly（用户已提出，未实现）

**用户原话**：「一天的数据和上一次比统计说服力不足，每天先只记一个分数，
到一周了再汇总更新一次」—— **这个判断是对的，已采纳。**

**问题**：现在 `up/flat/down` 有**两套判定，且互相矛盾**：
| 层 | 谁判 | 依据 |
|---|---|---|
| 日结 `DailySummary.domain_updates[].direction` | **LLM** | prompt 里喂计数，让模型开口说 |
| 状态机 `propose_transition` | **Python** | 7/14 天窗口计数 |

同一件事，一套问模型、一套数计数。而日结那个方向判断**只被 `coach.py:280`
读进 prompt**，不参与任何判定 —— 危害有限，但它把一个**可判定**的问题
交给了不可复现的字符串，违反「模型给证据，Python 做判定」。

**打算怎么改**：
- **每天只记一个分数** —— `sessions` 表已经存了每天的 `quality`（`quality_for()` 算的），
  **数据已经在记了，不需要新表**
- 新增 `weekly_direction(conn, cid, week)`：按能区对比本周 vs 上周的
  `avg(quality)` 与成功率，出 `up/flat/down`
- **阈值要承认噪声**：差异小于某幅度就判 `flat`；
  **样本不足（本周 <3 次）就说不足，不猜**
- `DailySummary.domain_updates` 降级为"当天事实"；**方向只在周报里出现**

**已定**：LLM 那次调用**保留**，只写 `flags` + `parent_note`（这两个确实需要归纳）。
LLM 调用数不变，仍是每天 1 次。

**两个待确认（动手前问）**：
1. daily 的 direction **直接删掉**，还是保留但标注"仅供参考"？
   （若删，要改 `coach.py:280` 喂什么给它）
2. 周对比按**能区**（5 个）还是**技能细项**（几十个）？
   细项更具体但样本更少、更容易"样本不足"。

**明确不做**：在反馈表单上加"比上次轻松/差不多/费劲"三个按钮。
—— 那等于把系统该做的判断推给家长，且**单日对比统计上没有意义**。

---

### ★ 前端手测发现的 bug：反馈表印了别的活动的判据（已修）

**症状**：生成卡片（`gro-007`）后，从候选点「用它」加进 `cog-001`、`soc-002`，
**三行反馈表全都显示 gro-007 的内容** ——
`活动：gro-007 一二三木头人` / `判断标准：能连续 5 轮听到口令后停下不动`。

**根因**：`App.vue` 传给 `FeedbackItem` 的三个 prop 是**无条件取卡片那条活动**：
```vue
:title="card?.activity?.activity_id === f.activity_id ? ... : f.activity_id"  ← 只有 title 加了判断
:difficulty="card?.activity?.difficulty ?? '—'"     ← 无判断
:n-steps="card?.n_steps || 3"                       ← 无判断
:form-text="card?.feedback_form || ''"              ← 无判断
```
**危害比「显示错」更严重**：家长会**照着 gro-007 的成功判据去判断 cog-001 做没做到**，
然后把结论填进去 —— 错判据比没有判据更有害。

**修法**（用户选定）：`/api/plan` 每条候选补上自己的 `n_steps` / `feedback_form`
（用与 `/api/card` **同一个** `render_feedback_form`，两处不会漂移）；
`PlanView` 暴露 `infoOf(activity_id)`；`App.vue` 按行取，取不到就留空。

**★ 教训**：
1. **`vite build` 通过 ≠ 渲染正确。** 这个 bug 编译期毫无征兆 ——
   它不是语法错，是**数据用错了源**，构建器看不出来，只有真点按钮才暴露。
   阶段 5 交付时写的「浏览器实测通过」验的是 curl 能通，不是渲染对。
2. **错判据比空判据有害。** 取不到信息时让 `form-text` 留空、`n_steps` 退回 3 ——
   空白家长看得出是「没信息」，印着别人的判据则会被当成真的照着填。
3. **`title` 加了判断而 `form-text` 没加**，这种「改了一处忘了另一处」
   很难靠读代码发现，只能靠真跑一遍。

---

## ⚠️ 已知遗留（本轮发现）

**Phase 2（红旗）新增的：**

- **计划暂停没有过期策略。** urgent 没人关就永远停着计划生成。
  这是有意的 fail-closed，且横幅让它可见可操作；Phase 3 该重新审视一次，
  到时**只改 `store.plan_paused` 那一条 SQL**。
- **红旗状态变更没有完整审计。** 现在只有 `status_changed_at` 记时点，
  没有"谁在什么时候从什么改成了什么"。留给 Phase 5 的 `workflow_audit_events`。
- **评估报告里的 urgent 也会暂停当前计划**（哪怕是几个月前那份报告）。
  这是产品判断不是技术判断，当时选了 fail-closed；要反转同样只改那一条 SQL。

**更早就在的：**

- **`DailySummary.domain_updates` 默认值为何是 `(FieldInfo,)` 尚未定位。**
  已用形状校验挡住后果，但**为什么**没查清。下一个 session 若有余力，
  值得单独二分一次（⚠️ 别再手写"看起来一样"的最小复现 —— 我试了 3 次全部
  复现不出来，每次都得出 `[]` 然后推翻自己重来。正确做法是**直接二分真文件**）。
- **`refusal_counts >= 2` 没有冷却期结束条件**（阶段 4b 遗留，语义变了但问题还在）
- **抽取质量仍未验证**：outcome 由家长点之后绕开了它，
  但"从家长补充描述里抽事件"的准确率依然只有 live/capture 才测得到
- **`--no-llm` 下状态机仍被闸门冻结**（降级画像 0.1）—— 这是对的，闸门该关就关

---

## 阶段 5 的手动测试清单（原方案，已按实现调整）

**启动**：
```bash
ASD_DB_PATH=/tmp/ganyu_dev.db ./.venv/bin/python server.py
cd frontend && npm run dev     # ⚠️ 5173 被占时会自动退到 5174，看启动日志
```
**测之前**：打开左上角「离线模式」开关（live 模式每次出卡片要 30-45s）。

- [ ] 建档：贴报告 → SSE 进度 → 出画像。空库会降级成可信度 0.1，**这是对的**
- [ ] 建档红旗：贴含 C11「会去咬自己的袖子」的原文 → 页面**顶部**出现置顶常驻的红旗横幅
- [ ] ★ 红旗状态：「已看到」**不**解除暂停，「已处理」才解除；已处理的移进折叠区不消失
- [ ] ★ 提交含自伤的观察后**不刷新**横幅就多一条「来自当天观察」；
      且那天 `observations` 真的有行（Phase 0/1 这里是 0 —— 这是本轮最要紧的修复）
- [ ] 逐条反馈：Day1 只填 gro-007 填「不配合」，fin-001/gro-006 点「跳过」
      → 响应里 `refusal_counts` 必须是 `{'gro-007': 1}`
- [ ] 「其他观察」框：填超市捂耳朵 → 事件进 observations，`refusal_counts` **一条都不加**
- [ ] 逐条 + note：note 非空的那条才调 LLM，**Network 里这条慢**，其余秒回
- [ ] 日结：提交后**自动**出现在响应里，`daily_summary` 表行数 +1
- [ ] 冷却：连续两天拒同一条 → 第三天它从候选消失，理由「已连续抗拒 2 次」
- [ ] 进度：可信度 0.1 时显示「卡在未开始，本可到入门 —— 升级闸门拦下了」（**正确输出**）
- [ ] **并发**：`Promise.all` 打 progress+summaries 不再 500（换 PG 后应彻底消失）

## 降级方案

时间紧就**直接交 CLI demo**，前端可以整个砍掉 ——
`loop.py --no-llm --script hardcoded`（0.0s，五日闭环 + 冷却 + 日结落库）
加 `--seed-history` 已覆盖全部技术叙事：多 Agent、结构化检索、长期记忆、状态机、闭环。
