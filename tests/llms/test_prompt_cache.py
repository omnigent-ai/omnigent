"""Tests for provider-neutral prompt caching (``omnigent.llms.prompt_cache``).

Provider HTTP is replaced by an ``httpx.MockTransport`` that records the exact
request bytes, so payload assertions are byte-level and no live provider is
contacted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.codex_executor import (
    CodexExecutor,
    _codex_turn_usage_from_totals,
    _extract_codex_last_turn_usage,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.llms.client import Client
from omnigent.llms.context_window import ModelPricing, compute_llm_cost
from omnigent.llms.prompt_cache import (
    NATIVE_HARNESS_CAPABILITY,
    PROMPT_CACHE_ENV_VAR,
    PromptCacheMechanism,
    PromptCacheMode,
    PromptCacheObservation,
    PromptCacheOutcome,
    PromptCachePolicy,
    PromptCacheReason,
    apply_anthropic_cache_control,
    observe_harness_usage,
    openai_prompt_cache_key,
    plan_prompt_cache,
    resolve_prompt_cache_policy,
    stable_prefix_digest,
)
from omnigent.llms.types import Response, ResponseCompletedEvent, Usage
from omnigent.runtime.telemetry import record_prompt_cache

_INSTRUCTIONS = "You are a careful assistant. Follow the house style guide."
_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a record.",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save",
            "description": "Save a record.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
_SECRET_PROMPT = "user secret: tangerine-4471"


def _input(text: str = _SECRET_PROMPT) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


def _anthropic_body(**usage: int) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "model": "claude-test",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 12, "output_tokens": 3, **usage},
    }


def _openai_body(cached: int | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": 2000, "output_tokens": 10, "total_tokens": 2010}
    if cached is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return {
        "model": "gpt-test",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
        ],
        "usage": usage,
    }


@dataclass
class _Provider:
    """Records request bodies and replays scripted responses."""

    responses: list[httpx.Response]
    bodies: list[bytes] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def json_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(body) for body in self.bodies]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Provider]:
    real_async_client = httpx.AsyncClient

    def _install(*responses: httpx.Response) -> _Provider:
        recorder = _Provider(responses=list(responses))
        transport = httpx.MockTransport(recorder.handle)

        def _factory(**kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        return recorder

    monkeypatch.delenv(PROMPT_CACHE_ENV_VAR, raising=False)
    return _install


async def _create(**kwargs: Any) -> Response:
    result = await Client().responses.create(**kwargs)
    assert isinstance(result, Response)
    return result


# ── Policy resolution ────────────────────────────────────────


def test_policy_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROMPT_CACHE_ENV_VAR, raising=False)
    assert resolve_prompt_cache_policy(None) == PromptCachePolicy(PromptCacheMode.DISABLED)


def test_policy_reads_env_and_explicit_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROMPT_CACHE_ENV_VAR, "Opportunistic")
    assert resolve_prompt_cache_policy(None).enabled
    assert not resolve_prompt_cache_policy("disabled").enabled
    assert resolve_prompt_cache_policy(PromptCacheMode.OPPORTUNISTIC).enabled


def test_unknown_policy_value_fails_open_to_disabled() -> None:
    assert resolve_prompt_cache_policy("always-please") == PromptCachePolicy()


# ── Payloads: disabled is byte-identical ─────────────────────


@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_disabled_payload_is_byte_identical(
    provider: Callable[..., _Provider],
    stream: bool,
) -> None:
    if stream:
        sse = (
            'data: {"type":"message_start","message":{"id":"m","model":"claude-test",'
            '"usage":{"input_tokens":5}}}\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":1}}\n'
        )
        recorder = provider(httpx.Response(200, text=sse))
    else:
        recorder = provider(httpx.Response(200, json=_anthropic_body()))
    kwargs: dict[str, Any] = {
        "input": _input(),
        "instructions": _INSTRUCTIONS,
        "model": "anthropic/claude-test",
        "tools": _TOOLS,
        "stream": stream,
        "connection_params": {"api_key": "k"},
    }
    for policy in (None, "disabled", PromptCachePolicy()):
        result = await Client().responses.create(**kwargs, prompt_cache=policy)
        if stream:
            assert not isinstance(result, Response)
            async for _ in result:
                pass
    assert len(recorder.bodies) == 3
    assert len(set(recorder.bodies)) == 1
    assert b"cache_control" not in recorder.bodies[0]


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_disabled_payload_is_byte_identical(
    provider: Callable[..., _Provider],
    stream: bool,
) -> None:
    if stream:
        sse = f"event: response.completed\ndata: {json.dumps({'response': _openai_body()})}\n\n"
        recorder = provider(httpx.Response(200, text=sse))
    else:
        recorder = provider(httpx.Response(200, json=_openai_body()))
    kwargs: dict[str, Any] = {
        "input": _input(),
        "instructions": _INSTRUCTIONS,
        "model": "openai/gpt-test",
        "tools": _TOOLS,
        "stream": stream,
        "connection_params": {"api_key": "k"},
    }
    for policy in (None, "disabled"):
        result = await Client().responses.create(**kwargs, prompt_cache=policy)
        if stream:
            assert not isinstance(result, Response)
            async for _ in result:
                pass
    assert len(recorder.bodies) == 2
    assert recorder.bodies[0] == recorder.bodies[1]
    assert b"prompt_cache" not in recorder.bodies[0]


# ── Anthropic explicit breakpoints ───────────────────────────


async def test_anthropic_opportunistic_marks_stable_prefix_only(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(
        httpx.Response(200, json=_anthropic_body()),
        httpx.Response(
            200, json=_anthropic_body(cache_read_input_tokens=1800, cache_creation_input_tokens=0)
        ),
    )
    first = await _create(
        input=_input("first question"),
        instructions=_INSTRUCTIONS,
        model="anthropic/claude-test",
        tools=_TOOLS,
        connection_params={"api_key": "k"},
        prompt_cache="opportunistic",
    )
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]
    second = await _create(
        input=history,
        instructions=_INSTRUCTIONS,
        model="anthropic/claude-test",
        tools=_TOOLS,
        connection_params={"api_key": "k"},
        prompt_cache="opportunistic",
    )

    first_body, second_body = recorder.json_bodies()
    cache_control = {"type": "ephemeral"}
    for body in (first_body, second_body):
        assert body["system"] == [
            {"type": "text", "text": _INSTRUCTIONS, "cache_control": cache_control}
        ]
        assert "cache_control" not in body["tools"][0]
        assert body["tools"][-1]["cache_control"] == cache_control
        assert "cache_control" not in json.dumps(body["messages"])
    # The dynamic suffix grows while the cached prefix bytes stay identical.
    assert first_body["tools"] == second_body["tools"]
    assert first_body["system"] == second_body["system"]
    assert first_body["messages"] == second_body["messages"][:1]

    assert first.prompt_cache is not None
    assert first.prompt_cache.outcome is PromptCacheOutcome.MISS
    assert second.prompt_cache == PromptCacheObservation(
        mechanism=PromptCacheMechanism.EXPLICIT_BREAKPOINTS,
        outcome=PromptCacheOutcome.HIT,
        controllable=True,
        reason=PromptCacheReason.APPLIED,
        cache_read_input_tokens=1800,
        cache_creation_input_tokens=0,
    )


def test_apply_anthropic_cache_control_does_not_mutate_input() -> None:
    payload = {"system": "sys", "tools": [{"name": "t", "input_schema": {}}], "messages": []}
    snapshot = json.dumps(payload)
    cached = apply_anthropic_cache_control(payload)
    assert json.dumps(payload) == snapshot
    assert cached["tools"][0]["cache_control"] == {"type": "ephemeral"}


def test_apply_anthropic_cache_control_without_stable_content_is_noop() -> None:
    payload = {"model": "claude-test", "messages": [{"role": "user", "content": "hi"}]}
    assert apply_anthropic_cache_control(payload) == payload


async def test_anthropic_rejected_breakpoints_fail_open(
    provider: Callable[..., _Provider],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = provider(
        httpx.Response(
            400,
            json={"error": {"message": "tools.1.cache_control: Extra inputs are not permitted"}},
        ),
        httpx.Response(200, json=_anthropic_body()),
    )
    with caplog.at_level(logging.DEBUG):
        result = await _create(
            input=_input(),
            instructions=_INSTRUCTIONS,
            model="anthropic/claude-test",
            tools=_TOOLS,
            connection_params={"api_key": "k", "base_url": "https://gateway.example/v1"},
            prompt_cache="opportunistic",
        )
    cached_body, plain_body = recorder.bodies
    assert b"cache_control" in cached_body
    assert b"cache_control" not in plain_body
    assert result.prompt_cache is not None
    assert result.prompt_cache.outcome is PromptCacheOutcome.BYPASS
    assert result.prompt_cache.reason is PromptCacheReason.PROVIDER_REJECTED
    assert _SECRET_PROMPT not in caplog.text
    assert _INSTRUCTIONS not in caplog.text


async def test_anthropic_stream_rejected_breakpoints_fail_open(
    provider: Callable[..., _Provider],
) -> None:
    sse = (
        'data: {"type":"message_start","message":{"id":"m","model":"claude-test",'
        '"usage":{"input_tokens":5,"cache_read_input_tokens":0}}}\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":1}}\n'
    )
    recorder = provider(
        httpx.Response(400, text='{"error":{"message":"cache_control not supported"}}'),
        httpx.Response(200, text=sse),
    )
    stream = await Client().responses.create(
        input=_input(),
        instructions=_INSTRUCTIONS,
        model="anthropic/claude-test",
        tools=_TOOLS,
        stream=True,
        connection_params={"api_key": "k"},
        prompt_cache="opportunistic",
    )
    assert not isinstance(stream, Response)
    completed = [event async for event in stream if isinstance(event, ResponseCompletedEvent)]
    assert len(recorder.bodies) == 2
    assert b"cache_control" not in recorder.bodies[1]
    observation = completed[0].response.prompt_cache
    assert observation is not None
    assert observation.reason is PromptCacheReason.PROVIDER_REJECTED
    assert observation.outcome is PromptCacheOutcome.BYPASS


async def test_anthropic_unrelated_error_is_not_retried(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(httpx.Response(400, json={"error": {"message": "max_tokens too big"}}))
    with pytest.raises(httpx.HTTPStatusError):
        await _create(
            input=_input(),
            instructions=_INSTRUCTIONS,
            model="anthropic/claude-test",
            connection_params={"api_key": "k"},
            prompt_cache="opportunistic",
        )
    assert len(recorder.bodies) == 1


# ── OpenAI automatic prefix caching ──────────────────────────


async def test_openai_opportunistic_adds_only_supported_cache_key(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(
        httpx.Response(200, json=_openai_body()),
        httpx.Response(200, json=_openai_body(cached=1792)),
    )
    common: dict[str, Any] = {
        "instructions": _INSTRUCTIONS,
        "model": "openai/gpt-test",
        "tools": _TOOLS,
        "connection_params": {"api_key": "k"},
        "prompt_cache": "opportunistic",
    }
    await _create(input=_input("turn one"), **common)
    second = await _create(input=_input("turn two, different suffix"), **common)

    first_body, second_body = recorder.json_bodies()
    added = set(first_body) - {"model", "input", "instructions", "tools"}
    assert added == {"prompt_cache_key"}
    assert "prompt_cache_retention" not in first_body
    # Stable prefix fields keep request order so automatic prefix reuse still applies.
    assert first_body["instructions"] == second_body["instructions"]
    assert first_body["tools"] == second_body["tools"]
    assert first_body["prompt_cache_key"] == second_body["prompt_cache_key"]
    assert list(first_body)[:4] == ["model", "input", "instructions", "tools"]

    assert second.usage is not None
    assert second.usage.input_tokens_include_cache
    assert second.prompt_cache == PromptCacheObservation(
        mechanism=PromptCacheMechanism.AUTOMATIC_PREFIX,
        outcome=PromptCacheOutcome.HIT,
        controllable=True,
        reason=PromptCacheReason.APPLIED,
        cache_read_input_tokens=1792,
    )


def test_openai_payload_fields_are_supported_by_pinned_sdk() -> None:
    import inspect

    from openai.resources.responses import AsyncResponses

    parameters = inspect.signature(AsyncResponses.create).parameters
    assert "prompt_cache_key" in parameters


async def test_openai_caller_supplied_cache_key_is_kept(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(httpx.Response(200, json=_openai_body()))
    await _create(
        input=_input(),
        instructions=_INSTRUCTIONS,
        model="openai/gpt-test",
        connection_params={"api_key": "k"},
        prompt_cache="opportunistic",
        prompt_cache_key="caller-key",
    )
    assert recorder.json_bodies()[0]["prompt_cache_key"] == "caller-key"


async def test_openai_custom_endpoint_is_observe_only(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(httpx.Response(200, json=_openai_body(cached=0)))
    result = await _create(
        input=_input(),
        instructions=_INSTRUCTIONS,
        model="openai/gpt-test",
        connection_params={"api_key": "k", "base_url": "https://proxy.example/v1"},
        prompt_cache="opportunistic",
    )
    assert b"prompt_cache" not in recorder.bodies[0]
    assert result.prompt_cache is not None
    assert result.prompt_cache.controllable is False
    assert result.prompt_cache.reason is PromptCacheReason.CUSTOM_ENDPOINT
    assert result.prompt_cache.outcome is PromptCacheOutcome.MISS


async def test_openai_rejected_cache_key_fail_open(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(
        httpx.Response(400, json={"error": {"message": "Unknown parameter: 'prompt_cache_key'."}}),
        httpx.Response(200, json=_openai_body()),
    )
    result = await _create(
        input=_input(),
        instructions=_INSTRUCTIONS,
        model="openai/gpt-test",
        connection_params={"api_key": "k"},
        prompt_cache="opportunistic",
    )
    assert b"prompt_cache_key" in recorder.bodies[0]
    assert b"prompt_cache_key" not in recorder.bodies[1]
    assert result.prompt_cache is not None
    assert result.prompt_cache.reason is PromptCacheReason.PROVIDER_REJECTED


# ── Digest / key stability ───────────────────────────────────


def test_digest_changes_with_stable_content_only() -> None:
    base = stable_prefix_digest(_INSTRUCTIONS, _TOOLS)
    assert stable_prefix_digest(_INSTRUCTIONS, _TOOLS) == base
    assert stable_prefix_digest(_INSTRUCTIONS + " v2", _TOOLS) != base
    assert stable_prefix_digest(_INSTRUCTIONS, _TOOLS[:1]) != base
    assert openai_prompt_cache_key(_INSTRUCTIONS, _TOOLS) != openai_prompt_cache_key(
        "other instructions", _TOOLS
    )


async def test_openai_key_ignores_dynamic_suffix_and_tracks_stable_prefix(
    provider: Callable[..., _Provider],
) -> None:
    recorder = provider(httpx.Response(200, json=_openai_body()))
    common: dict[str, Any] = {
        "model": "openai/gpt-test",
        "tools": _TOOLS,
        "connection_params": {"api_key": "k"},
        "prompt_cache": "opportunistic",
    }
    await _create(input=_input("a"), instructions=_INSTRUCTIONS, **common)
    await _create(input=_input("b" * 500), instructions=_INSTRUCTIONS, **common)
    await _create(input=_input("a"), instructions=_INSTRUCTIONS + " (v2)", **common)
    keys = [body["prompt_cache_key"] for body in recorder.json_bodies()]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    assert all(_INSTRUCTIONS not in key and len(key) <= 64 for key in keys)


# ── Unsupported providers ────────────────────────────────────


def test_unsupported_provider_bypasses() -> None:
    plan = plan_prompt_cache("gemini", PromptCachePolicy(PromptCacheMode.OPPORTUNISTIC))
    assert not plan.apply
    assert plan.reason is PromptCacheReason.UNSUPPORTED_PROVIDER


async def test_unsupported_provider_request_is_unchanged(
    provider: Callable[..., _Provider],
) -> None:
    body = {
        "id": "c",
        "model": "llama-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    recorder = provider(httpx.Response(200, json=body))
    for policy in ("disabled", "opportunistic"):
        result = await _create(
            input=_input(),
            instructions=_INSTRUCTIONS,
            model="groq/llama-test",
            tools=_TOOLS,
            connection_params={"api_key": "k"},
            prompt_cache=policy,
        )
        assert result.prompt_cache is not None
        assert result.prompt_cache.outcome is PromptCacheOutcome.BYPASS
        assert result.prompt_cache.mechanism is PromptCacheMechanism.NONE
    assert recorder.bodies[0] == recorder.bodies[1]


# ── Accounting ───────────────────────────────────────────────


def test_anthropic_cache_usage_prices_read_and_write_separately() -> None:
    usage = Usage(
        input_tokens=100,
        output_tokens=10,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=400,
    )
    pricing = ModelPricing(
        input_per_token=1.0,
        output_per_token=2.0,
        cache_read_per_token=0.1,
        cache_write_per_token=1.25,
    )
    assert usage.to_cost_usage() == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 400,
    }
    assert compute_llm_cost(usage.to_cost_usage(), pricing) == pytest.approx(100 + 20 + 100 + 500)


def test_openai_inclusive_cached_tokens_are_not_double_billed() -> None:
    usage = Usage(
        input_tokens=2000,
        output_tokens=10,
        cache_read_input_tokens=1792,
        input_tokens_include_cache=True,
    )
    assert usage.to_cost_usage() == {
        "input_tokens": 208,
        "output_tokens": 10,
        "cache_read_input_tokens": 1792,
    }


# ── Native harnesses: observable, not controllable ───────────


@pytest.mark.parametrize(
    "executor_cls",
    [ClaudeNativeExecutor, ClaudeSDKExecutor, CodexExecutor, CodexNativeExecutor],
)
def test_native_harness_capability_is_observable_not_controllable(
    executor_cls: type[Executor],
) -> None:
    capability = executor_cls.prompt_cache_capability(object.__new__(executor_cls))
    assert capability == NATIVE_HARNESS_CAPABILITY
    assert capability.observable is True
    assert capability.controllable is False


def test_base_executor_declares_no_capability() -> None:
    assert Executor().prompt_cache_capability() is None


def test_claude_usage_maps_to_observation() -> None:
    observation = observe_harness_usage(
        {
            "input_tokens": 12,
            "output_tokens": 40,
            "cache_read_input_tokens": 30000,
            "cache_creation_input_tokens": 512,
        }
    )
    assert observation == PromptCacheObservation(
        mechanism=PromptCacheMechanism.VENDOR_MANAGED,
        outcome=PromptCacheOutcome.HIT,
        controllable=False,
        reason=PromptCacheReason.VENDOR_MANAGED,
        cache_read_input_tokens=30000,
        cache_creation_input_tokens=512,
    )
    write_only = observe_harness_usage({"input_tokens": 12, "cache_creation_input_tokens": 900})
    assert write_only.outcome is PromptCacheOutcome.WRITE


def test_codex_usage_maps_to_observation() -> None:
    last = _extract_codex_last_turn_usage(
        {"tokenUsage": {"last": {"inputTokens": 5000, "cachedInputTokens": 4096}}},
        "gpt-test",
    )
    assert last is not None
    observation = observe_harness_usage(last)
    assert observation.outcome is PromptCacheOutcome.HIT
    assert observation.cache_read_input_tokens == 4096
    assert observation.cache_creation_input_tokens is None

    uncached = _codex_turn_usage_from_totals(
        {"inputTokens": 900, "cachedInputTokens": 0, "outputTokens": 5, "totalTokens": 905},
        None,
        "gpt-test",
    )
    miss = observe_harness_usage(uncached)
    assert miss.outcome is PromptCacheOutcome.MISS
    assert miss.cache_read_input_tokens == 0


def test_codex_native_cumulative_usage_maps_to_observation() -> None:
    observation = observe_harness_usage(
        {"cumulative_input_tokens": 9000, "cumulative_cache_read_input_tokens": 7000}
    )
    assert observation.cache_read_input_tokens == 7000
    assert observation.outcome is PromptCacheOutcome.HIT


def test_harness_without_usage_is_unknown() -> None:
    observation = observe_harness_usage(None)
    assert observation.outcome is PromptCacheOutcome.UNKNOWN
    assert observation.reason is PromptCacheReason.NO_USAGE


# ── Telemetry safety ─────────────────────────────────────────


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


async def test_telemetry_and_logs_carry_no_prompt_or_cache_key(
    provider: Callable[..., _Provider],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = provider(httpx.Response(200, json=_openai_body(cached=1024)))
    with caplog.at_level(logging.DEBUG):
        result = await _create(
            input=_input(),
            instructions=_INSTRUCTIONS,
            model="openai/gpt-test",
            tools=_TOOLS,
            connection_params={"api_key": "k"},
            prompt_cache="opportunistic",
        )
    key = recorder.json_bodies()[0]["prompt_cache_key"]
    assert result.prompt_cache is not None
    span = _RecordingSpan()
    record_prompt_cache(span, result.prompt_cache)  # type: ignore[arg-type]

    assert span.attributes == {
        "omnigent.prompt_cache.mechanism": "automatic_prefix",
        "omnigent.prompt_cache.outcome": "hit",
        "omnigent.prompt_cache.controllable": True,
        "omnigent.prompt_cache.reason": "applied",
        "omnigent.prompt_cache.read_tokens": 1024,
    }
    exported = json.dumps(span.attributes) + repr(result.prompt_cache) + caplog.text
    digest = stable_prefix_digest(_INSTRUCTIONS, _TOOLS)
    for sensitive in (_SECRET_PROMPT, _INSTRUCTIONS, key, digest, digest[:32]):
        assert sensitive not in exported


def test_executor_adapter_records_cache_only_for_declaring_executors() -> None:
    from omnigent.runtime.harnesses._executor_adapter import _record_harness_prompt_cache

    usage = {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 700}
    native = object.__new__(CodexExecutor)
    span = _RecordingSpan()
    _record_harness_prompt_cache(span, native, usage)
    assert span.attributes["omnigent.prompt_cache.mechanism"] == "vendor_managed"
    assert span.attributes["omnigent.prompt_cache.controllable"] is False
    assert span.attributes["omnigent.prompt_cache.read_tokens"] == 700

    undeclared = _RecordingSpan()
    _record_harness_prompt_cache(undeclared, Executor(), usage)
    assert undeclared.attributes == {}
