# 活动库方法级溯源

`activity_data.py` 中 50 条活动的 `method` 标签（`NDBI` / `ESDM` / `ABA` / `PRT` / `RBT`）
所依据的公开出处。**溯源粒度是"方法"，不是"活动逐条"** —— 具体活动是家庭场景下的改编，
不是任何一本书的复制。面试时按这个口径说，见文末。

---

## 0. 国内锚点：家庭干预是国家指南明确背书的方向

**《孤独症谱系障碍儿童干预康复指南（2026年版）》**
国家卫生健康委办公厅，国卫办妇幼函〔2026〕302号，2026-09-11 印发

- 通知原文：https://www.nhc.gov.cn/fys/c100078/202609/486028fcef6e477c8ec1b8c88ce62c2d.shtml
- 官方解读：https://www.nhc.gov.cn/fys/c100077/202609/4aa20e2af33d440880b485adcb8b33c3.shtml

与本项目直接相关的三点：

1. **8 项基本原则中包含"家庭参与"**，干预内容 9 大方面中**"家庭干预"单列一节**。
2. 附件"常用于孤独症儿童的行为干预策略"列举：应用行为分析（ABA）、结构化教学（TEACCH）、
   自然情境教学（NAT）、作业治疗（OT）、语言行为教学法（VB）、**早期介入丹佛模式（ESDM）**、
   游戏疗法、社交故事、PECS、AAC、感统训练 —— 本活动库的方法标签几乎逐字对应这份清单。
3. 配套文件链（家庭干预被视为第一选择的依据）：
   - 《0~6岁儿童孤独症筛查干预服务规范（试行）》国家卫健委，2022
   - 《孤独症儿童家庭实施早期干预指南》中国残疾人康复协会团体标准，2022
   - 《孤独症谱系障碍婴幼儿家庭实施早期干预专家共识》，2021
   - 《儿童孤独症诊疗康复指南》卫生部，2010（ABA / DTT 的官方表述）

**开场话术**：国家指南把家庭干预列为基本原则和独立干预内容，指南同时指出家长缺的是
可执行的方法。本系统就是把指南附录里的行为干预策略，落成家长每天 30 分钟能做的动作。

---

## 1. NDBI（自然发展行为干预）— 库内 33 条

| 文献 | 期刊 | DOI / ID |
|---|---|---|
| Sandbank M, Bottema-Beutel K, Crowley S, et al. Project AIM: Autism intervention meta-analysis for studies of young children. | Psychological Bulletin, 2020, 146(1):1–29 | `10.1037/bul0000215` / PMID 31763860 / PMC8783568 |
| Sandbank M, Bottema-Beutel K, Crowley S, et al. Naturalistic Developmental Behavioral Interventions: Empirically Validated Treatments for ASD. | J Autism Dev Disord, 2015 | `10.1007/s10803-015-2407-8` |
| Crank JE, Sandbank M, Dunham K, et al. Understanding the effects of NDBI: A Project AIM meta-analysis. | J Autism Dev Disord, 2020, 50(5):1713–1727 | `10.1007/s10803-020-04528-3` |
| Sandbank M, et al. Project AIM: updated systematic review and secondary analysis. | BMJ, 2023, 383:e076733 | `10.1136/bmj-2023-076733` |

原始数据（全部效应量、纳入研究表）：**https://osf.io/kr2cd**

⚠️ 引用陷阱：二级资料常把 2020 NDBI 论文标成 JADD 50(9):2953–2979，
该卷期页码实际属于 Fuller & Kaiser 的社交沟通 meta 分析。正确为 **50(5):1713–1727**。

---

## 2. ESDM（早期介入丹佛模式）— 库内 45 条

| 文献 | 期刊 | DOI / ID |
|---|---|---|
| Dawson G, Rogers S, Munson J, et al. Randomized, controlled trial of an intervention for toddlers with autism: the Early Start Denver Model. | Pediatrics, 2010, 125(1):e17–e23 | `10.1542/peds.2009-0958` / PMID 19948568 / NCT00090415 |
| Rogers SJ, et al. A multisite randomized controlled two-phase trial of ESDM compared to treatment as usual. | JAACAP, 2019, 58(9):853–865 | `10.1016/j.jaac.2019.01.004` / PMID 30768394 |
| Rogers SJ, Estes A, et al. Effects of a brief ESDM-based parent intervention on toddlers at risk for ASD: an RCT. | JAACAP, 2012, 51(10):1052–1065 | `10.1016/j.jaac.2012.08.003` |
| Rogers SJ, Dawson G. Early Start Denver Model for Young Children with Autism. | Guilford Press, 2010 | 手册，非试验 |

⚠️ **第一作者是 Dawson，不是 Rogers** —— 2010 年那篇 RCT 常被误引。
证据强度备注：Ireland WhatWorks 将 Dawson 2010 评为 Level 2+，
但因缺 ITT 分析未纳入总体评级。

ESDM 手法与数据字段的对应关系（`method_rationale` 中"ESDM 核心"的条目）：
控制距离调节难度、把物品放在看得见够不着处诱发主动表达 —— 二者均为公开教学元素。

---

## 3. ABA（应用行为分析）— 库内 13 条

| 文献 | 说明 | 标识 |
|---|---|---|
| BACB/CASP. *ABA Treatment of Autism Spectrum Disorder: Practice Guidelines for Healthcare Funders and Managers*（2012 初版，2014 第 2 版，2020 转 CASP） | 实践指南 | https://casproviders.org/asd-guidelines |
| Rodgers M, et al. Intensive behavioural interventions based on ABA for young children with autism: international collaborative IPD meta-analysis. | Autism, 2021, 25(4):1137–1153 | `10.1177/1362361320985680` |
| Rodgers M, et al.（NIHR HTA 单册）. Early intensive behavioural interventions for autistic children: systematic review and cost-effectiveness. | 24(35):1–306, 2020 | `10.3310/hta24350` |
| Steinbrenner JR, et al. *Evidence-Based Practices for Children, Youth, and Young Adults With Autism*（NCAEP） | EBP 清单 | 北卡州立，2020 |
| Hyman SE, et al. AAP 临床报告 | 儿科立场 | `10.1542/peds.2020-003752` |
| Lovaas OI. Symptom improvement in autistic children… | J Consult Clin Psychol, 1987, 55(1):3–9 | 奠基性研究 |

HTA 的限定结论（面试可能追问）：证据在症状严重度、语言、安置上均有限；
IQ 提升约 10–14 分（1–2 年）；成本效益依赖长期获益的证实。

---

## 4. PRT（关键反应训练）— 库内 9 条

| 文献 | 说明 | 标识 |
|---|---|---|
| A Systematic Review of Linguistic and Verbal Behavior Outcomes of Pivotal Response Treatment | JADD, 2020 | `10.1007/s10803-019-04307-3` |
| Lerman DC, Garfinkle TB, Crittenden JA. 原始 PRT 报告 | JABA | — |
| Kohler MD, Gill CT. pivotal response training 概念表述 | — | — |

PRT 五类策略中，与本活动库直接相关的是：**动机（改变活动本身以引发兴趣，而非重复已会行为）**
和 **自然情境强化**。数据中体现为 `method_rationale` 提到"把目标行为嵌进孩子本来就想做的事"。

---

## 5. ⚠️ RBT — 待处理，标签建议撤除

库内仅 2 条：`gro-001 推球走直线`、`gro-003 气球不落地`。

**术语歧义**：
- 中文语境：RBT = 强化疗法（Reinforcement Therapy）= **离散单元教学法 DTT 的别称**，
  高结构化回合式训练，源于 Lovaas。
- 英文语境：RBT = Registered Behavior Technician（注册行为技术员，BACB 认证岗位）。

**问题**：这两条活动是自然游戏化的，没有回合结构与固定强化协议 ——
按上述任一定义都不该标 RBT，它们属于 NDBI / NAT。

**建议**：撤除 RBT 标签，其想表达的"强化"内容已由 `prompt_hierarchy`
（提示层级与渐隐）、`error_correction`（纠错协议）字段承载，那是更准确的位置。

撤除后的方法分布：ESDM=45、NDBI=33、ABA=13、PRT=9。

---

## 6. 证据强度的边界（主动声明，不要等被问）

Project AIM（2020）的结论链条：

1. 不设质量限制 → behavioral / developmental / NDBI 均显著
2. 限定 RCT → 仅 developmental 与 NDBI 保持显著
3. 限定 RCT **且** 无检测偏倚的结局 → **无任何干预类型达到显著**

**本项目的口径**：我验证的不是疗效，是"能否把干预动作稳定落成家长可执行的日程"。
疗效问题交给 RCT 与临床医生；本项目做的是最后一公里的工程化。
方法有效性有文献支持；我的贡献是把文献里的教学元素转成家长可执行、
可追溯、可动态调整的日常动作，**不是重新验证疗效**。
