"""Tests for the server-side intelligent model routing module.

Covers tier inference, the RoutingClient protocol, the default
LLMRoutingClient, and the public ``route_turn`` entry point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.server.smart_routing import (
    _VALID_TIERS,
    LLMRoutingClient,
    RoutingResult,
    _build_rubric,
    infer_tiers,
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

    async def route(
        self,
        message: str,
        available_tiers: dict[str, list[str]],
    ) -> RoutingResult | None:
        return self._result


# ── infer_tiers ─────────────────────────────────────────────────────


def test_infer_tiers_claude_sdk() -> None:
    """claude-sdk maps to the claude tier template."""
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    assert "cheap" in tiers
    assert "medium" in tiers
    assert "expensive" in tiers
    assert any("haiku" in m for m in tiers["cheap"])
    assert any("opus" in m for m in tiers["expensive"])


def test_infer_tiers_native_harnesses() -> None:
    assert infer_tiers("claude-native") is not None
    assert infer_tiers("codex-native") is not None


def test_infer_tiers_codex() -> None:
    """codex maps to the gpt tier template."""
    tiers = infer_tiers("codex")
    assert tiers is not None
    assert any("gpt" in m for m in tiers["cheap"])
    assert any("gpt-5-5" in m for m in tiers["expensive"])


def test_infer_tiers_openai_agents() -> None:
    tiers = infer_tiers("openai-agents")
    assert tiers is not None


def test_infer_tiers_unknown_harness() -> None:
    """Unknown harnesses return None (not routable)."""
    assert infer_tiers("cursor") is None
    assert infer_tiers("antigravity") is None
    assert infer_tiers(None) is None


# ── _build_rubric ───────────────────────────────────────────────────


def test_build_rubric_includes_all_tiers() -> None:
    tiers = {
        "cheap": ["m-cheap"],
        "medium": ["m-mid"],
        "expensive": ["m-exp"],
    }
    rubric = _build_rubric(tiers)
    assert "m-cheap" in rubric
    assert "m-mid" in rubric
    assert "m-exp" in rubric
    assert "strict JSON" in rubric


# ── LLMRoutingClient ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_routing_client_returns_result() -> None:
    verdict = {
        "tier": "expensive",
        "model": "databricks-claude-opus-4-8",
        "rationale": "hard refactor",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    result = await client.route("refactor auth", tiers)
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"
    assert result.tier == "expensive"
    assert result.rationale == "hard refactor"


@pytest.mark.asyncio
async def test_llm_routing_client_clamps_hallucinated_model() -> None:
    verdict = {
        "tier": "expensive",
        "model": "hallucinated-model",
        "rationale": "hard",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    result = await client.route("hard task", tiers)
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"


@pytest.mark.asyncio
async def test_llm_routing_client_rejects_unknown_tier() -> None:
    verdict = {"tier": "gigantic", "model": "m", "rationale": "x"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    result = await client.route("hello", tiers)
    assert result is None


@pytest.mark.asyncio
async def test_llm_routing_client_rejects_empty_model() -> None:
    verdict = {"tier": "cheap", "model": "", "rationale": "x"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    result = await client.route("hello", tiers)
    assert result is None


@pytest.mark.asyncio
async def test_llm_routing_client_returns_none_on_error() -> None:
    """LLM errors produce None (fail-open)."""

    class _BrokenLLM:
        async def create(self, **kwargs: Any) -> None:
            raise TypeError("boom")

    client = LLMRoutingClient(_BrokenLLM())
    tiers = infer_tiers("claude-sdk")
    assert tiers is not None
    result = await client.route("hello", tiers)
    assert result is None


# ── route_turn (integration) ───────────────────────────────────────


@dataclass
class _FakeCaps:
    routing_client: Any = None  # type: ignore[explicit-any]


@pytest.mark.asyncio
async def test_route_turn_uses_caps_routing_client() -> None:
    expected = RoutingResult(
        model="databricks-claude-haiku-4-5",
        tier="cheap",
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
    assert v["tier"] == "cheap"


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


def test_valid_tiers_constant() -> None:
    assert frozenset({"cheap", "medium", "expensive"}) == _VALID_TIERS
