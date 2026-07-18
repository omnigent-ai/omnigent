"""Tests for the server-side intelligent model routing module.

Covers model inference, the RoutingClient protocol, the default
LLMRoutingClient, and the public ``route_turn`` entry point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.server.smart_routing import (
    LLMRoutingClient,
    RoutingResult,
    _build_rubric,
    fetch_runner_models,
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

    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
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
    available = {
        "claude-sdk": ["databricks-claude-haiku-4-5", "databricks-claude-opus-4-8"],
    }
    rubric = _build_rubric(available)
    assert "databricks-claude-haiku-4-5" in rubric
    assert "databricks-claude-opus-4-8" in rubric
    assert "strict JSON" in rubric
    assert "haiku" in rubric and "opus" in rubric


def test_build_rubric_shows_harness_names() -> None:
    available = {
        "claude-sdk": ["databricks-claude-haiku-4-5"],
        "codex": ["databricks-gpt-5-4-nano"],
    }
    rubric = _build_rubric(available)
    assert "claude-sdk" in rubric
    assert "codex" in rubric
    assert "databricks-claude-haiku-4-5" in rubric
    assert "databricks-gpt-5-4-nano" in rubric


# ── LLMRoutingClient ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_routing_client_returns_result() -> None:
    verdict = {
        "harness": "claude-sdk",
        "model": "databricks-claude-opus-4-8",
        "rationale": "hard refactor",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("refactor auth", {"claude-sdk": models})
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"
    assert result.rationale == "hard refactor"
    assert result.harness == "claude-sdk"


@pytest.mark.asyncio
async def test_llm_routing_client_harness_mismatch_re_resolves() -> None:
    """If the judge picks a harness that doesn't own the model, fall back."""
    claude_models = infer_models("claude-sdk")
    assert claude_models is not None
    verdict = {
        "harness": "codex",  # codex doesn't have claude models
        "model": "databricks-claude-opus-4-8",
        "rationale": "deep reasoning",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    result = await client.route(
        "hard task", {"claude-sdk": claude_models, "codex": ["databricks-gpt-5-4"]}
    )
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"
    # harness re-resolved to the one that owns the model
    assert result.harness == "claude-sdk"


@pytest.mark.asyncio
async def test_llm_routing_client_unknown_harness_re_resolves() -> None:
    """If the judge returns an unrecognised harness, fall back to model ownership."""
    models = infer_models("claude-sdk")
    assert models is not None
    verdict = {
        "harness": "hallucinated-harness",
        "model": "databricks-claude-haiku-4-5",
        "rationale": "simple task",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    result = await client.route("hello", {"claude-sdk": models})
    assert result is not None
    assert result.model == "databricks-claude-haiku-4-5"
    assert result.harness == "claude-sdk"


@pytest.mark.asyncio
async def test_llm_routing_client_clamps_hallucinated_model() -> None:
    verdict = {"harness": "claude-sdk", "model": "hallucinated-model", "rationale": "hard"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hard task", {"claude-sdk": models})
    assert result is not None
    assert result.model == models[0]  # clamped to cheapest


@pytest.mark.asyncio
async def test_llm_routing_client_rejects_empty_model() -> None:
    verdict = {"harness": "claude-sdk", "model": "", "rationale": "x"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hello", {"claude-sdk": models})
    assert result is None


@pytest.mark.asyncio
async def test_llm_routing_client_returns_none_on_error() -> None:
    class _BrokenLLM:
        async def create(self, **kwargs: Any) -> None:
            raise TypeError("boom")

    client = LLMRoutingClient(_BrokenLLM())
    models = infer_models("claude-sdk")
    assert models is not None
    result = await client.route("hello", {"claude-sdk": models})
    assert result is None


# ── fetch_runner_models ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_runner_models_parses_catalog() -> None:
    catalog_payload = {
        "workers": {
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-claude-haiku-4-5", "family": "claude"},
                    {"id": "databricks-claude-opus-4-8", "family": "claude"},
                ],
                "note": "",
            },
            "claude_code": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-claude-haiku-4-5", "family": "claude"},
                    {"id": "databricks-claude-sonnet-4-6", "family": "claude"},
                ],
                "note": "",
            },
        }
    }
    mock_response = MagicMock()
    mock_response.json.return_value = catalog_payload
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    result = await fetch_runner_models("conv_123", mock_client)
    assert result is not None
    assert "databricks-claude-haiku-4-5" in result["self"]
    assert "databricks-claude-opus-4-8" in result["self"]
    assert "databricks-claude-sonnet-4-6" in result["claude_code"]


@pytest.mark.asyncio
async def test_fetch_runner_models_returns_none_on_http_error() -> None:
    import httpx

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connection refused"))

    result = await fetch_runner_models("conv_123", mock_client)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_runner_models_returns_none_on_empty_workers() -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {"workers": {}}
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    result = await fetch_runner_models("conv_123", mock_client)
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
        harness="claude-sdk",
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


@pytest.mark.asyncio
async def test_route_turn_uses_runner_catalog_when_available() -> None:
    """route_turn uses live runner catalog instead of static table when provided."""
    expected = RoutingResult(
        model="databricks-claude-opus-4-8",
        rationale="complex task",
        harness="self",
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-claude-haiku-4-5"},
                    {"id": "databricks-claude-opus-4-8"},
                ],
                "note": "",
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    caps = _FakeCaps(routing_client=_FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, _v = await route_turn(
            "claude-sdk",
            "complex task",
            session_id="conv_123",
            runner_client=mock_client,
        )
    assert model == "databricks-claude-opus-4-8"
    # Runner endpoint was called
    mock_client.get.assert_called_once()
    call_url = mock_client.get.call_args[0][0]
    assert "conv_123" in call_url and "models" in call_url


@pytest.mark.asyncio
async def test_route_turn_falls_back_to_static_when_runner_unavailable() -> None:
    """Falls back to infer_models when runner catalog fetch fails."""
    import httpx

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("runner down"))

    expected = RoutingResult(
        model="databricks-claude-haiku-4-5",
        rationale="simple",
        harness="claude-sdk",
    )
    caps = _FakeCaps(routing_client=_FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, _v = await route_turn(
            "claude-sdk",
            "hello",
            session_id="conv_123",
            runner_client=mock_client,
        )
    # Still routes — fell back to static infer_models
    assert model == "databricks-claude-haiku-4-5"


# ── ExternalRoutingClient ─────────────────────────────────────────────


def _patch_httpx(transport: Any) -> Any:
    """Patch httpx.AsyncClient to use a MockTransport."""
    import httpx

    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> Any:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return patch("httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_external_routing_client_sends_snake_case_and_parses() -> None:
    """available_models -> snake_case route_options; response -> RoutingResult."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "route_selection": [
                    {"route_option": {"model": "claude-opus-4-8", "harness": "claude"}}
                ],
                "rationale": "task_v0 matched rule 'bugfix_to_opus'.",
            },
        )

    client = ExternalRoutingClient(
        base_url="https://host/ai-gateway/routing/v1", router_name="task_v0"
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route(
            "fix this code: x = y + 2",
            {"claude": ["claude-opus-4-8"], "codex": ["gpt-5-5"]},
        )

    assert result is not None
    assert result.model == "claude-opus-4-8"
    assert result.harness == "claude"
    assert result.rationale == "task_v0 matched rule 'bugfix_to_opus'."
    assert captured["url"] == "https://host/ai-gateway/routing/v1/routes:select"
    body = captured["body"]
    assert body["route_selector"]["router_name"] == "task_v0"  # snake_case
    assert body["task"]["prompt"] == "fix this code: x = y + 2"
    assert body["route_options"] == [
        {"model": "claude-opus-4-8", "harness": "claude"},
        {"model": "gpt-5-5", "harness": "codex"},
    ]


@pytest.mark.asyncio
async def test_external_routing_client_empty_available_models_skips() -> None:
    """No candidates -> no HTTP call, returns None."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = ExternalRoutingClient(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {}) is None
    assert called is False


@pytest.mark.asyncio
async def test_external_routing_client_swallows_http_error() -> None:
    """A router outage returns None so the turn proceeds."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = ExternalRoutingClient(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None


@pytest.mark.asyncio
async def test_external_routing_client_empty_selection_returns_none() -> None:
    """An empty route_selection (e.g. router declined) yields None."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"route_selection": [], "rationale": ""})

    client = ExternalRoutingClient(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None


@pytest.mark.asyncio
async def test_external_routing_client_sends_bearer_auth() -> None:
    """When built with auth, the request carries the bearer header."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient, _bearer_auth

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "m", "harness": "h"}}]},
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1", router_name="task_v0", auth=_bearer_auth("dapi-XYZ")
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        await client.route("hi", {"h": ["m"]})
    assert captured["authorization"] == "Bearer dapi-XYZ"
