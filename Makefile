.PHONY: install db-up db-down migrate test dev demo feedback ingest ingest-no-embed \
        ingest-sections concepts search eval health clean \
        review-export review-mark review-apply review-list \
        web web-install \
        drop-old-book ocr run-log

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

# ---- 前端手动测试台（frontend_rdi/，Vue 3 + Vite）----
# 端口 5173。前端只有这一套（旧的 frontend/ 已挪进 legacy/）。
web-install:
	cd frontend_rdi && npm install

# 打真后端（make dev，8000 端口）。先起 make dev 和 make db-up。
web:
	cd frontend_rdi && npm run dev

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
# 会把指南正文发给 embedding 服务（BAAI/bge-m3）做向量化
#
# 源是**分页 Markdown**（data/ocr/guideline2026/page_001.md …）：
# 指南是扫描件，PDF 里没有文字层，页码靠"一个文件一页"保留。
#
# **这份源不在版本库里**（.gitignore 的 data/ocr/）——
# 指南全文进公开仓库和 rights_status=internal_review_only 自相矛盾。
# clone 之后先 OCR 出来：make ocr <pdf>
ingest:
	$(PY) -m rdi_agent.rag.ingest.pipeline --md-dir data/ocr/guideline2026

# 只解析和切块，不向量化（不外发正文，用于本地核对结构）
ingest-no-embed:
	$(PY) -m rdi_agent.rag.ingest.pipeline --md-dir data/ocr/guideline2026 --no-embed

# 重新生成章节树（会丢掉人工改过的边界）
ingest-sections:
	$(PY) -m rdi_agent.rag.ingest.pipeline --md-dir data/ocr/guideline2026 \
		--rebuild-sections --no-embed

# 写入核心术语表（中文口语 -> 指南里的规范术语）
concepts:
	$(PY) -m rdi_agent.rag.ingest.concepts

# 删除旧的英文书来源（the_rdi_book_2009）及其全部块和章节
# 已经跑过了（2026-10-24），留着是为了以后再遇到同类情况有现成脚本
drop-old-book:
	$(PY) -m tools.drop_rdi_book

# 把扫描件 OCR 成入库源（分页 Markdown）。
# 用法: make ocr <pdf 路径>
# 需要 PADDLEOCR_TOKEN 环境变量。
ocr:
	@test -n "$(PDF)" || (echo "用法: make ocr PDF=<pdf 路径>" && exit 1)
	$(PY) tools/ocr_pdf_paddle.py "$(PDF)" --out data/ocr/guideline2026

# 产出人工评估用的执行日志（data/index/exec_log.md + .json，不进版本库）
run-log:
	$(PY) -m tools.run_log

# ---- 人工抽查标记（计划 §9.1 第 11 步）----
# 标记存在 data/index/chunk_reviews.json（进版本库），入库时按内容哈希回填，
# 所以 make ingest 重建不会把人工核对的结果冲掉。

# 导出待核对的候选片段（默认均匀取样 50 个，覆盖全书各章）
review-export:
	$(PY) -m rdi_agent.rag.ingest.review export --limit $(or $(LIMIT),50)

# 标记：make review-mark KEY=sha256:xxx STATUS=reviewed
#       或 make review-mark CANDIDATES=data/index/review_candidates.jsonl
review-mark:
	$(PY) -m rdi_agent.rag.ingest.review mark \
		$(if $(KEY),--key $(KEY),) \
		$(if $(CANDIDATES),--candidate-file $(CANDIDATES),) \
		$(if $(STATUS),--status $(STATUS),) \
		$(if $(REVIEWER),--reviewer $(REVIEWER),) \
		$(if $(NOTE),--note $(NOTE),)

# 把标记写回数据库（只 UPDATE review_status，不重新入库、不重算向量）
review-apply:
	$(PY) -m rdi_agent.rag.ingest.review apply

# 看标记统计
review-list:
	$(PY) -m rdi_agent.rag.ingest.review list

# 检索：make search Q="引导式参与" [TOPK=8] [NOVECTOR=1]
search:
	$(PY) -m rdi_agent.rag.search "$(Q)" --top-k $(or $(TOPK),8) $(if $(NOVECTOR),--no-vector,)

eval:
	$(PY) -m evals.run_retrieval --cases evals/retrieval_cases.jsonl --json data/index/eval_report.json

health:
	curl -s localhost:8000/health | python -m json.tool

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
