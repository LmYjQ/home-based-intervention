.PHONY: install db-up db-down migrate test dev demo feedback ingest search eval health clean

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

ingest:
	$(PY) -m rdi_agent.rag.ingest.pipeline --pdf data/documents/the_rdi_book.pdf

search:
	$(PY) -m rdi_agent.rag.search "孩子在幼儿园集体活动时哭" --top-k 8 --show-pages

eval:
	$(PY) -m evals.run_retrieval --cases evals/retrieval_cases.jsonl --report data/index/eval_report.json

health:
	curl -s localhost:8000/health | python -m json.tool

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
