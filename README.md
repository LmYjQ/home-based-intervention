# 家长干预教练 Agent

> 把医生的评估报告，转成家长每天在家能执行的干预计划，并根据每天的反馈动态调整。

面向**自闭症谱系障碍（ASD）儿童**的家庭干预场景。目标用户是拿到评估报告、却不知道"明天具体该做什么"的家长。

**不是**训练 App，**不是**评估工具，**不是**疗效验证研究。这个项目只做一件事：把文献里的教学元素，稳定地落成家长能执行、可追溯、会随反馈调整的日常动作。

四个技术展示点：**多 Agent 协作 · RAG 检索 · 长期记忆 · 状态机**。

---

## 快速开始

不需要 API Key，**0.1 秒**跑完整条闭环：

```bash
python3 -m venv .venv
./.venv/bin/pip install openai pydantic python-dotenv
# 国内网络慢的话加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple

cd asd_agent
../.venv/bin/python loop.py --no-llm --script hardcoded --seed-history
```

最后会看到这一屏 —— 家长一段反馈，真的改变了第二天的候选：

```
  Day1 候选前 5：gro-007  fin-001  gro-006  fin-004  fin-008
  Day2 候选前 5：fin-004  fin-008  cog-001  lan-002  lan-004
  ★ 因家长 Day1 的反馈退出：gro-007
      gro-007 —— 已连续抗拒 2 次，进入冷却期
  因其他硬过滤退出（与反馈无关）：fin-001、gro-006
      fin-001 —— 最近 3 天刚练过，避免重复
  新进候选：cog-001、lan-002、lan-004
```

**这条链上没有任何一步需要重新跑排期 LLM。** 反馈 → 结构化事件 → 抗拒计数 / 场景分布 → 检索硬过滤，全是 Python。

各模块的自检（每个文件都能独立当脚本跑）：

```bash
cd asd_agent
for f in vocab activities capabilities statemachine store redflag coach evaluator observe; do
  ../.venv/bin/python $f.py; done
```

---

## 目录结构

```
ganyu/
├── README.md                  ← 你在这
├── demand.md                  需求原文（面试自述的素材）
├── plan.md                    实现计划（设计决策的出处）
├── c3bcca26-*.png             干预模式分类参考图（活动库方法标签的依据）
└── asd_agent/
    ├── PROGRESS.md            ← 进度交接、实测结果、踩过的坑（换 session 动手前先读）
    ├── GRILLING.md            面试拷问清单（"答不上来的项 = 现场会被问倒的项"）
    ├── sources.md             方法级溯源（文献出处 / 国内指南锚点 / 证据边界）
    ├── results/2026-10-16/    verify_schema 的 A/B 实验产物
    │
    ├── vocab.py               五张受控词表 + 五能区/技能细项/状态机/材料类别
    ├── schema.py              Observation 的 Pydantic 定义，Literal 约束
    ├── verify_schema.py       12 条真实家长语句的 A/B 回归集（不要改）
    ├── llm.py                 唯一的 LLM 出口：chat_json + LLMTrace + live/replay/capture
    ├── activities.py          Activity/Step/Material + 喜好模型 + retrieve() 检索
    ├── activity_data.py       50 条活动数据
    ├── capabilities.py        SkillAssessment / ParentConstraints / AbilityProfile
    ├── profile.py             报告 → 能力画像（含红旗前置、回指校验、降级）
    ├── statemachine.py        五态机（纯函数、零 token、可重放）
    ├── store.py               7 张表，skill_state 只追加 + 三个必需查询
    ├── redflag.py             独立规则扫描，11 条规则 × 三档严重度
    ├── planner.py             规划 Agent：候选由 Python 检索，LLM 只编排
    ├── coach.py               教练 Agent：诊断（可校验推理链）→ 家长卡片
    ├── evaluator.py           评估 Agent：只给证据，判定留给状态机
    ├── observe.py             家长一段话 → 多条结构化事件 + 红旗 + 降级
    └── loop.py                ★ 唯一 demo 入口：端到端链路 + 三日闭环
```

---

## 架构

```
  评估报告
      │
      ▼
  profile.py ──────► AbilityProfile（能力画像 + 可信度）
      │                        │
      │                        ▼
      │                 statemachine.py ──► 五态状态转移
      │                        │          未开始→入门→练习中→已掌握→泛化中
      │                        ▼
      │                   store.py（长期记忆，7 张表，只追加）
      │                        │
      │                        ▼
      │                 activities.py retrieve()
      │                   （硬过滤 + 8 项加权打分，纯 Python，毫秒级）
      │                        │
      ▼                        ▼
  planner.py ──────────► 一周计划（LLM 只编排 + 写 why_this）
                                   │
                                   ▼
                            coach.py ──► 家长卡片（今天做什么 / 为什么 / 怎么退）
                                   │
                            家长执行 + 反馈（一段自然语言）
                                   │
                                   ▼
                            observe.py ──► Observation 列表 + 红旗
                                   │
                                   ▼
                     抗拒计数 / 场景分布 ──► 明天候选真的变了
```

### 三个 Agent 各自的职责边界

| Agent | LLM 负责 | Python 负责 |
|---|---|---|
| `planner.py` | 编排候选、写 `why_this` 理由 | 候选检索、时长预算校验、材料可行性 |
| `coach.py` | 诊断（带可校验推理链）、生成家长卡片 | 换哪条活动、`keep/ease/drop/swap` 的落地、只收紧不放松 |
| `evaluator.py` | 只给证据（这次算不算泛化） | 状态转移判定、升级闸门、回退阈值 |

---

## 核心设计原则

> **计数交给 Python，语义交给 LLM。**

模型只回答语义判断（"今天这次成功算不算泛化"、"这活动踩雷了吗"）；凡是可判定的（时长够不够、换哪条活动、该不该升级、材料齐不齐）全部交给 Python 硬规则。

这条原则落在四处：planner 的时长与预算校验、coach 的 `impact` 落地、evaluator 的状态转移、profile 的整体可信度。**它带来的直接好处是：7B 的短板不会污染系统正确性。**

配套的两条：

- **不引入 ReAct / tool-calling 循环。** 7B 在 SiliconFlow 上多轮工具调用不可靠，单次 5–15s，面试现场必崩。所有 Agent 都是「一次受限解码调用 + Python 确定性编排」。
- **每条 LLM 路径都有降级路径 + 延迟可见。** 降级路径不是残次品 —— 活动定义里本来就带 `steps` / `success_criteria` / `prompt_hierarchy` / `error_correction`，所以"没有教练 Agent"的卡片是完整的，缺的只是退路没讲透。**降级路径的质量决定模型挂掉时它还是不是个有用的工具。**

---

## 关键安全性质

这三条是可以直接演示、也可以直接讲的硬性质，不是口号。

**① 喜好只能改变"同等适配度"活动之间的排序，永远不能推翻教学判断。**

家长可以配置孩子的喜好（喜欢小汽车、喜欢洗澡场景）。检索打分里喜好权重只有 0.10，而状态匹配是 0.30。更关键的是：喜好**只在硬过滤通过之后**参与打分，**不能复活被剔除的活动**。

```
喜欢 soc-009 但前置 soc-008 未完成 → passed=False
原因：前置活动尚未完成：soc-008      ← 喜好救不了被硬过滤的活动
```

而"不喜欢"走扣分**而不硬剔除** —— 家长需要知道"我听到了你的偏好，以及为什么这条还是出现了"。

**② 校验规则的严重度必须和它的失败代价匹配。**

软问题静默钳制，硬问题直接报错。

```
推断条目却给了高 confidence  →  静默钳制到 0.35（不是 raise）
skill_item 不在词表里        →  硬报错（真编造，必须拦）
```

早期版本对第一条用了 `raise`，实测 7B 几乎必然踩中（它倾向给所有条目同样的 0.95），连续两次不合规就整份画像作废。**为了一个软数据质量问题丢掉全部数据，量级完全搞反了。** 该规则真正要保证的只是"推断条目不得充当升级依据"，钳制完全满足，而失败率归零。

**③ 升级闸门先看画像可信度，再看状态证据 —— 数据一样，轨迹可以完全不同。**

`statemachine.py` 里，画像可信度不过线就**直接否决所有升级**（`profile_confidence < upgrade_min_confidence` → 返回 `None`），可信度过了才轮到各状态自己的证据阈值。

同一份 35 条练习记录，只改画像可信度：

```
0.85 → 5 次转移（entry→practicing→mastered→generalizing→mastered）
0.30 → 0 次转移
```

闸门拦下时，系统会明确说出来，而不是硬凑一个结论：

```
⏸ 共同注意：卡在未开始，本可到已掌握 —— 画像可信度不足，升级闸门拦下了（数据不够，不猜）
```

**这是面试时最值得演示的一屏：数据一字不差，轨迹完全不同。**

---

## 检索是怎么打分的

`activities.py` 的 `retrieve()` 是纯 Python 的硬过滤 + 加权打分，毫秒级，**不碰 LLM**。

硬过滤（不通过就直接出局，并带可读的剔除原因）：

- 材料是否家里有（三层匹配：相等 → 剥量词双向子串 → 字符重叠兜底）
- 时间预算（10/15/20/30/45 分钟全部返回非空候选）
- 前置活动是否已完成
- 冷却期（连续抗拒 2 次进冷却）
- 最近排期去重

加权打分（合计 1.00）：

```
0.30 状态匹配      ← 最高项，不可动摇
0.15 技能差距      0.14 前置就绪      0.10 喜好命中      0.10 新鲜度
0.08 材料契合      0.08 方法契合      0.05 场景贴合
```

材料匹配**刻意让它漏而不是猜**：`水杯` vs `杯子` 只共有"杯"字，不算匹配。材料匹配错了会让家长白准备一堆没用的东西，比少匹配一条活动糟糕得多。

---

## 跑真实模型

需要 `.env`：

```bash
LLM_PROVIDER=siliconflow
SILICONFLOW_API_KEY=sk-...
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
LLM_TIMEOUT_S=60
```

默认模型 `Qwen/Qwen2.5-7B-Instruct`，OpenAI 兼容接口。

**超时分两条，这是有意的**：

| 链路 | 超时 | 来自 |
|---|---|---|
| 每日链路（教练卡片、行为观察） | **20s 硬上限** | `LLM_TIMEOUT_S` 会被 `min(..., 20.0)` 强制压到 20s |
| 报告抽取（一次性建档） | **90s** | `LLM_EXTRACT_TIMEOUT_S`，实测 7B 出 ~1100 token 要 45–50s |

用同一个上限去卡两条链路，结果不是"慢一点"，而是整份画像直接拿不到。所以这两个数是分开的。

```bash
cd asd_agent
../.venv/bin/python llm.py                    # 健康检查
../.venv/bin/python profile.py                # 报告抽取（约 45s）
../.venv/bin/python loop.py --trace           # 端到端，会打 LLM trace
```

**面试现场务必用 `DEMO_MODE=replay`** —— 3 天闭环约 16 次 LLM 调用、5–7 分钟，replay 全流程 <1 秒、零网络依赖。

`llm.py` 内建三种模式：

| 模式 | 行为 |
|---|---|
| `live` | 真调模型（默认） |
| `replay` | 只读本地文件；**文件不存在时直接失败**，不静默回落到 live |
| `capture` | 真调模型并把原始响应逐字落盘，供之后转成 replay |

> 现场兜底 fixture 必须用 `DEMO_MODE=capture` 从真实 live 输出逐字落盘。手工编的 fixture 是这类 demo 最常见的"看起来能跑、一换数据就崩"的来源。

---

## 面试时怎么讲

**一分钟版本：**

> 我做了一个家庭干预智能体，解决的是评估报告到日常执行之间的断层问题。传统 App 只能提供静态内容，但每个孩子的短板不同、进展不同，需要动态调整。我设计了一个多 Agent 系统：规划 Agent 基于能力画像从干预方法库中检索和组合每日活动，教练 Agent 根据家长反馈动态调整难度，评估 Agent 定期判断是否该升级目标。整套系统跑在 7B 小模型上，靠"计数交给 Python、语义交给 LLM"这条边界保证正确性。

**技术深挖的四个点：**

1. **为什么用多 Agent 而不是单体** —— 规划和执行反馈是不同心智模式，拆开各自优化，也便于加新能力（比如后续加"机构沟通 Agent"帮家长整理和老师沟通的要点）。
2. **RAG 怎么做** —— 自建 50 条结构化活动库（基于 NDBI/ESDM/ABA 公开教学元素），本地结构化检索不做 embedding，候选由 Python 检索、LLM 只编排。
3. **长期记忆怎么管** —— 7 张表、`skill_state` 只追加不覆盖，状态机纯函数零 token 可重放，进展查询全是纯 SQL。
4. **怎么评估效果** —— 用"家长执行率"和"能力域状态变化"作为指标。

**演示顺序建议：**

1. `loop.py --no-llm --script hardcoded --seed-history` → 闭环 + 升级闸门对照（0.1s）
2. `loop.py --no-llm --script hardcoded` → 三天链路，7 阶段全展开（检索 → 教练 → 执行 → 写入 → 观察 → 评估 → 明天的候选）
3. `activities.py --validate` → 50 条活动的自检（引用了存在但放错位置的 ID 靠交叉验证才发现）
4. 挑一个没做用户访谈就承认、说明观察来源（见 `GRILLING.md`）

---

## 方法溯源与证据边界

活动库的 `method` 标签（NDBI / ESDM / ABA / PRT）依据公开出处，详见 [`asd_agent/sources.md`](asd_agent/sources.md)。**溯源粒度是"方法"，不是"活动逐条"** —— 具体活动是家庭场景下的改编，不是任何一本书的复制。

**主动声明边界，别等被问。**

`Project AIM(2020)` 三级收紧后**无任何干预类型达到统计显著**。本项目的口径是：

> **我验证的不是疗效，是"能否把干预动作稳定落成家长可执行的日程"。**

方法有效性有文献支持；我的贡献是把文献里的教学元素转成家长可执行、可追溯、可动态调整的日常动作，**不是重新验证疗效**。

国内锚点：国家卫健委《孤独症谱系障碍儿童干预康复指南（2026年版）》把家庭参与列为 8 项基本原则之一，9 大干预内容中"家庭干预"单列一节。**需求不是猜的，是有政策锚点的。**

---

## 已知局限（主动写明）

- **`activities.py` 的冷却判定是 30 天滚动计数，没有冷却期结束条件** —— 抗拒两次，这条活动一个月不再出现。对 demo 是好事（变化可见），对产品是缺陷。
- **三个 Agent 的真实模型路径未做全量实测** —— 约束解码下的实际合规率、`CoachCard` 会不会截断、每步真实耗时，都还需要现场跑一遍确认。
- **没有真实用户访谈** —— 需求依据是政策锚点 + 自身观察，不是用户研究。
- **本项目无任何真实患者数据**，全部是合成 fixture。

---

## 下一步

代码层面阶段 4（闭环接线）已完成，`loop.py` 覆盖全部技术叙事。剩下的是包装层：

- `server.py`（~300）—— 同步路由 + JobManager + SSE。**所有 LLM 路径一律 job_id + SSE，绝不让前端同步等。**
- `static/index.html`（~450）—— 原生 JS 三 tab + Agent Trace 面板，**不引任何 CDN**（现场无网会白屏）。
- fixtures 回放 + 演示脚本。

**降级方案**：时间紧就砍掉这一层，直接交 CLI demo。技术叙事一点不少，0.1s 跑通。

---

## 文档索引

| 文件 | 内容 |
|---|---|
| [`asd_agent/PROGRESS.md`](asd_agent/PROGRESS.md) | 进度交接：实际做到哪、验证结果、**踩过的坑（勿回退）** |
| [`asd_agent/GRILLING.md`](asd_agent/GRILLING.md) | 面试拷问清单 |
| [`asd_agent/sources.md`](asd_agent/sources.md) | 方法级溯源 + 证据边界 |
| [`plan.md`](plan.md) | 实现计划（设计决策的出处） |
| [`demand.md`](demand.md) | 需求原文 |
