.PHONY: install db-up db-down migrate test dev ingest search eval health clean

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
