# LangGraph 接入 — 换 session 交接

> 配套 `PROGRESS.md`（老规矩：动手前先读那份）。本文件只记 **LangGraph 接入**这件事。
> 完整设计在 `~/.claude/plans/python-llm-langgraph-hitl-time-travel-i-gleaming-octopus.md`
> （新 session 不会自动读，需手动打开或让我重述）。

---

## 🔖 换 session 先看这一屏

**当前阶段：Phase 0 ✅ 已完成，图已跑通并有 9 组断言守着。** 下一步是 Phase 1（观察链扩层 + 澄清 interrupt 接进 HTTP）。

已完成三件事，都实测过：

1. **`store.connect()` 改 autocommit**（`store.py:100`，一个参数 + docstring）。
   这是接入 checkpointer 的**硬前置** —— 原来长连接非 autocommit 会留下永不结束的事务，
   把 `PostgresSaver.setup()` 的 `CREATE INDEX CONCURRENTLY` 卡死。
2. **LangGraph 全部关键行为已实测**（`asd_agent/_spike_lg.py` / `_spike_fork.py`，15 条断言全绿）。
   **不要凭记忆或文档抄 API**，下面「实测结论」一节是照着实际输出写的，1.x 与网上 0.x 示例有出入。
3. **`asd_agent/graph/` 已落地**（Phase 0，2026-10-20）：state / checkpointer / nodes / builder / selfcheck。
   7 个节点、1 个 ⚡ 澄清 interrupt、图级验收全绿，`asd_agent/` 现有模块一行没改。
   图的形状与「落库只在 interrupt 之后」这条结构约束见第四节。

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

## 五、待确认 / 遗留

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
- **`TodayPage.vue:107` 的 `canSubmit` 只看 `feedbacks.some(f => f.outcome)`，
  完全不看 `otherNotes`** → 家长只写自然语言观察、没做任何活动时**按钮点不动**。
  而技术方案 §1.1 第一步恰恰是「家长用自然语言记录」。Phase 1 一起修。
- **前端零持久化**（grep `localStorage` 空）→ 家长填一半刷新即丢。草稿改存 checkpoint。
- **技能/场景归属靠进程内活动库反查**，全库没有指向 `activity_id` 的外键
  （`sessions.activity_id` 是裸 TEXT，`sessions_for_skill` 靠 `load_activities()` 建 by_id）。
  「生活片段一等输入」若要关联活动，没有数据库约束可依赖。

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
