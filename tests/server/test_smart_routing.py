"""Tests for the server-side intelligent model routing module.

Covers model inference, the RoutingClient protocol, the default
LLMRoutingClient, and the public ``route_turn`` entry point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.server.smart_routing import (
    LLMRoutingClient,
    RoutingResult,
    _build_rubric,
    infer_models,
    route_turn,
)

# ── Stubs ───────────────────────────────────────────────────────────


@dataclass
class _FakeOutputText:
    text: str
    type: str = "output_text"


@dataclass
class _FakeMessageOutput:
    content: list[_FakeOutputText]
    type: str = "message"


@dataclass
class _FakeResponse:
    """Minimal stub matching omnigent.llms.types.Response."""

    output: list[_FakeMessageOutput]


class _FakeLLMClient:
    """Fake PolicyLLMClient that returns a canned verdict."""

    def __init__(self, verdict: dict[str, Any]) -> None:
        self._verdict = verdict

    async def create(self, **kwargs: Any) -> _FakeResponse:
        text = json.dumps(self._verdict)
        return _FakeResponse(
            output=[_FakeMessageOutput(content=[_FakeOutputText(text=text)])],
        )


class _FakeRoutingClient:
    """Stub RoutingClient for route_turn integration tests."""

    def __init__(self, result: RoutingResult | None) -> None:
        self._result = result

    async def route(self, message: str, available_models: list[str]) -> RoutingResult | None:
        del message, available_models
        return self._result


# ── infer_models ────────────────────────────────────────────────────


def test_infer_models_claude_sdk() -> None:
    """claude-sdk returns the claude model list."""
    models = infer_models("claude-sdk")
    assert models is not None
    assert any("haiku" in m for m in models)
    assert any("opus" in m for m in models)
    # Ordered cheapest → most powerful
    haiku_idx = next(i for i, m in enumerate(models) if "haiku" in m)
    opus_idx = next(i for i, m in enumerate(models) if "opus" in m)
    assert haiku_idx < opus_idx


def test_infer_models_native_harnesses() -> None:
    assert infer_models("claude-native") is not None
    assert infer_models("codex-native") is not None


def test_infer_models_codex() -> None:
    models = infer_models("codex")
    assert models is not None
    assert any("gpt" in m for m in models)


def test_infer_models_openai_agents() -> None:
    assert infer_models("openai-agents") is not None


def test_infer_models_pi() -> None:
    """pi is multi-model — both Claude and GPT."""
    models = infer_models("pi")
    assert models is not None
    assert any("haiku" in m for m in models)
    assert any("gpt" in m for m in models)


def test_infer_models_unknown_harness() -> None:
    assert infer_models("cursor") is None
    assert infer_models("antigravity") is None
    assert infer_models(None) is None


# ── _build_rubric ───────────────────────────────────────────────────


def test_build_rubric_includes_all_models() -> None:
    models = ["databricks-claude-haiku-4-5", "databricks-claude-opus-4-8"]
    rubric = _build_rubric(models)
    assert "databricks-claude-haiku-4-5" in rubric
    assert "databricks-claude-opus-4-8" in rubric
    assert "strict JSON" in rubric
    # Naming conventions are explained
    assert "haiku" in rubric and "opus" in rubric


# ── LLMRoutingClient ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_routing_client_returns_result() -> None:
    verdict = {
        "model": "databricks-claude-opus-4-8",
        "rationale": "hard refactor",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("refactor auth", models)
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"
    assert result.rationale == "hard refactor"
    assert not hasattr(result, "tier")


@pytest.mark.asyncio
async def test_llm_routing_client_clamps_hallucinated_model() -> None:
    verdict = {"model": "hallucinated-model", "rationale": "hard"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hard task", models)
    assert result is not None
    assert result.model == models[0]  # clamped to cheapest


@pytest.mark.asyncio
async def test_llm_routing_client_rejects_empty_model() -> None:
    verdict = {"model": "", "rationale": "x"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hello", models)
    assert result is None


@pytest.mark.asyncio
async def test_llm_routing_client_returns_none_on_error() -> None:
    class _BrokenLLM:
        async def create(self, **kwargs: Any) -> None:
            raise TypeError("boom")

    client = LLMRoutingClient(_BrokenLLM())
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hello", models)
    assert result is None


# ── route_turn (integration) ───────────────────────────────────────


@dataclass
class _FakeCaps:
    routing_client: Any = None  # type: ignore[explicit-any]


@pytest.mark.asyncio
async def test_route_turn_uses_caps_routing_client() -> None:
    expected = RoutingResult(
        model="databricks-claude-haiku-4-5",
        rationale="trivial",
    )
    caps = _FakeCaps(routing_client=_FakeRoutingClient(expected))
    with patch(
        "omnigent.runtime._globals._caps",
        new=caps,
    ):
        model, v = await route_turn("claude-sdk", "hello")
    assert model == "databricks-claude-haiku-4-5"
    assert v is not None
    assert "tier" not in v


@pytest.mark.asyncio
async def test_route_turn_returns_none_when_no_client() -> None:
    caps = _FakeCaps(routing_client=None)
    with patch(
        "omnigent.runtime._globals._caps",
        new=caps,
    ):
        model, _v = await route_turn("claude-sdk", "hello")
    assert model is None


@pytest.mark.asyncio
async def test_route_turn_unknown_harness() -> None:
    model, _v = await route_turn("cursor", "hello")
    assert model is None
    assert _v is None
