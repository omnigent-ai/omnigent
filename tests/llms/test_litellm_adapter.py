"""Tests for llms.adapters.litellm — kwargs building, response normalization,
SDK/proxy dispatch, streaming, tool pass-through, and the optional-dependency
import guard.

litellm is an optional dependency and is not installed in the test environment,
so the call-path tests inject a fake ``litellm`` module via ``sys.modules``.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.adapters.litellm import LiteLLMAdapter, _to_chat_dict

# ── _build_kwargs ─────────────────────────────────────────


def test_build_kwargs_sdk_mode_minimal() -> None:
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs(
        [{"role": "user", "content": "hi"}], "gpt-4o", None, False, {}, None, None
    )
    assert kw["model"] == "gpt-4o"
    assert kw["messages"] == [{"role": "user", "content": "hi"}]
    assert kw["stream"] is False
    assert "tools" not in kw
    # SDK mode: no base_url, so litellm resolves the endpoint from the model.
    assert "api_base" not in kw
    assert "api_key" not in kw
    assert kw["timeout"] == 120  # non-streaming default
    assert kw["drop_params"] is True
    assert "stream_options" not in kw  # only requested for streaming calls


def test_build_kwargs_proxy_mode_and_credentials() -> None:
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs(
        [{"role": "user", "content": "hi"}],
        "gpt-4o",
        None,
        True,
        {},
        {"api_key": "sk-x", "base_url": "http://proxy:4000"},
        30,
    )
    # Proxy mode: connection base_url maps to litellm's api_base.
    assert kw["api_base"] == "http://proxy:4000"
    assert kw["api_key"] == "sk-x"
    assert kw["stream"] is True
    assert kw["timeout"] == 30  # explicit override wins over the default


def test_build_kwargs_default_base_url_from_init_is_stripped() -> None:
    adapter = LiteLLMAdapter(base_url="http://default-proxy:4000/")
    kw = adapter._build_kwargs([], "m", None, False, {}, None, None)
    assert kw["api_base"] == "http://default-proxy:4000"  # trailing slash stripped


def test_build_kwargs_connection_base_url_overrides_init_default() -> None:
    adapter = LiteLLMAdapter(base_url="http://default:4000")
    kw = adapter._build_kwargs(
        [], "m", None, False, {}, {"base_url": "http://override:9000"}, None
    )
    assert kw["api_base"] == "http://override:9000"


def test_build_kwargs_tools_passed_through() -> None:
    adapter = LiteLLMAdapter()
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    kw = adapter._build_kwargs([], "m", tools, False, {}, None, None)
    assert kw["tools"] == tools


def test_build_kwargs_extra_passthrough_but_explicit_args_win() -> None:
    adapter = LiteLLMAdapter()
    # `extra` carries provider kwargs but must NOT override the contract args.
    kw = adapter._build_kwargs(
        [{"role": "user", "content": "real"}],
        "real-model",
        None,
        False,
        {"temperature": 0.7, "model": "SHOULD-BE-IGNORED", "messages": "IGNORED"},
        None,
        None,
    )
    assert kw["temperature"] == 0.7
    assert kw["model"] == "real-model"
    assert kw["messages"] == [{"role": "user", "content": "real"}]


def test_build_kwargs_drop_params_overridable_via_extra() -> None:
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs([], "m", None, False, {"drop_params": False}, None, None)
    assert kw["drop_params"] is False


def test_build_kwargs_streaming_default_timeout() -> None:
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs([], "m", None, True, {}, None, None)
    assert kw["timeout"] == 300  # streaming default


def test_build_kwargs_streaming_requests_usage() -> None:
    # Streaming must ask for usage so token telemetry is captured (the reducer
    # only reads usage from a usage-bearing chunk), mirroring the OpenAI adapter.
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs([], "m", None, True, {}, None, None)
    assert kw["stream_options"] == {"include_usage": True}


def test_build_kwargs_stream_options_overridable_via_extra() -> None:
    adapter = LiteLLMAdapter()
    kw = adapter._build_kwargs([], "m", None, True, {"stream_options": {}}, None, None)
    assert kw["stream_options"] == {}


# ── _to_chat_dict ─────────────────────────────────────────


def test_to_chat_dict_passes_dict_through() -> None:
    assert _to_chat_dict({"choices": []}) == {"choices": []}


def test_to_chat_dict_unwraps_model_dump() -> None:
    class Resp:
        def model_dump(self) -> dict[str, Any]:
            return {"id": "x", "choices": [{"message": {"content": "hi"}}]}

    assert _to_chat_dict(Resp()) == {"id": "x", "choices": [{"message": {"content": "hi"}}]}


def test_to_chat_dict_unwraps_to_dict_fallback() -> None:
    class Resp:
        def to_dict(self) -> dict[str, Any]:
            return {"k": "v"}

    assert _to_chat_dict(Resp()) == {"k": "v"}


# ── litellm injection helper ──────────────────────────────


def _install_fake_litellm(monkeypatch: pytest.MonkeyPatch, *, acompletion: Any) -> None:
    """Inject a fake ``litellm`` module exposing ``acompletion``."""
    fake = types.ModuleType("litellm")
    fake.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)


# ── import guard ──────────────────────────────────────────


def test_missing_litellm_raises_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # `sys.modules[name] = None` makes `import name` raise ImportError, so the
    # guard fires deterministically whether or not litellm is installed.
    monkeypatch.setitem(sys.modules, "litellm", None)
    adapter = LiteLLMAdapter()
    with pytest.raises(OmnigentError) as exc:
        asyncio.run(
            adapter.chat_completions(
                [{"role": "user", "content": "hi"}], "gpt-4o", None, False, {}
            )
        )
    assert "omnigent[litellm]" in str(exc.value)


# ── call path (non-streaming) ─────────────────────────────


def test_non_streaming_returns_normalized_dict_and_passes_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResp:
        def model_dump(self) -> dict[str, Any]:
            return {"id": "r1", "choices": [{"message": {"content": "answer"}}]}

    async def fake_acompletion(**kwargs: Any) -> FakeResp:
        captured.update(kwargs)
        return FakeResp()

    _install_fake_litellm(monkeypatch, acompletion=fake_acompletion)

    adapter = LiteLLMAdapter()
    tools = [{"type": "function", "function": {"name": "f"}}]
    result = asyncio.run(
        adapter.chat_completions(
            [{"role": "user", "content": "hi"}],
            "gpt-4o",
            tools,
            False,
            {"temperature": 0.1},
            connection_params={"api_key": "sk", "base_url": "http://proxy:4000"},
            timeout=42,
        )
    )
    assert result == {"id": "r1", "choices": [{"message": {"content": "answer"}}]}
    assert captured["model"] == "gpt-4o"
    assert captured["stream"] is False
    assert captured["tools"] == tools  # tool/function-call pass-through
    assert captured["api_base"] == "http://proxy:4000"
    assert captured["api_key"] == "sk"
    assert captured["timeout"] == 42
    assert captured["temperature"] == 0.1


# ── call path (streaming) ─────────────────────────────────


def test_streaming_yields_normalized_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeChunk:
        def __init__(self, text: str) -> None:
            self._text = text

        def model_dump(self) -> dict[str, Any]:
            return {"choices": [{"delta": {"content": self._text}}]}

    class FakeStream:
        def __init__(self) -> None:
            self._chunks = iter([FakeChunk("he"), FakeChunk("llo")])

        def __aiter__(self) -> FakeStream:
            return self

        async def __anext__(self) -> FakeChunk:
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration from None

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> FakeStream:
        captured.update(kwargs)
        return FakeStream()

    _install_fake_litellm(monkeypatch, acompletion=fake_acompletion)

    adapter = LiteLLMAdapter()

    async def _collect() -> list[dict[str, Any]]:
        stream = await adapter.chat_completions(
            [{"role": "user", "content": "hi"}], "gpt-4o", None, True, {}
        )
        return [chunk async for chunk in stream]  # type: ignore[union-attr]

    chunks = asyncio.run(_collect())
    assert chunks == [
        {"choices": [{"delta": {"content": "he"}}]},
        {"choices": [{"delta": {"content": "llo"}}]},
    ]
    assert captured["stream"] is True
