.PHONY: install db-up db-down migrate test dev demo feedback ingest ingest-no-embed \
        ingest-sections concepts search eval health clean

PY := .venv/bin/python
PIP := .venv/bin/pip
COMPOSE := docker compose -f deploy/docker-compose.yml

install:
	python3.11 -m venv .venv
	$(PIP) install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev]"

db-up:
	$(COMPOSE) up -d db

db-down:
	$(COMPOSE) down

migrate:
	.venv/bin/alembic upgrade head

test:
	.venv/bin/pytest -q

dev:
	.venv/bin/uvicorn rdi_agent.api.main:app --reload

# 固定演示输入，走完 追问 → 方案 → 选择 → 反馈
demo:
	$(PY) -m rdi_agent.cli --demo

# 交互式 CLI
cli:
	$(PY) -m rdi_agent.cli

# 录入一次尝试的反馈
feedback:
	$(PY) -m rdi_agent.cli --feedback

# 全量入库：解析 -> 切块 -> 向量 -> 写库 + ingest_report.json
# 会把书的正文发给 embedding 服务（BAAI/bge-m3）做向量化
ingest:
	$(PY) -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf

# 只解析和切块，不向量化（不外发正文，用于本地核对结构）
ingest-no-embed:
	$(PY) -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf --no-embed

# 重新生成章节树（会丢掉人工改过的边界）
ingest-sections:
	$(PY) -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf \
		--rebuild-sections --no-embed

# 写入核心术语表（中文别名 -> 书里的英文术语）
concepts:
	$(PY) -m rdi_agent.rag.ingest.concepts

# 检索：make search Q="引导式参与" [TOPK=8] [NOVECTOR=1]
search:
	$(PY) -m rdi_agent.rag.search "$(Q)" --top-k $(or $(TOPK),8) $(if $(NOVECTOR),--no-vector,)

eval:
	$(PY) -m evals.run_retrieval --cases evals/retrieval_cases.jsonl --report data/index/eval_report.json

health:
	curl -s localhost:8000/health | python -m json.tool

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
