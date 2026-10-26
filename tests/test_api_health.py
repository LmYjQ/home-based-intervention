"""API 冒烟测试。

用 FastAPI 的 TestClient 打 /health，验证：
- 数据库不可用时返回 503 而不是 500，并且逐项说明坏在哪。
- replay 模式下 LLM 检查不触网。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rdi_agent.api.main import app
from rdi_agent.config import reset_settings


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_returns_json(self, client, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "replay")
        reset_settings()
        resp = client.get("/health")
        assert resp.status_code in (200, 503)
        body = resp.json()
        # 逐项结构固定
        for key in (
            "status",
            "database",
            "pgvector",
            "fulltext",
            "checkpointer",
            "llm",
        ):
            assert key in body
        assert body["keyword_backend"] == "postgres_fts"
        assert body["embedding_model"] == "BAAI/bge-m3"

    def test_degraded_when_db_down(self, client, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "replay")
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://nobody:nobody@127.0.0.1:1/none"
        )
        reset_settings()
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["database"]["ok"] is False
        assert body["database"]["error"]

    def test_root(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["health"] == "/health"
