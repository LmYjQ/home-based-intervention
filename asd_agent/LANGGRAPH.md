# LangGraph 接入 — 换 session 交接

> 配套 `PROGRESS.md`（老规矩：动手前先读那份）。本文件只记 **LangGraph 接入**这件事。
> 完整设计在 `~/.claude/plans/python-llm-langgraph-hitl-time-travel-i-gleaming-octopus.md`
> （新 session 不会自动读，需手动打开或让我重述）。
>
> **进度：Phase 0 / 1 / 2 / 3 / 4 均已完成（2026-10-21 复核全绿）。下一步 = Phase 5（time travel）。**

---

## 🔖 换 session 先看这一屏

**当前阶段：Phase 4 ✅ 已完成并已提交（反馈链 + 幂等键 + 「接下来怎么办」）。**
**下一步 = Phase 5（time travel：checkpoint 历史 / 分叉视图 / 审计事件）。**

### Phase 4 做完什么

**家长重复点一次「提交」，系统不会把今天记两遍。** 四条具体后果被修掉：

1. **重复提交不再让 `refusal_counts` 翻倍。** 以前 `/api/feedbacks` 没有幂等保护，
   提交两次 → sessions 里两条 refused → 计数到 2 → **当天误触冷却阈值** ——
   那条活动被踢出明天的候选，而界面上看不出为什么。
   现在**两层**保证：落库那半有唯一键（数据的性质），
   checkpoint 那半按输入指纹直接返回上次结果（不重跑图、不重付 LLM）。
2. **「接下来怎么办」第一次有了可判定的答案**：`next_action` =
   `continue` / `adjust` / `pause` / `escalate`，**纯 Python 判**，
   随 `/api/feedbacks` 的响应一起给前端。
3. **家长的原话有两个入口，现在两个都被红旗扫过。** 以前只有「其他观察」框
   （走观察链）被扫，逐条活动的补充描述**从来没被扫过** ——
   "他玩着玩着就去咬自己的袖子"这句话压根没进过 `redflag.scan`。
4. **`/api/card` 读已生效的那一版计划**，不再自己再排一次。
   以前两条路径各自 `pack_day`，结果**当前恰好一致** —— 那是巧合，不是保证。

图级验收 **23 → 29 组**，HTTP 层 **10 → 12 组**，两个自检 2026-10-21 实跑全绿。
细节见第四节之五。

### 🎯 Phase 5 开工前必须知道的四件事

1. **`run_id` = 「一天的执行」的身份**，它同时是 checkpoint 的命名空间
   （thread_id）与落库行的幂等键的一半。反馈链的 thread 是
   `run:{child}:{date}`；分叉出来的 thread 是 `run:{child}:{date}#fork@{ckpt}`，
   run_id 跟着变，**分叉的行不会撞上真实历史**。
   → Phase 5 做分叉时，**run_id 必须跟着 thread_id 走**，
   别用 `store.day_run_id()`（那永远是那一天的正牌身份）。
2. **三条唯一索引就是 Phase 4 的幂等承诺**，Phase 5 的分叉必须尊重它们：
   `sessions(child,date,activity,run_id,seq)` /
   `skill_state(child,skill,run_id,from,to)` / `daily_summary(child,date,run_id)`。
   ★ 迁移时老库里**合法存在重复行**（同一天两条 refused、同一天两条日结），
   所以先回填再建索引 —— 见第四节之五「本轮踩到的坑」第 1 条。
3. **分叉必须重新过安全闸门与必要审批**（技术方案 §5.5 明确要求）。
   现在的三条链各自有各自的 thread，分叉时**每一条都要分叉**，
   别只分叉观察链 —— 反馈链的 run 是**要落库**的。
4. **`workflow_audit_events` 表还没有**（Phase 5 的活）。
   现成的来源是 `StateSnapshot.metadata`（`step` / `source`），实测见第二节 ⑧。
   三条链的 thread 前缀不同，审计事件的 `thread_id` 应当原样存（不要截断前缀）——
   否则三张图的事件会混在一起，而它们本来就是三件不同的事。

### ⚠️ Phase 5 开工前要知道的两件环境事

1. **`server.py` 在仓库根目录，不在 `asd_agent/`。**
   `cd asd_agent && ../.venv/bin/python -m uvicorn server:app` 会
   `Could not import module "server"`。PROGRESS.md 入口那节的
   `-c "import uvicorn,server"` 写法（带 `PYTHONPATH=.:./asd_agent`）才是对的。
2. **PG 容器是 `--rm` 的，跑完就没了。**
   每次开工第一步先 `docker ps -a --filter name=ganyu_pg`，没有就按
   PROGRESS.md 那条 `docker run` 重起（**端口绑 127.0.0.1**）。

Phase 3 做完什么（回顾）：

**"系统排的计划"第一次要家长点头。** 三条具体后果被修掉：

1. **`plans` 表从 4 列变成 8 列**（`status` / `version` / `approved_by` /
   `approved_at`）。改之前它只有 `id / child_id / plan_date / created_at / payload`，
   也就是说"最后写入的那条就是当前生效的计划" —— 而"生效"这件事没有任何记录。
2. **新增 ⚡ 审批 interrupt**（`approve` / `edit` / `reject`），
   挂在**实质性变更**上：连续两天输入相同 → 第二天**不打断家长**。
3. **`/api/card` 的卡片缓存搬到了新表 `day_cards`**。它原来写的是 `plans` 表，
   两种东西挤在一张表上的后果见第四节之四。

图级验收 **14 → 23 组**，HTTP 层 **9 → 10 组**，两个自检 2026-10-21 实跑全绿。
细节见第四节之四。

### 🎯 Phase 4 开工前必须知道的四件事（已全部兑现，存档备查）

1. **`store.plan_paused(child_id)` 是「暂停计划生成」的唯一口径。**
   Phase 3 的 `generate_plan` **不自己判断暂停**，它读 `state["paused"]`，
   而那个值由图入口 `plan_graph.plan_inputs()` 调 `store.plan_paused()` 算好注入。
   Phase 4 的 `next_action` 同样只读注入值（`run_inputs` 里算一次）。
   **别在节点体里再写一遍 `exists urgent ...`** —— 那就是第二处定义。
2. **反馈链是第三张独立的图**（`graph/run_graph.py`），thread 前缀 `run:`。
   它与观察图、计划图**都不共用 thread**：同 thread 的 invoke 会接着上一次跑
   （第二节 ③ 的实测），而反馈链的 run 是**要落库**的。
3. **反馈链的 `apply_transitions` 是它唯一写业务表的节点**，排在全部计算之后。
   `record_execution` / `analyze_outcome` / `next_action` 一个字节都不写 ——
   这条用「跑一遍去掉写库节点的图」验（selfcheck [28]），
   而不是用 trace 顺序糊弄过去。
4. **实质性变更判据在 `planner.is_substantive_change()`**（纯函数，零 token）。
   它比的是**指纹**（`planner.plan_fingerprint`），不是逐字段 diff ——
   逐字段比会把每次重新生成都变成一次打断，于是"只在真变化时审批"
   这条判据被它自己吃掉了。

Phase 2 做完什么（回顾）：

**红旗从"响应里飘一次"变成长期记录。** 三条具体后果被修掉：

1. `escalate → END` → **`escalate → publish → END`**。原来 urgent 那天的
   家长原话**从不进 `observations` 表**，最该被记下来的那天什么都没留下。
2. 新表 `red_flags` + 唯一索引幂等 + 全部严重度都落 + 逐 code 的转诊指引。
3. 「暂停计划生成」从一句响应里的字符串变成**可持久查询**的判据
   `store.plan_paused(child_id)`。

图级验收 **14 组**，HTTP 层 **9 组**。细节见第四节之三。

### 🎯 Phase 3 开工前必须知道的四件事（存档备查）

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

新增两处**以前没人守**的东西：`asd_agent/http_selfcheck.py`（**现在 12 组**，
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
**Phase 4 改了 `sessions` / `skill_state` / `daily_summary` 三张表（加列 + 加唯一索引），
同样必须重启后端。**

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

## 四之四、Phase 3 ✅ 已完成（2026-10-21 提交）：计划链 + 审批 interrupt

### 图长什么样（接完之后）

```
START → select_goal → retrieve_activities → generate_plan → validate_plan
                → substantive_change? ──是──→ ⚡ approve_plan ─┐
                                                       approve ─┤
                                                       reject ──┤→ freeze_plan
                                                              │
                                          否 ──────────────────┘
                                                                  ↓
                                                          publish_plan → END
```

**★ 计划链是独立的一张图**（`graph/plan_graph.py`），不是观察图后面接一段。
技术方案 §5.1 画的是同一张图，落地时拆开了，三个理由：

1. **触发时机不同。** 观察链由「家长写了一段话」触发，
   计划链由「家长打开今天 / 重新生成」触发。焊在一起的话，
   家长写一句"今天他在超市捂耳朵"就会重排一次计划 ——
   而他要的是被记下来。
2. **thread_id 会打架。** 同 thread 的 `invoke` 会**接着上一次跑**
   （第二节 ③ 的实测）。两张图共用 `{child}:{date}` 的话，
   家长写一段话就会把计划链的 run 续下去。
   计划链用 `plan:` 前缀，与草稿的 `draft:` 一样是**命名空间隔离**。
3. **写库节点会变成两个。** 观察图的 `publish` 写 observations + red_flags，
   计划链的 `publish_plan` 写 plans。两条链各有各的末尾写库节点，
   各自满足「所有 interrupt 之后才落库」。

### ★ 实质性变更判据（纯 Python，`planner.is_substantive_change`）

判据只有一条，但它是**整个 Phase 3 的守门人**：
「这份草稿与当前生效的那一版有没有实质区别」。

比的是**指纹**（`planner.plan_fingerprint`），不是逐字段 diff。指纹只收
**会影响执行**的字段：目标技能集合、每条活动的材料集合 / 难度 /
**提示阶梯起点级别** / 时长。

★ 为什么用指纹而不是逐字段比：
  审批要回答的是"有没有**实质区别**"，而家长点"重新生成"之后
  卡片文案（why_this / 家长提醒）几乎必然不同 —— 那不是实质区别，
  那是同一次安排的两种说法。逐字段比会把每次重新生成都变成一次打断，
  于是"只在真变化时审批"这条判据**被它自己吃掉了**。

★ 判据的六条（任一命中即算变更）：
  · 目标技能集合不同
  · 任一活动的材料集合不同
  · 任一活动的难度不同
  · 任一活动的**提示阶梯起点级别**不同
    （只取 `prompt_hierarchy[0]`，即"从哪一级开始提示"；
      阶梯中间那级变了不影响执行）
  · `safety_gate` 在本轮触发过
  · 计划跨度数 > 1 天

★ 改难度 / 改材料 / 改起点提示级别，各自都能触发 ——
  自检 [18] 组实测：`daily_minutes` 从 30 砍到 12 →
  `['不再练：命名物体、因果理解、涂鸦、踢球', '去掉活动：fin-008、gro-006']`。

### 必须做的迁移（已做）

| 表 | 补什么 | 为什么 |
|---|---|---|
| `plans` | `status` / `version` / `approved_by` / `approved_at` | 之前"最后写入即生效"，而"生效"没有任何记录 |
| `sessions` | `plan_id` / `plan_version` | 练习记录要能回答"孩子做的是哪一版安排下的" |
| `day_cards`（**新表**） | `/api/card` 的当天卡片缓存 | 见下 |

★ **迁移不是 `CREATE TABLE IF NOT EXISTS` 就完事的**：
  那个语句**不会给已存在的表加列**。实测报
  `UndefinedColumn: column "status" does not exist` ——
  在**新库**上全绿，在**用过的库**上直接炸，而那正是开发中天天在用的那个。
  → 补列走 `_migrate()` 里的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（幂等）。

★ 而且**索引不能建在补列之前**：
  建表块里引用 `status` 列的 `CREATE INDEX` 会在老库上先炸一次。
  补列与建索引必须都在 `_migrate()` 里，且**索引跟着补列走**。

### ★ `/api/card` 的卡片缓存必须搬出 `plans`

原来卡片缓存写的是 `plans` 表。Phase 3 起 `plans` 是**版本化的计划**，
两者挤在一张表上的后果不是"字段多了几个"：

**`active_plan()` 会把一张卡片当成一份已批准的计划端出去。**
卡片 payload 里没有 fingerprint，下一次审批就拿它去比指纹，
判出来是"什么都没变" —— 于是**任何真变化都不会再打断家长**。

这是一个静默到极点的 bug：界面上一切正常，只是审批从此不再生效。
（实测：`/api/card` 跑完之后 `active_plan()` 返回的就是卡片 payload。）

→ 新表 `day_cards`，`/api/card` 改写 `save_card` / `latest_card`。

### 审批 interrupt 的两阶段协议

| 路由 | 说明 |
|---|---|
| `POST /api/plan/run` | 跑计划链。**毫秒级，不用 job**（全程纯 Python，零 LLM）。返回 `phase=review\|done` |
| `POST /api/plan/review` | 回答审批。没有待审批时**必须 409** |
| `GET /api/plan/status` | 纯读 checkpoint，刷新后靠它发现"还有个计划等你批" |
| `GET /api/plan/versions` | 某一天的全部版本（家长改过几次、哪一版被拒了） |

★ **409 守卫与 `/api/observe/resume` 完全同款**：
  `Command(resume=...)` 打在没有 pending interrupt 的 thread 上，
  LangGraph 会把它当成**新输入从头跑一遍整条图** ——
  plans 表里多出一行版本。**不报错、不抛异常，就是数据多了一版。**
  断言在 `http_selfcheck.py` 的 [10] 组。

★ **非法 action → 400 而不是 422**：
  FastAPI 那层**不重复声明** `Literal`（抄一份就是两处口径，
  抄的那份迟早落后）。语义由图里 `response_schema=PlanReview` 判，
  报错信息直接用图自己的话："Input should be 'approve', 'edit' or 'reject'"。

★ **路由注册顺序很要紧**：
  `/api/plan/status` 与 `/api/plan/{child_id}` 形状相同，
  FastAPI 按**声明顺序**匹配。顺序错了，
  `GET /api/plan/status` 会被当成 `child_id="status"` 命中那条老路由 ——
  症状是 400「还没有这个孩子的画像」，而那条路由根本没读过 profile。
  **报错信息完全指不到真正的原因。**

### ★ `edit` 的处理与循环守卫

`edit` = 家长说"改一下"。这一版的需求是**改**不是**批**，
所以 `edit` 回到 `retrieve_activities` 再走一遍，然后**不再弹第二次审批**
（`already_reviewed`）—— 家长已经表达过不满了，再问一次
"这次可以吗"是在追问而不是在尊重。

守卫有**两条**，缺一不可：
1. `already_reviewed` —— edit 之后不打断（上面那条）
2. `edit_loop` + `MAX_EDIT_LOOPS` —— **兜底**。
   有了 (1) 之后这条在正常路径上够不着，
   但它守的是"将来有人把 (1) 去掉"之后的世界。
   而那个环在界面上看不出来（图每轮都"成功"了），
   唯一的表现是 CPU 一直在转、plans 表一直不写。

★ 两条守卫都留在代码里，是因为删掉 (2) 的人会说"(1) 已经挡住了" ——
  而这正是删它的理由：(2) 守的正是 (1) 被删掉之后的世界。
  守卫写在**节点体**里（`approve_plan`）而不是条件边里 ——
  条件边拿到的是 state 的只读视图，在那里赋值看着生效、下一轮读到的还是旧值。

### `planner.plan_week` 的去留（搬过来的未验证项，已定）

结论：**保留它，但只作为 CLI demo 的周计划**；HTTP 的计划链走 `pack_day`。

- 它**不是死代码** —— `loop.py` 的五日闭环 demo 每天用它排一周
  （`--no-llm` 时用 `greedy_fallback_plan`）。那是"多 Agent 协作"展示点之一。
- **接进 `generate_plan` 是错的**：图里的计划链是**一天**的，它是**一周**的。
  塞进去的话 `validate_plan` 会**每天都判成有变更**（七天内容各不相同），
  于是每天打断家长一次 —— 正是技术方案 §10.3 要防的"家长负担"。
- LANGGRAPH.md 第四节之三真正要防的是"**看起来发生过而其实没有**"：
  读代码的人以为"LLM 出了个计划"，而 HTTP 上根本没有 LLM 参与。
  → 边界写在 `planner.plan_week` 的 docstring 里。

### 本轮踩到的坑（已修，勿回退）

1. **`current_fingerprint` 忘了带进图 → 第二天输入完全相同也弹审批。**
   理由还写着"第一次生成计划"（因为 `active_plan` 返回 None）。
   实测：补上之后，第二天 `needs_review=False`，一路跑到 `publish_plan`。
   ★ 指纹取的是**存进去时算好的那份**，不是现在从活动库重算 ——
   活动库改了一条活动的难度，会让一份半年前的计划凭空变成"有变更"，
   而那次变更根本没人提过。
2. **暂停的那份计划落成了 `status='active'`** ——
   `/api/card` 读到它会把"今天什么都不做"当成一份合法计划端出去。
   → 暂停落 `status='paused'`，`latest_plan` 读不到它。
3. **`PlanRequest` 是 dataclass、`PreferenceSet` 是 Pydantic**，
   放进 state 后 msgpack 打出
   `Deserializing unregistered type ... will be blocked in a future version`。
   也就是说**今天能读、明天可能直接 raise**，而 checkpoint 的寿命比代码长。
   → 新增 `activities.request_to_dict` / `request_from_dict`，
   state 存**纯 dict**（与 observation 存 dict 同一条理由）。
4. **`edit` 的计数写在条件边里 → 守卫形同虚设**（见上）。
5. **路由顺序 → `/api/plan/status` 被参数路由吃掉**（见上）。
6. **自检里 `o4` 可能未绑定**：`for` 循环第一次就 break 时变量不存在。
   写断言前先想清楚循环体可能跑几次（PROGRESS.md 阶段 4 坑 2 的同款）。

## 四之五、Phase 4 ✅ 已完成（2026-10-21 提交）：反馈链 + 幂等键

### 图长什么样（接完之后，三条链各一张图）

```
观察链  {child}:{date}          record_intake → extract → validate → ⚡ clarify
                               → safety_gate → escalate? → publish → END
计划链  plan:{child}:{date}     select_goal → retrieve → generate → validate
                               → ⚡ approve_plan → freeze_plan → publish_plan → END
反馈链  run:{child}:{date}      record_execution → analyze_outcome → next_action
                               → apply_transitions → END
```

★ 反馈链**没有 interrupt**，也没有分支 —— 它是一条直线。
  看着不像 LangGraph，但它的价值在别处：
  · 四个节点各自可单测（三个纯函数 + 一次 LLM）
  · checkpoint 落在「一次执行」这个粒度上（run_id）
  · 落库集中在最后一个节点，幂等键于是**只有一处**

### ★★ 幂等的两层，缺一不可

| 层 | 保证什么 | 靠什么 |
|---|---|---|
| 落库那半 | 同一次执行重跑一次**不多写一行**；家长更正**覆盖**当天那一条 | 三个唯一索引 + `DO UPDATE` |
| checkpoint 那半 | 同一天提交**一模一样**的内容 → **不重跑图、不重付 LLM** | `feedback.feedback_digest`（sha256） |

★ 只做第 1 层的话，**数据是对的，但家长要为一次重复提交再等 20-60 秒**
  （图里那一次 LLM 是日结）。只做第 2 层的话，一次「换个入口」就绕过去了。
  → 两层都要。

★ 指纹**不含**时间戳之类会变的东西，也不受**提交顺序**影响 ——
  否则这个守卫形同虚设，而它失效的方式是「静默地多付一次 LLM」。

### `run_id` 是什么

`run_id = day_run_id(child, date) = "run:{child}:{date}"` ——
它**同时是** checkpoint 的命名空间（thread_id）与落库行的幂等键的一半。
让这两者是同一个字符串，于是"重跑一次同一天"在两处都是同一个身份：
checkpoint 里认得出这是同一次执行，落库时也认得出，于是幂等键自然成立。

分叉出来的 thread 是 `run:{child}:{date}#fork@{checkpoint_id}`，
run_id 跟着变 → **分叉的行不会撞上真实历史**（方案 §5.5）。

### 三条唯一索引

| 表 | 键 | 为什么键里有 `seq` / `from_state` |
|---|---|---|
| `sessions` | `(child_id, session_date, activity_id, run_id, seq)` | 同一天同一条活动**本来就可以有两行**（家长点了一次"部分做到"，补充描述里又说"后来又不肯了"）。`source` 区分"家长点的"与"桥接补记的"，`seq` 区分同一次执行里的第几条 |
| `skill_state` | `(child_id, skill_item, run_id, from_state, to_state)` | 一天最多一条同向转移（`apply_transitions` 每次只算一条） |
| `daily_summary` | `(child_id, on_date, run_id)` | 一天一份日结 |

★ `sessions` 写用 **DO UPDATE 而不是 DO NOTHING**：
  家长会**更正**（"我昨天记错了，其实他做了"）。
  DO NOTHING 会让更正静默失效 —— 界面上看不出，数据是错的。
  改的是「家长对当天事实的陈述」，而**审计线**（`skill_state`）仍然纯追加、绝不重写。

### `next_action`：四个取值，判据全在 Python

| 取值 | 判据 | 来源 |
|---|---|---|
| `escalate` | **今天新出的** urgent 红旗 | `redflag.scan`（逐条 note 扫出来的） |
| `pause` | 有**未关闭的** urgent | **只调 `store.plan_paused`** —— 全项目唯一口径 |
| `adjust` | 有**回退型**状态转移 | `statemachine.propose_transition` |
| `continue` | 其余 | — |

★ 为什么 `escalate` 排在 `pause` 前面：两者都来自红旗，但家长要做的**动作**不同 ——
  escalate 是"今天刚出的事，现在就处理"（转诊指引），
  pause 是"之前那件事还没处理完，先别排新计划"。合成一个动作，家长就不知道该做什么。

★ **抗拒不判成 `adjust`**：一条没配合是常态，
  而它造成的后果（那条活动进冷却、明天不再推）由检索器负责 ——
  那是"候选变了"，不是"难度要调"。两件事混成一个标签的话，
  `adjust` 就会天天响，而它响的时候反而没人当回事。

### 顺带修掉的三个洞

1. **家长的原话有两个入口，以前只扫了一个。**
   「其他观察」框（走观察链）被 `redflag.scan` 扫过，
   而**逐条活动的补充描述从来没被扫过** ——
   "他玩着玩着就去咬自己的袖子"这句话压根没进过 `redflag.scan`。
   不是扫了没命中，是**压根没扫**。现在两个入口都扫（`source_kind='note'`），
   命中 urgent 时 `next_action=escalate` 且计划生成被暂停。
2. **`ActivityFeedback.outcome` 现在在构造时就拒非法值。**
   它以前是裸 str，非法值能一路走到 INSERT —— 于是库里躺着一行
   `outcome='succeess'`，而统计只认四个词，那行**安静地不存在**。
3. **`loop.py` 的 live 路径在第一次出卡片时就崩。**
   `run_day` 里有一句 `out_prev = []` 把调用方传进来的历史**直接扔掉**，
   下一行又把它当 `ObservationOutcome` 用 → `AttributeError`。
   12 个离线回归一个都不碰 live 路径，所以没人发现。
   （live 路径本轮**未实跑** —— 没有 replay fixture；改的是类型，不是行为。）

### 卡片读已生效的那一版计划（遗留项 3）

以前 `/api/card` 与计划链是两条独立路径，各自 `pack_day`，
结果**当前恰好一致** —— 那是巧合，不是结构上保证一致。
将来任何一个改了装箱规则，两条路就会分叉，
而**家长看到的卡片与被批准的计划不是同一份**。

现在：有生效的计划时，卡片就是那几条（一个不多一个不少）；
没有生效的计划时回落到原来的装箱。

★ 教练 Agent 的 `ease`/`swap`/`drop` **不生效**（`coach_today(keep_focus=True)`）：
  诊断照跑（家长看得见"为什么"），但不许换掉家长批准的那条 ——
  家长点了批准，系统就再替他改一次，那批准就没有意义了。

★ **缓存的键现在包含计划版本**：以前只看 `profile_id`，
  于是"家长批准了第 2 版"不会让旧卡片作废 —— 界面上还是第 1 版那几条，
  而家长批的是第 2 版。**批准就白批了。**

### 本轮踩到的坑（已修，勿回退）

1. **★ 老库里合法存在的重复行会让 `CREATE UNIQUE INDEX` 直接失败。**
   同一天两条 refused（家长点的 + 桥接补记的）、同一天两条日结（重跑留下的）、
   同一对 `from→to` 两次（回退→升级→再回退）—— **全都是合法的**。
   不先错开就建索引 → 索引创建失败 → **失败在迁移函数里** →
   整个库连不上，症状是"服务起不来"，而真正的原因一个字都没提。
   → 先回填：`sessions` 按 `(child,date,activity)` 分组编号 `seq`；
   `skill_state` / `daily_summary` 编成 `legacy-<id>`。
   **已用模拟老库实测**（把新列 drop 掉、插重复行、再连一次）。
2. **`seq` 必须在 `observe.refusal_sessions` 里定，不能在调用方定。**
   初版在 `plan_feedbacks` 里重新编号 —— 两个地方编号就会有一处漏，
   而漏的表现是"两条抗拒撞成一条"（DO UPDATE 覆盖掉一条），
   **抗拒计数随之少一次** —— 而抗拒计数是冷却期的依据。
3. **`sessions` 的 `source`/`seq` 传不到 `observe.log_refusals` 那个老函数里。**
   那个函数只收 `(conn, child_id, hits)`，而幂等键需要 `seq` 与 `run_id`。
   → 删掉它，写库搬进 `feedback.write_feedbacks`。
4. **`plan_from_state` 还原出来的 `refusal_hits` 是 dict，不是对象。**
   `write_feedbacks` 里 `getattr(h, "seq", 1)` 对 dict 恒取默认值 1 →
   两条桥接记录撞成一行。→ 两种形态都认（`isinstance(h, dict)`）。
5. **`llm_calls` 必须在跑完之后算。**
   以前在跑之前算，于是命中 checkpoint 守卫时（实际一次 LLM 都没付）
   前端仍显示"刚才那几秒在抽事件"。
6. **断言写反了：** 「重复提交不多写一行」这条判据**正确时界面上看不出任何东西**
   （数字一样）。所以必须有断言守着，否则有人把唯一索引去掉，
   只会表现为"家长偶尔会发现抗拒次数不对" —— 而那个现象太容易被当成"记错了"。

## 五、待确认 / 遗留

**Phase 4 的三条遗留已全部解决**（2026-10-21）：

| # | 问题 | 怎么解决的 |
|---|---|---|
| 1 | `/api/feedbacks` **无幂等保护**，重复提交让 `refusal_counts` 翻倍误触冷却阈值 2 | 落库那半有唯一键（数据的性质）+ checkpoint 那半按输入指纹直接返回（不重跑不重付 LLM）。见第四节之五 |
| 2 | `apply_transitions` 幂等键要加 `run_id` 隔离，否则**重跑一次多一行转移**（静默的状态抖动） | `run_id` = 「一天的执行」的身份（= thread_id），进 `skill_state` / `daily_summary` / `sessions` 三条唯一键 |
| 3 | `/api/card` 与计划链两条独立路径，Phase 4 合并（让卡片**读**已生效计划） | 卡片按生效计划的 activity_ids 出；教练的 `ease`/`swap` 不再生效；缓存的键加上计划版本 |

**Phase 5 开工时该优先解决的三条**（按建议顺序）：

| # | 问题 | 位置 |
|---|---|---|
| 1 | `workflow_audit_events` 表还没有 —— 红旗状态变更只有 `status_changed_at` 记时点，没有"谁在什么时候从什么改成了什么" | 下面「Phase 2 期间新发现的」 |
| 2 | 分叉视图还没有 —— 「昨天那条其实做了」→ 改掉再看今天怎么变，现在只能手工换 thread_id 跑 | 第四节之五「Phase 5 开工前必须知道的四件事」 |
| 3 | 身份鉴权还是没有 —— 13 条路由零用户概念，`child_id` 硬编码，`allow_origins=["*"]` | 技术方案 §9（Phase 6） |

**Phase 3 期间新发现的：**

- ~~**计划链与 `/api/card` 是两条独立的路径。**~~ **Phase 4 已合并**：
  卡片按生效计划的 activity_ids 出（一个不多一个不少），
  教练的 `ease`/`swap` 不再生效（家长批准过的那一版不许被换掉），
  缓存的键加上了计划版本（批了第 2 版 → 旧卡片自动作废）。
- ~~**计划链目前不产生卡片。**~~ **Phase 4 已解决**：
  卡片仍然由 `/api/card` 那条 LLM 路径出，但它**读**已生效的计划 ——
  于是"家长批准的是哪几条"与"家长看到的是哪几条"是同一份。
- **暂停仍然没有过期策略。** urgent 没人关就永远停着计划生成。
  Phase 3 **没有**重新审视它 —— 结论是**维持现状**：
  这是正确的临床默认（fail-closed），横幅让它可见可操作，
  而 Phase 3 引入的 `plans.status='paused'` 让"停着"这件事**有了一条记录**
  （以前是静默的）。要加过期策略仍只改 `store.plan_paused` 那一条 SQL。
- **审批只发生在"计划变了"时，而计划变了多半来自家长自己的反馈。**
  也就是说：家长填完昨天的反馈 → 今天的计划变了 → 要家长再点一次批准。
  听起来绕，但它保证了"每天点一次"不会发生 ——
  变的只有**实质**部分（技能/材料/难度/提示级别），文案变了不算。

**Phase 2 期间新发现的：**

- **暂停没有过期策略。** urgent 没人关就永远停着计划生成。
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
