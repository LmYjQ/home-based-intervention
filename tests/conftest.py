"""pytest 公共 fixture。"""

from __future__ import annotations

import os

import pytest

from rdi_agent.config import reset_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    """每个用例前后都清掉 Settings 缓存，避免用例之间串环境变量。"""
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def no_db(monkeypatch):
    """把数据库指向一个必然连不上的地址，用于测降级路径。"""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://nobody:nobody@127.0.0.1:1/none"
    )
    reset_settings()
    yield


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """默认不碰真实模型接口；需要真调用的用例自己显式覆盖。"""
    if "DEMO_MODE" not in os.environ:
        monkeypatch.setenv("DEMO_MODE", "replay")
        reset_settings()
