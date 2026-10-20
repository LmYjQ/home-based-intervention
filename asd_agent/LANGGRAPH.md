# LangGraph 接入 — 换 session 交接

> 配套 `PROGRESS.md`（老规矩：动手前先读那份）。本文件只记 **LangGraph 接入**这件事。
> 完整设计在 `~/.claude/plans/python-llm-langgraph-hitl-time-travel-i-gleaming-octopus.md`
> （新 session 不会自动读，需手动打开或让我重述）。

---

## 🔖 换 session 先看这一屏

**当前阶段：Phase 2 ✅ 已完成（红旗落库 + 转诊指引 + 置顶横幅）。**
**下一步 = Phase 3（计划链）** —— 开工前先读本节末尾的
「🎯 Phase 3 开工前必须知道的四件事」，那一屏是为你写的。

### Phase 2 做完什么

**红旗从"响应里飘一次"变成长期记录。** 三条具体后果被修掉：

1. `escalate → END` → **`escalate → publish → END`**。原来 urgent 那天的
   家长原话**从不进 `observations` 表**，最该被记下来的那天什么都没留下。
2. 新表 `red_flags` + 唯一索引幂等 + 全部严重度都落 + 逐 code 的转诊指引。
3. 「暂停计划生成」从一句响应里的字符串变成**可持久查询**的判据
   `store.plan_paused(child_id)`。

图级验收 **14 组**，HTTP 层 **9 组**。细节见第四节之三。

### 🎯 Phase 3 开工前必须知道的四件事

1. **`generate_plan` 必须调 `store.plan_paused(child_id)`，不许自己再发明一个。**
   那是「能不能生成计划」的**唯一口径**（存在 `severity='urgent'` 且
   `status<>'closed'` 的红旗；**按孩子不按天**）。理由与那条查询里的注释。
   要加过期策略就改那一条 SQL，**不要**加第二处定义。
2. **落库仍然只能在 `publish` 一个节点里。** Phase 3 要插的 `⚡ approve_plan`
   在 `safety_gate` **之后**，所以图会变成
   `safety_gate → escalate → publish` 与 `safety_gate → … → generate → ⚡ approve → freeze → publish`。
   `freeze_plan` **不许自己写 `plans` 表** —— 一旦有第二个写库节点，
   「interrupt 恢复重跑节点」（第二节 ⑤）就会在某天咬人。
3. **`plans` 表现在只有 4 列**（`id / child_id / plan_date / created_at / payload`），
   没有 `status` / `version`。**这是 Phase 3 要补的第一块迁移**，
   也是当初图上故意不画计划链的原因（`builder.py` 模块 docstring 记着）。
   `status` 的可变性有先例可抄：`red_flags.status` 的约定见第四节之三，
   两处一起引入、只解释一遍。
4. **`planner.plan_week` 在 HTTP 里是死代码**（无任何路由调用它）。
   要么接进 `generate_plan`，要么**删掉整条 LLM 链路只保留 `pack_day`** ——
   别让它继续躺着，那会让"LLM 出了个计划"这件事看起来发生过而其实没有。

Phase 1 做完四件事，全部有断言或浏览器实测守着：

1. **四层字段**（技术方案 §4）：`fact_fields` 在 `schema.Observation`（逐事件），
   `parent_interpretation` / `hypotheses` / `missing_information` 在 `observe.ObservationBatch`（跨事件）。
   **全部可选带默认值**，原因见第四节「向后兼容」那条。
2. **澄清 interrupt 接进 HTTP**：`POST /api/observe/run` → 可能返回 `phase:"clarify"`，
   `POST /api/observe/resume` 回答，`GET /api/observe/status` 供刷新后重新发现待答的问题。
3. **`TodayPage.vue` 的 `canSubmit` 修了**：原来只看 `feedbacks.some(f => f.outcome)`、
   完全不看 `otherNotes` → 家长只写自然语言观察时按钮点不动，
   技术方案 §1.1 的第一步在界面上根本走不通。
4. **草稿存 checkpoint**：前端此前零持久化，填一半刷新即丢。
   现在存 `draft:{child_id}:{date}` 独立 thread（`graph/draft.py`）。

新增两处**以前没人守**的东西：`asd_agent/http_selfcheck.py`（**9 组**，
因为 `server.py` 不在任何回归集里）与 `graph/selfcheck.py` 的 [9]-[14] 组。

Phase 0 之前的三件事也仍然成立：

- **`store.connect()` 改 autocommit**（`store.py:100`）—— 接入 checkpointer 的硬前置。
- **LangGraph 全部关键行为已实测**（`_spike_lg.py` / `_spike_fork.py`）。
  **不要凭记忆或文档抄 API**，第二节是照着实际输出写的。
- **`asd_agent/graph/` 的形状与「落库只在 interrupt 之后」这条结构约束见第四节。**

### 环境

```bash
# 已装（uv 建的 venv，没有 pip）
langgraph                      1.2.16     ← 本来就在，别再「装」
langchain-core                 1.6.12
langgraph-checkpoint-postgres  3.2.0      ← 本次新装，独立包不在主包里
psycopg-pool                   3.3.3      ← 上面那个包的依赖

# PG 容器（端口绑 127.0.0.1，不要绑 0.0.0.0）
docker run -d --rm --name ganyu_pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ganyu -p 127.0.0.1:55432:5432 81bd698b4594
```

⚠️ 本机 8000 被占，后端用 **8123**，前端 `vite.config.js` 的代理 target 要同步改。

⚠️ **Phase 2 加了新表 `red_flags` —— 改了 `store.py` 的表之后必须重启后端**：
`store._init` 只在某个 schema 的连接**首次创建**时跑，跑着的服务不会凭空多出这张表，
新路由会 500 `relation "red_flags" does not exist`。

---

## 一、为什么必须先修 `store.py`

不是洁癖问题，有两个真实后果：

1. `CREATE INDEX CONCURRENTLY` 等的是**整个 database** 的 virtualxid，**与 schema 无关**
   → 任何一条 `idle in transaction` 都能把 checkpointer 的 `setup()` 卡死。
   实测卡了 3 分钟不出，换个没有僵死后端的独立 database 立刻全绿。
2. 长期空转事务会挡 VACUUM。

**为什么改成 autocommit 是零语义变化**（这是当时做这个决定的关键依据，别再重新论证一遍）：

```
grep -n "commit()\|rollback()" asd_agent/*.py server.py
→ 只有 commit()，一次 rollback() 都没有
→ 11 个 commit() 全在单个写函数结尾（ensure_child / save_profile / append_state /
   save_plan / log_session / log_observation / save_summary …）
→ 全项目没有一处多语句事务
```

事务模型本来就是「一条语句一个事务」，只是手写出来的，而手写版本恰好在**每次读之后**
留下一个永不结束的事务。实测：非 autocommit 时 SELECT 后 `transaction_status=2`（ACTIVE，事务开着），
autocommit 时 `=0`（无事务）；且 `commit()` 在 autocommit 连接上是无害 no-op。

**那些 `commit()` 保留着** —— autocommit 下是 no-op，关掉 autocommit 仍然正确。
别去「清理」它们。

### 实测结果（全部在 `ganyu` 库上，就是之前卡死的那个）

```
12 个离线回归                       12/12 全绿
loop.py --no-llm --script hardcoded 五日闭环 + 冷却 + 日结落库，正常
HTTP 全链路（建档→卡片job→反馈→close） 全通；refusal_counts={}、闸门正确拦下
并发 15 轮 × 12 = 180 请求           0 失败（与 PROGRESS.md 基线一致）
跑完 PG 连接状态                     idle × 1，无 idle in transaction
spike 全套                          全绿
```

> ⚠️ **这依赖「没有别人开着长事务」。** 将来若有别的工具用非 autocommit 连同一个库，
> `setup()` 照样会被挡。所以 saver 仍按「自己的 autocommit 连接」写，`_SETUP_DONE`
> 缓存也保留 —— 双保险。
>
> 这次卡死的元凶是你自己昨天开的两个 dev server（`server.py` / `uvicorn 8123`），
> 各留了一条 idle-in-transaction，已挂 12 小时。**已被我停掉**（vite 保留）。
> 它们已不在回归集里，所以这类问题只会靠打接口发现。

---

## 二、LangGraph 实测结论（照抄这里，别抄文档）

以下是**实测输出**，不是文档摘抄。langgraph 1.2.16 + langgraph-checkpoint-postgres 3.2.0。

### ① `PostgresSaver` 没有 `schema=` 参数

```
__init__(self, conn, pipe=None, serde=None)          ← 签名里没有 schema
from_conn_string(conn_string, *, pipeline=False)     ← contextmanager
setup() / list() / delete_thread(thread_id)
```

表建在哪取决于连接的 `search_path`。`setup()` 建 4 张表：
`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`。

**导出里没有 `AsyncPostgresSaver`**（3.2.0 只有同步的 `PostgresSaver`）。
与现有 sync FastAPI 正合适，不用改 async。

### ② saver 不能用 `store.connect()` 的连接

会抛：

```
psycopg.errors.ActiveSqlTransaction: CREATE INDEX CONCURRENTLY cannot run inside a transaction block
```

`setup()` 里有 CONCURRENTLY，而 `store.connect()` 是事务式的（虽然现在开了 autocommit，
但 saver 应该持有自己的连接，生命周期解耦）。**已实测可用的写法**：

```python
_SETUP_LOCK = threading.Lock()
_SETUP_DONE: set[str] = set()

def saver_for(path: str | None = None) -> PostgresSaver:
    sch = store._schema_of(path or store.DEFAULT_DB)
    conn = psycopg.connect(store.DEFAULT_DSN, autocommit=True)   # 必须 autocommit
    conn.row_factory = dict_row
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
    conn.execute(f'SET search_path TO "{sch}"')     # ← 表自然落在这个 schema
    s = PostgresSaver(conn)
    with _SETUP_LOCK:            # setup() 含 CONCURRENTLY，重复跑很贵
        if sch not in _SETUP_DONE:
            s.setup()
            _SETUP_DONE.add(sch)
    return s
```

实测：表建在正确 schema ✓，`public` 里 0 张 ✓，`store.reset()` 的
`DROP SCHEMA CASCADE` 会连 checkpoint 一起清掉 ✓（语义正好等于「删掉这个临时库」）。

### ③ ★ 分叉必须换 `thread_id` —— 方案 §5.5 的技术前提

| 姿势 | 实测结果 |
|---|---|
| `invoke(..., past.config)` 同 thread | 状态不变 ✓，但**原 thread checkpoint 4 → 6**（历史被追加污染） |
| `update_state(past.config, {...})` 同 thread | **原 thread 状态被改写**（`answer` 从 `'10-17'` 变 `'EDITED'`）⚠️ |
| **换 `thread_id` + 历史 state 作输入** | **完全隔离**：原 thread 4→4、`answer` 不变、编辑带得进新 thread ✓ |

生产上 `thread_id = f"{child_id}:{date}"` 天然唯一。

### ④ `thread_id` 不可复用

同名 run 的 checkpoint 会**累积**（实测 4 → 8 → 10）。测试必须带唯一后缀
（`_spike_fork.py` 用 `SPIKE_RUN` 环境变量做）。

### ⑤ ★ `interrupt()` 恢复时会重跑整个节点

实测最终 state 里 `before_interrupt` 出现 **2 次**。所以：
**节点在 `interrupt()` 之前不得有任何不可重复的副作用**（DB 写入、通知、发消息）。
这是硬约束，不是建议。

> ⚠️ **2026-10-20 补测：这个现象只在 `PostgresSaver` 下出现，用 `InMemorySaver` 验不出来。**
> 同一个探针（节点在 interrupt 之前原地改 state）：
>
> | checkpointer | 恢复后 hits |
> |---|---|
> | `InMemorySaver` | `['before_interrupt', 'after']` ← **看不出重跑** |
> | `PostgresSaver` | `['before_interrupt', 'before_interrupt', 'after']` |
>
> 后果很具体：谁拿 `InMemorySaver` 写单测，看到"只执行了一次"，
> 就会推断"恢复不重跑"，然后放心地把写库放到 interrupt 之前 ——
> 上生产（用 PG）就双写。**任何验这条约束的测试必须用生产 saver。**
> 探针在 `graph/selfcheck.py::_probe_rerun`，与图一起走，别把它删了。
>
> 顺带一个推论：**别指望在 state 里"记录"重跑**。生产节点在 interrupt
> 之前不做任何事，所以真图里根本没有可观测的重跑痕迹 ——
> 这说明约束被遵守了，不是说明重跑不存在。
> （我一度想用 `Annotated[list, operator.add]` 在 trace 里留痕，实测无效：
> 返回值只在节点**完成**时 apply，原地 append 跨不过 checkpoint 序列化。
> 与 reducer 选哪个无关，是机制本身。）

### ⑥ `Command.resume` 有两种形态

```python
Command(resume=value)                  # 给下一个 interrupt
Command(resume={interrupt_id: value})  # 定向到具体某个 interrupt（多个并存时用）
```
两种实测都可用。interrupt id 从 `graph.get_state(cfg).interrupts[0].id` 取。

### ⑦ `interrupt()` 有个 1.x 新参数，值得用

```python
interrupt(value, *, response_schema: dict | type | None = None)
```
可用 Pydantic 直接约束 resume 载荷 —— 正好落实技术方案 §5.4
「前端提交的操作必须经过服务端 Schema 校验」。别用裸 dict。

### ⑧ 审计元数据

每个 `StateSnapshot` 有 `.values` / `.next` / `.config` / `.metadata`
（`metadata.step` 从 -1 起，`metadata.source` 为 `input` 或 `loop`）。
这就是技术方案 §6 `workflow_audit_events` 的现成来源。

### ⑨ 关键 API 签名

```python
from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt, Command
from langgraph.checkpoint.postgres import PostgresSaver

g.add_node(name, fn, *, defer=False, retry_policy=None,
           error_handler=None, destinations=None, timeout=None)
g.add_edge(start, end)
g.add_conditional_edges(source, path, path_map=None)
g.compile(checkpointer=saver, *, store=None, debug=False, name=None)

graph.invoke(input_or_Command, config, stream_mode="values")
graph.get_state(config, *, subgraphs=False) -> StateSnapshot
graph.get_state_history(config, *, filter=None, before=None, limit=None)
graph.update_state(config, values, as_node=None, task_id=None)
graph.stream(...) / graph.astream(...)
```

`config` 恒为 `{"configurable": {"thread_id": ...}}`。

---

## 三、我踩过的坑（新 session 别重蹈）

**全是方法论错误，不是库的坑：**

1. **断言按直觉写，不是按库行为写。** 我断言「从历史 checkpoint 能重跑」，
   实际它接着原 run 走了。PROGRESS.md 阶段 4 坑 2 同款：**写断言前先读一遍被断言的函数**。
   修正过程就是 `_spike_fork.py` 存在的意义。
2. **`hasattr(r, "values")` 恒为真** —— `dict.values` 是绑定方法，
   `hasattr` 通过后 `.get` 挂在方法上炸掉。`invoke()` 返回 dict、`get_state()` 返回
   `StateSnapshot`，两者都从 `.values` 拿，要显式区分。
3. **所有实验共用一个 thread** → 测「有没有污染」时读到的是前面实验的残留。
   每个实验必须独立 thread。
4. **`fresh()` 原本返回 `past.config` 而不是 `past`**，导致 `dict(past.values)`
   报 `'builtin_function_or_method' object is not iterable`。

**另有一条 API 坑**：`langgraph.checkpoint.postgres` 是**独立发行包**
（`langgraph-checkpoint-postgres`），主包里没有 —— 只 `import langgraph` 是装不上的。

**还有一条 HTTP 侧的**：PROGRESS.md 已有「`server.py` 不在回归集里」的警告。
这次 `store.py` 改了之后我照样单独起了服务打接口，中途自己写错了两处：
`/api/card` 走 job+SSE（返回 `job_id` 要轮询 `/api/jobs/{id}`），以及
`ActivityFeedback.prompt_level` 是 **int 0-3** 不是字符串。

---

## 四、Phase 0 ✅ 已完成（2026-10-20）

`asd_agent/graph/` 已落地，**`asd_agent/` 任何现有模块一行没改**，
12 个离线回归 + `activities --validate` + `loop.py --script hardcoded` 全绿。

| 文件 | 内容 |
|---|---|
| `graph/state.py` | `InterventionState`（**按现有实体收敛到 20 个字段**，不是方案 §5.3 那二十多个）、`ClarifyAnswer` / `EscalationVerdict` / `PlanReview`、`thread_id_for` / `fork_config` |
| `graph/checkpointer.py` | 上面实测过的 `saver_for()` + 自检 |
| `graph/nodes.py` | 7 个节点体，全部是现有纯函数的薄封装（每个都注明调的是哪个函数） |
| `graph/builder.py` | `StateGraph` 定义、边、条件路由 |
| `graph/selfcheck.py` | **图级验收，9 组断言**（Phase 0 的验收标准） |

### 图现在长什么样（只画底座已存在的部分）

```
START → record_intake → extract_observation → validate_observation
                                            ├─ needs_clarification? → ⚡ clarify → 回抽取
                                            └─ safety_gate → urgent? ├─ 是 → escalate → END
                                                              └─ 否 → publish → END
```

**计划链（select_goal / retrieve / generate / validate_plan / approve_plan / freeze_plan）
和反馈链（record_execution / analyze_outcome / next_action）故意没画** ——
它们各自缺一块 Phase 0 明确不许动的东西：`plans` 表没有 `status`/`version`，
`sessions` 没有 `plan_id`/`run_id`，红旗没有落库位置。
现在画就只能画一版**注定要改**的图，而图的形状会渗进 checkpoint 的历史里。
`state.py` 已经把 `PlanReview` 的形状定好，Phase 3 接的时候不必再改 state 定义。

### ★ 图的形状不是审美，是一条实测约束的结果

`publish` 是**全图唯一写业务表的节点，且排在所有 interrupt 之后**。
理由是 ⑤：恢复会重跑所在节点，于是凡是在 interrupt 之前写过库的东西，
resume 一次就写两遍。**这条不能靠"我记得判重"，只能靠节点的位置。**

（`publish` 自己仍按日期做了幂等兜底 —— 那是第二道，不是第一道。）

### 跑法

```bash
cd asd_agent
ASD_DB_PATH=/tmp/graph_selfcheck.db ../.venv/bin/python -m graph.selfcheck   # 图级验收
ASD_DB_PATH=/tmp/graph_ckpt_check.db ../.venv/bin/python -m graph.checkpointer
```

### Phase 0 踩到的坑（已修，勿回退）

1. **★ `clarify → extract → validate` 是环，没有出口守卫会死循环。**
   澄清后重抽，若仍判"缺信息"就会再问一遍同一个问题 ——
   而重复追问不只是烦，是**系统把自己没听懂的锅推给人**。
   → `clarify_asked` 只允许问一次。答完不管清不清楚都往下走；
   真需要更细的信息是 Phase 1 `missing_information` 的活，不是再问一遍。

2. **★ 抽取的幂等判据不能用"observation 字段在不在"。**
   澄清之后该字段还在，但语料**确实变了**，那一轮必须重抽。
   → 判据是 `observed_text == parent_text`（语料变没变），
   顺带白赚一件事：分叉出来的 thread 带着上一轮结果直接继续时不会重复付 LLM。

3. **★ `publish` 里曾用 `clarified_text != parent_text` 判"要不要重落库"** ——
   而 `_reextract` 会把 `parent_text` 直接改成 merged，两者恒相等，
   那个比较永远为假，于是澄清后的重抽结果**永远不落库**（静默丢数据）。
   → 改用 `clarify_asked`。**这类 bug 没有任何报错**，是本次最险的一处。

4. **我写的两条断言本身是错的**（PROGRESS.md 阶段 4 / 4b 同款）：
   - "新 thread 的语料不含原 thread 的澄清回答" —— 分叉**就该继承**历史 state，
     断言它不继承等于把方案 §5.5 要的能力（"昨天那条其实做了"→ 改掉再看今天怎么变）
     断言成了"不该继承"。
   - "interrupt 前节点都跑过了" 写成 `"extract_observation" in trace` ——
     而 trace 里那一项是 `extract_observation(reused)`，列表成员判断要求完全相等。

5. **`people_present=["parent"]` 被 Pydantic 拒了** ——
   词表里是 `mother`/`father`，没有 `parent`。**这是 schema 在正常工作**，
   写验收脚本时它会替你挡住"看起来合理"的假数据。

### 两个必须写死的边界（否则一定会有人踩回来）

1. **Checkpoint 与业务数据库是两回事。** `sessions` / `skill_state` / `observations` /
   `daily_summary` 全部保留 append-only。Checkpoint 是**追加的** resume + provenance 层，
   不是替代品。**别因为「反正有 checkpoint 了」去删业务表** —— SQL 可查性和跨进程能力会一起没。
2. **模型给证据，Python 做判定。** 节点体默认写纯函数；引入 LLM 节点必须写清楚
   它为什么不能是判定函数。LangGraph 让「加个节点就交给模型」变容易了，
   而这恰好会回退现有设计最好的那条原则。
   本项目的落法：`observe.extract_observations` 提供**文案**，
   `validate_observation` / `safety_gate` 决定**要不要问 / 要不要拦**。

---

## 四之二、Phase 1 ✅ 已完成（2026-10-20）：图接进 HTTP

`server.py` 此前**完全没有 import 过 `graph/`** —— 编排能力离线可验、线上不可达。
现在接上了，顺带修掉两个**一接线就生效**的潜伏缺陷。

### 新增路由

| 路由 | 形态 | 说明 |
|---|---|---|
| `POST /api/observe/run` | job+SSE | `{child_id, date, text, no_llm?}` → `{job_id}`；终帧 `phase = clarify \| done` |
| `POST /api/observe/resume` | job+SSE | 同 `thread_id` + `Command(resume=...)` |
| `GET /api/observe/status` | 直接 JSON | 纯 `get_state`。**刷新后靠它发现待答的追问** |
| `GET/PUT/DELETE /api/draft` | 直接 JSON | 毫秒级，不走 job |

### ★ 四条接线约束（改之前先读）

1. **resume 的身份是 `(child_id, date)`，不是 `job_id`。**
   `_Job` 是进程内一次性容器（重启即失、没有 resume 概念），
   `api.js` 的 `streamJob` 也只管一次 job 寿命 —— **两者都不用改**。
   连续性在 checkpoint 里，这正是引入 checkpointer 的理由。

2. **★ 没有待回答的澄清时 `resume` 必须 409。**
   `Command(resume=...)` 打在没有 pending interrupt 的 thread 上，
   LangGraph 会把它当成**新输入从头跑一遍整条图** → 重新抽取、重新落库。
   **不报错、不抛异常，就是数据写了两遍。**
   守卫必须在 `graph.invoke` 之前。断言在 `http_selfcheck.py` 的 [6] 与 [8]。

3. **中断判定用 `snap.next` / `snap.interrupts`，不用 state 里的 `needs_clarification`。**
   resume 跑完之后那个字段是**上一次**的值，拿它判会把「已经答过了」
   误报成「还在等」。

4. **「其他观察」只有一个写入口。**
   `/api/feedbacks` 不再把 `other_notes` 传给 `apply_feedbacks`（否则同一段话抽两次、
   写两行、付两次 LLM，`refusal` 与 `recent_scenes` 被算两遍）。
   `apply_feedbacks` **本身没改** —— `loop.py` 的 CLI 还在用它那条路径。
   `FeedbackIn.other_notes` 保留但标 deprecated，避免旧前端静默丢掉家长写的话。

### ★ 修掉的三个既有缺陷（图一接进 HTTP 就生效）

1. **`publish` 的幂等判据从「这一天」换成「这一段语料」。**
   原判据 `_today_has_observations(child, date)`（这天有没有**任何**一行）在 Phase 0
   成立，是因为当时只有图一个写入口。现在 `apply_feedbacks` 同一天也会从逐条活动的
   note 写 `observations` 行 → 那个粗判据会把「其他观察」误判成「今天已落库」而
   **静默跳过**。改成按 `observations.source` 判/删，**不需要任何迁移**
   （source 列早就在存整段原话）。顺带与读侧对齐 —— `server.py` 早就按
   `(observed_at, source)` 分组了。

2. **`_delete_today` 按 `child+date` 删全部 → 改成按 source 删。**
   澄清重抽时原来会连带删掉同一天逐条 note 写的行。那是数据丢失，不是覆盖。

3. **写库键与查库键不一致（新 selfcheck [11] 暴露的）。**
   `publish` 的幂等判据查 `state["date"]`，而 `observe.persist` 写的是
   `observed_at=outcome.date`。两者不等时「查的那天」和「写的那天」不是同一天，
   幂等判据永远匹配不上 —— **每次跑都重写一遍**，行还落到了空日期上。
   现在把 date 钉成 `state["date"]`：图的坐标是权威。
   分叉、恢复、带 observation 重跑这三条路上两者本来就不相等。

### 向后兼容：为什么四个新字段绝不能 required

`server.py` 的教练诊断用**裸 `except: continue`** 重校验历史 `observations.payload`，
`graph/nodes.py` 的 publish 同样重校验旧 checkpoint。
新字段一旦 required：**每一条旧 observation 都会校验失败并被静默跳过**（没有报错，
只是家长和教练突然看不到几个月前的观察），旧 checkpoint 直接 raise。
断言在 `selfcheck.py` 的 [9] 组（含一条 Phase 1 之前的 payload 字面量）。

同理，`--no-llm` 降级时四层**全空、一条都不编**（断言 [10]）——
空不是「坏了」，是没得说。

### ★ 提示词：小模型跟例子，不跟论述

四层的三条（parent_interpretation / hypotheses / missing_information）
**抽象描述版实测三层全空** —— 7B 合规地输出了 `null`/`[]`，但内容是空的
（schema 标不标 required 都一样：required 时是「合规但空」）。
改成「照这个例子写」之后，家长确实说了解读时能抽出来，家长没说时正确留 null。
规则单独放在 `_RULES_LAYERS`（`observe.py`）——
**绝不能塞进 `_RULES_1_6`**，那个块与 `verify_schema.build_prompt()` 逐字对齐，
断言在 `selfcheck.py` 的 [12] 组。

### 草稿为什么不复用主图

`graph/draft.py` 用独立 thread 前缀 `draft:`。三条理由，最后一条是要害：
**草稿图从不 invoke**（只用 `update_state` 写 / `get_state` 读），
没有任何节点会被执行，所以「interrupt 恢复会重跑节点」这条硬约束
**在结构上不可能被违反** —— 而不是靠加一个「记得判重」的 if。

---

## 四之三、Phase 2 ✅ 已完成（2026-10-20）：红旗落库 + 转诊指引

Phase 0/1 的 `safety_gate → escalate → END` 有三处具体后果，不是"功能没做完"：

1. **最要紧的数据在最重要的那天丢了。** `escalate → END` 跳过了 `publish`，
   于是"孩子今天咬自己"这句话**从不进 `observations` 表**。没有报错，
   只是最该被记下来的那一天什么都没留下 —— 而 `observe.persist` 本来
   会给那几行打上 `redflag_triggered`，`store.what_last_week` 的
   "本周有几天碰到红旗"也因此一直是 0。
2. **红旗无处可落。** `RedFlagList.vue` 吃的是一次响应里的 `report`，
   `v-if` 一卸载就永久消失。
3. **"暂停计划生成"只是一句响应里的字符串**，没有第二个消费者。

### ★ 改的是边，不是判定

`_route_safety` 一行没动（仍是 `redflag.scan` 的 `highest == "urgent"`），
只把 `escalate → END` 换成 `escalate → publish → END`。两条理由，第二条是主要的：

- urgent 那天不再是"什么都不写"的一天。
- **Phase 3 要在 `safety_gate` 之后插 `⚡ approve_plan`。**
  红旗若写在 `escalate` 里，那时它就落在 interrupt 之前 → resume 一次写两遍
  （第五节 ⑤）。放进 `publish` 对那个未来的 interrupt **结构上免疫**。

`publish` 仍是**唯一**写业务表的节点，现在写两张表。这不是"一个节点干两件事"，
是"唯一一个能安全写库的位置，正好要写两张"。

### ★ 红旗写入刻意在降级分支**之外**

`publish` 里那个 `if degraded or not obs: ids = []`（降级时 observations 一行都不编）
**没有**把红旗包进去。理由是 `redflag.py` 存在的第二个理由：
「模型挂了红旗照样能报」。把这段写进那个 if，全项目最该有的保障会在一次
"顺手重构"里没了，而且**没有任何东西会报错**。
断言在 `selfcheck.py` [14]：`--no-llm` 跑 C11 → `observations` **0 行**、
`red_flags` **有行**。

### 表与幂等

`red_flags` 一张表。幂等键 = `(child_id, observed_at, code, source_hash)`，
靠**唯一索引 + `ON CONFLICT DO NOTHING`**，不是 SELECT-then-INSERT
（`connect()` 是每线程一条连接，并发下两个线程都能"看到没有这行"）。
- `source_hash` 是 sha256 —— btree unique index 遇到超长 text 会报
  *index row size exceeds maximum*，而 `other_notes` 允许 20000 字符。
  原文仍存 `source` 列，只是不进索引。
- 键里**不含 `thread_id`** —— 分叉出来的 thread 重跑同一段话，
  不应该在真实历史里多出一条红旗。
- `ON CONFLICT DO NOTHING` 仍然会消耗 sequence 值，所以 id 会有跳号。这是正常的。

**★ 全项目唯一可变的业务字段是 `status`。** 行的身份（命中了什么、哪段话）
append-only、永不重写、从不删除；只有家长的确认在动。
Phase 3 的 `plans.status` 会用同一套约定，两处一起引入。

### 转诊指引长在 `RULES` 上

`redflag.py` 每条规则多一个 `referral = {who, do, avoid, emergency}`。
`avoid`（不要做什么）是**防害字段**，不允许为空。
`guidance_for(report)` 纯函数聚合多个 urgent → `{summary, items, do_not, emergency}`；
`summary` **仍是一句 str**（老 checkpoint 与老前端读的就是这个键），
`items` 是新加的结构。

之前那句硬编码在 `nodes.escalate` 的通用文案有个具体后果：
命中"异食"和命中"撞头"给家长的是同一句话，而这两件事该找的人根本不一样。

### 「暂停」= 一条查询，给 Phase 3 的合同

```python
store.plan_paused(conn, child_id)
# 存在 severity='urgent' 且 status<>'closed' 的红旗
```

- **未关闭**而不是未看到 → fail-closed（与"模型给证据，Python 做判定"同源）
- 按**孩子**判而不是按天判 → 10-20 的自伤必须让 10-21 的计划也停着
- 没有为它单开一列或一张表（`plans.status` 是 Phase 3 的）
- **Phase 3 的 `generate_plan` 必须调它，不许自己再发明一个。**
  要加过期策略就改那一条 SQL，不要加第二处定义。

`state["escalation"]["paused"]` 是**事件**（这次升级发生了），
`plan_paused` 是**当前状态**（现在能不能生成计划）。两者回答的不是同一个问题。

### 第二个写入口：`POST /api/profile`

建档时 `redflag.scan(report_text)` 早就在跑，扫到的红旗同样只飘一次。
现在走**同一对函数**（`redflag.rows_for` 纯 + `store.append_red_flags` 落），
`source_kind='report'`（这一列不进唯一键，只为告诉前端"来自评估报告还是当天观察"）。
⚠️ 写入必须放在 `ensure_child` **之后** —— `red_flags.child_id` 有外键。

副作用（有意为之）：评估报告里几个月前的 urgent 也会暂停当前计划。
宁可 fail-closed，横幅会显示那条旗的日期。

### HTTP 与前端

| 路由 | 说明 |
|---|---|
| `GET /api/redflags?child_id=&status=` | `{flags, paused}`，**纯读**。`paused` 与列表同一次查询算出来，前端不许自己推 |
| `PATCH /api/redflags/{id}/status` | 只收 `Literal` 三态。422 非法值 / 404 不存在 / 409 非法转移 |

```
open → acknowledged | closed ；acknowledged → closed | open ；closed → open
```

- **只有 `closed` 解除暂停**（"已看到" ≠ "已处理"）。
- 409 而不是 422：请求本身合法，冲突的是当前状态 —— 与 `/api/observe/resume` 那个 409 同一类。
- 非法转移的判据是 `redflag.can_transition`，**纯函数、可单测**；
  `store.set_red_flag_status` 只执行。刻意分成两半：混在一起就会出现
  "为了省一次查询把判据搬进 SQL"，那就再也测不动了。

前端**新建** `RedFlagBanner.vue`，没有复用 `RedFlagList.vue`：
后者吃一次扫描结果、无按钮；前者吃已落库的行、要按状态给动作。
放在 `TodayPage.vue` 根 div 的**第一个子元素** —— 暂停是整天的事，
排在反馈区的话家长会先读完"今天练这三条"再看到"今天要停"。

### 本轮踩到的坑（已修，勿回退）

1. **`ON CONFLICT DO NOTHING` 之后 `RETURNING` 没有行** → `fetchone()` 是 `None`。
   `append_red_flags` 因此**只返回真正新增的 id**；重跑一次得到 `[]` 是对的。
2. **`observations.redflag_triggered` 的来源改成 safety_gate 那次扫描**。
   原来取抽取时那次，而两次扫描的语料不一定相同（澄清把回答拼在后面）——
   两处各判一次，迟早有一天对不上，而那是一个没有报错的数据。
3. **判据是"存在任意一条未关闭的 urgent"，不是"那一条"**。
   自检里一度只关掉一条就断言"暂停解除"，红了 —— 同一个孩子两次 urgent
   是常态。这不是测试写复杂了，正是"按孩子判"那句话的后果。

---

## 四之四、Phase 3 待接（还没画这部分图）

> 本节是从旧计划文件（`~/.claude/plans/python-llm-langgraph-hitl-time-travel-i-gleaming-octopus.md`
> 「Phase 3」那一节）搬过来的，**新 session 不会自动读那份计划文件**。
> 标 ⚠️ 的地方是**搬过来时没重新验证的**，动手前先读一遍真文件确认。

### 图长什么样（Phase 3 接完之后）

```
… → safety_gate → (urgent? escalate ─────────────→ publish → END)
                 └─ no → select_goal → retrieve_activities → generate_plan
                                                          ↓
                                                    validate_plan
                                                          ↓
                                          substantive_change?  ← 纯 Python 判据
                                       ├─ 是 → ⚡ approve(approve|edit|reject)
                                       │         edit → 回 retrieve_activities
                                       └─ 否 ─┴─→ freeze_plan → publish → END
```

★ **注意两条支路最后都汇到 `publish`。** 红旗写入在 Phase 2 已经归它了
（第四节之三），Phase 3 不要再造第二个写库节点。

### 实质性变更判据（纯 Python，`graph/nodes.py`）

`validate_plan` 比对 draft 与当前 active 版本，命中任一条即 interrupt：

- `target_skill_items` 集合不同
- 任一活动的 `materials` 集合不同
- 任一活动的 `difficulty` 不同
- 任一活动的 `prompt_hierarchy` **起点提示级别**不同
- `safety_gate` 在本轮触发过
- 计划跨度数 > 1 天（跨天安排需家长知情）

**这不是简化方案 §5.1，是修正它的一个内在矛盾**：方案 §1.2 写
"小步计划、低压力、避免每次游戏都变成训练"，而主图
`generate_plan → approve_plan(interrupt)` 意味着每天多点一次，
与 §10.3 自己列的"用户留存和家长负担"指标冲突。
变更是**可判定的**，所以判据必须是代码而不是措辞。

★ 落地时的坑：上面每一条都要求**拿得到"当前 active 版本"**，
而 `plans` 表现在没有 `status`。迁移补上 `status` 时，
`active` 版本的查询口径要写死在一个函数里（与 `store.plan_paused` 同款理由）。

### 必须做的迁移

| 表 | 补什么 |
|---|---|
| `plans` | `status`(draft/pending_review/approved/active/paused/completed/rejected) / `version` / `approved_by` / `approved_at` |
| `sessions` | `plan_id` / `plan_version`（现在只有裸 `activity_id`） |

### 验收断言（Phase 3 要补的）

- 「只在真变化时审批」：连续两天同计划 → 第二天**不产生 interrupt**；
  改一项材料 → 产生。（这条是纯 Python 判据唯一的守门人）
- 审批 interrupt 往返（走 HTTP）+ 重复 resume 被拒（409）
- **分叉跑出来的计划必须重新过 `safety_gate` 与必要审批**（方案 §5.5 明确要求）——
  本项目已有实测支撑：分叉必须换 `thread_id`（第二节 ③），
  而 `fork_config` 已经在 `state.py` 里写好了

### ⚠️ 搬过来没验证的部分

- `coach.py` + `planner.py` 合并成 `generate_plan` + `validate_plan` 的具体接口形状
- `coach.today` 现在怎么出卡片、`retrieve` 的候选打分能不能直接复用
  （`activities.retrieve/_hard_filter/_score` 是"原样复用"那一档，见计划文件的保留表）
- `DailySummary.domain_updates` 默认值是 `(FieldInfo,)` 那个 bug ——
  计划文件把它排在 Phase 6，并注明**别再手写最小复现**（试了 3 次全失败），
  正确做法是**直接二分真文件**。Phase 3 重构时顺手做掉。

---

## 五、待确认 / 遗留

**Phase 2 期间新发现的：**

- **暂停没有过期策略。** urgent 没人关就永远停着计划生成。
  我认为这是正确的临床默认（fail-closed）且横幅让它可见可操作，
  但 Phase 3 应该重新审视一次 —— 到时改 `store.plan_paused` 那一条 SQL。
- **红旗状态变更没有完整审计。** 现在只有 `status_changed_at` 记时点，
  没有"谁在什么时候从什么改成了什么"。留给 Phase 5 的 `workflow_audit_events`。
- **`ON CONFLICT DO NOTHING` 会消耗 sequence 值**，所以 `red_flags.id` 有跳号
  （实测 id 4 缺失）。这不是丢数据，别去"修"。

**Phase 1 期间新发现的：**

- **7B 对四层的「内容」仍然偏保守**：家长说了解读时 `parent_interpretation` 能抽到，
  但 `hypotheses` / `missing_information` 在多数输入上仍是空的。
  这是模型诚实（规则 10 明写「宁可空着不要凑满」），不是 bug。
  演示时若要展示这两层，建议用**家长明确说了「我猜他是…」**的输入。
- **澄清按日期挂 thread**：家长第二天才回答 → 那个 thread 成孤儿。
  demo 范围可接受，但产品上需要「未完成的澄清」列表。
- **`_JOBS` 无 TTL、无上限**（`server.py`，Phase 0 就有），
  每跑一次 observe 就多一条，长跑的 demo 会慢慢涨。
- **前端 `otherNotes` 提交后不清空**：`App.vue` 保持 TodayPage 挂载，
  家长重复点提交会把同一段话再发一次。`publish` 的幂等键能挡住落库，
  但会白跑一次抽取。

**Phase 1 之前就有的，仍然未动：**

- **`DailySummary.domain_updates` 默认值是 `(FieldInfo,)` 元组不是 `[]`**，
  只在 live 路径触发，12 个离线回归一个都没报。已用形状校验挡住后果但**根因未定位**。
  PROGRESS.md 明确说：**别再手写「看起来一样」的最小复现**（试了 3 次全失败），
  正确做法是**直接二分真文件**。Phase 3 重构时顺手做掉。
- **`planner.plan_week` 的 LLM 链路在 HTTP 里是死代码**（无任何路由调用它）。
  Phase 3 合并成 `generate_plan` + `validate_plan` 时，要么接上要么删掉整条链路
  只保留 `pack_day`。
- **`refusal_counts >= 2` 没有冷却期结束条件**（老问题，语义变过但问题还在）。
- **`/api/feedbacks` 无幂等保护**（只有 `/api/close` 有）。重复提交会让
  `refusal_counts` 翻倍 → 当天误触冷却阈值 2。Phase 4 改由 checkpoint 保证。
- **技能/场景归属靠进程内活动库反查**，全库没有指向 `activity_id` 的外键
  （`sessions.activity_id` 是裸 TEXT，`sessions_for_skill` 靠 `load_activities()` 建 by_id）。
  「生活片段一等输入」若要关联活动，没有数据库约束可依赖。
- **`llm.py` 的 capture 模式只存 response 不存 prompt**，replay 的 key 是 prompt 的
  sha256 —— **改一行 prompt 全线 fixture 失效**（Phase 1 改了两处 prompt）。
  方案 §6 要求审计，Phase 0 计划改成同时落 prompt 原文与 trace，**尚未做**。

---

## 六、留下的 spike 文件

`asd_agent/_spike_lg.py`（15 条断言：schema 落点 / interrupt 往返 / history / fork 隔离 /
reset 关系 / 4 线程并发）、`_spike_fork.py`（fork 姿势对照）。
上面所有实测结论的证据。可以当 Phase 0 的回归基线，也可以删。

跑：
```bash
cd asd_agent
ASD_DB_PATH=/tmp/spike_lgmain.db ../.venv/bin/python _spike_lg.py
SPIKE_RUN=r1 ../.venv/bin/python _spike_fork.py
```
