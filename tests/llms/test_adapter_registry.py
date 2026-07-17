"""Tests for llms.adapters.get_adapter — provider → adapter resolution."""

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.adapters import clear_cache, get_adapter
from omnigent.llms.adapters.openai import OpenAIAdapter, OpenAICompatibleAdapter


@pytest.fixture(autouse=True)
def _clear_adapter_cache() -> None:
    clear_cache()
    yield
    clear_cache()


def test_llama_server_resolves_to_chat_completions_adapter() -> None:
    """llama.cpp's llama-server speaks Chat Completions, not the Responses
    API, so it must resolve to OpenAICompatibleAdapter (like Ollama) rather
    than the OpenAIAdapter used for provider ``openai``."""
    adapter = get_adapter("llama-server")

    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert not isinstance(adapter, OpenAIAdapter)
    assert adapter._base_url == "http://localhost:8080/v1"


def test_llama_server_base_url_override() -> None:
    adapter = get_adapter("llama-server", base_url="http://192.168.1.5:9000/v1")

    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._base_url == "http://192.168.1.5:9000/v1"


def test_openai_still_uses_responses_adapter() -> None:
    """Regression guard: adding llama-server must not change how the bare
    ``openai`` provider resolves (it keeps the Responses-API adapter)."""
    assert isinstance(get_adapter("openai"), OpenAIAdapter)


def test_unknown_provider_raises() -> None:
    with pytest.raises(OmnigentError, match="Unknown provider 'not-a-provider'"):
        get_adapter("not-a-provider")
