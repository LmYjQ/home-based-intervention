# RDI 家长干预建议智能体

基于 LangGraph 的家庭问题解决助手：通过 RDI 书籍 RAG 提供可追溯的专业依据，
通过结构化长期记忆积累家庭背景，通过反馈闭环持续调整后续建议。

> **这不是诊断工具。** 系统提供的是教育性信息和家庭观察支持，
> 不能替代个体化临床评估或认证 RDI 顾问的指导。

## 当前进度

| 阶段 | 状态 |
|---|---|
| 阶段 0 重置与地基 | ✅ 已完成（2026-10-21） |
| 阶段 1 最小业务闭环 | ⬜ 未开始 |
| 阶段 2 书籍 RAG（2A/2B/2C） | ⬜ 未开始 |
| 阶段 3 长期记忆 | ⬜ 未开始 |
| 阶段 4 反馈与方案迭代 | ⬜ 未开始 |
| 阶段 5 持久化与调试 | ⬜ 未开始 |
| 阶段 6 工程化与演示 | ⬜ 未开始 |

**唯一执行文档：[`langgraph_family_assistant_phased_plan.md`](langgraph_family_assistant_phased_plan.md)。**
动手前先读它，每个阶段的验收标准都在里面。

## 快速开始

```bash
# 1. 环境
python3.11 -m venv .venv && source .venv/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev]"
cp .env.example .env      # 填 LLM_API_KEY

# 2. 数据库（PostgreSQL 16 + pgvector）
docker compose -f deploy/docker-compose.yml up -d db

# 3. 迁移
alembic upgrade head

# 4. 跑起来
uvicorn rdi_agent.api.main:app --reload
curl -s localhost:8000/health | python -m json.tool
```

`/health` 会逐项自检：数据库、pgvector 扩展、全文检索后端、模型配置、replay 状态。
任何一项坏了都返回 503，body 里说清是哪一环。

## 常用命令

```bash
make db-up       # 起数据库
make migrate     # 跑迁移
make test        # 跑测试
make dev         # 起服务
make health      # 查健康状态
```

## 技术底座

- **编排** LangGraph `StateGraph`（`interrupt` / `Command(resume=...)` / Checkpointer / Time Travel）
- **后端** FastAPI + Uvicorn
- **数据** PostgreSQL 16 + pgvector（业务表、向量、全文检索、Checkpointer 全在一个实例）
- **全文检索** Postgres 内置 `tsvector` + GIN + `ts_rank_cd`（**不是严格 BM25**；需要时切 ParadeDB `pg_search`）
- **模型** SiliconFlow OpenAI 兼容接口（Chat / `BAAI/bge-m3` / `BAAI/bge-reranker-v2-m3`）
- **结构化输出** 一律 `response_format={"type":"json_schema"}`，不用 `json_object`

## 目录

```
rdi_agent/
├── api/         FastAPI 入口、/health
├── graph/       State、图装配、节点
├── schemas/     Pydantic 契约（硬规则由校验器强制）
├── db/          engine、ORM 模型、repository
├── rag/         入库、检索、重排、查询改写
├── services/    记忆、反馈
└── llm.py       唯一 LLM 出口
```

## 旧代码

`asd_agent/`、`server.py`、`frontend/` 是**冻结的 legacy**，不再修改也不作为实现依据。
它保留在仓库里只作参考；快照 tag 是 `snapshot-before-rebuild`。

## 版权

`data/documents/` 里的 RDI 书籍文件**不进仓库**（已在 `.gitignore`）。
其授权状态未核实，按 `internal_review_only` 处理：只用于本地研发和内部演示，
不对外提供原文，不做大段引用。商业化前必须单独完成版权审查。
