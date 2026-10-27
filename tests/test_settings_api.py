"""LLM 设置的运行时覆盖（模型名、超时）。

**测的是"改了立刻生效"这条要求**：

- 改模型名 → 下一次 ``llm.chat_json`` 用的就是新模型；
- 改超时 → **client 必须重置**（client 是按超时缓存的，
  不重置的话等于没改）；
- 改完能回到 ``.env`` 的默认值。

**为什么这个能改**：provider 排队时把超时调短试试、换个模型对比一下效果。
改 ``.env`` 要重启服务，改代码更糟。
"""

from __future__ import annotations

import pytest

from rdi_agent import llm
from rdi_agent.api import settings as settings_api
from rdi_agent.config import (
    get_settings,
    llm_overrides,
    reset_settings,
    set_llm_overrides,
)
from rdi_agent.schemas.case import CaseSummary


class _FakeResponse:
    """只够走完 ``chat_json`` 的取值路径。"""

    class _Choice:
        class _Message:
            content = '{"observed_facts": []}'

        message = _Message()
        finish_reason = "stop"

    choices = [_Choice()]
    usage = None


@pytest.fixture(autouse=True)
def _clean():
    """每个用例前后都清干净 —— 覆盖是进程级的，不能互相影响。"""
    reset_settings()
    yield
    reset_settings()


class TestOverrides:
    def test_model_and_timeout_change_together(self):
        result = set_llm_overrides(
            llm_model="Qwen/Qwen2.5-14B-Instruct", llm_timeout_s=45
        )
        assert result["llm_model"] == "Qwen/Qwen2.5-14B-Instruct"
        assert result["llm_timeout_s"] == 45

    def test_unset_field_keeps_its_value(self):
        """前端只填了超时时，模型名不该被清掉。"""
        set_llm_overrides(llm_model="Qwen/Qwen2.5-14B-Instruct")
        set_llm_overrides(llm_timeout_s=45)
        current = llm_overrides()
        assert current["llm_model"] == "Qwen/Qwen2.5-14B-Instruct"
        assert current["llm_timeout_s"] == 45

    def test_reset_returns_to_env_default(self):
        default_model = get_settings().llm_model
        default_timeout = get_settings().llm_timeout_s
        set_llm_overrides(llm_model="Qwen/Qwen2.5-14B-Instruct", llm_timeout_s=45)

        from rdi_agent.config import reset_llm_overrides

        reset_llm_overrides()
        current = llm_overrides()
        assert current["llm_model"] == default_model
        assert current["llm_timeout_s"] == default_timeout

    def test_empty_model_is_rejected(self):
        with pytest.raises(ValueError):
            set_llm_overrides(llm_model="   ")

    def test_non_positive_timeout_is_rejected(self):
        with pytest.raises(ValueError):
            set_llm_overrides(llm_timeout_s=0)

    def test_timeout_change_resets_the_client(self):
        """**改超时必须重置 client**。

        client 是按超时缓存的（``llm._CLIENTS``），
        不重置的话新超时不会生效 —— 等于改了没改。
        """
        set_llm_overrides(llm_timeout_s=45)
        client = llm.client()
        # client 记着自己用的超时
        assert client.timeout == 45

    def test_max_attempts_default_is_two(self):
        """默认重试 2 次。

        **为什么从 3 降到 2**：超时 120 秒 × 3 次 = 最坏 6 分钟，
        家长面对一个不动的按钮。2 次是"还值得再等等"的上限。
        """
        assert get_settings().llm_max_attempts == 2


class TestModelReachesCall:
    @pytest.mark.asyncio
    async def test_new_model_is_used_by_the_next_call(self, monkeypatch):
        """改完模型名，下一次调用就该用新模型。

        **测的是出口**：不是"配置读到了"，而是"真的发给 provider 的
        model 字段变了"。中间隔着一个 ``settings.llm_model`` 的读取，
        只测配置会漏掉"取了但没用"。
        """
        seen: list[str] = []

        class _FakeCompletions:
            async def create(self, **kwargs):
                seen.append(kwargs.get("model") or "")
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        monkeypatch.setattr(llm, "client", lambda timeout_s=None: _FakeClient())
        # conftest 默认 DEMO_MODE=replay —— 那样在调 client 之前就短路了
        monkeypatch.setenv("DEMO_MODE", "live")
        reset_settings()

        set_llm_overrides(llm_model="Qwen/Qwen2.5-14B-Instruct")
        try:
            await llm.chat_json(
                system="s",
                user="u",
                model_cls=CaseSummary,
                name="case_summary",
                stage="intake_parse",
            )
        except Exception:
            # 假返回体不完整，反序列化失败没关系 ——
            # 我们只关心"发给 provider 的 model 是什么"。
            pass

        assert seen == ["Qwen/Qwen2.5-14B-Instruct"], (
            f"模型名没生效，发过去的是 {seen}"
        )


class TestApi:
    @pytest.mark.asyncio
    async def test_get_returns_current_values(self):
        result = await settings_api.get_llm_settings()
        assert "llm_model" in result
        assert "llm_timeout_s" in result
        assert "llm_max_attempts" in result

    @pytest.mark.asyncio
    async def test_post_changes_values(self):
        result = await settings_api.update_llm_settings(
            settings_api.LLMSettingsRequest(
                llm_model="Qwen/Qwen2.5-14B-Instruct", llm_timeout_s=45
            )
        )
        assert result["llm_model"] == "Qwen/Qwen2.5-14B-Instruct"
        assert result["llm_timeout_s"] == 45

    @pytest.mark.asyncio
    async def test_post_with_reset_returns_to_default(self):
        default_model = llm_overrides()["llm_model"]
        await settings_api.update_llm_settings(
            settings_api.LLMSettingsRequest(llm_model="Qwen/Qwen2.5-14B-Instruct")
        )
        result = await settings_api.update_llm_settings(
            settings_api.LLMSettingsRequest(reset=True)
        )
        assert result["llm_model"] == default_model

    def test_non_positive_timeout_is_rejected_at_the_schema(self):
        """超时 ≤ 0 在**契约层**就被挡掉（``Field(gt=0)``）。

        不靠接口里再判一次 —— 契约挡住的东西不会走到业务代码。
        （接口里那道 ``ValueError`` 是给直接调 ``set_llm_overrides``
        的路径兜底的，HTTP 请求走不到。）
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            settings_api.LLMSettingsRequest(llm_timeout_s=-1)

    @pytest.mark.asyncio
    async def test_empty_model_is_rejected_by_the_endpoint(self):
        """模型名全空白 → 接口返回 400，不是 500。"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await settings_api.update_llm_settings(
                settings_api.LLMSettingsRequest(llm_model="   ")
            )
        assert exc.value.status_code == 400
