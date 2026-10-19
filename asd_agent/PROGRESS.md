# 家长干预教练 Agent — 进度交接

面试 demo。完整设计方案见 `~/.claude/plans/app-mvp-jolly-kazoo.md`（已批准）。
本文件记录**实际做到哪、验证结果如何、下一步做什么**，供换 session 继续。

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
| `pipeline_demo.py` | 300 | 端到端 7 阶段 CLI，`--no-llm` 全离线 0.0s |
| `sources.md` | 130 | **方法级溯源**（用户补充）：文献出处、指南锚点、证据边界 |

### 阶段 4 已完成 ✅（闭环接线）
| 文件 | 行数 | 说明 |
|---|---|---|
| `observe.py` | ~480 | 家长一段话 → 多条结构化事件 + 红旗 + 回指校验 + 降级 |
| `loop.py` | ~450 | 三日闭环 CLI，含 `assert_loop_closed()` 交叉核对 |
| `vocab.py`（改） | +60 | FBA 功能分类 `CONSEQUENCE_FUNCTION` / `function_of()` |
| `store.py`（改） | +15 | session 补 note 列；红旗计数改 `COUNT(DISTINCT observed_at)` |
| `coach.py`（改） | +130 | 接上 ABC；功能一致性校验；功能驱动的加强规则 |


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
| `evaluator.py` | 428 | 1 次 | 日评估 + 转移建议（**判定留给 Python**） |
| `pipeline_demo.py` | 300 | 全链路 | 7 阶段 CLI，带 `--no-llm` 全离线路径 |

**核心边界（本项目最值得讲的一条工程原则）：**
模型只回答语义判断（"今天这次成功算不算泛化"、"这活动踩雷了吗"），
凡是可判定的（时长够不够、换哪条活动、该不该升级）全部交给 Python。
同一��原则落在四处：planner 的时长与预算校验、coach 的 impact 落地、
evaluator 的 state 转移、profile 的整体可信度。

### 阶段 3 实测结果（**全部离线，0.0s，9 个入口全绿**）

```bash
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator; do
  ../.venv/bin/python $f.py; done
../.venv/bin/python pipeline_demo.py --no-llm      # ★ 7 阶段端到端
```

`pipeline_demo.py --no-llm` 的输出直接印证了两条设计：

```
[6] 状态机 → 转移判定
    没有转移：画像可信度 0.10 低于升级闸门 0.5，全部升级被拦下
[7] 记忆库 → 周回顾（纯 SQL）
    最近 30 天：⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足（数据不够，不猜）
```

**⚠️ 未实测：三个 Agent 的真实模型路径。**
权限分类器以"数据外发"为由拦下了 `pipeline_demo.py`（无 LLM）的调用。
待验三件事，**阶段 4 之前必须补**：
1. 四个 schema 在 `json_schema` 约束解码下的**实际合规率**
2. **`CoachCard` 会不会截断** —— 设计文档自标"最可能现场翻车的单点"
   （6 步 + prompt_ladder + 4 段退路，max_tokens=3000）。
   `llm.py` 已有 `finish_reason=="length"` → ×1.5 自动重试 → 仍失败则落兜底卡片
3. **每步真实耗时** —— 教练两次串行，按 7B 实测速度预计 **30–45s**，
   超出 PROGRESS 原先假设的 20s。已定：阶段 4 一律走 job_id + SSE，不在前端同步等。

自己跑：
```
! ../.venv/bin/python pipeline_demo.py --trace
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

### 验证（11 个离线入口全绿，0.0s）
```bash
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator observe; do
  ../.venv/bin/python $f.py; done
../.venv/bin/python pipeline_demo.py --no-llm
../.venv/bin/python loop.py --no-llm --script hardcoded      # ★ 闭环演示
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

### 下一步：阶段 5-6（API 与前端包装层）

**代码层面阶段 4 已完成** —— 闭环由 `loop.py` 承担，不再是 API 层面的事。
剩下的是包装层：

- **`server.py`**（~300）。同步路由 + JobManager + SSE。
  **所有 LLM 路径一律 job_id + SSE，绝不让前端同步等。**
  建议路由（`observe.py` 已经把接口切好了）：
  ```
  POST /api/profile           → {job_id}      建档 ~45s，必须走 job
  GET  /api/jobs/{id}/stream                 SSE 逐步进度
  POST /api/observations     → Observation + redflag_report
  GET  /api/plan/{cid}        → 候选 + 每条的"为什么推/为什么不推"
  POST /api/sessions/{cid}/coach → 家长卡片
  GET  /api/progress/{cid}    → what_progressed（纯 SQL，秒回）
  ```
  红旗按 `Depends(check_redflag)` 挂在 `/api/observations` 上，
  不存在"某条路径忘了调"。加 pytest：C11 原文打这个 endpoint 必须 200 且
  `redflag_triggered=true`。
- **`static/index.html`**（~450）。原生 JS 三 tab + Agent Trace 面板，
  **不引任何 CDN**（现场无网会白屏）。
- **fixtures 回放 + 演示脚本。** 回放文件**必须用 `DEMO_MODE=capture`
  从真实 live 输出逐字落盘**，手工编的 fixture 是这类 demo 最常见的
  "看起来能跑一换数据就崩"的来源。

**开工前必做**：实测阶段 3/4 新增路径的 LLM 行为（见「阶段 4 未实测」三条）。

**降级方案**：时间紧就砍掉这一层，直接交 CLI demo ——
`pipeline_demo.py`（单日链路）+ `loop.py`（三日闭环）。
技术叙事（多 Agent、结构化检索、长期记忆、状态机、闭环）一点不少，现在已能跑通。
