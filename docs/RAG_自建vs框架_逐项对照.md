# RAG 自建 vs 框架：逐项对照

> **这份文档回答一个问题**：RAG 这部分为什么自己写，不套 LangChain / LlamaIndex？
>
> 结论先行：**不上框架**。但理由不是"自建更酷"，而是逐条核对之后发现——
> **框架能替的部分恰好是项目规则最密的部分，而项目真正的复杂度在框架覆盖不到的地方。**
>
> 相关：`langgraph_family_assistant_phased_plan.md` §3.3（技术选型）、
> §9.2（阶段 2B 检索）、`rdi_agent/rag/`（实现）。
>
> **写于 2026-10-22**，对应阶段 2A 落地后的实际代码状态。
> 文中所有行数和依赖版本都是实测的，不是估计的。

---

## 0. 先纠正一个事实

**本项目从来没有用过 LangChain。**

`pyproject.toml` 曾经声明 `langchain-core` 和 `langchain-openai`，
但全项目**零处 import**：

```bash
$ grep -rn "langchain" --include=*.py . | grep -v .venv | wc -l
0
```

`rdi_agent/llm.py` 用的是裸 `openai.AsyncOpenAI`（`llm.py:25`）。
计划 §3.3 原写"只使用 Chat Model 和结构化输出"，与代码不符，已在计划里记正。

两个声明的实际命运：

| 声明 | 实际状态 | 处置 |
|---|---|---|
| `langchain-core` | langgraph 的**硬依赖**，会被传递安装 | 从 `pyproject.toml` 删除（不需要重复声明） |
| `langchain-openai` | 纯多余，没有任何东西用 | 从 `pyproject.toml` 删除 |

所以"要不要用框架"这个问题，**准确的说法是"要不要从零换成框架"**——
不存在"已经在用框架但想优化"这个选项。

> **顺带一个版本事实**（查了 wheel 才敢写）：LangChain 1.x 把
> `langchain` 主包做成了薄壳，检索器都在 **`langchain-classic`** 里，
> `reciprocal_rank_fusion` 和 `EnsembleRetriever` 在
> `langchain_classic/retrievers/ensemble.py`。
> 而**已装的 `langchain_core` 1.6.12 里既没有 `EnsembleRetriever`
> 也没有 `reciprocal_rank_fusion`**。
> 也就是说"用 LangChain 做检索"至少要额外装 `langchain-classic`（或 `langchain-community`）。

---

## 1. 先看行数：框架能替的范围有多大

```
RAG 目录共 2293 行
├── rag/search.py              313   ← 检索（框架理论上能替）
└── rag/ingest/               1980   ← 入库
    ├── parse_pdf.py           131
    ├── clean.py               149
    ├── sections.py            210
    ├── chunk.py               251
    ├── embeddings.py          114
    ├── concepts.py            256
    ├── pipeline.py            395
    └── review.py              458
```

**86% 的代码在入库，框架完全覆盖不到**（理由见 §3）。
能被框架替的只有 313 行。

---

## 2. 检索这 313 行：框架能替，但替了要覆写什么

框架**确实**提供了对应能力——这点得承认，不能贬低框架：

| 我们的实现 | 框架对应物 | 位置 |
|---|---|---|
| `_rrf_merge()` RRF 融合 | `EnsembleRetriever` | `langchain_classic/retrievers/ensemble.py` |
| 父子块检索 | `MultiVectorRetriever` / `ParentDocumentRetriever` | `langchain_classic/retrievers/multi_vector.py` |
| 向量召回 | `PGVector` | `langchain_community/vectorstores/` |
| BM25 / 关键词召回 | `BM25Retriever` | `langchain_classic/retrievers/bm25.py` |

看起来是能替的。但逐条读框架源码之后，有五个地方必须覆写：

### 2.1 关键词路：框架没有 Postgres FTS 这条路

我们要的是 `websearch_to_tsquery('english', ...)` + `ts_rank_cd`，
因为 `content_tsv` 是**生成列**（`to_tsvector('english', ...)` STORED）+ GIN 索引。

- `langchain_community` 的 `BM25Retriever` 是**内存**倒排（`rank_bm25`），
  不走 Postgres，1049 块全部进内存，而且**丢掉了 `ts_rank_cd` 的排序**。
- 框架里没有 Postgres FTS 的原生 retriever。要用，只能自己写一个
  `BaseRetriever` 子类——**那还是自己写，只是外面套了层框架接口**。

### 2.2 RRF：框架的实现和我们要的语义有偏差

框架的 `weighted_reciprocal_rank`：

```python
rrf_score[key] += weight / (rank + self.c)     # c: int = 60
```

我们的：

```python
score += 1.0 / (RRF_K + rank)                    # RRF_K = 60
```

公式**一致**，`c=60` 也一致。但有两点差异：

1. **框架默认按 `doc.page_content` 去重**（`ensemble.py:330`），
   我们按 `chunk_id` 去重。`content_normalized` 里两个不同块可能
   文本高度相似（同一段的父块和子块），按内容去重会**误合并**。
   框架支持 `id_key` 改掉，但要显式传。
2. **框架的权重语义是 `weight / (rank + c)`**，
   意味着两路权重不同则分数量级不同。我们是等权 RRF——
   2B 要调参（"每次只改一个变量"），等权 + 可调 K 更适合做 A/B。

> 博客上常说 "LangChain 的 RRF 里 `c=60` 硬编码不可调"——
> **这是错的**，读源码 `ensemble.py:70` 就是 `c: int = 60`，是个可改的字段。
> 但 `weights` 在 RRF 模式下确实被"等权化"了（这点博客没说错）。
> 这些细节正说明：**光看文档不够，得读源码。**

### 2.3 父子块：框架要的是"另一个存储"，我们要的是"同一张表的外键"

框架的 `MultiVectorRetriever` 需要**两个**存储：

```python
vectorstore: VectorStore   # 存子块的向量
docstore: BaseStore[str, Document]   # 存父块，单独的 KV 存储
id_key: str = "doc_id"     # 子块 metadata 里指回父块的 id
```

检索时：子块命中 → 取 `metadata[id_key]` → 去 `docstore.mget(ids)` 拿父块。

我们的做法是**同一张 `knowledge_chunks` 表的外键**：

```
child.parent_chunk_id -> parent.id     （同一张表，join 一下就有）
```

差别不只是写法：

- 框架要多维护一个 `docstore`（`InMemoryStore` 是纯内存，上万块会炸），
  我们的父块就在旁边，`JOIN` 一下的事。
- **我们的父块和子块共用一张表，所以人工审核标记、页码、章节信息
  对父块和子块是一套逻辑**。框架里父块在另一个 store，
  `review_status` / `pdf_page_start` 这些字段得在两个地方各维护一遍。
- 框架的 `MultiVectorRetriever` 有个已知坑：`vectorstore.from_texts()`
  **不会**自动关联父子，必须手动传 id 并 `mset` 到 byte store，
  否则**静默退化成扁平检索**——不报错，只是效果变差。
  这种"配错了不报错"的行为，在我们的场景里很难查。

> **我们自己也踩了同类的坑**（2026-10-22 修）：写库时用
> `chunk_ids.get(item.parent_chunk_index or -1)` 找父块 id，
> 而 **`0 or -1` 在 Python 里是 `-1`** —— 第一章的子块（父块序号正好是 0）
> 找不到父块，`parent_chunk_id` 变成 `NULL`。
>
> 后果是双重的：
> 1. 检索里 `is_parent` 靠 `parent_chunk_id IS NULL` 判定，
>    这 3 个**子块**被当成父块 —— 默认只回子块时它们直接被过滤掉；
> 2. 父块扩展找不到它们的父块。
>
> 表现很隐蔽：**入库报告说"父块 251 / 子块 798"，但库里查出来是
> 254 / 795**。切块器是对的，是写库那一步丢的。
> 修复后加了回归测试 `test_first_parent_children_link_to_their_parent`，
> 并验证过：把修复改回去，测试确实会红。
>
> 这个例子说明的不是"自建更好"，而是**同表外键方案对数据一致性的要求更高**——
> 框架那套"父块存在另一个 docstore"的方案，反而不会有这种"序号 0"的陷阱，
> 因为它根本不靠序号关联，靠 id。

### 2.4 人工审核状态：框架完全没有这个概念

`review_status`（`unreviewed` / `reviewed` / `needs_correction`）是我们自己的字段。
计划 §5.4 要求检索时排除 `needs_correction`。

框架的 `BaseRetriever` 没有"按人工审核状态过滤"这回事——
要么塞进 metadata 当普通 filter 条件（但框架的 filter 语义
各向量库不一样，pgvector 那套得自己写），要么在拿到结果后过滤
（那就**召回了 30 条再扔掉**，白跑 embedding）。

我们直接写在 SQL 的 `WHERE` 里（`rag/search.py` 的 `REVIEW_FILTER`），
过滤发生在数据库内部，索引照走。

### 2.5 每条命中必须带 PDF 页码——这是硬约束

计划要求"输出必须能翻回去核对"：每条结果给章节 + PDF 页码 + 原文片段。
**没有页码，家长没法核实"书里真这么写吗"。**

框架的 `Document` 只有 `page_content` + `metadata`。
塞页码进 metadata 是可行的，但……

看 §3.1 就知道，**页码这个字段的语义本身就是这本书特有的**：
印刷页码丢了（`print_page_*` 只能置空）、章节区间和目录嵌套互相矛盾、
同一份内容要同时挂"物理页序"和"目录层级"两套坐标。
框架的 `metadata` 是扁平的 dict，**没有地方表达"这个坐标不可信"**。
我们靠 `book_sections` 表 + `section_map.json` 的 `notes` 字段来表达。

---

## 3. 入库这 1980 行：框架完全帮不上

这部分不是"我们喜欢手写"，是**框架没有这些概念**。

### 3.1 这个 PDF 是 calibre 从 mobi 重排的，有具体毛病

`parse_pdf.py` 的实测结果：

```
392 页 → 解析 355 / 跳过 37
  no_text_layer:       17 页（封面、Part 分隔页、章节扉页）
  section_label_page:  19 页（只写了 "Chapter One" 这种标签）
  too_short:            1 页
```

框架的 PDF loader（`PyMuPDFReader` 等）只会**把文字抽出来**，
不会告诉你"这 37 页是空的分隔页"。

更麻烦的是正文**每 70~80 字符硬换行**——那是排版产物，不是段落边界。
`clean.py` 的规则每一条都对应一个实测现象：

| 规则 | 实测依据 |
|---|---|
| 空行分段 | calibre 用空行分隔段落，空行是真实的段落边界 |
| 行内换行接回 | 硬换行会把一个词劈成两半，FTS 和向量都命中不了 |
| 行尾连字符**不拆** | 全书只有 96 处行尾连字符，**逐条看过**，全是 `hyper-` / `seven-year-olds` / `goal-directed` 这种真连字符 |
| 页眉页脚按频次剔除 | 同一行在 >25% 的非空页出现**且** ≤60 字符，两个条件缺一不可 |
| 原文 / 规范化分开存 | FTS 用 `english` 配置，弯引号会把词切开 |

> "行尾连字符要保留"这条，是**逐条看过 96 处**才敢这么定的。
> 通用规则（默认拆连字符）在这里会产出 `hypertalker` 这种错词。
> 框架的 chunker 不会给你这个判断。

### 3.2 目录和分页互相矛盾

这是 `sections.py` 最难的地方。calibre 的目录把
`Introduction`(p17) 挂在 `PART ONE`(p31) **下面**——子章节的页码比父级还小。

我们最终用**两套规则各管一件事**：

- **页区间**按物理页序：结束页 = 后面第一个条目（不论层级）的起始页 − 1
- **父子关系**按目录嵌套：calibre 的 nav 列表顺序表达了嵌套

两者对不上时，写进 `section_map.json` 的 `notes`：

```
起始页(p17)在父级 PART ONE: Setting the Stage for RDI(p31) 之前 ——
目录页码与嵌套不一致，父级按目录保留，待人工确认
```

**框架没有"目录和自身分页打架"这个概念**——它的层级节点解析假定两者一致。

### 3.3 印刷页码不可恢复

calibre 重排时把原书页码丢了。全书 392 页里**只有 1 行是纯数字**（p315 的 "287"），
不足以反推全书页码。

所以 `print_page_start` / `print_page_end` **一律置空**，
检索只承诺"能按 PDF 页码翻回去"，不承诺印刷页码。

计划 §5.3 明写"印刷页码无法确认就置空，**不猜**"。框架的
`Document` metadata 是你填什么它存什么，**不会提醒你这里有个不能确认的字段**。

### 3.4 切块要保语义边界，还要分知识类型

`chunk.py` 的两个决定：

- **按段落切，不按句子也不按字符**。段落是作者自己划的语义边界，
  在段落中间下刀会切出"前半句接后半句"这种读不通的块。
- 这本书的句子本身就长（实测一句 1000 字符），段落更是整页级别。
  所以子块目标设成比单元小（`CHILD_TARGET_CHARS=500` vs 实际单元约 1000），
  让一个单元自己成为一个子块。实测子块平均 1310 字符。

还有 `classify()` 打知识类型，实测分布：

```
narrative 862 / reference 185 / reflection 2 / appendix 0
```

（`reference` 是参考文献章节整段进来的；`reflection` 少是因为它靠
正文里出现 "reflection question" 判定。）

框架的 chunker 给不了 `knowledge_type`——这是我们自己的检索调优维度
（不同类型的块该给不同权重）。

### 3.5 术语表必须是这本书真的用的词

`concepts.py` 踩过一个坑，值得记下来：

首版我按 RDI 通用词汇塞了 50 个术语，扫全书正文一算——
`affective perspective`、`unit of attention`、`engagement cycle`、
`functional play` **一次都没出现**。书里说的是
`parent-child engagement`、`over-prompting`、`participation relationship`。

重建为 52 条**逐条核实过在正文出现过**的词（`is_explicit_in_source`
是**扫全书算出来的**，不是人工断言）。

框架的 synonym / alias 机制接受任何词，不验证它在语料里存不存在。
把书里没有的词塞进查询扩展，只会**把检索带偏**。

### 3.6 人工标记必须在"重建"后还在

`review.py` 的核心约束：入库是**先删后建**的重建流程
（`pipeline.write_to_db` 按 `source_id` 全删再重建）。
标记如果只存在库里，重建一次就全没了。

所以标记存文件（`data/index/chunk_reviews.json`），
以**内容哈希**为键，入库时**回填**。

用 `chunk_index` 做键会有个更危险的问题：改了切分参数，
`chunk_index` 整体位移，标记会**错位到别的片段上**——那比丢标记更糟。
内容哈希跟着文字走：文字改了，标记自动失效（被改过的片段必须重新核对），
失效的标记写进 `ingest_report.json` 的 `review.stale`，**不静默**。

框架的向量库没有"重建后人工标记还在"这个概念——
因为框架不假设你会删库重建。

---

## 4. 汇总

| # | 我们的自定义 | 框架 | 结论 |
|---|---|---|---|
| 1 | Postgres FTS（`ts_rank_cd` + 生成列 + GIN） | 内存 BM25 | **要自己写** |
| 2 | RRF（等权，按 `chunk_id` 去重） | `EnsembleRetriever` | 能替，但去重键和权重语义要改 |
| 3 | 父子块（同表外键 join） | `MultiVectorRetriever`（另建 docstore） | **架构不合** |
| 4 | `review_status` 过滤 | 无此概念 | **要自己写** |
| 5 | 页码可定位（含"不可信"的表达） | 扁平 metadata | **表达不了** |
| 6 | calibre 重排清洗（96 处连字符逐条核过） | 通用 chunker | **框架没这概念** |
| 7 | 目录 / 分页矛盾（两套规则 + notes） | 假定二者一致 | **框架没这概念** |
| 8 | 印刷页码不可恢复 → 置空 | 你填它就存 | **框架不提醒** |
| 9 | `knowledge_type` 分类 | 无 | **要自己写** |
| 10 | 书特有的术语表（52 条实测存在） | synonym/alias 不验证存在性 | **框架会带偏** |
| 11 | 重建后人工标记还在 | 不假设你会重建 | **要自己写** |

**11 项里，框架能真正替掉的只有第 2 项（RRF），而且还要改两处语义。**

---

## 5. 这份文档的方法论：怎么核对，而不是怎么相信

上面的每条结论都做了**可复现的核对**，不是凭印象：

| 结论 | 怎么核的 |
|---|---|
| 项目没用 LangChain | `grep -rn "langchain" --include=*.py .` → 0 命中 |
| `langchain_core` 1.6.12 里没有 RRF | 解包 wheel 实际看文件，不是看文档 |
| `EnsembleRetriever` 在 `langchain-classic` | 下载 wheel 解包，`find | xargs grep` |
| `c=60` 是可改字段 | 读 `langchain_classic/retrievers/ensemble.py:70` |
| 框架的 RRF 按 `page_content` 去重 | 读同文件 `:330` 附近的 `unique_by_key` |
| `MultiVectorRetriever` 要另建 docstore | 读 `multi_vector.py` 的字段定义 |
| 入库 1980 行 / 检索 313 行 | `wc -l` |
| 跳过页的分布 | `ingest_report.json` 实测 |
| 52 条术语都在书里出现过 | 扫全书正文算的，不是人工断言 |
| `or -1` 的 bug | 改回去 → 回归测试变红 |

**光看文档不够。** 上面 §2.2 那条"博客说 `c=60` 硬编码不可调"就是靠读源码
证伪的——如果照博客写，文档里就会有一条假事实。

---

## 6. 那"简化代码"怎么办

不靠框架，靠**收敛重复**。已做（commit `ef3a025`）：

- `rag/search.py` 里两路召回原本各抄了一份 SELECT 列和行转候选的逻辑
  → 抽成共用的 `_SELECT_COLUMNS` + `_run_candidate_query`（2/2 → 1/1）。
  动机很实际：加 `review_status` 过滤时差点只加到一路。
- `expand_with_concepts` 原来每次检索把整张术语表拉回内存逐条扫
  → 改成 SQL 里过滤。扫了全部 **176 个别名**做新旧实现对比，**0 处不一致**。

**能靠框架省的 313 行里，框架自己要你装 `langchain-classic`，
并覆写其中至少 5 处语义。省下的代码量小于覆写引入的复杂度。**

---

## 7. 什么情况下这个结论会变

写下来是为了将来能推翻它。出现下面任一条，就该重新评估：

| 触发条件 | 该做什么 |
|---|---|
| **知识源从 1 本书变成几十份**（不同书、不同清洗规则） | 框架的 loader 生态开始有价值；或写自己的 loader 插件层 |
| **章节结构统一了**（多本书目录格式一致，不再有矛盾） | `sections.py` 的两套规则可以合并，框架的层级解析就用得上 |
| **印刷页码能可靠恢复** | 页码约束放松，metadata 扁平化就够了 |
| **2B 评测显示 RRF 权重需要按路调优** | 框架的 `weights` 语义（等权化）反而碍事，继续自己写 |
| **需要多路（>2 路）召回** | `EnsembleRetriever` 的多路支持开始有省 |
| **评测集（25 条）跑出 Recall@5 < 0.80 且排查后确认是检索实现问题** | 拿框架做对照实验，看差距在哪一环 |

**2B 的评测结果会直接检验这份文档的每一条结论。**
如果自建实现跑不出 0.80，那"框架帮不上"就是错的——
要么是对框架的能力判断有误，要么是自建实现有 bug。
**评测集是这份文档的证伪机制，不是它的注脚。**

---

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `rdi_agent/rag/ingest/parse_pdf.py` | 逐页抽取 + 跳过原因 |
| `rdi_agent/rag/ingest/clean.py` | calibre 重排痕迹清洗 |
| `rdi_agent/rag/ingest/sections.py` | 章节树（页区间 / 父子两套规则） |
| `rdi_agent/rag/ingest/chunk.py` | 父子块切分 + `knowledge_type` |
| `rdi_agent/rag/ingest/embeddings.py` | bge-m3 批量向量化 |
| `rdi_agent/rag/ingest/concepts.py` | 书特有的中文别名表 |
| `rdi_agent/rag/ingest/review.py` | 人工标记（内容哈希为键） |
| `rdi_agent/rag/ingest/pipeline.py` | 串起来 + 写库 + 入库报告 |
| `rdi_agent/rag/search.py` | 双路召回 + RRF + 概念桥接 |
| `langgraph_family_assistant_phased_plan.md` | §3.3 技术选型、§5.2 入库、§5.4 检索、§9.2 阶段 2B |
