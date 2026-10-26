# RDI 家长干预建议智能体：知识库 Schema、RAG 检索流程与 LangGraph 节点设计

> **文档类型**：技术设计 / MVP 实施方案\
> **目标读者**：产品、后端、AI 应用工程与专业审核人员\
> **知识基础**：Steven E. Gutstein，《The RDI Book》（2009）\
> **版本**：v1.0\
> **状态**：设计稿，需结合实际 PDF 解析结果、授权条件与专业审核迭代

------------------------------------------------------------------------

## 1. 项目目标与边界

### 1.1 项目目标

构建一个面向家长的、以《The RDI Book》为主要知识来源的 RAG
智能体。家长可以用日常语言描述孩子在家庭、幼儿园或社区中的表现，系统经过必要追问、证据检索和安全检查后，提供**有来源、可观察、可反馈、可持续调整**的家庭互动建议。

系统不是"输入一个问题，向量检索几段文本，再让模型自由发挥"的问答机器人，而是一个闭环流程：

1.  理解家长描述的事实和目标。
2.  判断当前信息是否足够；不足时优先提出少量、高价值的追问。
3.  从书本证据库检索相关内容，并区分原书内容、应用转化和模型假设。
4.  判断证据是否足以支持回答；不足时降低结论强度或明确说明局限。
5.  生成低风险、可观察、可调整的家庭互动建议。
6.  收集实施反馈，更新儿童情境记录和待验证假设。
7.  后续对话结合历史，但不把历史假设当成事实。

### 1.2 设计原则

-   **证据可追溯**：每个重要的 RDI 观点尽可能关联章节和 PDF
    页码；页码必须来自实际解析结果，不得手工猜测。
-   **来源分层**：原书内容、人工整理、AI
    应用转化、外部研究、专业审核结论必须区分。
-   **观察优先于诊断**：记录"发生了什么"，不直接把某种行为归因于某一能力缺陷或诊断。
-   **个体化但不武断**：历史记录用于形成暂定假设和调整建议，不用于过早定型孩子。
-   **先安全、后优化**：儿童明显不适、持续升级或拒绝时，建议应以减轻压力、暂停或寻求支持为先。
-   **小步试验**：一次优先建议一个可执行的互动尝试，并给出观察指标和停止条件。
-   **保留专业边界**：系统提供教育性信息和家庭观察支持，不替代诊断、个体化临床评估或认证
    RDI 顾问的专业指导。
-   **尊重版权**：未经许可不向用户大段输出原书内容；默认提供摘要、短引文（如授权和适用规则允许）及页码定位。

### 1.3 原书的使用边界

《The RDI Book》出版于 2009
年。作者在前言中明确说明，该书不是完整的操作手册，也不能让读者仅凭阅读就获得成为
RDI 顾问所需的专业能力；作者建议家长寻求受过认证的 RDI 顾问指导。因此：

1.  本系统可以将本书作为 RDI 理论、术语、原则与作者案例的知识来源。
2.  不应把模型生成的具体活动步骤都描述成"书中要求"或"经原书验证的方法"。
3.  不应默认书中观点代表当前全部研究共识。
4.  面向真实家庭提供服务前，应补充当前指南、系统综述或可靠研究，并由合格专业人员审核。
5.  若没有找到足够的书本证据，应如实说明"当前知识库未找到足够依据"，不能让模型补写成原书观点。

------------------------------------------------------------------------

## 2. 系统总体架构

``` mermaid
flowchart TD
    A[家长自然语言输入] --> B[会话与儿童上下文加载]
    B --> C[事实/推断/目标抽取]
    C --> D{信息足够吗?}
    D -- 否 --> E[生成澄清问题]
    E --> A
    D -- 是 --> F[查询改写与概念扩展]
    F --> G[混合检索：全文 + 向量 + 元数据]
    G --> H[重排与父块扩展]
    H --> I[证据包构建]
    I --> J{证据充分且相关吗?}
    J -- 否 --> K[说明局限 / 继续追问 / 建议专业支持]
    J -- 是 --> L[生成候选建议]
    L --> M[安全与来源审查]
    M --> N[面向家长的回答]
    N --> O[反馈采集]
    O --> P[记忆候选提取与冲突检查]
    P --> Q[更新儿童情境记录 / 暂定假设]
    Q --> R[下一轮个体化检索]
```

### 2.1 建议的技术组件

  ----------------------------------------------------------------------------------------
  能力                    建议实现                说明
  ----------------------- ----------------------- ----------------------------------------
  PDF 解析                PyMuPDF，必要时 OCR     保留 PDF 页码、章节、标题、段落顺序

  数据模型                Python + Pydantic       对节点输入输出、证据包、建议卡进行校验

  数据库                  PostgreSQL / Supabase   元数据、儿童观察、反馈、审计记录

  向量检索                pgvector                语义召回

  关键词检索              PostgreSQL Full Text    保留术语和精确表达的召回能力
                          Search 或 BM25 服务     

  重排                    可替换的 reranker       提升前若干候选的相关性

  工作流                  LangGraph               支持条件分支、追问暂停、状态持久化

  模型接入                LLM Adapter             避免业务流程绑定单一模型供应商

  评测                    固定测试集 + 人工审核   测量引用准确率、建议支持度和安全性
  ----------------------------------------------------------------------------------------

如果项目已使用 Supabase，第一版可以继续用 Supabase PostgreSQL +
pgvector；LangGraph
负责流程编排。儿童个人数据表应与书本知识表分开，并使用严格的用户级访问控制。

------------------------------------------------------------------------

## 3. 知识库分层设计

不要把所有信息混在一个向量集合里。建议建立四类知识资产。

### 3.1 A 层：原书证据库 `book_evidence`

保存从书中直接解析、可定位的文本片段。原文应作为受版权约束的内部证据，不意味着可以原样输出给终端用户。

每条记录尽量只表达一个完整的概念、原则、论证步骤或案例；同时保留所属章节和父级段落，以免切块后失去语境。

### 3.2 B 层：结构化概念库 `rdi_concepts`

保存经人工整理的概念关系，例如：

-   概念名称与别名
-   原书中的定义或描述
-   相关章节
-   关联概念
-   原书是否明确使用该术语
-   适用边界和不确定性
-   审核状态

概念之间的关联可帮助检索扩展，但**不能把概念图谱中的关联自动当作因果关系**。

### 3.3 C 层：应用转化库 `application_patterns`

把原书原则转成家长容易理解的应用框架。每条记录必须明确标注为"应用转化"，不可伪装成原书直接给出的步骤。

例如：原书讨论逐步处理互动复杂度时，应用层可以提供"先观察孩子在当前场景中的舒适度，再考虑是否调整一个情境变量"的通用框架。具体活动是否适合某个孩子仍需结合上下文判断，不能直接从概念推导成固定处方。

### 3.4 D 层：外部证据库 `external_evidence`（建议第二阶段）

用于补充当前研究、指南、专业机构资料等。每条记录应包含出版年份、来源类型、链接或
DOI、研究设计、适用人群、局限和审核状态。

外部证据与原书观点发生差异时，不要静默合并。应保留来源、时间和分歧，由审核规则决定如何向家长表达。

------------------------------------------------------------------------

## 4. 数据库 Schema

以下为推荐的逻辑 Schema。字段可按 MVP 精简；示例中的
ID、页码和内容仅用于说明结构，不代表已经从 PDF 核验的实际数据。

### 4.1 `knowledge_sources`：来源表

  ----------------------------------------------------------------------------------------------------------------
  字段                    类型                    用途
  ----------------------- ----------------------- ----------------------------------------------------------------
  `id`                    UUID / text             主键

  `source_type`           enum                    `book`、`guideline`、`research`、`expert_review`

  `title`                 text                    来源标题

  `authors`               jsonb                   作者列表

  `publication_year`      int                     出版年份

  `edition`               text                    版本信息

  `language`              text                    语言

  `rights_status`         enum                    `unknown`、`internal_review_only`、`licensed`、`public_domain`
                                                  等

  `source_uri`            text                    内部文件位置或可信 URL

  `content_hash`          text                    版本校验

  `created_at`            timestamptz             创建时间
  ----------------------------------------------------------------------------------------------------------------

### 4.2 `book_sections`：章节层级表

  字段                  类型            用途
  --------------------- --------------- ------------------------------------------
  `id`                  UUID / text     主键
  `source_id`           FK              关联来源
  `parent_section_id`   FK nullable     上级章节
  `section_code`        text            稳定章节编码
  `title_original`      text            原文标题
  `title_zh`            text nullable   中文标题或译名
  `section_order`       int             章节顺序
  `pdf_page_start`      int             PDF 起始页（从 1 计数）
  `pdf_page_end`        int             PDF 结束页
  `print_page_start`    int nullable    印刷页码，能确认时填写
  `print_page_end`      int nullable    印刷页码
  `parse_status`        enum            `pending`、`parsed`、`reviewed`、`error`

**注意**：PDF 页码和书籍印刷页码是不同字段。若 PDF
页码与印刷页码存在偏移，不要用一个固定偏移量在所有页面上盲目换算。

### 4.3 `knowledge_chunks`：知识片段表

  -------------------------------------------------------------------------------------------------------------------------------
  字段                    类型                    用途
  ----------------------- ----------------------- -------------------------------------------------------------------------------
  `id`                    UUID / text             主键

  `source_id`             FK                      来源

  `section_id`            FK                      所属章节

  `parent_chunk_id`       FK nullable             父块 ID

  `chunk_index`           int                     章节内顺序

  `content_original`      text                    原始文本，受权限与版权控制

  `content_normalized`    text                    检索用规范化文本

  `summary_zh`            text nullable           中文摘要，不是原文

  `knowledge_type`        enum                    `theory`、`principle`、`case`、`goal`、`obstacle`、`limitation`、`definition`

  `concept_ids`           jsonb                   关联概念 ID

  `pdf_page_start`        int                     PDF 起始页

  `pdf_page_end`          int                     PDF 结束页

  `print_page_start`      int nullable            印刷页起始

  `print_page_end`        int nullable            印刷页结束

  `embedding`             vector                  向量

  `parse_confidence`      float                   解析质量估计

  `review_status`         enum                    `unreviewed`、`reviewed`、`needs_correction`

  `created_at`            timestamptz             创建时间

  `updated_at`            timestamptz             更新时间
  -------------------------------------------------------------------------------------------------------------------------------

建议采用"父块 +
子块"结构：子块负责精准召回，父块用于补充上下文。初始可测试约 300--600
中文字符等效长度、10--15%
重叠，但这只是实验起点；应根据标题、论证完整性、案例边界和检索评测调整，不要把固定长度当成规则。

### 4.4 `rdi_concepts`：概念表

  字段                      类型          用途
  ------------------------- ------------- ----------------------
  `id`                      UUID / text   主键
  `canonical_name`          text          规范概念名
  `aliases`                 jsonb         别名、常见中文表达
  `definition_summary`      text          经整理的概念摘要
  `source_chunk_ids`        jsonb         支持该概念的片段
  `is_explicit_in_source`   boolean       原书是否明确表达
  `related_concepts`        jsonb         相关概念，不等于因果
  `limitations`             jsonb         适用边界
  `review_status`           enum          审核状态
  `version`                 int           版本

### 4.5 `application_patterns`：应用转化表

  -------------------------------------------------------------------------------------------------
  字段                         类型                    用途
  ---------------------------- ----------------------- --------------------------------------------
  `id`                         UUID / text             主键

  `title`                      text                    应用框架标题

  `trigger_context`            jsonb                   可能适用的场景

  `prerequisites`              jsonb                   使用前需了解的信息

  `related_concept_ids`        jsonb                   关联概念

  `suggested_parent_actions`   jsonb                   可供模型考虑的家长行动选项

  `observation_targets`        jsonb                   需要观察的反应

  `stop_conditions`            jsonb                   暂停或降低要求的条件

  `contraindications`          jsonb                   不宜直接使用的情形

  `source_chunk_ids`           jsonb                   依据的原书片段

  `application_type`           enum                    `editorial_adaptation`、`expert_reviewed`

  `reviewer_id`                text nullable           审核者

  `review_status`              enum                    `draft`、`reviewed`、`approved`、`retired`

  `version`                    int                     版本
  -------------------------------------------------------------------------------------------------

### 4.6 `child_profiles`：儿童稳定档案

只保存对后续建议确有帮助、相对稳定的信息。避免不必要地收集可识别信息。

  字段                      类型            用途
  ------------------------- --------------- --------------------------------
  `id`                      UUID            主键
  `user_id`                 UUID / text     家长账户或租户 ID
  `display_label`           text            家长自定义称呼，不要求真实姓名
  `age_band`                text nullable   年龄段，按产品需要收集
  `communication_profile`   jsonb           表达与理解方式
  `strengths_interests`     jsonb           兴趣与优势
  `support_preferences`     jsonb           已知有帮助的支持方式
  `family_goals`            jsonb           家长当前目标
  `known_contexts`          jsonb           家庭、幼儿园等场景概况
  `confirmed_facts`         jsonb           家长确认的稳定事实
  `updated_at`              timestamptz     更新时间

健康、诊断等敏感信息应遵循最小化原则；没有明确产品需求和适当保护措施时，不要默认收集。

### 4.7 `child_observations`：情境观察表

一条记录尽量只描述一次具体事件或一个清晰观察窗口。

  ----------------------------------------------------------------------------------------------------------
  字段                    类型                    用途
  ----------------------- ----------------------- ----------------------------------------------------------
  `id`                    UUID                    主键

  `user_id`               UUID / text             数据隔离

  `child_id`              UUID                    儿童档案

  `conversation_id`       text                    关联会话

  `observed_at`           timestamptz nullable    事件时间

  `setting`               text                    家庭、幼儿园、户外等

  `participants`          jsonb                   参与者及关系

  `antecedent`            text                    事情发生前的情境

  `observable_behavior`   text                    可观察行为，避免推断性标签

  `adult_response`        text                    成人做了什么

  `child_response`        text                    孩子随后如何反应

  `outcome`               text                    结果

  `source_kind`           enum                    `parent_report`、`teacher_report`、`professional_report`

  `confidence`            text                    信息完整度或可信程度说明

  `created_at`            timestamptz             创建时间
  ----------------------------------------------------------------------------------------------------------

### 4.8 `child_hypotheses`：待验证假设表

  ---------------------------------------------------------------------------------------------------------
  字段                              类型                    用途
  --------------------------------- ----------------------- -----------------------------------------------
  `id`                              UUID                    主键

  `child_id`                        UUID                    儿童

  `hypothesis_text`                 text                    暂定解释

  `supporting_observation_ids`      jsonb                   支持记录

  `contradicting_observation_ids`   jsonb                   反例

  `confidence_level`                enum                    `low`、`medium`、`high`，不能替代统计概率

  `scope_conditions`                jsonb                   假设成立的可能情境

  `status`                          enum                    `candidate`、`active`、`weakened`、`rejected`

  `last_reviewed_at`                timestamptz             最近复核时间

  `created_by`                      enum                    `model`、`parent`、`professional`
  ---------------------------------------------------------------------------------------------------------

模型生成的假设默认是
`candidate`，不能直接升级成已确认事实。对重要或敏感假设，应由家长确认或专业人员复核。

### 4.9 `intervention_trials`：家庭尝试与反馈表

  ------------------------------------------------------------------------------------------------------------------------------
  字段                     类型                    用途
  ------------------------ ----------------------- -----------------------------------------------------------------------------
  `id`                     UUID                    主键

  `child_id`               UUID                    儿童

  `conversation_id`        text                    关联会话

  `target`                 text                    本次想观察的目标

  `plan_json`              jsonb                   建议卡完整内容

  `source_evidence_ids`    jsonb                   证据 ID

  `application_type`       text                    原书摘要、AI 转化、专业审核等

  `planned_context`        jsonb                   计划尝试的场景

  `status`                 enum                    `proposed`、`accepted`、`attempted`、`not_attempted`、`stopped`、`reviewed`

  `parent_feedback`        text nullable           家长反馈

  `child_response`         jsonb nullable          观察到的反应

  `implementation_notes`   text nullable           实际执行情况

  `review_summary`         text nullable           后续总结

  `created_at`             timestamptz             创建时间

  `updated_at`             timestamptz             更新时间
  ------------------------------------------------------------------------------------------------------------------------------

### 4.10 `conversation_state` 与 `memory_events`

会话状态适合放在 LangGraph checkpointer
中；长期业务记忆应放在应用数据库中。二者用途不同：

-   **Checkpoint**：保存某一次工作流执行的中间状态、节点位置、待恢复的追问等。
-   **长期记忆**：跨会话复用的儿童档案、观察、假设和尝试结果。
-   **知识库**：书本或外部资料的相对稳定知识。

建议 `memory_events`
记录每次记忆变更的来源、字段、变更前后值、是否经家长确认和时间戳，以便审计、纠错和撤回。

------------------------------------------------------------------------

## 5. 知识处理与入库流程

### 5.1 解析流水线

1.  **登记来源**：记录书名、作者、版本、出版年份、文件哈希和版权状态。
2.  **抽取文本**：用 PyMuPDF 解析每页文本，保留 PDF 页码。
3.  **章节识别**：解析目录、标题、子标题、附录，形成层级树。
4.  **段落恢复**：修复跨页段落、断词、页眉页脚和换行。
5.  **OCR 补充**：仅对文本缺失或明显错误的页面进行 OCR。
6.  **章节边界校验**：检查标题、附录、表格、脚注和案例是否被错误拆分。
7.  **切分父子块**：按语义边界切分，而不是单纯按字符数切割。
8.  **生成摘要与标签**：分别生成摘要、知识类型、概念标签；保存生成模型和提示词版本。
9.  **人工抽查**：核对原文、页码、摘要是否忠实，尤其检查否定、条件、限制和案例结论。
10. **生成向量并入库**：记录 embedding 模型与版本。
11. **建立回归集**：用固定家长问题测试召回和引用。
12. **发布版本**：保留索引版本，支持回滚。

### 5.2 切块规则

-   标题和其下的解释内容应保持关联。
-   一个案例尽量保留"背景---互动---作者分析---结论"的完整结构。
-   如果一个段落依赖前文限定条件，应保留父块或相邻上下文。
-   不要把作者举例和作者的一般原则混为一条。
-   不要把"可能""通常""在特定条件下"等限定词删掉。
-   原文、中文摘要和模型生成的应用建议必须分字段存储。
-   PDF 页码必须是可复现的真实定位；若印刷页码无法确认，则置空，不猜测。

### 5.3 建议的知识类型枚举

``` python
from enum import StrEnum

class KnowledgeType(StrEnum):
    THEORY = "theory"
    PRINCIPLE = "principle"
    DEFINITION = "definition"
    CASE = "case"
    GOAL = "goal"
    OBSTACLE = "obstacle"
    LIMITATION = "limitation"
    AUTHOR_CAVEAT = "author_caveat"
    APPLICATION = "application"
    EXTERNAL_EVIDENCE = "external_evidence"
```

------------------------------------------------------------------------

## 6. RAG 检索流程

### 6.1 查询理解

对家长原始问题提取以下内容：

-   原始问题（必须保留）
-   观察到的事实
-   家长的担忧与目标
-   情境：地点、参与者、活动、发生频率、前后变化
-   已尝试的做法及结果
-   缺失的关键事实
-   可能相关的概念（仅作为检索线索，不是结论）

例如，家长说："孩子在幼儿园大家一起坐的时候就哭，老师单独陪着还好一点，怎么办？"

可以抽取：

``` json
{
  "observed_facts": [
    "集体活动时会哭",
    "老师单独陪伴时表现相对好一些"
  ],
  "parent_goal": "希望孩子更舒适地参与幼儿园活动",
  "context": {
    "setting": "幼儿园",
    "activity": "集体围坐活动"
  },
  "missing_information": [
    "是否存在噪声、拥挤或其他感官负担",
    "哭泣发生在活动开始前还是过程中",
    "成人陪伴时具体做了什么",
    "孩子是否能表达不舒服或请求离开"
  ],
  "retrieval_concepts": [
    "guided participation",
    "interaction complexity",
    "context differences"
  ],
  "diagnostic_conclusion": null
}
```

上例中的概念只是检索词候选，并不意味着已经确定孩子的问题原因。

### 6.2 何时追问，何时直接回答

不要为每个问题都发起长问卷。只有当缺失信息会实质改变建议、安全性或适用性时，才优先追问。

**优先追问的情况：**

-   家长描述过于简略，无法确定具体场景。
-   可能有疼痛、明显感官不适、强烈焦虑或其他需要优先处理的情况。
-   不清楚孩子是否能够接受成人靠近、提示或共同参与。
-   多种解释会导向明显不同的建议。
-   需要判断某个建议是否适合当前孩子，但关键前提未知。

**可以先给通用建议的情况：**

-   已有足够信息提出低风险的观察建议。
-   建议本身是记录情境、减少不必要压力、观察前后变化等。
-   回答明确说明局限，并邀请家长补充反馈。

每轮通常只问 1--3 个最关键的问题，避免让家长觉得在填评估量表。

### 6.3 混合检索

推荐执行以下步骤：

1.  原始问题语义检索。
2.  关键词全文检索，优先覆盖原书术语、章节名和关键短语。
3.  用已确认的情境元数据过滤不相关内容。
4.  用受控的概念别名扩展查询。
5.  合并候选并去重。
6.  reranker 重排。
7.  对命中的子块加载父块或相邻上下文。
8.  按来源类型和审核状态组装证据包。

不建议把所有儿童历史记录直接拼进向量检索文本。儿童观察是个体上下文，原书知识是一般知识，二者应分别检索后再合并。

### 6.4 检索伪代码

``` python
async def retrieve_evidence(query, child_context, filters):
    rewritten = await rewrite_query(
        original_query=query,
        confirmed_facts=child_context.confirmed_facts,
        recent_observations=child_context.recent_observations,
    )

    vector_hits = await vector_search(
        query=rewritten.semantic_query,
        filters=filters,
        top_k=30,
    )

    keyword_hits = await fulltext_search(
        query=rewritten.keyword_query,
        filters=filters,
        top_k=30,
    )

    candidates = deduplicate(vector_hits + keyword_hits)
    reranked = await rerank(query=query, candidates=candidates)
    selected = reranked[:8]

    expanded = await load_parent_context(selected)
    return build_evidence_bundle(
        original_query=query,
        rewritten_query=rewritten,
        chunks=expanded,
    )
```

上面的 `top_k`
只是初始配置，应通过评测调整。生产实现还需要超时、异常回退、日志脱敏和索引版本管理。

### 6.5 证据包 Schema

``` python
from pydantic import BaseModel, Field
from typing import Literal

class EvidenceItem(BaseModel):
    evidence_id: str
    source_type: Literal[
        "book", "external_research", "professional_review",
        "child_observation", "application_pattern"
    ]
    title: str
    section_title: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    print_page_start: int | None = None
    print_page_end: int | None = None
    summary: str
    source_excerpt: str | None = None
    relevance_reason: str
    limitations: list[str] = Field(default_factory=list)
    review_status: str
    may_quote_to_user: bool = False

class EvidenceBundle(BaseModel):
    original_question: str
    observed_facts: list[str]
    unresolved_questions: list[str]
    evidence: list[EvidenceItem]
    evidence_gaps: list[str]
    retrieval_version: str
```

`source_excerpt`
是否可以展示给用户，必须受版权策略控制。一般情况下，回答优先展示摘要、章节与页码，不输出大段原文。

### 6.6 证据门控（Evidence Gate）

回答生成前必须判断证据是否够用。

  ------------------------------------------------------------------------------------------------------------
  条件                                系统行为
  ----------------------------------- ------------------------------------------------------------------------
  有直接相关且定位准确的证据          可解释相关原则，并引用章节和页码

  只有间接相关证据                    明确说这是可能的参考，不声称原书直接回答了该情形

  检索结果互相矛盾                    呈现分歧或进入人工审核，不静默挑选

  没有找到可靠证据                    说明知识库暂未找到依据；可提供一般性的低风险观察建议并标明不是原书结论

  缺失信息会改变建议                  先追问

  涉及诊断、治疗决策或明显风险        不凭 RAG 自动下结论，转向专业评估建议
  ------------------------------------------------------------------------------------------------------------

示例门控输出：

``` json
{
  "evidence_status": "indirect",
  "supported_claims": [
    "不同情境下孩子的参与表现可能存在差异"
  ],
  "unsupported_claims": [
    "孩子哭泣一定是因为社交动机不足"
  ],
  "missing_information": [
    "噪声敏感性",
    "活动前后的情绪变化"
  ],
  "decision": "ask_follow_up_or_offer_low_risk_observation"
}
```

------------------------------------------------------------------------

## 7. 家长建议的输出契约

建议输出不是一段无结构的"育儿技巧"，而是一个可审查的建议卡。

### 7.1 建议卡 Schema

``` python
from pydantic import BaseModel, Field
from typing import Literal

class SourceCitation(BaseModel):
    source_id: str
    source_type: Literal[
        "book", "external_research", "professional_review",
        "application_adaptation"
    ]
    title: str
    chapter: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    claim_supported: str

class ParentAction(BaseModel):
    action: str
    context: str
    steps: list[str] = Field(default_factory=list)
    observe: list[str] = Field(default_factory=list)
    reduce_or_stop_if: list[str] = Field(default_factory=list)

class RecommendationCard(BaseModel):
    plan_id: str
    observed_facts: list[str]
    tentative_interpretations: list[str]
    explanation: str
    parent_actions: list[ParentAction]
    success_indicators: list[str]
    stop_conditions: list[str]
    source_citations: list[SourceCitation]
    application_label: Literal[
        "book_summary", "ai_adaptation", "expert_reviewed"
    ]
    uncertainty_notes: list[str]
    follow_up_questions: list[str]
```

### 7.2 面向家长的推荐回答结构

1.  **先复述观察**：只重述家长提供的现象，不给孩子贴标签。
2.  **说明目前能与不能判断的内容**：提出少量可能性，并标注为待验证假设。
3.  **解释相关原则**：用易懂语言说明 RDI
    相关概念；给出来源、章节和可核实页码。
4.  **给出一个小步尝试**：优先低风险、容易观察、可随时调整的行动。
5.  **说明观察指标**：记录孩子是否愿意参与、是否放松、是否主动表达、需要多少支持，以及哪些条件影响表现。
6.  **说明停止条件**：明显不适、持续升级或明确拒绝时，降低要求、暂停或调整环境，不强迫孩子完成。
7.  **安排反馈**：邀请家长描述实际尝试结果，以便下次调整。

### 7.3 示例回答（仅演示格式）

> 你观察到孩子在幼儿园集体围坐时会哭，而老师单独陪伴时相对好一些。这个差异值得记录，但目前还不能判断原因一定是社交能力、语言理解或感官问题。
>
> **可以先观察一件事：**集体活动开始前、刚开始和哭泣时，分别发生了什么？例如环境是否突然变吵、孩子是否知道接下来要做什么、成人是否要求他马上加入。先记录，不急着增加参与要求。
>
> 如果孩子愿意，可以在比较熟悉、压力较低的情境中进行短暂的共同活动，并观察他是否愿意继续、是否主动发起互动，以及什么时候开始不舒服。若不适明显加重，就暂停或降低要求，不把"坚持完成"当作成功。
>
> 这是一种基于情境观察的应用建议，不代表《The RDI
> Book》对你家孩子作出了具体判断。待核对原书相关章节后，系统应在此处附上实际检索到的章节与页码。
>
> 为了更有针对性地调整建议，我想先确认：孩子是在活动开始前就哭，还是其他小朋友靠近、老师发出要求后才哭？

此例不应被存成原书直接建议；它展示的是输出格式和不确定性表达。

------------------------------------------------------------------------

## 8. LangGraph 工作流设计

### 8.1 节点总览

建议第一版采用以下节点：

  ---------------------------------------------------------------------------------------------------------------------
  节点                         输入                       主要职责                             输出
  ---------------------------- -------------------------- ------------------------------------ ------------------------
  `load_context`               用户 ID、儿童 ID、会话状态 加载权限范围内的档案和近期观察       `child_context`

  `parse_parent_input`         家长原话                   抽取事实、目标、情境、已尝试行动     `case_summary`

  `check_sufficiency`          `case_summary`、风险信号   判断是否需要追问                     `sufficiency_decision`

  `generate_followup`          缺失信息                   生成少量关键问题                     `followup_questions`

  `rewrite_query`              原始问题、事实、概念候选   生成语义查询和关键词查询             `retrieval_queries`

  `retrieve_knowledge`         查询、过滤条件             混合检索书本和应用层知识             `retrieval_candidates`

  `rerank_and_expand`          候选片段                   重排、去重、加载父块                 `evidence_bundle`

  `evidence_gate`              证据包、事实摘要           检查支持度、证据缺口和来源质量       `evidence_decision`

  `build_recommendation`       事实、证据、历史记录       生成结构化建议卡                     `recommendation`

  `safety_review`              建议卡、风险信号           检查压力、强迫、越界诊断和来源错误   `review_result`

  `render_answer`              审核结果                   输出家长可理解的回答与引用           `final_answer`

  `capture_feedback`           家长反馈                   记录是否尝试、实施情况与观察结果     `trial_feedback`

  `propose_memory_updates`     新信息、旧记忆、反馈       提出记忆更新候选，识别冲突           `memory_candidates`

  `confirm_or_commit_memory`   候选更新、确认状态         对需要确认的信息征求确认后写入       `memory_update_result`
  ---------------------------------------------------------------------------------------------------------------------

### 8.2 Graph State

``` python
from typing import TypedDict, Any

class RDIState(TypedDict, total=False):
    user_id: str
    child_id: str
    conversation_id: str
    turn_id: str

    user_message: str
    child_context: dict[str, Any]
    case_summary: dict[str, Any]
    sufficiency_decision: dict[str, Any]
    followup_questions: list[str]

    retrieval_queries: dict[str, Any]
    retrieval_candidates: list[dict[str, Any]]
    evidence_bundle: dict[str, Any]
    evidence_decision: dict[str, Any]

    recommendation: dict[str, Any]
    review_result: dict[str, Any]
    final_answer: str

    feedback: dict[str, Any]
    memory_candidates: list[dict[str, Any]]
    memory_update_result: dict[str, Any]

    error_code: str | None
    trace_metadata: dict[str, Any]
```

State
中只放工作流所需的最小信息。不要将不必要的儿童敏感信息复制到日志或第三方追踪平台。对于较大的原文片段，可在
State 中只保留证据 ID，由节点按权限读取。

### 8.3 条件路由

``` python
def route_after_sufficiency(state: RDIState) -> str:
    decision = state.get("sufficiency_decision", {})
    if decision.get("requires_followup"):
        return "followup"
    return "retrieve"

def route_after_evidence_gate(state: RDIState) -> str:
    decision = state.get("evidence_decision", {})
    if decision.get("needs_followup"):
        return "followup"
    if decision.get("high_risk"):
        return "professional_support"
    if not decision.get("can_support_personalized_advice"):
        return "limited_answer"
    return "recommend"

def route_after_safety(state: RDIState) -> str:
    result = state.get("review_result", {})
    if result.get("blocked"):
        return "safe_fallback"
    if result.get("needs_human_review"):
        return "human_review"
    return "render"
```

### 8.4 Graph 组装示意

``` python
from langgraph.graph import StateGraph, START, END

builder = StateGraph(RDIState)

builder.add_node("load_context", load_context)
builder.add_node("parse_parent_input", parse_parent_input)
builder.add_node("check_sufficiency", check_sufficiency)
builder.add_node("generate_followup", generate_followup)
builder.add_node("rewrite_query", rewrite_query)
builder.add_node("retrieve_knowledge", retrieve_knowledge)
builder.add_node("rerank_and_expand", rerank_and_expand)
builder.add_node("evidence_gate", evidence_gate)
builder.add_node("build_recommendation", build_recommendation)
builder.add_node("safety_review", safety_review)
builder.add_node("render_answer", render_answer)
builder.add_node("capture_feedback", capture_feedback)
builder.add_node("propose_memory_updates", propose_memory_updates)
builder.add_node("confirm_or_commit_memory", confirm_or_commit_memory)

builder.add_edge(START, "load_context")
builder.add_edge("load_context", "parse_parent_input")
builder.add_edge("parse_parent_input", "check_sufficiency")

builder.add_conditional_edges(
    "check_sufficiency",
    route_after_sufficiency,
    {
        "followup": "generate_followup",
        "retrieve": "rewrite_query",
    },
)

builder.add_edge("generate_followup", END)
builder.add_edge("rewrite_query", "retrieve_knowledge")
builder.add_edge("retrieve_knowledge", "rerank_and_expand")
builder.add_edge("rerank_and_expand", "evidence_gate")

builder.add_conditional_edges(
    "evidence_gate",
    route_after_evidence_gate,
    {
        "followup": "generate_followup",
        "professional_support": "render_answer",
        "limited_answer": "render_answer",
        "recommend": "build_recommendation",
    },
)

builder.add_edge("build_recommendation", "safety_review")
builder.add_conditional_edges(
    "safety_review",
    route_after_safety,
    {
        "safe_fallback": "render_answer",
        "human_review": "render_answer",
        "render": "render_answer",
    },
)
builder.add_edge("render_answer", END)

# 用户提供反馈后，可以在单独的反馈入口继续执行：
# capture_feedback -> propose_memory_updates -> confirm_or_commit_memory
graph = builder.compile(checkpointer=checkpointer)
```

这是架构示意，不是未经修改即可运行的完整代码。真实实现需要补齐节点函数、异常处理、状态
schema、持久化配置、追问恢复逻辑和权限校验。建议将"回答主流程"和"反馈/记忆更新流程"分开入口，避免每次回答都无条件写长期记忆。

### 8.5 追问与 Checkpoint

当系统提出追问后，下一轮用户回复必须恢复原工作流上下文，而不是新建一个无关联的评估任务。

建议区分以下 ID：

-   `user_id`：账户/租户主体，用于权限隔离。
-   `child_id`：儿童档案 ID。
-   `conversation_id`：业务会话 ID。
-   `thread_id`：LangGraph checkpoint
    的线程标识，通常映射到一个可恢复的工作流会话。
-   `turn_id`：单轮用户输入 ID。
-   `plan_id`：一张建议卡的业务 ID。
-   `observation_id`：一条观察记录 ID。
-   `trial_id`：一次家庭尝试 ID。
-   `checkpoint_id`：LangGraph 某个状态快照的标识，由 checkpointer
    管理。

不要把 `checkpoint_id` 当成儿童 ID 或业务会话 ID。Checkpoint
保存执行状态，业务数据库保存可查询、可审计的长期记录。

对于同一儿童的不同会话，是否复用 `thread_id`
取决于产品设计。更容易维护的方式通常是：每个业务会话使用稳定的
`thread_id`；跨会话个体化通过 `child_id`
加载长期档案与观察，而不是把所有历史对话无限塞进一个 Thread。

------------------------------------------------------------------------

## 9. 长期记忆设计与更新规则

### 9.1 四层记忆

  --------------------------------------------------------------------------------------------------------------------------------------------
  层级              内容                                       示例                                     更新原则
  ----------------- ------------------------------------------ ---------------------------------------- --------------------------------------
  L1 稳定档案       沟通方式、兴趣、优势、常见环境、家长目标   "孩子喜欢绘本"                           家长确认或多次稳定观察后更新

  L2 情境观察       时间、地点、参与者、行为、成人回应、结果   "集体围坐时哭，单独陪伴时较平稳"         尽量保留具体情境，不抽象成标签

  L3 待验证假设     对重复模式的暂定解释                       "熟悉成人在场时可能更容易参与"           同时记录支持证据、反例和适用条件

  L4 干预尝试       做了什么、孩子反应、家长感受、下一步       "尝试缩短共同活动时长后，孩子愿意继续"   区分未尝试、已尝试、结果不明确和停止
  --------------------------------------------------------------------------------------------------------------------------------------------

### 9.2 每次对话后的记忆更新

1.  抽取新增事实，保留其来源（家长、老师、专业人员或模型推断）。
2.  与已有观察进行去重和冲突检测。
3.  判断是否为长期有用的信息；不要把每个临时情绪或单次行为都保存为稳定档案。
4.  对模型推断创建"候选假设"，而不是直接写入确认事实。
5.  对影响后续建议的重要变更向家长确认。
6.  记录记忆变更事件、来源会话、时间和版本。
7.  在下一次回答中只取与当前问题相关的记忆，避免将全部历史无差别拼接进
    Prompt。
8.  允许家长查看、更正和删除儿童记录，并说明删除范围和保留政策。

### 9.3 记忆更新候选格式

``` json
{
  "memory_candidate_id": "candidate_uuid",
  "child_id": "child_uuid",
  "memory_type": "context_observation",
  "content": "家长报告：孩子在幼儿园集体围坐时容易哭；老师单独陪伴时相对好一些。",
  "source": {
    "conversation_id": "conversation_uuid",
    "turn_id": "turn_uuid",
    "reported_by": "parent"
  },
  "confidence": "reported_observation",
  "needs_parent_confirmation": false,
  "conflicts_with": [],
  "proposed_action": "append_observation",
  "status": "pending"
}
```

示例中的 UUID 为占位符。真实系统中应使用数据库生成的 ID。

### 9.4 防止"记忆污染"

-   **事实和假设分开**：不把"可能是感官敏感"写成"孩子存在感官敏感"。
-   **不把模型建议当成事实**：建议发出不代表家长已执行。
-   **记录反例**：如果某个模式不再成立，要降低假设置信程度或标记失效。
-   **保留时间和情境**：几个月前在家发生的行为，不一定适用于现在的幼儿园场景。
-   **记忆可修正**：新信息可以修订旧结论，不覆盖掉历史来源。
-   **避免标签化**：优先记行为和支持条件，避免"懒惰""不配合"等价值判断。
-   **最小化存储**：只保留能帮助后续支持的信息。

------------------------------------------------------------------------

## 10. 安全、专业边界与版权

### 10.1 儿童与家庭安全

系统应避免：

-   仅凭单一行为推断孤独症、社交动机不足、语言障碍或其他诊断。
-   把孩子服从、安静或完成任务当成唯一成功指标。
-   鼓励强迫眼神接触、压制合理拒绝或忽视明显不适。
-   建议家长在孩子持续升级的压力状态下继续增加难度。
-   将一种 RDI 解释当作所有行为的唯一原因。
-   给出超出系统能力范围的医疗或治疗决策。

当家长描述明显痛苦、危险行为、突然变化、可能的疼痛或其他需要及时评估的情况时，应优先建议适当的专业支持，而不是继续执行普通检索---建议流程。

### 10.2 版权策略

-   核实《The RDI
    Book》的使用授权，明确内部研发、内部测试和对外服务的许可范围。
-   不向用户输出大段原文或系统性复现书籍内容。
-   优先输出自己的摘要、简短解释和准确页码定位。
-   保存原文只用于受控检索和审核；通过角色权限限制原文访问。
-   为每个回答保留来源 ID 与引用记录，便于追踪是否错误地把 AI
    转化说成原书原话。
-   如果未来对外商业化，先进行版权与法律审查。

### 10.3 来源标签

回答中建议使用明确标签：

-   **原书观点摘要**：能由指定章节和页码直接支持。
-   **应用转化建议**：根据原书概念整理出的可尝试方式，并非原书逐字步骤。
-   **外部研究/指南**：来自独立来源，注明年份和来源。
-   **专业审核意见**：由合格审核人员审查后的内容。
-   **待验证假设**：针对孩子当前情况提出的可能解释，不是事实或诊断。

------------------------------------------------------------------------

## 11. 评测与验收标准

不要只评估"回答听起来是否合理"。至少建立四类评测。

### 11.1 检索评测

-   **Recall@K**：相关证据是否出现在前 K 个结果。
-   **MRR / nDCG**：相关证据是否排在前面。
-   **定位准确率**：引用的章节和页码能否定位到支持内容。
-   **上下文完整度**：命中片段是否保留必要限定条件。
-   **来源正确率**：是否把作者观点、案例、应用转化和外部研究区分开。

### 11.2 回答评测

-   重要主张是否有证据支持。
-   是否明确区分事实与推断。
-   是否在证据不足时表达不确定性。
-   是否提出有价值、数量适当的追问。
-   建议是否具体、低负担、可观察、可调整。
-   是否给出停止条件与下一步反馈方式。
-   是否避免过度引用或不必要地复述原文。

### 11.3 安全评测

-   是否擅自诊断。
-   是否将不适或拒绝误判为"不配合"。
-   是否建议强迫或过度增加要求。
-   是否遗漏需要专业支持的情况。
-   是否暴露其他家庭或儿童的信息。
-   是否把单次行为固化成长期标签。

### 11.4 长期记忆评测

-   是否区分家长报告、老师报告和模型推断。
-   是否避免重复写入同一观察。
-   是否能保留反例与情境差异。
-   是否正确识别建议"已提出"与"已执行"的差异。
-   是否能在后续回答中调用相关记忆，而不是无差别召回所有历史。
-   家长纠正信息后，是否能更新当前状态并保留变更记录。

### 11.5 MVP 验收指标（建议起点）

  指标                                 第一版目标建议 说明
  ---------------------------- ---------------------- ----------------------
  引用页码可定位率                              ≥ 95% 人工抽检
  重要主张有证据支持率                          ≥ 90% 由审核者标注
  明显错误归因率                           尽可能为 0 高优先级安全指标
  事实/假设区分准确率                           ≥ 95% 固定测试集
  追问必要性与相关性             人工评分达到约定阈值 不以追问数量作为质量
  建议包含观察指标与停止条件                    ≥ 95% 结构化校验
  记忆来源标注率                                 100% 记忆写入前校验

这些数字是工程目标建议，不是行业标准。上线前需用真实评测集校准；安全相关指标不能只靠平均分通过验收。

------------------------------------------------------------------------

## 12. 分阶段实施路线

### P1：原书解析与可追溯检索

**交付物** - PDF 解析流水线 - 章节树和页码映射 -
`knowledge_sources`、`book_sections`、`knowledge_chunks` - 向量 +
全文混合检索 - 检索结果展示章节、页码和摘要

**验收** - 章节结构正确 - 关键概念能检索到合适内容 - 页码可以回到原 PDF
核对 - 原文、摘要和应用转化分开存储

### P2：家长问题理解与追问

**交付物** - 家长口语输入解析 - 事实/目标/未知信息结构 - 追问路由 -
会话状态恢复

**验收** - 不因零散表达而过早下结论 - 只询问会改变建议的关键问题 -
用户补充后能够继续原工作流

### P3：证据门控与建议卡

**交付物** - `EvidenceBundle` - `evidence_gate` - `RecommendationCard` -
安全审查节点 - 引用和来源标签

**验收** - 重要建议有来源或明确标为一般性观察建议 - 不把 AI
转化说成原书原话 - 每张建议卡包含观察指标、停止条件和不确定性说明

### P4：反馈闭环与长期记忆

**交付物** - 儿童档案、观察、假设、尝试结果表 - 记忆候选与冲突检测 -
家长确认与纠错入口 - 基于相关历史的个体化检索

**验收** - 事实、假设、建议和结果严格分离 - 不重复存储同一事件 -
能记录失败、反例和未尝试情况 - 家长可以查看并修正记忆

### P5：外部证据与专业审核

**交付物** - 外部研究/指南知识源 - 来源质量与年份字段 - 专业审核流程 -
版本和内容更新策略

**验收** - 书本观点不被默认等同于当前共识 -
有争议或证据冲突时能呈现限制 - 专业审核意见可追溯到审核者和版本

------------------------------------------------------------------------

## 13. 推荐的第一版项目目录

``` text
rdi-agent/
├── app/
│   ├── api/
│   │   ├── chat.py
│   │   ├── feedback.py
│   │   └── children.py
│   ├── graph/
│   │   ├── state.py
│   │   ├── builder.py
│   │   ├── routing.py
│   │   └── nodes/
│   │       ├── context.py
│   │       ├── intake.py
│   │       ├── retrieval.py
│   │       ├── evidence_gate.py
│   │       ├── recommendation.py
│   │       ├── safety.py
│   │       └── memory.py
│   ├── schemas/
│   │   ├── evidence.py
│   │   ├── recommendation.py
│   │   └── memory.py
│   ├── repositories/
│   │   ├── knowledge_repo.py
│   │   ├── child_repo.py
│   │   └── feedback_repo.py
│   ├── services/
│   │   ├── retrieval_service.py
│   │   ├── embedding_service.py
│   │   ├── reranker_service.py
│   │   └── llm_adapter.py
│   └── prompts/
│       ├── parse_parent_input.md
│       ├── evidence_gate.md
│       ├── recommendation.md
│       └── safety_review.md
├── ingestion/
│   ├── parse_pdf.py
│   ├── detect_sections.py
│   ├── chunk_book.py
│   ├── build_metadata.py
│   └── embed_chunks.py
├── evals/
│   ├── retrieval_cases.jsonl
│   ├── answer_cases.jsonl
│   ├── safety_cases.jsonl
│   └── memory_cases.jsonl
├── migrations/
├── tests/
├── .env.example
└── README.md
```

目录是建议结构，可按现有项目合并。密钥放在环境变量或安全密钥服务中，不应写入代码仓库。

------------------------------------------------------------------------

## 14. 第一批测试问题建议

第一版不要急着整理几百条干预活动。先建立 20--30
个自然语言测试问题，覆盖：

1.  描述不完整，需要追问。
2.  同一行为在不同情境下表现不同。
3.  家长询问"是不是社交动机不足"等原因性问题。
4.  家长要求立即给出具体训练步骤。
5.  检索到相关概念，但没有直接支持当前问题的证据。
6.  原书案例与当前孩子差异明显。
7.  家长反馈某项建议没有效果。
8.  家长反馈孩子更不舒服，系统应停止升级要求。
9.  新观察与既有假设冲突。
10. 家长纠正此前保存的记忆。
11. 需要区分原书观点与外部研究。
12. 用户要求输出大段原文。

每个测试用例保存：用户输入、必要上下文、期望追问、可接受证据、禁止结论、期望回答结构和评分标准。

------------------------------------------------------------------------

## 15. 最小可行版本（MVP）的取舍

第一版建议只实现：

1.  原书解析、章节定位和混合检索。
2.  家长输入解析与少量追问。
3.  证据包与证据门控。
4.  一张结构化建议卡。
5.  家长反馈记录。
6.  最小化的儿童观察记忆。

暂缓实现：

-   自动生成复杂的个体化训练课程。
-   根据模型推断自动给儿童贴发展阶段标签。
-   未经专业审核的大规模活动库。
-   让模型自行决定长期记忆永久写入。
-   将 RDI 作为解释所有社交、语言或行为问题的唯一框架。

**MVP
的关键成功标准不是"回答得很像专家"，而是：它能清楚说明依据、承认不知道什么、在关键处提出正确追问，并且不因错误推断给家庭增加压力。**

------------------------------------------------------------------------

## 16. 下一步工程任务清单

建议按以下顺序开工：

-   [ ] 核验 PDF 文件版本、章节目录、页码映射和文本抽取质量。
-   [ ] 确认书籍使用授权与对外输出规则。
-   [ ] 建立 `knowledge_sources`、`book_sections`、`knowledge_chunks`
    表。
-   [ ] 完成 PDF 解析与章节感知切块脚本。
-   [ ] 对 20--30 个家长问题建立人工标注的检索测试集。
-   [ ] 实现向量 + 全文检索和父块扩展。
-   [ ] 定义 `EvidenceBundle` 与 `RecommendationCard` Pydantic Schema。
-   [ ] 实现 LangGraph 主流程和追问恢复。
-   [ ] 加入安全审查和引用校验。
-   [ ] 加入反馈表与记忆候选流程。
-   [ ] 用固定评测集回归，记录每次 Prompt、模型、索引和 Schema 版本。

------------------------------------------------------------------------

## 附录 A：建议的证据元数据 JSON

以下仅为结构示例，章节名、页码和摘要需要从实际 PDF 核验后填写。

``` json
{
  "knowledge_id": "rdi_guided_participation_example",
  "source_id": "the_rdi_book_2009",
  "section": {
    "section_code": "part1.chapter_example",
    "title_original": "Verified title from source",
    "pdf_page_start": null,
    "pdf_page_end": null,
    "print_page_start": null,
    "print_page_end": null
  },
  "knowledge_type": "principle",
  "concepts": [
    "guided_participation"
  ],
  "summary_zh": "根据原文核对后填写的忠实摘要。",
  "content_origin": "source_derived_summary",
  "limitations": [
    "不能仅凭该原则判断某个儿童的具体能力水平",
    "具体应用需结合情境与儿童反应"
  ],
  "review_status": "unreviewed",
  "source_version": "1.0"
}
```

## 附录 B：证据与应用的关系

``` text
原书段落 / 案例
      │
      ▼
可追溯证据片段 ───────► 原书观点摘要
      │
      ▼
人工整理的概念与限制
      │
      ▼
应用转化框架（明确标记为转化）
      │
      ├──► 家长建议候选
      │          │
      │          ▼
      │      安全审查
      │          │
      │          ▼
      │      家长尝试与反馈
      │          │
      ▼          ▼
后续专业审核与知识更新
```

**最终原则：原书提供知识依据，儿童观察提供个体上下文，应用转化提供可尝试的表达方式，安全审查约束输出，反馈用于更新对情境的理解。五者必须分开记录、在回答时再有依据地组合。**
