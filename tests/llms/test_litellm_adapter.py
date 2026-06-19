"""Tests for llms.adapters.litellm — kwargs building, model resolution,
response normalization, and the lazy-import / call paths (no network)."""

import asyncio
import sys
import types
from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.adapters.litellm import (
    LiteLLMAdapter,
    _resolve_model,
    _to_dict,
)

# ── _resolve_model ───────────────────────────────────────


def test_resolve_model_sdk_mode_passes_through() -> None:
    """Without a base_url (SDK mode) the model is used unchanged."""
    assert _resolve_model("gpt-4o", None) == "gpt-4o"
    assert _resolve_model("anthropic/claude-3-5-sonnet", None) == "anthropic/claude-3-5-sonnet"


def test_resolve_model_proxy_mode_prefixes() -> None:
    """A base_url (proxy mode) prefixes the model with litellm_proxy/."""
    assert _resolve_model("gpt-4o", "http://localhost:4000") == "litellm_proxy/gpt-4o"


def test_resolve_model_proxy_mode_no_double_prefix() -> None:
    """An already-prefixed model is not prefixed twice."""
    assert (
        _resolve_model("litellm_proxy/gpt-4o", "http://localhost:4000") == "litellm_proxy/gpt-4o"
    )


# ── _build_kwargs ────────────────────────────────────────


def _adapter() -> LiteLLMAdapter:
    return LiteLLMAdapter()


def test_build_kwargs_basic_non_streaming() -> None:
    """Base kwargs: model, messages, stream=False, default 120s timeout, no extras."""
    kwargs = _adapter()._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        tools=None,
        stream=False,
        extra={},
        connection_params=None,
        timeout=None,
    )
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["stream"] is False
    assert kwargs["timeout"] == 120
    assert "tools" not in kwargs
    assert "stream_options" not in kwargs
    assert "api_key" not in kwargs
    assert "api_base" not in kwargs


def test_build_kwargs_streaming_defaults() -> None:
    """Streaming sets stream=True, include_usage stream_options, and 300s timeout."""
    kwargs = _adapter()._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        tools=None,
        stream=True,
        extra={},
        connection_params=None,
        timeout=None,
    )
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["timeout"] == 300


def test_build_kwargs_explicit_timeout_wins() -> None:
    """An explicit timeout overrides the streaming/non-streaming default."""
    kwargs = _adapter()._build_kwargs(
        messages=[],
        model="gpt-4o",
        tools=None,
        stream=True,
        extra={},
        connection_params=None,
        timeout=42,
    )
    assert kwargs["timeout"] == 42


def test_build_kwargs_tools_and_extra_passed_through() -> None:
    """Tools and arbitrary extra kwargs are forwarded."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    kwargs = _adapter()._build_kwargs(
        messages=[],
        model="gpt-4o",
        tools=tools,
        stream=False,
        extra={"temperature": 0.7, "reasoning_effort": "high"},
        connection_params=None,
        timeout=None,
    )
    assert kwargs["tools"] == tools
    assert kwargs["temperature"] == 0.7
    assert kwargs["reasoning_effort"] == "high"


def test_build_kwargs_extra_cannot_clobber_control_params() -> None:
    """``extra`` must not override model/messages/stream/timeout."""
    kwargs = _adapter()._build_kwargs(
        messages=[{"role": "user", "content": "real"}],
        model="gpt-4o",
        tools=None,
        stream=False,
        extra={"model": "evil", "stream": True, "messages": [], "timeout": 1},
        connection_params=None,
        timeout=None,
    )
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["messages"] == [{"role": "user", "content": "real"}]
    assert kwargs["stream"] is False
    assert kwargs["timeout"] == 120


def test_build_kwargs_api_key_forwarded() -> None:
    """An api_key in connection_params is forwarded to LiteLLM."""
    kwargs = _adapter()._build_kwargs(
        messages=[],
        model="gpt-4o",
        tools=None,
        stream=False,
        extra={},
        connection_params={"api_key": "sk-test"},
        timeout=None,
    )
    assert kwargs["api_key"] == "sk-test"


def test_build_kwargs_proxy_mode_sets_api_base_and_prefix() -> None:
    """A base_url selects proxy mode: api_base set, model prefixed."""
    kwargs = _adapter()._build_kwargs(
        messages=[],
        model="gpt-4o",
        tools=None,
        stream=False,
        extra={},
        connection_params={"base_url": "http://localhost:4000", "api_key": "sk"},
        timeout=None,
    )
    assert kwargs["api_base"] == "http://localhost:4000"
    assert kwargs["model"] == "litellm_proxy/gpt-4o"
    assert kwargs["api_key"] == "sk"


# ── _to_dict ─────────────────────────────────────────────


def test_to_dict_passthrough_dict() -> None:
    """A plain dict is returned unchanged."""
    d = {"id": "x", "choices": []}
    assert _to_dict(d) is d


def test_to_dict_uses_model_dump() -> None:
    """An object exposing model_dump() is converted via it."""

    class FakeResp:
        def model_dump(self) -> dict[str, Any]:
            return {"id": "from_model_dump"}

    assert _to_dict(FakeResp()) == {"id": "from_model_dump"}


def test_to_dict_falls_back_to_dict_method() -> None:
    """An object exposing only dict() is converted via it."""

    class LegacyResp:
        def dict(self) -> dict[str, Any]:
            return {"id": "from_dict"}

    assert _to_dict(LegacyResp()) == {"id": "from_dict"}


def test_to_dict_raises_on_unconvertible() -> None:
    """An object with no dict conversion raises OmnigentError."""
    with pytest.raises(OmnigentError, match="Unexpected LiteLLM response type"):
        _to_dict(object())


# ── factory wiring ───────────────────────────────────────


def test_get_adapter_returns_litellm_adapter() -> None:
    """The ``litellm`` provider resolves to a LiteLLMAdapter via the factory."""
    # Import the class here (not just at module top) so it binds the same
    # module generation that ``get_adapter`` resolves: a sibling test
    # (test_init_lazy_imports) purges ``omnigent.llms.*`` from sys.modules,
    # which would otherwise leave the top-level import pointing at a stale
    # class object and break the isinstance check.
    from omnigent.llms.adapters import clear_cache, get_adapter
    from omnigent.llms.adapters.litellm import LiteLLMAdapter as FactoryAdapter

    clear_cache()
    adapter = get_adapter("litellm")
    assert isinstance(adapter, FactoryAdapter)


# ── chat_completions: lazy import + call paths ───────────


def test_chat_completions_raises_when_litellm_missing(monkeypatch: Any) -> None:
    """When litellm is not importable, a clear OmnigentError is raised."""
    # Setting the module to None in sys.modules makes ``import litellm`` fail
    # deterministically, regardless of whether the extra is installed.
    monkeypatch.setitem(sys.modules, "litellm", None)

    async def call() -> None:
        await _adapter().chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            tools=None,
            stream=False,
            extra={},
        )

    with pytest.raises(OmnigentError, match="optional 'litellm'"):
        asyncio.run(call())


def _install_fake_litellm(monkeypatch: Any, captured: dict[str, Any], result: Any) -> None:
    """Inject a fake ``litellm`` module whose ``acompletion`` records kwargs."""

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return result

    fake = types.ModuleType("litellm")
    fake.acompletion = fake_acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)


def test_chat_completions_non_streaming_calls_acompletion(monkeypatch: Any) -> None:
    """Non-streaming returns the model_dump()'d response and forwards kwargs."""

    class FakeResp:
        def model_dump(self) -> dict[str, Any]:
            return {"id": "resp-1", "choices": []}

    captured: dict[str, Any] = {}
    _install_fake_litellm(monkeypatch, captured, FakeResp())

    result = asyncio.run(
        _adapter().chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            tools=None,
            stream=False,
            extra={"temperature": 0.5},
            connection_params={"api_key": "sk-xyz"},
        )
    )

    assert result == {"id": "resp-1", "choices": []}
    assert captured["model"] == "gpt-4o"
    assert captured["stream"] is False
    assert captured["temperature"] == 0.5
    assert captured["api_key"] == "sk-xyz"


def test_chat_completions_streaming_yields_chunk_dicts(monkeypatch: Any) -> None:
    """Streaming yields each chunk as a Chat Completions dict."""

    class FakeChunk:
        def __init__(self, content: str) -> None:
            self._content = content

        def model_dump(self) -> dict[str, Any]:
            return {"choices": [{"delta": {"content": self._content}}]}

    async def fake_stream() -> Any:
        for token in ("a", "b", "c"):
            yield FakeChunk(token)

    async def fake_acompletion(**kwargs: Any) -> Any:
        # Streaming acompletion is awaited, then async-iterated.
        return fake_stream()

    fake = types.ModuleType("litellm")
    fake.acompletion = fake_acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    async def run() -> list[dict[str, Any]]:
        iterator = await _adapter().chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-3-5-sonnet",
            tools=None,
            stream=True,
            extra={},
        )
        assert not isinstance(iterator, dict)
        return [chunk async for chunk in iterator]

    chunks = asyncio.run(run())
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["a", "b", "c"]
