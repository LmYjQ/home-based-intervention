"""LLM 适配器测试。

重点验证三件事：
1. 走的是 json_schema 而不是 json_object。
2. replay 模式不访问网络，且文件缺失时**直接失败**（不静默回落 live）。
3. schema 不合规时把错误路径回灌做一次修复重试。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from rdi_agent import llm
from rdi_agent.config import get_settings, reset_settings


class _Mini(BaseModel):
    # ok 必填：否则第一条 "缺 ok 字段" 的响应其实是合法的，测不出重试
    model_config = ConfigDict(extra="forbid")

    ok: bool
    note: str = ""


def _write_replay(tmp_path, stage: str, payload: dict) -> None:
    p = tmp_path / f"{stage}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestReplay:
    def test_replay_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "replay")
        monkeypatch.setenv("REPLAY_DIR", str(tmp_path))
        reset_settings()
        llm.configure(replay_dir=tmp_path)
        with pytest.raises(llm.LLMError, match="回放缺失"):
            llm.chat_json(
                system="s", user="u", model_cls=_Mini, name="mini", stage="mini"
            )

    def test_replay_reads_file_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "replay")
        reset_settings()
        llm.configure(replay_dir=tmp_path)
        # key 由 stage + name + system + user 决定，先算出来
        key = llm._replay_key("mini", "s", "u")
        _write_replay(tmp_path, f"mini.{key}", {"content": '{"ok": false, "note": "x"}'})

        result, trace = llm.chat_json(
            system="s", user="u", model_cls=_Mini, name="mini", stage="mini"
        )
        assert result.ok is False
        assert result.note == "x"
        assert trace.source == "replay"
        assert trace.latency_ms == 0

    def test_health_in_replay_does_not_call_network(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "replay")
        reset_settings()
        info = llm.health()
        assert info["mode"] == "replay"
        assert info["llm_ok"] is True
        assert "不依赖网络" in info["note"]


class TestConfig:
    def test_keyword_backend_default(self):
        assert get_settings().keyword_backend == "postgres_fts"

    def test_no_key_means_llm_unavailable(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("DEMO_MODE", "live")
        reset_settings()
        info = llm.health()
        assert info["has_key"] is False
        assert info["llm_ok"] is False
        assert "LLM_API_KEY" in info["error"]


class TestJsonSchemaWiring:
    def test_uses_json_schema_not_json_object(self, monkeypatch, tmp_path):
        """捕获真正发出去的 request，确认 response_format 是 json_schema。"""
        captured: dict = {}

        class _FakeChoice:
            message = type("M", (), {"content": '{"ok": true, "note": ""}'})()
            finish_reason = "stop"

        class _FakeResp:
            choices = [_FakeChoice()]
            usage = None

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _FakeResp()

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = type("C", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(llm, "_make", lambda timeout_s: _FakeOpenAI())
        llm.reset_client()
        monkeypatch.setenv("DEMO_MODE", "live")
        monkeypatch.setenv("LLM_API_KEY", "dummy")
        reset_settings()
        llm.configure(mode="live")

        result, trace = llm.chat_json(
            system="s", user="u", model_cls=_Mini, name="mini", stage="mini"
        )
        assert result.ok is True
        rf = captured["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "mini"
        assert "properties" in rf["json_schema"]["schema"]
        assert trace.source == "live"

    def test_schema_violation_is_retried_with_error_feedback(self, monkeypatch):
        """第一次返回缺字段，第二次修复成功。"""
        responses = ['{"note": "缺 ok 字段"}', '{"ok": true, "note": "修好了"}']
        calls: list[dict] = []

        class _FakeChoice:
            def __init__(self, content):
                self.message = type("M", (), {"content": content})()
                self.finish_reason = "stop"

        class _FakeResp:
            def __init__(self, content):
                self.choices = [_FakeChoice(content)]
                self.usage = None

        class _FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return _FakeResp(responses[len(calls) - 1])

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = type("C", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(llm, "_make", lambda timeout_s: _FakeOpenAI())
        llm.reset_client()
        monkeypatch.setenv("DEMO_MODE", "live")
        monkeypatch.setenv("LLM_API_KEY", "dummy")
        reset_settings()
        llm.configure(mode="live")

        result, trace = llm.chat_json(
            system="s", user="u", model_cls=_Mini, name="mini", stage="mini"
        )
        assert result.ok is True
        assert trace.attempt == 2
        assert len(calls) == 2
        # 第二次把错误路径回灌给模型
        assert "不符合要求" in calls[1]["messages"][-1]["content"]
