"""Tests for the server-side intelligent model routing module.

Covers model inference, the RoutingClient protocol, the default
LLMRoutingClient, and the public ``route_turn`` entry point.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.server.routing_backend import RoutingBackends
from omnigent.server.smart_routing import (
    _AUTO_ROUTING_HARNESSES,
    LLMRoutingClient,
    RoutingResult,
    _build_rubric,
    fetch_runner_models,
    harness_bars_model,
    infer_models,
    route_session_harness,
    route_turn,
)
from tests.server.helpers import FakeCaps, FakeRoutingClient

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


@dataclass
class _SettingsCaps:
    """Caps stub carrying only the routing settings."""

    routing_settings: Any = None  # type: ignore[explicit-any]


@dataclass
class _LastError:
    """Routing-client stub with an arbitrary ``last_error`` value."""

    last_error: Any = None  # type: ignore[explicit-any]


_TEST_MODELS = {
    "claude-sdk": [
        "databricks-claude-haiku-4-5",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-opus-4-8",
    ],
    "claude-native": ["databricks-claude-haiku-4-5"],
    "codex": ["databricks-gpt-5-4-nano", "databricks-gpt-5-5"],
    "codex-native": ["databricks-gpt-5-4-nano"],
    "openai-agents": ["databricks-gpt-5-4-nano"],
    "pi": [
        "databricks-claude-haiku-4-5",
        "databricks-claude-sonnet-4-6",
        "databricks-gpt-5-4-nano",
        "databricks-gpt-5-5",
    ],
}


def _models_for(harness: str | None) -> list[str] | None:
    models = _TEST_MODELS.get(harness or "")
    return list(models) if models is not None else None


def _catalog_client() -> MagicMock:
    workers = {
        "claude_code": _TEST_MODELS["claude-sdk"],
        "codex": _TEST_MODELS["codex"],
        "pi": _TEST_MODELS["pi"],
        "self": _TEST_MODELS["claude-sdk"],
    }
    response = MagicMock()
    catalog_workers: dict[str, dict[str, Any]] = {}
    for worker, models in workers.items():
        entries: list[dict[str, Any]] = []
        for model in models:
            entry: dict[str, Any] = {"id": model}
            if worker == "pi":
                if "claude" in model:
                    entry.update(family="claude", wire_apis=["openai-chat"])
                elif "gpt" in model:
                    entry.update(
                        family="openai",
                        wire_apis=["openai-chat", "openai-responses"],
                    )
            entries.append(entry)
        catalog_workers[worker] = {
            "source": "catalog",
            "verified": True,
            "models": entries,
            "note": "",
        }
    response.json.return_value = {"workers": catalog_workers}
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client


# ── test catalog fixtures ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("harness", "contains"),
    [
        ("claude-sdk", ("haiku", "opus")),
        ("claude-native", ()),
        ("codex-native", ()),
        ("codex", ("gpt",)),
        ("openai-agents", ()),
        # pi is multi-model — both Claude and GPT.
        ("pi", ("haiku", "gpt")),
    ],
)
def test_models_fixture_returns_family_models(harness: str, contains: tuple[str, ...]) -> None:
    models = _models_for(harness)
    assert models is not None
    for token in contains:
        assert any(token in m for m in models)


def test_models_fixture_claude_ordered_cheapest_first() -> None:
    models = _models_for("claude-sdk")
    assert models is not None
    haiku_idx = next(i for i, m in enumerate(models) if "haiku" in m)
    opus_idx = next(i for i, m in enumerate(models) if "opus" in m)
    assert haiku_idx < opus_idx


def test_model_lists_cover_current_claude_generations() -> None:
    """A stale list turns a live model into an unservable arm (sonnet-5 → haiku)."""
    models = infer_models("claude-sdk")
    assert models is not None
    assert "databricks-claude-sonnet-5" in models


def test_substitute_model_applies_the_pick_when_the_catalog_serves_it() -> None:
    from omnigent.server.smart_routing import substitute_model

    catalog = ["databricks-claude-opus-4-8", "databricks-claude-sonnet-5"]
    assert substitute_model("claude-opus-4-8", catalog, prefixes=("databricks-",)) == (
        "databricks-claude-opus-4-8"
    )


def test_substitute_model_falls_back_within_family() -> None:
    from omnigent.server.smart_routing import substitute_model

    prefixes = ("databricks-",)
    # claude pick the workspace does not serve → the sonnet fallback arm.
    claude_catalog = ["databricks-claude-sonnet-5", "databricks-claude-haiku-4-5"]
    assert substitute_model("claude-opus-4-8", claude_catalog, prefixes=prefixes) == (
        "databricks-claude-sonnet-5"
    )
    # gpt and glm both fall back to luna (glm reads as the gpt family).
    gpt_catalog = ["databricks-gpt-5-6-luna", "databricks-gpt-5-6-sol"]
    assert substitute_model("gpt-5-6-terra", gpt_catalog, prefixes=prefixes) == (
        "databricks-gpt-5-6-luna"
    )
    assert substitute_model("glm-5-2", gpt_catalog, prefixes=prefixes) == (
        "databricks-gpt-5-6-luna"
    )


def test_substitute_model_honors_the_tier_over_the_family_fallback() -> None:
    from omnigent.server.smart_routing import substitute_model

    prefixes = ("databricks-",)
    # task_v1 names the frozen opus arm claude-opus-4-8, but this workspace
    # serves opus-5 for the opus line. The router meant "escalate to opus", so
    # the servable opus wins over the sonnet family fallback (the reported bug:
    # an escalate pick was collapsed to sonnet-5).
    catalog = [
        "databricks-claude-opus-5",
        "databricks-claude-sonnet-5",
        "databricks-claude-haiku-4-5",
    ]
    assert substitute_model("claude-opus-4-8", catalog, prefixes=prefixes) == (
        "databricks-claude-opus-5"
    )
    # The newest model of the tier wins when several are servable.
    two_opus = ["databricks-claude-opus-4-7", "databricks-claude-opus-5"]
    assert substitute_model("claude-opus-4-8", two_opus, prefixes=prefixes) == (
        "databricks-claude-opus-5"
    )
    # A same-tier match never crosses the tier down: only when no opus is
    # servable does it fall to the sonnet family default.
    assert (
        substitute_model(
            "claude-opus-4-8",
            ["databricks-claude-sonnet-5", "databricks-claude-haiku-4-5"],
            prefixes=prefixes,
        )
        == "databricks-claude-sonnet-5"
    )


def test_substitute_model_declines_when_no_fallback_is_servable() -> None:
    from omnigent.server.smart_routing import substitute_model

    prefixes = ("databricks-",)
    # The family fallback (luna) is absent from the catalog → honest decline.
    assert substitute_model("gpt-5-6-terra", ["databricks-gpt-5-6-sol"], prefixes=prefixes) is None
    # A barred fallback is skipped, not applied.
    assert (
        substitute_model(
            "claude-opus-4-8",
            ["databricks-claude-sonnet-5"],
            prefixes=prefixes,
            barred=("databricks-claude-sonnet-5",),
        )
        is None
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-opus-4-8", "claude"),
        ("databricks-gpt-5-5", "gpt"),
        ("gpt-5-6-sol", "gpt"),
        # The router's glm arm and its servable spellings must all read as
        # the codex family, or a pick lands on the wrong harness.
        ("glm-5-2", "gpt"),
        ("databricks-glm-5-2", "gpt"),
        ("system.ai.glm-5-2", "gpt"),
        ("databricks-kimi-k2-6", "gpt"),
        ("databricks-meta-llama-3.3-70b-instruct", "other"),
    ],
)
def test_model_family_agrees_with_the_shared_token_rule(model: str, expected: str) -> None:
    """This file's family view must match the dispatch gate's.

    :param model: Model id under test.
    :param expected: The family it must land in.
    """
    from omnigent.model_override import model_family_mismatch
    from omnigent.server.smart_routing import _model_family

    assert _model_family(model) == expected
    if expected == "gpt":
        assert model_family_mismatch("codex", model) is None


def test_catalog_models_for_harness_matches_worker_rows() -> None:
    from omnigent.server.smart_routing import catalog_models_for_harness

    catalog = {
        "self": ["databricks-claude-sonnet-5"],
        "codex": ["databricks-gpt-5-5"],
    }
    # A worker row for the counterpart family, matched by family not id.
    assert catalog_models_for_harness(catalog, "codex-native") == ["databricks-gpt-5-5"]
    # "self" only counts for the session's own harness.
    assert catalog_models_for_harness(catalog, "claude-native") is None
    assert catalog_models_for_harness(catalog, "claude-native", allow_self=True) == [
        "databricks-claude-sonnet-5"
    ]
    assert catalog_models_for_harness(None, "claude-native", allow_self=True) is None


def test_infer_models_unknown_harness() -> None:
    assert infer_models("cursor") is None
    assert infer_models("antigravity") is None
    assert infer_models(None) is None


def test_models_fixture_unknown_harness() -> None:
    assert _models_for("cursor") is None
    assert _models_for("antigravity") is None
    assert _models_for(None) is None


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


# The judge's verdict resolves to (model, harness). A harness the model does not
# belong to — whether a real other harness or a hallucinated one — re-resolves to
# the harness that owns the picked model.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict_harness", "verdict_model", "available", "expect_harness"),
    [
        ("claude-sdk", "databricks-claude-opus-4-8", None, "claude-sdk"),
        (
            "codex",
            "databricks-claude-opus-4-8",
            {"codex": ["databricks-gpt-5-4"]},
            "claude-sdk",
        ),
        ("hallucinated-harness", "databricks-claude-haiku-4-5", None, "claude-sdk"),
    ],
)
async def test_llm_routing_client_resolves_harness_to_the_model_owner(
    verdict_harness: str,
    verdict_model: str,
    available: dict[str, list[str]] | None,
    expect_harness: str,
) -> None:
    models = _models_for("claude-sdk")
    assert models is not None
    catalog = {"claude-sdk": models, **(available or {})}
    verdict = {"harness": verdict_harness, "model": verdict_model, "rationale": "r"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    result = await client.route("task", catalog)
    assert result is not None
    assert result.model == verdict_model
    assert result.harness == expect_harness
    assert result.rationale == "r"


@pytest.mark.asyncio
async def test_llm_routing_client_clamps_hallucinated_model() -> None:
    verdict = {"harness": "claude-sdk", "model": "hallucinated-model", "rationale": "hard"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = _models_for("claude-sdk")
    assert models is not None
    result = await client.route("hard task", {"claude-sdk": models})
    assert result is not None
    assert result.model == models[0]  # clamped to cheapest


@pytest.mark.asyncio
async def test_llm_routing_client_rejects_empty_model() -> None:
    verdict = {"harness": "claude-sdk", "model": "", "rationale": "x"}
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = _models_for("claude-sdk")
    assert models is not None
    result = await client.route("hello", {"claude-sdk": models})
    assert result is None


@pytest.mark.asyncio
async def test_llm_routing_client_returns_none_on_error() -> None:
    class _BrokenLLM:
        async def create(self, **kwargs: Any) -> None:
            raise TypeError("boom")

    client = LLMRoutingClient(_BrokenLLM())
    models = _models_for("claude-sdk")
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
async def test_fetch_runner_models_orders_reported_cost_tiers() -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "self": {
                "models": [
                    {"id": "premium-model", "cost_tier": "premium"},
                    {"id": "unknown-model"},
                    {"id": "economy-model", "cost_tier": "economy"},
                    {"id": "standard-model", "cost_tier": "standard"},
                ]
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    result = await fetch_runner_models("conv_123", mock_client)

    assert result == {
        "self": ["economy-model", "standard-model", "premium-model", "unknown-model"]
    }


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


@pytest.mark.asyncio
async def test_route_turn_uses_caps_routing_client() -> None:
    expected = RoutingResult(
        model="databricks-claude-haiku-4-5",
        rationale="trivial",
        harness="claude-sdk",
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch(
        "omnigent.runtime._globals._caps",
        new=caps,
    ):
        model, v = await route_turn(
            "claude-sdk",
            "hello",
            session_id="conv_123",
            runner_client=_catalog_client(),
        )
    assert model == "databricks-claude-haiku-4-5"
    assert v is not None
    assert "tier" not in v


@pytest.mark.asyncio
async def test_route_turn_returns_none_when_no_client() -> None:
    caps = FakeCaps(routing_client=None)
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

    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
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
async def test_route_turn_prefers_the_callers_session_vocabulary() -> None:
    """A pane can only switch onto its picker rows, so those win over the gateway."""
    expected = RoutingResult(
        model="databricks-claude-opus-5",
        rationale="complex task",
        harness="self",
    )
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "databricks-claude-opus-4-8"}],
                "note": "",
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    routing_client = FakeRoutingClient(expected)

    caps = FakeCaps(routing_client=routing_client)
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, _v = await route_turn(
            "claude-native",
            "complex task",
            session_id="conv_123",
            runner_client=mock_client,
            catalog=["databricks-claude-opus-5", "databricks-gpt-5-5"],
        )

    assert model == "databricks-claude-opus-5"
    # The wider runner catalog was never consulted, and the out-of-family id
    # in the vocabulary was still dropped.
    mock_client.get.assert_not_called()
    assert routing_client.offered == [{"claude-native": ["databricks-claude-opus-5"]}]


@pytest.mark.asyncio
async def test_route_turn_drops_out_of_family_catalog_models() -> None:
    """A native terminal's catalog can list other families; they aren't offered."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-gpt-5-5"},
                    {"id": "databricks-claude-opus-4-8"},
                    {"id": "databricks-gpt-5-4-mini"},
                ],
                "note": "",
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    client = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-5", rationale="gpt task", harness="codex-native")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, _v = await route_turn(
            "codex-native",
            "narrow fix",
            session_id="conv_123",
            runner_client=mock_client,
        )
    assert model == "databricks-gpt-5-5"
    assert client.offered == [{"codex-native": ["databricks-gpt-5-5", "databricks-gpt-5-4-mini"]}]


@pytest.mark.asyncio
async def test_route_turn_offers_and_applies_a_glm_pick_on_codex() -> None:
    """A codex session's GLM/Kimi endpoints are offered and a GLM pick applies.

    They ride the same Responses wire codex speaks, so the in-family filter
    must keep them; dropping them would leave the router unable to pick the
    delegate arm the workspace actually serves.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-gpt-5-5"},
                    {"id": "databricks-glm-5-2"},
                    {"id": "databricks-kimi-k2-6"},
                    {"id": "databricks-claude-opus-4-8"},
                ],
                "note": "",
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    client = FakeRoutingClient(
        RoutingResult(model="databricks-glm-5-2", rationale="delegate arm", harness="codex-native")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, _v = await route_turn(
            "codex-native",
            "refactor the parser",
            session_id="conv_glm",
            runner_client=mock_client,
        )
    assert client.offered == [
        {
            "codex-native": [
                "databricks-gpt-5-5",
                "databricks-glm-5-2",
                "databricks-kimi-k2-6",
            ]
        }
    ]
    assert model == "databricks-glm-5-2"


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
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, _v = await route_turn(
            "claude-sdk",
            "hello",
            session_id="conv_123",
            runner_client=mock_client,
        )
    # Still routes — fell back to the static infer_models table.
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
async def test_external_routing_client_roundtrips_provider_prefix() -> None:
    """Send bare ids out; recover the exact catalog id from the bare answer.

    A Databricks catalog carries a ``databricks-`` prefix the router doesn't
    want, so we send bare ids and map the router's (bare) pick back to the
    local prefixed id the runner needs.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        # Router echoes the bare id it was given.
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "claude-opus-4-8"}}]},
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1", router_name="task_v0", model_prefixes=["databricks-"]
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route(
            "hi", {"self": ["databricks-claude-opus-4-8", "databricks-gpt-5-5"]}
        )

    # Outbound: configured prefix stripped for the router's vocabulary.
    assert captured["body"]["route_options"] == [
        {"model": "claude-opus-4-8", "harness": "self"},
        {"model": "gpt-5-5", "harness": "self"},
    ]
    # Inbound: mapped back to the local (prefixed) catalog id.
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"


@pytest.mark.asyncio
async def test_external_routing_client_strips_first_matching_prefix() -> None:
    """With multiple prefixes, the first matching one is stripped per id."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "claude-opus-4-8"}}]},
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1",
        router_name="task_v0",
        model_prefixes=["databricks-", "system.ai."],
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route(
            "hi", {"self": ["databricks-claude-opus-4-8", "system.ai.claude-sonnet-5"]}
        )

    # Each id has its own matching prefix stripped.
    assert captured["body"]["route_options"] == [
        {"model": "claude-opus-4-8", "harness": "self"},
        {"model": "claude-sonnet-5", "harness": "self"},
    ]
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"


@pytest.mark.asyncio
async def test_external_routing_client_maps_back_by_harness() -> None:
    """The same bare id under two harnesses maps back to distinct local ids.

    A Databricks-authed harness carries the ``databricks-`` prefix while a
    subscription harness (e.g. Codex) uses the bare id; both reduce to the
    same router id, so the (harness, router-id) key keeps them distinct.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        # Router picks the codex option (bare id, codex harness).
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "gpt-5-5", "harness": "codex"}}]},
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1", router_name="task_v0", model_prefixes=["databricks-"]
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route(
            "hi",
            {"pi": ["databricks-gpt-5-5"], "codex": ["gpt-5-5"]},
        )

    # Both harnesses reduce to router id "gpt-5-5"; the pick maps back to the
    # codex local id, not pi's prefixed one.
    assert result is not None
    assert result.model == "gpt-5-5"
    assert result.harness == "codex"


@pytest.mark.asyncio
async def test_external_routing_client_strips_the_default_prefixes() -> None:
    """Unconfigured, the client still speaks the one shared prefix list.

    The server-side seam and the client must agree about which prefixes a
    catalog id carries; a client left on an empty list is exactly the
    double-resolution downgrade. Ids carrying no known prefix pass through.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "claude-opus-4-8"}}]},
        )

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route("hi", {"self": ["databricks-claude-opus-4-8", "gpt-5-5"]})

    assert captured["body"]["route_options"] == [
        {"model": "claude-opus-4-8", "harness": "self"},
        {"model": "gpt-5-5", "harness": "self"},
    ]
    # Restored to the exact catalog id the runner needs.
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"


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
async def test_external_routing_client_rejects_out_of_set_model() -> None:
    """A model the router was never offered is rejected, not persisted.

    Parity with the built-in judge: the returned model would become the
    session's ``model_override``, so an out-of-set pick returns None and the
    turn proceeds on the agent's default model.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "route_selection": [
                    {"route_option": {"model": "hallucinated-model", "harness": "claude"}}
                ]
            },
        )

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


@pytest.mark.asyncio
async def test_external_routing_client_mints_fresh_token_per_call_from_profile() -> None:
    """With a databricks_profile, each call re-authenticates (OAuth refresh)."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    tokens = iter(["Bearer tok-1", "Bearer tok-2"])
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization"))
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "m", "harness": "h"}}]},
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1", router_name="task_v0", databricks_profile="agent"
    )

    # Stub the SDK Config so each authenticate() yields the next token — proving
    # the client re-resolves auth per call rather than caching a stale bearer.
    class _FakeConfig:
        def authenticate(self) -> dict[str, str]:
            return {"Authorization": next(tokens)}

    client._sdk_config = _FakeConfig()  # type: ignore[attr-defined]

    with _patch_httpx(httpx.MockTransport(handler)):
        await client.route("hi", {"h": ["m"]})
        await client.route("hi again", {"h": ["m"]})
    assert captured == ["Bearer tok-1", "Bearer tok-2"]


# ── Per-request auth: auth_provider and the ambient SDK chain ──────────────
#
# One managed process serves many workspaces, so the credential cannot be bound
# at construction: ``auth_provider`` is evaluated per route() call and carries
# the calling identity. The ambient chain covers the other server posture — no
# profile, no api_key, a workspace credential in the environment — and engages
# only when the ambient host IS the router host, so an unauthenticated endpoint
# stays unauthenticated.


def _auth_handler(captured: list[str | None]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization"))
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": {"model": "m", "harness": "h"}}]},
        )

    return handler


@pytest.mark.asyncio
async def test_auth_provider_is_evaluated_on_every_call() -> None:
    """Per-caller identity: the provider runs per route(), never once."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    tokens = iter(["caller-1", "caller-2"])
    captured: list[str | None] = []

    client = ExternalRoutingClient(
        base_url="https://host/v1",
        router_name="task_v0",
        auth_provider=lambda: {"Authorization": f"Bearer {next(tokens)}"},
    )
    with _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
        await client.route("hi again", {"h": ["m"]})
    assert captured == ["Bearer caller-1", "Bearer caller-2"]


@pytest.mark.asyncio
async def test_auth_provider_wins_over_the_static_auth() -> None:
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient, _bearer_auth

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://host/v1",
        router_name="task_v0",
        auth=_bearer_auth("static-key"),
        databricks_profile="agent",
        auth_provider=lambda: {"Authorization": "Bearer per-caller"},
    )
    with _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
    assert captured == ["Bearer per-caller"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [None, {}, {"Authorization": "  "}])
async def test_auth_provider_returning_nothing_sends_no_credential(headers: Any) -> None:  # type: ignore[explicit-any]
    """The provider is the sole authority: no credential means unauthenticated."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient, _bearer_auth

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://host/v1",
        router_name="task_v0",
        auth=_bearer_auth("static-key"),
        auth_provider=lambda: headers,
    )
    with _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
    assert captured == [None]


@pytest.mark.asyncio
async def test_a_raising_auth_provider_still_routes() -> None:
    """A provider that blows up degrades to unauthenticated, not to a failed turn."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def boom() -> dict[str, str]:
        raise RuntimeError("no identity")

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://host/v1", router_name="task_v0", auth_provider=boom
    )
    with _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        assert await client.route("hi", {"h": ["m"]}) is not None
    assert captured == [None]


@pytest.mark.asyncio
async def test_auth_provider_carries_more_than_the_bearer_header() -> None:
    """A managed provider may need to name the workspace as well as the caller."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            {
                "authorization": request.headers.get("Authorization"),
                "workspace": request.headers.get("X-Databricks-Workspace-Id"),
                "content_type": request.headers.get("Content-Type"),
            }
        )
        return httpx.Response(
            200, json={"route_selection": [{"route_option": {"model": "m", "harness": "h"}}]}
        )

    client = ExternalRoutingClient(
        base_url="https://host/v1",
        router_name="task_v0",
        auth_provider=lambda: {
            "Authorization": "Bearer u-1",
            "X-Databricks-Workspace-Id": "42",
        },
    )
    with _patch_httpx(httpx.MockTransport(handler)):
        await client.route("hi", {"h": ["m"]})
    assert captured == {
        "authorization": "Bearer u-1",
        "workspace": "42",
        "content_type": "application/json",
    }


class _AmbientConfig:
    """Stand-in for a databricks-sdk ``Config`` resolved from the environment."""

    def __init__(self, host: str, token: str = "ambient-tok") -> None:
        self.host = host
        self._token = token
        self.calls = 0

    def authenticate(self) -> dict[str, str]:
        self.calls += 1
        return {"Authorization": f"Bearer {self._token}-{self.calls}"}


@contextmanager
def _ambient_sdk(config: Any) -> Any:  # type: ignore[explicit-any]
    """Patch the SDK ``Config`` the ambient chain builds; ``None`` raises."""

    def factory(**kwargs: Any) -> Any:  # type: ignore[explicit-any]
        assert not kwargs, "the ambient chain takes no profile"
        if config is None:
            raise ValueError("default auth: cannot configure default credentials")
        return config

    with patch("databricks.sdk.config.Config", factory):
        yield


@pytest.mark.databricks
@pytest.mark.asyncio
async def test_ambient_credential_is_used_when_it_names_the_router_host() -> None:
    """No profile, no api_key: the workspace credential in the environment answers."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://ws.example.invalid/ai-gateway/routing/v1", router_name="task_v0"
    )
    config = _AmbientConfig("https://ws.example.invalid")
    with _ambient_sdk(config), _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
        await client.route("hi again", {"h": ["m"]})
    # Re-authenticated per call, so an expiring OAuth token is refreshed.
    assert captured == ["Bearer ambient-tok-1", "Bearer ambient-tok-2"]


@pytest.mark.databricks
@pytest.mark.asyncio
async def test_ambient_credential_is_withheld_from_another_host() -> None:
    """The guard: a workspace token never reaches an endpoint off that workspace."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://router.acme.invalid/v1", router_name="task_v0"
    )
    config = _AmbientConfig("https://ws.example.invalid")
    with _ambient_sdk(config), _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
        await client.route("hi again", {"h": ["m"]})
    assert captured == [None, None]
    # The chain is probed once, not on every call.
    assert config.calls == 0


@pytest.mark.databricks
@pytest.mark.asyncio
async def test_no_ambient_credential_leaves_the_request_unauthenticated() -> None:
    """A plain unauthenticated endpoint keeps working when the SDK resolves nothing."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: list[str | None] = []
    client = ExternalRoutingClient(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _ambient_sdk(None), _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
    assert captured == [None]


@pytest.mark.databricks
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # Scheme-less and trailing-slash spellings still match the router host.
        ("ws.example.invalid", "Bearer ambient-tok-1"),
        ("https://ws.example.invalid/", "Bearer ambient-tok-1"),
        # A same-suffix host is a DIFFERENT host, not a match.
        ("https://evil-ws.example.invalid", None),
        ("", None),
    ],
)
async def test_ambient_host_matching(host: str, expected: str | None) -> None:
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    captured: list[str | None] = []
    client = ExternalRoutingClient(
        base_url="https://ws.example.invalid/ai-gateway/routing/v1", router_name="task_v0"
    )
    with (
        _ambient_sdk(_AmbientConfig(host)),
        _patch_httpx(httpx.MockTransport(_auth_handler(captured))),
    ):
        await client.route("hi", {"h": ["m"]})
    assert captured == [expected]


@pytest.mark.databricks
@pytest.mark.asyncio
async def test_the_ambient_chain_is_skipped_when_any_other_mode_is_configured() -> None:
    """Explicit config wins; the ambient chain is the last resort only."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient, _bearer_auth

    captured: list[str | None] = []
    config = _AmbientConfig("https://ws.example.invalid")
    client = ExternalRoutingClient(
        base_url="https://ws.example.invalid/v1",
        router_name="task_v0",
        auth=_bearer_auth("static-key"),
    )
    with _ambient_sdk(config), _patch_httpx(httpx.MockTransport(_auth_handler(captured))):
        await client.route("hi", {"h": ["m"]})
    assert captured == ["Bearer static-key"]
    assert config.calls == 0


@pytest.mark.asyncio
async def test_external_routing_client_records_last_error_on_http_failure() -> None:
    """A 4xx/5xx sets last_error with the gateway's unwrapped message."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error_code": 401,
                "message": "Credential was not sent or was of an unsupported type for this API.",
            },
        )

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route("hi", {"h": ["m"]})
    assert result is None
    assert client.last_error is not None
    assert "401" in client.last_error
    assert "Credential was not sent" in client.last_error


def test_router_error_detail_unwraps_nested_message() -> None:
    from omnigent.server.smart_routing import _router_error_detail

    # Doubly-encoded: outer message holds another JSON object.
    body = json.dumps(
        {
            "error_code": "BAD_REQUEST",
            "message": json.dumps({"error": {"message": "task_v0 requires [...] models"}}),
        }
    )
    assert _router_error_detail(body) == "task_v0 requires [...] models"
    # Plain body passes through (trimmed).
    assert _router_error_detail("boom") == "boom"


@pytest.mark.asyncio
async def test_route_session_harness_surfaces_router_error_detail() -> None:
    """When the client exposes last_error, route_session_harness surfaces it."""

    class _FailingClient:
        last_error = "router returned HTTP 401: Credential was not sent"

        async def route(
            self, _message: str, _available: dict[str, list[str]]
        ) -> RoutingResult | None:
            return None

    caps = FakeCaps(routing_client=_FailingClient())
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "hi", session_id="conv_123", runner_client=_catalog_client()
        )
    assert harness is None
    assert model is None
    assert error is not None
    assert "401" in error
    assert "Credential was not sent" in error


# ── route_session_harness ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_session_harness_picks_harness_and_model() -> None:
    """route_session_harness returns (harness, model, verdict) from the router."""
    expected = RoutingResult(
        model="databricks-claude-opus-4-8",
        rationale="complex codebase task",
        harness="claude-sdk",
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "refactor the auth module",
            session_id="conv_123",
            runner_client=_catalog_client(),
        )
    assert harness == "claude-sdk"
    assert model == "databricks-claude-opus-4-8"
    assert verdict is not None
    assert "rationale" in verdict
    assert error is None


@pytest.mark.asyncio
async def test_route_session_harness_passes_discovered_sdk_harnesses() -> None:
    """The live worker catalog supplies every routable harness candidate."""
    received_harnesses: list[str] = []

    class _CapturingClient:
        async def route(
            self, _message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            received_harnesses.extend(available_models.keys())
            return RoutingResult(model="databricks-claude-haiku-4-5", rationale="x", harness="pi")

    caps = FakeCaps(routing_client=_CapturingClient())
    with patch("omnigent.runtime._globals._caps", new=caps):
        await route_session_harness(
            "quick task", session_id="conv_123", runner_client=_catalog_client()
        )
    for h in _AUTO_ROUTING_HARNESSES:
        assert h in received_harnesses, f"harness {h!r} missing from candidate set"


@pytest.mark.asyncio
async def test_route_session_harness_uses_catalog_session_id_for_fetch() -> None:
    """catalog_session_id (the parent) drives the catalog fetch, not session_id.

    A sub-agent's own catalog is "self"-only; routing must use the parent's
    full spawnable-worker catalog so the candidate set is stable.
    """
    from unittest.mock import MagicMock

    fetched_paths: list[str] = []

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "claude_code": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "m1"}],
                "note": "",
            },
        }
    }
    mock_response.raise_for_status = MagicMock()

    async def _get(path: str, **_: Any) -> Any:
        fetched_paths.append(path)
        return mock_response

    mock_client = MagicMock()
    mock_client.get = _get

    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model="m1", rationale="x", harness="claude-sdk")
        )
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        await route_session_harness(
            "hi",
            session_id="child_sess",
            catalog_session_id="parent_sess",
            runner_client=mock_client,
        )
    # The catalog was fetched for the PARENT, not the child.
    assert any("parent_sess" in p for p in fetched_paths)
    assert not any("child_sess" in p for p in fetched_paths)


@pytest.mark.asyncio
async def test_route_session_harness_uses_live_catalog_skips_absent_harness() -> None:
    """With a runner_client, harnesses absent from the live catalog are excluded."""
    from unittest.mock import AsyncMock, MagicMock

    # Live catalog has claude-sdk and codex but NOT pi.
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "claude-sdk": {
                "source": "catalog",
                "verified": True,
                "models": [
                    {"id": "databricks-claude-haiku-4-5"},
                    {"id": "databricks-claude-opus-4-8"},
                ],
                "note": "",
            },
            "codex": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "databricks-gpt-5-4-nano"}],
                "note": "",
            },
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    received_harnesses: list[str] = []

    class _CapturingClient:
        async def route(
            self, _message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            received_harnesses.extend(available_models.keys())
            return RoutingResult(
                model="databricks-claude-haiku-4-5", rationale="simple", harness="claude-sdk"
            )

    caps = FakeCaps(routing_client=_CapturingClient())
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, _error = await route_session_harness(
            "hello",
            session_id="conv_test",
            runner_client=mock_client,
        )

    assert "pi" not in received_harnesses, "pi should be excluded: absent from live catalog"
    assert "claude-sdk" in received_harnesses
    assert "codex" in received_harnesses
    assert harness == "claude-sdk"
    assert model == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_route_session_harness_maps_worker_names_to_harnesses() -> None:
    """Live catalog keyed by worker names (claude_code, codex) maps to harness ids."""
    from unittest.mock import AsyncMock, MagicMock

    # Catalog uses SUB-AGENT worker names, as the real runner returns.
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            "claude_code": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "databricks-claude-opus-4-8"}],
                "note": "",
            },
            "codex": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "databricks-gpt-5-4-nano"}],
                "note": "",
            },
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    received: list[str] = []

    class _CapturingClient:
        async def route(
            self, _message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            received.extend(available_models.keys())
            return RoutingResult(
                model="databricks-claude-opus-4-8", rationale="complex", harness="claude-sdk"
            )

    caps = FakeCaps(routing_client=_CapturingClient())
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "refactor everything",
            session_id="conv_child",
            runner_client=mock_client,
        )
    # Worker name claude_code → harness id claude-sdk in the candidate set.
    assert "claude-sdk" in received
    assert "codex" in received
    assert harness == "claude-sdk"
    assert model == "databricks-claude-opus-4-8"
    assert error is None


@pytest.mark.asyncio
async def test_route_session_harness_falls_back_when_catalog_has_only_self() -> None:
    """An unrecognized self-only catalog still routes off the static table.

    ``"self"`` is not in :data:`_WORKER_NAME_TO_HARNESS`, so live matching
    yields no auto-harness candidates and :func:`infer_models` supplies them.
    """
    from unittest.mock import AsyncMock, MagicMock

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {
            # "self" is not in _WORKER_NAME_TO_HARNESS, so live matching yields
            # no auto-harness candidates.
            "self": {
                "source": "catalog",
                "verified": True,
                "models": [{"id": "databricks-claude-opus-4-8"}],
                "note": "",
            },
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    routing_client = FakeRoutingClient(
        RoutingResult(
            model="databricks-claude-haiku-4-5", rationale="simple", harness="claude-sdk"
        )
    )
    caps = FakeCaps(routing_client=routing_client)
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "hello",
            session_id="conv_child",
            runner_client=mock_client,
        )
    assert error is None
    assert harness == "claude-sdk"
    assert model == "databricks-claude-haiku-4-5"
    # The self-only row never reached the router; the static table did.
    assert routing_client.offered
    assert set(routing_client.offered[0]) == set(_AUTO_ROUTING_HARNESSES)


@pytest.mark.asyncio
async def test_route_session_harness_returns_none_when_no_client() -> None:
    """route_session_harness returns (None, None, None, error) when no routing client."""
    caps = FakeCaps(routing_client=None)
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness("hello")
    assert harness is None
    assert model is None
    assert verdict is None
    assert error is not None  # error message propagated


@pytest.mark.asyncio
async def test_route_session_harness_returns_none_for_empty_message() -> None:
    """route_session_harness returns (None, None, None) for empty user text."""
    caps = FakeCaps(routing_client=FakeRoutingClient(None))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, _error = await route_session_harness("")
    assert harness is None
    assert model is None


@pytest.mark.asyncio
async def test_route_session_harness_sends_full_candidate_set_unfiltered() -> None:
    """The candidate set sent to the router is not pruned before selection.

    The external task_v0 router enforces a required model set and 400s if any
    required model is missing, so compatibility metadata is applied to its
    verdict rather than filtering the candidates sent to it.
    """
    pi_models: list[str] = []

    class _CapturingClient:
        async def route(
            self, _message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            pi_models.extend(available_models.get("pi", []))
            return RoutingResult(model="databricks-gpt-5-4-nano", rationale="x", harness="codex")

    caps = FakeCaps(routing_client=_CapturingClient())
    with patch("omnigent.runtime._globals._caps", new=caps):
        await route_session_harness(
            "hello", session_id="conv_123", runner_client=_catalog_client()
        )
    # Every discovered Pi model is still sent to the router.
    assert "databricks-claude-haiku-4-5" in pi_models
    assert "databricks-gpt-5-5" in pi_models


@pytest.mark.asyncio
async def test_route_session_harness_keeps_responses_capable_model_on_pi() -> None:
    """Pi can keep a model whose catalog advertises the Responses wire.

    Uses gpt-5-4-nano rather than gpt-5-5: pi's own completions path 400s on
    gpt-5.5+ reasoning models regardless of what the endpoint advertises, so
    :data:`_HARNESS_EXCLUDED_MODELS` bars those outright.
    """
    expected = RoutingResult(
        model="databricks-gpt-5-4-nano", rationale="responses-capable", harness="pi"
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "do something", session_id="conv_123", runner_client=_catalog_client()
        )
    assert harness == "pi"
    assert model == "databricks-gpt-5-4-nano"
    assert error is None


@pytest.mark.asyncio
async def test_route_session_harness_redirects_claude_on_pi_to_claude_sdk() -> None:
    """A Claude endpoint without Messages support is redirected off Pi.

    sonnet-4-6 is not in :data:`_HARNESS_EXCLUDED_MODELS`, so the move is the
    catalog's advertised wire set alone.
    """
    expected = RoutingResult(model="databricks-claude-sonnet-4-6", rationale="mid", harness="pi")
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "quick q", session_id="conv_123", runner_client=_catalog_client()
        )
    assert harness == "claude-sdk", f"claude on pi should redirect to claude-sdk, got {harness!r}"
    assert model == "databricks-claude-sonnet-4-6"
    assert error is None


# ── task_v1 contract fixtures ─────────────────────────────────────────────
#
# Freeze the confirmed task_v1 behavior: the router infers a scenario from the
# model families offered, demands that scenario's full arm menu (extra models
# ignored), echoes the harness tag without reading it, and may select an arm
# this workspace has no endpoint for.

_CLAUDE_ARMS = ("claude-opus-4-8", "claude-sonnet-5")
_CODEX_ARMS = ("glm-5-2", "gpt-5-6-sol", "gpt-5-6-luna")


def _task_v1_client(**kwargs: Any) -> Any:
    from omnigent.server.smart_routing import ExternalRoutingClient

    kwargs.setdefault("base_url", "https://host/ai-gateway/routing/v1")
    kwargs.setdefault("router_name", "task_v1")
    kwargs.setdefault("model_prefixes", ["databricks-", "system.ai."])
    return ExternalRoutingClient(**kwargs)


def _capturing_handler(captured: dict[str, Any], pick: dict[str, Any]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"route_selection": [{"route_option": pick}], "rationale": "rule tree"},
        )

    return handler


_CODEX_CATALOG_ONLY = {"codex": ["databricks-gpt-5-4"]}
_CLAUDE_CATALOG_ONLY = {"claude-sdk": ["databricks-claude-haiku-4-5"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "router_name",
        "catalog",
        "pick",
        "present",
        "absent",
        "exact_menu",
        "first_offered",
        "expect_resolved",
    ),
    [
        # A codex-only session offers the codex menu and none of the claude
        # arms — the router must not be able to pick an unspawnable harness.
        # glm has no endpoint here and its fallback (luna) is not served either,
        # so the pick declines — the menu still followed the vocabulary.
        (
            "task_v1",
            _CODEX_CATALOG_ONLY,
            {"model": "glm-5-2", "harness": "codex"},
            _CODEX_ARMS,
            _CLAUDE_ARMS,
            None,
            # The caller's own catalog id leads; the router tolerates the extra.
            "gpt-5-4",
            False,
        ),
        # sonnet-5 has no endpoint here and the claude fallback is sonnet-5
        # itself, so the pick declines.
        (
            "task_v1",
            _CLAUDE_CATALOG_ONLY,
            {"model": "claude-sonnet-5", "harness": "claude-sdk"},
            _CLAUDE_ARMS,
            _CODEX_ARMS,
            None,
            None,
            False,
        ),
        # Both harnesses spawnable: every arm of both menus is on offer. opus
        # is unserved and the claude fallback (sonnet-5) is absent too → decline.
        (
            "task_v1",
            {**_CLAUDE_CATALOG_ONLY, **_CODEX_CATALOG_ONLY},
            {"model": "claude-opus-4-8", "harness": "claude-sdk"},
            _CLAUDE_ARMS + _CODEX_ARMS,
            (),
            None,
            None,
            False,
        ),
        # A router version with no menu entry gets exactly the caller's catalog;
        # the pick is served exactly, so it resolves.
        (
            "task_v0",
            _CODEX_CATALOG_ONLY,
            {"model": "gpt-5-4", "harness": "codex"},
            (),
            (),
            ["gpt-5-4"],
            None,
            True,
        ),
    ],
)
async def test_task_v1_menu_follows_the_session_vocabulary(
    router_name: str,
    catalog: dict[str, list[str]],
    pick: dict[str, str],
    present: Sequence[str],
    absent: Sequence[str],
    exact_menu: list[str] | None,
    first_offered: str | None,
    expect_resolved: bool,
) -> None:
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client(router_name=router_name)
    with _patch_httpx(httpx.MockTransport(_capturing_handler(captured, pick))):
        result = await client.route("refactor it", catalog)

    offered = [o["model"] for o in captured["body"]["route_options"]]
    if exact_menu is not None:
        assert offered == exact_menu
    if first_offered is not None:
        assert offered[0] == first_offered
    for arm in present:
        assert arm in offered
    for arm in absent:
        assert arm not in offered
    # The menu is the invariant; the result resolves only when the pick or its
    # family fallback is servable, else it declines (a sanctioned outcome).
    if expect_resolved:
        assert result is not None
        assert result.harness == pick["harness"]
    else:
        assert result is None


@pytest.mark.asyncio
async def test_task_v1_partial_menu_error_surfaces_last_error() -> None:
    """A menu rejection leaves the session unrouted with the router's reason."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_code": "BAD_REQUEST",
                "message": "scenario 'codex' requires its full menu; missing [gpt-5-6-luna]",
            },
        )

    client = _task_v1_client()
    with _patch_httpx(httpx.MockTransport(handler)):
        result = await client.route("hi", {"codex": ["databricks-gpt-5-4"]})

    assert result is None
    assert "requires its full menu" in (client.last_error or "")


@pytest.mark.asyncio
async def test_task_v1_unservable_arm_maps_to_servable_id() -> None:
    """gpt-5-6-sol has no endpoint here; the pick lands on the gpt family fallback."""
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client()
    with _patch_httpx(
        httpx.MockTransport(
            _capturing_handler(captured, {"model": "gpt-5-6-sol", "harness": "codex"})
        )
    ):
        result = await client.route(
            "hi", {"codex": ["databricks-gpt-5-4", "databricks-gpt-5-6-luna"]}
        )

    assert result is not None
    assert result.model == "databricks-gpt-5-6-luna"  # the gpt family fallback
    assert result.raw_model == "gpt-5-6-sol"  # what the router actually said
    assert result.harness == "codex"


@pytest.mark.asyncio
async def test_task_v1_ignores_echoed_harness_and_derives_from_arm() -> None:
    """The harness tag is passthrough, so a nonsense echo never wins."""
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client()
    with _patch_httpx(
        httpx.MockTransport(
            _capturing_handler(
                captured, {"model": "claude-opus-4-8", "harness": "not-a-real-harness"}
            )
        )
    ):
        result = await client.route(
            "refactor",
            {
                "claude-sdk": ["databricks-claude-haiku-4-5", "databricks-claude-sonnet-5"],
                "codex": ["databricks-gpt-5-4"],
            },
        )

    # opus has no endpoint here, so it lands on the claude fallback (sonnet-5) —
    # and the harness derives from the arm's family, never the nonsense echo.
    assert result is not None
    assert result.harness == "claude-sdk"
    assert result.model == "databricks-claude-sonnet-5"
    assert result.raw_model == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_task_v1_sends_selection_model_as_selector_config() -> None:
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client(selection_model="gpt-5-4-mini")
    with _patch_httpx(
        httpx.MockTransport(_capturing_handler(captured, {"model": "glm-5-2", "harness": "codex"}))
    ):
        await client.route("hi", {"codex": ["databricks-gpt-5-4"]})

    assert captured["body"]["route_selector"]["config"] == {"model": "gpt-5-4-mini"}


@pytest.mark.asyncio
async def test_task_v1_omits_selector_config_without_selection_model() -> None:
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client()
    with _patch_httpx(
        httpx.MockTransport(_capturing_handler(captured, {"model": "glm-5-2", "harness": "codex"}))
    ):
        await client.route("hi", {"codex": ["databricks-gpt-5-4"]})

    assert "config" not in captured["body"]["route_selector"]


@pytest.mark.asyncio
async def test_task_v1_keeps_raw_prompt_and_4000_char_cap() -> None:
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client()
    prompt = "x" * 5000
    with _patch_httpx(
        httpx.MockTransport(_capturing_handler(captured, {"model": "glm-5-2", "harness": "codex"}))
    ):
        await client.route(prompt, {"codex": ["databricks-gpt-5-4"]})

    assert captured["body"]["task"]["prompt"] == "x" * 4000


@pytest.mark.asyncio
async def test_route_session_harness_keeps_raw_pick_in_verdict() -> None:
    """An unservable pick is applied as a servable id but reported verbatim."""
    expected = RoutingResult(
        model="databricks-gpt-5-5",
        rationale="cheapest arm",
        harness="codex",
        raw_model="gpt-5-6-sol",
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "do it",
            harness_candidates=("codex",),
            catalog={"codex": ["databricks-gpt-5-5"]},
        )
    assert harness == "codex"
    assert model == "databricks-gpt-5-5"
    assert verdict is not None
    assert verdict["raw_model"] == "gpt-5-6-sol"
    assert error is None


# ── Route-options seam ────────────────────────────────────────────────────


def test_route_option_source_offers_only_the_catalog_for_another_router() -> None:
    """Only task_v1 demands an arm menu; another version gets the catalog."""
    from omnigent.server.smart_routing import TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(router_name="task_v9")
    options = source.build_route_options(["codex"], {"codex": ["databricks-gpt-5-4"]})
    assert [o.model for o in options] == ["gpt-5-4"]


def test_route_option_source_rejects_never_offered_pick() -> None:
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    catalog = {"codex": ["databricks-gpt-5-4"]}
    assert source.resolve_selection(RoutePick(model="hallucinated"), ["codex"], catalog) is None


def test_route_option_source_redirects_excluded_pick_off_pi() -> None:
    """A pick pi bars moves to a harness that runs it — when one is on offer."""
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    catalog = {
        "pi": ["databricks-gpt-5-5", "databricks-claude-haiku-4-5"],
        "codex": ["databricks-gpt-5-5"],
        "claude-sdk": ["databricks-claude-haiku-4-5"],
    }
    harnesses = ["pi", "codex", "claude-sdk"]
    gpt = source.resolve_selection(RoutePick(model="databricks-gpt-5-5"), harnesses, catalog)
    claude = source.resolve_selection(
        RoutePick(model="databricks-claude-haiku-4-5"), harnesses, catalog
    )
    assert gpt is not None and gpt.harness == "codex"
    assert claude is not None and claude.harness == "claude-sdk"


def test_resolve_selection_never_leaves_the_offered_harnesses() -> None:
    """pi alone on offer: a barred pick is substituted or declined, never redirected away.

    The redirect targets (codex / claude-sdk) are not candidates here, so
    choosing one would hand a child a harness its parent's family forbids.
    """
    # pi (not a routed harness, plan 3k) declines when the fallback is barred —
    # the session keeps its default.
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    catalog = {"pi": list(infer_models("pi") or ())}
    # haiku's claude fallback (sonnet-5) is servable and unbarred on pi.
    haiku = source.resolve_selection(
        RoutePick(model="databricks-claude-haiku-4-5"), ["pi"], catalog
    )
    assert haiku is not None
    assert haiku.harness == "pi"
    assert not harness_bars_model("pi", haiku.model)
    # luna's gpt fallback is luna itself, which pi bars → honest decline (None).
    # A decline never returns a harness outside the offer, so the invariant holds.
    luna = source.resolve_selection(RoutePick(model="gpt-5-6-luna"), ["pi"], catalog)
    assert luna is None


# ── GLM arm resolves to the gateway's model-route spelling ─────────────────
#
# glm has no local codex endpoint; the arm resolves through apply_servable_alias
# to the ``system.ai.glm-5-2`` model route the gateway actually serves.


def _substitute(arm: str, models: Sequence[str], harness: str = "codex") -> str:
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(model_prefixes=["databricks-", "system.ai."])
    resolved = source.resolve_selection(RoutePick(model=arm), [harness], {harness: list(models)})
    assert resolved is not None, f"{arm} resolved to nothing"
    assert resolved.raw_model == arm
    return resolved.model


@pytest.mark.parametrize(
    "catalog",
    [
        ("databricks-gpt-5-5", "databricks-glm-5-2"),
        ("databricks-gpt-5-5", "system.ai.glm-5-2"),
        ("databricks-gpt-5-5", "databricks-glm-5-2", "system.ai.glm-5-2"),
        # The picker's dotted spelling still keys to the dashed arm.
        ("databricks-gpt-5-5", "databricks-glm-5.2"),
    ],
)
def test_glm_arm_applies_the_gateway_model_route_spelling(catalog: Sequence[str]) -> None:
    """The glm arm applies as ``system.ai.glm-5-2`` whatever the catalog spells."""
    assert _substitute("glm-5-2", catalog) == "system.ai.glm-5-2"


def test_servable_alias_leaves_other_arms_alone() -> None:
    from omnigent.server.smart_routing import apply_servable_alias

    for model in ("databricks-gpt-5-5", "system.ai.claude-opus-4-8", "databricks-kimi-k2-6"):
        assert apply_servable_alias(model) == model


def test_aliased_glm_pick_reports_no_substitution() -> None:
    """The alias is a spelling, so the decision must not show a swap arrow."""
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource, _bare_id

    source = TaskV1RouteOptionSource(model_prefixes=["databricks-", "system.ai."])
    resolved = source.resolve_selection(
        RoutePick(model="glm-5-2"),
        ["codex"],
        {"codex": ["databricks-gpt-5-5", "databricks-glm-5-2"]},
    )
    assert resolved is not None
    assert resolved.model == "system.ai.glm-5-2"
    assert _bare_id(resolved.model) == _bare_id(resolved.raw_model) == "glm-5-2"


# ── routing_settings / routing_last_error accessors ───────────────────────


def test_routing_settings_reads_the_caps_it_is_handed() -> None:
    from omnigent.server.smart_routing import RoutingSettings, routing_settings

    settings = RoutingSettings(router_name="task_v9")
    assert routing_settings(_SettingsCaps(routing_settings=settings)) is settings


def test_routing_settings_defaults_without_caps_or_settings() -> None:
    from omnigent.server.smart_routing import RoutingSettings, routing_settings

    with patch("omnigent.runtime._globals._caps", new=None):
        assert routing_settings() == RoutingSettings()
    assert routing_settings(_SettingsCaps(routing_settings="not-settings")) == RoutingSettings()


def test_routing_settings_falls_back_to_the_process_globals() -> None:
    from omnigent.server.smart_routing import RoutingSettings, routing_settings

    settings = RoutingSettings(selection_model="gpt-5-4-mini")
    with patch("omnigent.runtime._globals._caps", new=_SettingsCaps(routing_settings=settings)):
        assert routing_settings() is settings


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (object(), None),  # a custom client predating the protocol field
        (FakeRoutingClient(None), None),
        (_LastError(None), None),
        (_LastError(""), None),
        (_LastError(123), None),
        (_LastError("router returned HTTP 401"), "router returned HTTP 401"),
    ],
)
def test_routing_last_error_normalizes_to_a_non_empty_string(
    client: Any, expected: str | None
) -> None:
    from omnigent.server.smart_routing import routing_last_error

    assert routing_last_error(client) == expected


def test_llm_routing_client_starts_with_no_last_error() -> None:
    assert LLMRoutingClient(_FakeLLMClient({})).last_error is None


@pytest.mark.asyncio
async def test_llm_routing_client_records_last_error_on_fail_open() -> None:
    class _Boom:
        async def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("judge exploded")

    client = LLMRoutingClient(_Boom())
    assert await client.route("hi", {"claude-sdk": ["databricks-claude-haiku-4-5"]}) is None
    assert client.last_error is not None
    assert "judge exploded" in client.last_error


# ── model_prefixes flow from RoutingSettings to the seam ───────────────────

_PREFIXED_CODEX_CATALOG: dict[str, list[str]] = {
    "codex": ["databricks-gpt-5-6-sol", "databricks-gpt-5-5-pro"]
}


def test_route_option_source_takes_model_prefixes_from_the_settings() -> None:
    from omnigent.server.smart_routing import RoutePick, RoutingSettings, route_option_source

    caps = _SettingsCaps(routing_settings=RoutingSettings(model_prefixes=("databricks-",)))
    with patch("omnigent.runtime._globals._caps", new=caps):
        resolved = route_option_source().resolve_selection(
            RoutePick(model="gpt-5-6-sol"), ["codex"], _PREFIXED_CODEX_CATALOG
        )
    assert resolved is not None
    assert resolved.model == "databricks-gpt-5-6-sol"


def test_route_option_source_defaults_to_the_shared_catalog_prefixes() -> None:
    """A deployment with no ``routing:`` block still matches catalog ids.

    The zero-config Databricks path resolves picks with the same prefix list
    the client uses; an empty default here silently downgraded a servable arm
    (a routed gpt-5-6-sol landing on gpt-5-5-pro).
    """
    from omnigent.server.smart_routing import RoutePick, RoutingSettings, route_option_source

    with patch("omnigent.runtime._globals._caps", new=_SettingsCaps(RoutingSettings())):
        resolved = route_option_source().resolve_selection(
            RoutePick(model="gpt-5-6-sol"), ["codex"], _PREFIXED_CODEX_CATALOG
        )
    assert resolved is not None
    assert resolved.raw_model == "gpt-5-6-sol"
    assert resolved.model == "databricks-gpt-5-6-sol"


def test_explicit_model_prefixes_win_over_the_settings() -> None:
    from omnigent.server.smart_routing import RoutePick, RoutingSettings, route_option_source

    catalog = {"codex": ["system.ai.gpt-5-6-sol"]}
    caps = _SettingsCaps(routing_settings=RoutingSettings(model_prefixes=("databricks-",)))
    with patch("omnigent.runtime._globals._caps", new=caps):
        resolved = route_option_source(model_prefixes=["system.ai."]).resolve_selection(
            RoutePick(model="gpt-5-6-sol"), ["codex"], catalog
        )
    assert resolved is not None
    assert resolved.model == "system.ai.gpt-5-6-sol"


# ── The frozen router tables are deployment-overridable ───────────────────
#
# The arm menus, the served-spelling aliases and the current-generation arms are
# facts probed against ONE gateway's catalog. A managed deployment fronting a
# different catalog sets them in its ``routing:`` block; the module constants
# stay the default so nothing changes for a deployment that names none of them.


def test_route_option_source_offers_a_configured_arm_menu() -> None:
    from omnigent.server.smart_routing import RoutingSettings, route_option_source

    settings = RoutingSettings(menus={"codex": ("acme-large", "acme-small")})
    options = route_option_source(settings).build_route_options(
        ["codex"], {"codex": ["databricks-gpt-5-4"]}
    )
    # The configured arms are injected and the default task_v1 ones are not.
    assert [o.model for o in options] == ["gpt-5-4", "acme-large", "acme-small"]


def test_a_configured_menu_applies_to_a_router_other_than_task_v1() -> None:
    """The router-name gate only guards the DEFAULT menu, not a configured one."""
    from omnigent.server.smart_routing import RoutingSettings, route_option_source

    settings = RoutingSettings(router_name="task_v9", menus={"codex": ("acme-large",)})
    options = route_option_source(settings).build_route_options(
        ["codex"], {"codex": ["databricks-gpt-5-4"]}
    )
    assert [o.model for o in options] == ["gpt-5-4", "acme-large"]


def test_an_unconfigured_menu_stays_off_for_a_router_other_than_task_v1() -> None:
    from omnigent.server.smart_routing import RoutingSettings, route_option_source

    settings = RoutingSettings(router_name="task_v9")
    options = route_option_source(settings).build_route_options(
        ["codex"], {"codex": ["databricks-gpt-5-4"]}
    )
    assert [o.model for o in options] == ["gpt-5-4"]


def test_an_empty_configured_menu_means_no_arms() -> None:
    """``menus: {}`` is honoured, not treated as absent."""
    from omnigent.server.smart_routing import RoutingSettings, route_option_source

    settings = RoutingSettings(menus={})
    options = route_option_source(settings).build_route_options(
        ["codex"], {"codex": ["databricks-gpt-5-4"]}
    )
    assert [o.model for o in options] == ["gpt-5-4"]


def test_a_configured_alias_maps_a_pick_onto_the_served_spelling() -> None:
    from omnigent.server.smart_routing import RoutePick, RoutingSettings, route_option_source

    settings = RoutingSettings(
        model_prefixes=("databricks-",),
        servable_aliases={"gpt-5-6-sol": "acme.serving.sol"},
    )
    resolved = route_option_source(settings).resolve_selection(
        RoutePick(model="gpt-5-6-sol"), ["codex"], {"codex": ["databricks-gpt-5-6-sol"]}
    )
    assert resolved is not None
    assert (resolved.model, resolved.raw_model) == ("acme.serving.sol", "gpt-5-6-sol")


def test_apply_servable_alias_reads_the_deployment_table() -> None:
    from omnigent.server.smart_routing import RoutingSettings, apply_servable_alias

    settings = RoutingSettings(servable_aliases={"gpt-5-6-sol": "acme.serving.sol"})
    with patch("omnigent.runtime._globals._caps", new=_SettingsCaps(settings)):
        assert apply_servable_alias("databricks-gpt-5-6-sol") == "acme.serving.sol"
        # A configured table replaces the default one wholesale.
        assert apply_servable_alias("databricks-glm-5-2") == "databricks-glm-5-2"
    assert apply_servable_alias("databricks-glm-5-2") == "system.ai.glm-5-2"


def test_infer_models_takes_the_current_generation_arms_from_the_settings() -> None:
    from omnigent.server.smart_routing import RoutingSettings

    settings = RoutingSettings(current_generation_models={"gpt": ("databricks-acme-1",)})
    with patch("omnigent.runtime._globals._caps", new=_SettingsCaps(settings)):
        models = infer_models("codex")
    assert models is not None
    assert models[-1] == "databricks-acme-1"
    assert "databricks-glm-5-2" not in models
    # The curated cost/capability spread is not a deployment knob.
    assert models[0] == "databricks-gpt-5-4-nano"


def test_task_v1_claude_arms_follows_a_configured_menu() -> None:
    """The claude-native alias pins can't drift from the deployment's own menu."""
    from omnigent.server.smart_routing import RoutingSettings, task_v1_claude_arms

    settings = RoutingSettings(menus={"cc": ("acme-opus", "acme-sonnet")})
    with patch("omnigent.runtime._globals._caps", new=_SettingsCaps(settings)):
        assert task_v1_claude_arms() == ("acme-opus", "acme-sonnet")
    assert task_v1_claude_arms() == ("claude-opus-4-8", "claude-sonnet-5")


def test_parse_routing_tables_reads_every_table() -> None:
    from omnigent.reasoning_effort import ModelEffortCaps
    from omnigent.server.smart_routing import parse_routing_tables

    tables = parse_routing_tables(
        {
            "menus": {"cc": ["acme-opus"], "codex": "acme-sol"},
            "servable_aliases": {"ACME.Sol": "acme.serving.sol"},
            "current_generation_models": {"gpt": ["acme-sol"]},
            "effort_caps": {"acme-sol": {"fallback": "medium", "unsupported": ["xhigh", "max"]}},
        }
    )
    assert tables["menus"] == {"cc": ("acme-opus",), "codex": ("acme-sol",)}
    # Alias keys are the comparison spelling, so a dotted/upper id still matches.
    assert tables["servable_aliases"] == {"acme-sol": "acme.serving.sol"}
    assert tables["current_generation_models"] == {"gpt": ("acme-sol",)}
    assert tables["model_effort_caps"] == ModelEffortCaps(
        fallback={"acme-sol": "medium"}, unsupported={"acme-sol": frozenset({"xhigh", "max"})}
    )


@pytest.mark.parametrize("cfg", [None, {}, {"router_name": "task_v1"}, {"menus": 5}])
def test_parse_routing_tables_leaves_absent_or_malformed_tables_at_their_default(
    cfg: Any,  # type: ignore[explicit-any]
) -> None:
    from omnigent.server.smart_routing import parse_routing_tables

    tables = parse_routing_tables(cfg)
    assert tables["menus"] is None
    assert tables["servable_aliases"] is None
    assert tables["current_generation_models"] is None
    assert tables["model_effort_caps"] is None


def test_configured_tables_reach_the_behaviour_through_the_config_block() -> None:
    """End to end: a ``routing:`` block's tables drive the seam's decisions."""
    from omnigent.cli import parse_routing_settings
    from omnigent.server.smart_routing import RoutePick, route_option_source

    settings = parse_routing_settings(
        {
            "model_prefix": ["databricks-"],
            "menus": {"codex": ["acme-sol"]},
            "servable_aliases": {"acme-sol": "acme.serving.sol"},
        }
    )
    source = route_option_source(settings)
    catalog = {"codex": ["databricks-gpt-5-4"]}
    assert [o.model for o in source.build_route_options(["codex"], catalog)] == [
        "gpt-5-4",
        "acme-sol",
    ]
    # The injected arm is unservable here, so it substitutes — and the pick that
    # IS servable resolves through the configured alias.
    resolved = source.resolve_selection(
        RoutePick(model="acme-sol"), ["codex"], {"codex": ["databricks-acme-sol"]}
    )
    assert resolved is not None
    assert resolved.model == "acme.serving.sol"


# ── RoutingSettings parsing (cli) ─────────────────────────────────────────

# Sentinel: an unset ``model_prefix`` leaves the module's shared list in place,
# so client and server never disagree about a catalog id's prefix.
_PREFIXES_DEFAULT = "<MODEL_ID_PREFIXES>"


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        # No routing block at all: every knob takes its documented default.
        (
            None,
            {
                "router_name": "task_v1",
                "selection_model": None,
                "model_prefixes": _PREFIXES_DEFAULT,
            },
        ),
        # Every key set: each one is read.
        (
            {
                "provider": "external",
                "router_name": "task_v2",
                "selection_model": "gpt-5-4-mini",
                "model_prefix": ["databricks-", "system.ai."],
            },
            {
                "router_name": "task_v2",
                "selection_model": "gpt-5-4-mini",
                "model_prefixes": ("databricks-", "system.ai."),
            },
        ),
        # A scalar prefix is wrapped; a malformed one falls back to the shared list.
        ({"model_prefix": "acme."}, {"model_prefixes": ("acme.",)}),
        ({"model_prefix": 5}, {"model_prefixes": _PREFIXES_DEFAULT}),
        # An absent key falls back; an EXPLICIT empty list means bare catalog ids.
        ({"router_name": "task_v2"}, {"model_prefixes": _PREFIXES_DEFAULT}),
        ({"model_prefix": []}, {"model_prefixes": ()}),
    ],
)
def test_parse_routing_settings(
    cfg: dict[str, Any] | None,  # type: ignore[explicit-any]
    expected: dict[str, Any],  # type: ignore[explicit-any]
) -> None:
    from omnigent.cli import parse_routing_settings
    from omnigent.server.smart_routing import MODEL_ID_PREFIXES

    settings = parse_routing_settings(cfg)
    for field, want in expected.items():
        got = getattr(settings, field)
        assert got == (MODEL_ID_PREFIXES if want is _PREFIXES_DEFAULT else want), field


# ── Default-on AIGW routing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cfg", "global_providers", "expected_profile"),
    [
        # The default databricks provider in the server's own config.
        (
            {
                "providers": {
                    "other": {"kind": "key"},
                    "ws": {
                        "kind": "databricks",
                        "profile": "test-routing-profile",
                        "default": True,
                    },
                }
            },
            None,
            "test-routing-profile",
        ),
        # No providers in the server config: the global block is consulted.
        (
            {},
            {"ws": {"kind": "databricks", "profile": "oss"}},
            "oss",
        ),
    ],
)
def test_default_on_synthesizes_client_for_databricks_provider(
    cfg: dict[str, Any],  # type: ignore[explicit-any]
    global_providers: dict[str, Any] | None,  # type: ignore[explicit-any]
    expected_profile: str,
) -> None:
    from omnigent.cli import _build_default_databricks_routing_client, parse_routing_settings
    from omnigent.server.smart_routing import ExternalRoutingClient

    host = f"https://{expected_profile}.cloud.databricks.com"
    creds = MagicMock()
    creds.host = host
    with (
        patch(
            "omnigent.onboarding.provider_config.load_config",
            return_value={"providers": global_providers or {}},
        ),
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            return_value=creds,
        ),
    ):
        client = _build_default_databricks_routing_client(cfg, parse_routing_settings(None))

    assert isinstance(client, ExternalRoutingClient)
    # The two things the synthesis decides: which workspace to POST to and
    # which profile mints its token. The router name and model prefixes come
    # straight from the settings, pinned in test_parse_routing_settings.
    assert client._url == f"{host}/ai-gateway/routing/v1/routes:select"
    assert client._databricks_profile == expected_profile


@pytest.mark.parametrize(
    ("cfg", "global_providers", "resolve_error"),
    [
        # The server config names providers, none of kind "databricks".
        ({"providers": {"openrouter": {"kind": "gateway"}}}, {}, None),
        # No providers in the server config either, and the global block has
        # no databricks provider to fall back to.
        ({}, {"openrouter": {"kind": "gateway"}}, None),
        # A databricks provider whose workspace host can't be resolved: no
        # url to POST to, so no client rather than a broken one.
        (
            {"providers": {"ws": {"kind": "databricks", "profile": "gone"}}},
            {},
            OSError("no such profile"),
        ),
    ],
)
def test_default_on_skips_without_a_routable_databricks_workspace(
    cfg: dict[str, Any],  # type: ignore[explicit-any]
    global_providers: dict[str, Any],  # type: ignore[explicit-any]
    resolve_error: Exception | None,
) -> None:
    from omnigent.cli import _build_default_databricks_routing_client, parse_routing_settings

    creds = MagicMock()
    creds.host = "https://ws.cloud.databricks.com"
    with (
        patch(
            "omnigent.onboarding.provider_config.load_config",
            return_value={"providers": global_providers},
        ),
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            side_effect=resolve_error,
            return_value=creds,
        ),
    ):
        client = _build_default_databricks_routing_client(cfg, parse_routing_settings(None))
    assert client is None


@pytest.mark.asyncio
async def test_route_session_harness_keeps_unknown_pi_compatibility() -> None:
    """Missing wire metadata from an older runner does not imply incompatibility."""
    expected = RoutingResult(model="databricks-claude-sonnet-4-6", rationale="mid", harness="pi")
    client = _catalog_client()
    payload = client.get.return_value.json.return_value
    sonnet = next(
        row for row in payload["workers"]["pi"]["models"] if row["id"].endswith("sonnet-4-6")
    )
    sonnet.pop("wire_apis")
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))

    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "quick q", session_id="conv_123", runner_client=client
        )

    assert harness == "pi"
    assert model == "databricks-claude-sonnet-4-6"
    assert error is None


@pytest.mark.asyncio
async def test_route_session_harness_falls_back_by_model_when_harness_absent() -> None:
    """When the router returns no harness, fall back to finding it by model."""
    expected = RoutingResult(
        model="databricks-gpt-5-4-nano",
        rationale="cheap task",
        harness=None,
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, _error = await route_session_harness(
            "what time is it?",
            session_id="conv_123",
            runner_client=_catalog_client(),
        )
    # codex precedes pi in _AUTO_ROUTING_HARNESSES, so a GPT model owned by both
    # deterministically resolves to codex.
    assert harness == "codex"
    assert model == "databricks-gpt-5-4-nano"


# ── Single resolution authority (no double-resolution) ─────────────────────


# The client owns resolution; the server must not re-resolve its pick (a second
# pass with a different prefix list once downgraded routed gpt-5-6-luna to
# gpt-5-4-mini). A router pick spelled bare is the same model, not a divergent raw.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "model", "raw_model", "catalog_models"),
    [
        (
            "claude-native",
            "databricks-claude-opus-4-8",
            "claude-opus-4-8",
            ["databricks-claude-opus-4-8", "databricks-claude-sonnet-5"],
        ),
        (
            "codex-native",
            "databricks-gpt-5-6-luna",
            "gpt-5-6-luna",
            ["databricks-gpt-5-4-mini", "databricks-gpt-5-6-luna"],
        ),
    ],
)
async def test_route_session_harness_applies_the_clients_pick_verbatim(
    harness: str, model: str, raw_model: str, catalog_models: list[str]
) -> None:
    expected = RoutingResult(
        model=model, rationale="client-resolved", harness=harness, raw_model=raw_model
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        got_harness, got_model, verdict, error = await route_session_harness(
            "do the thing",
            harness_candidates=(harness,),
            catalog={harness: catalog_models},
        )
    assert (got_harness, got_model) == (harness, model)
    assert error is None
    assert verdict is not None and "raw_model" not in verdict


@pytest.mark.asyncio
async def test_route_turn_substitutes_a_model_the_harness_gateway_bars() -> None:
    """A turn cannot change harness, so a barred pick moves to its family fallback."""
    client = FakeRoutingClient(
        RoutingResult(
            model="databricks-claude-haiku-4-5",
            rationale="cheap arm",
            harness="pi",
            raw_model="claude-haiku-4-5",
        )
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, verdict = await route_turn("pi", "quick lookup")
    # haiku 400s on pi's completions path; the claude fallback (sonnet-5) does not.
    assert model == "databricks-claude-sonnet-5"
    assert verdict is not None and verdict["raw_model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_route_turn_declines_when_nothing_the_harness_runs_fits() -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-5", rationale="capable", harness="pi")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, verdict = await route_turn(
            "pi", "hard task", catalog=["databricks-gpt-5-5", "databricks-gpt-5-5-pro"]
        )
    assert (model, verdict) == (None, None)


def test_resolve_selection_accepts_an_already_local_id() -> None:
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(model_prefixes=["databricks-", "system.ai."])
    catalog = {"claude-native": ["databricks-claude-opus-4-8"]}
    resolved = source.resolve_selection(
        RoutePick(model="databricks-claude-opus-4-8"), ["claude-native"], catalog
    )
    assert resolved is not None
    assert resolved.harness == "claude-native"
    assert resolved.model == "databricks-claude-opus-4-8"


# ── Prefix stripping never leaves a separator ──────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "model", "expected"),
    [
        ("system.ai", "system.ai.claude-opus-5", "claude-opus-5"),
        ("system.ai.", "system.ai.claude-opus-5", "claude-opus-5"),
        ("databricks", "databricks-gpt-5-6-sol", "gpt-5-6-sol"),
        ("databricks-", "databricks-gpt-5-6-sol", "gpt-5-6-sol"),
    ],
)
def test_to_router_id_never_yields_a_leading_separator(
    prefix: str, model: str, expected: str
) -> None:
    from omnigent.server.smart_routing import TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(model_prefixes=[prefix])
    assert source.to_router_id(model) == expected


def test_dotless_system_ai_prefix_still_offers_routable_arms() -> None:
    from omnigent.server.smart_routing import TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(model_prefixes=["system.ai"])
    options = source.build_route_options(
        ["claude-native"], {"claude-native": ["system.ai.claude-opus-5"]}
    )
    assert "claude-opus-5" in [option.model for option in options]
    assert not any(option.model.startswith(".") for option in options)


@pytest.mark.asyncio
async def test_route_turn_never_offers_pi_a_model_its_gateway_bars() -> None:
    """The barred rows are pruned before the router ever sees them."""
    offered: dict[str, list[str]] = {}

    class _CapturingClient:
        async def route(
            self, _message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            offered.update(available_models)
            return None

    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=_CapturingClient())):
        await route_turn("pi", "hello")
    assert offered, "pi should still have candidates"
    for model in offered["pi"]:
        assert not harness_bars_model("pi", model), model


@pytest.mark.asyncio
async def test_route_turn_declines_when_every_candidate_is_barred() -> None:
    client = FakeRoutingClient(RoutingResult(model="databricks-gpt-5-5", rationale="x"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, verdict = await route_turn("pi", "hi", catalog=["databricks-gpt-5-6-luna"])
    assert (model, verdict) == (None, None)


# ── Picker spellings with dots ──────────────────────────────────────────────
#
# Non-Databricks codex panes spell rows ``gpt-5.6-sol`` while the router's arms
# are dashed, so unnormalized ids missed every substitution chain.

_DOTTED_CODEX_CATALOG: list[str] = ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.5", "gpt-5.4-mini"]


@pytest.mark.parametrize(
    ("arm", "expected"),
    [
        ("gpt-5-6-sol", "gpt-5.6-sol"),
        ("gpt-5-6-luna", "gpt-5.6-luna"),
        # glm has no local endpoint; it falls back to luna, which this pane serves.
        ("glm-5-2", "gpt-5.6-luna"),
    ],
)
def test_dot_spelled_picker_rows_match_the_router_arms(arm: str, expected: str) -> None:
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    resolved = source.resolve_selection(
        RoutePick(model=arm), ["codex"], {"codex": list(_DOTTED_CODEX_CATALOG)}
    )
    assert resolved is not None and resolved.model == expected


def test_dot_spelled_rows_are_not_offered_twice() -> None:
    from omnigent.server.smart_routing import TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    options = source.build_route_options(["codex"], {"codex": list(_DOTTED_CODEX_CATALOG)})
    models = [option.model for option in options]
    assert len(models) == len(set(models))
    # One row per model, spelled as the router's own menu spells it.
    assert "gpt-5-6-luna" in models and "gpt-5.6-luna" not in models
    assert "gpt-5-6-sol" in models and "gpt-5.6-sol" not in models


# ── A child never escapes its allowed family ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["databricks-claude-haiku-4-5", "databricks-gpt-5-5"])
async def test_route_session_harness_keeps_a_pi_child_on_pi(model: str) -> None:
    """``allowed_family="pi"`` must never yield codex or claude-sdk."""
    # A pi child either stays on pi or declines (harness None) — a decline is
    # acceptable and never cross-harness. haiku steps to its servable claude
    # fallback (sonnet-5) and stays on pi; gpt-5-5 is barred on pi and its gpt
    # fallback (luna) is barred too with no pi fallback, so it declines.
    client = FakeRoutingClient(RoutingResult(model=model, rationale="x", harness="pi"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        harness, routed, _verdict, error = await route_session_harness(
            "do it", allowed_family="pi"
        )
    assert harness in ("pi", None), f"a pi child must stay on pi or decline, got {harness!r}"
    if harness == "pi":
        assert routed is not None and not harness_bars_model("pi", routed), routed
        assert error is None
    else:
        # A decline hands back nothing (never a non-pi harness), with a reason.
        assert routed is None
        assert error is not None


@pytest.mark.asyncio
async def test_route_session_harness_declines_when_no_offered_harness_fits() -> None:
    """Nothing on offer runs the pick and nothing substitutes: decline, loudly."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-5", rationale="x", harness="pi")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        harness, routed, verdict, error = await route_session_harness(
            "do it",
            allowed_family="pi",
            catalog={"pi": ["databricks-gpt-5-5"]},
        )
    assert (harness, routed, verdict) == (None, None, None)
    assert error is not None


# ── The rationale paraphrases the prompt, so it stays off INFO ──────────────


@pytest.mark.asyncio
async def test_route_turn_keeps_the_rationale_off_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-4", rationale="secret prompt paraphrase")
    )
    with (
        caplog.at_level(logging.INFO, logger="omnigent.server.smart_routing"),
        patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)),
    ):
        model, _verdict = await route_turn("codex", "hello")
    assert model == "databricks-gpt-5-4"
    assert "secret prompt paraphrase" not in caplog.text


@pytest.mark.asyncio
async def test_route_session_harness_keeps_the_rationale_off_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-4", rationale="secret prompt paraphrase")
    )
    with (
        caplog.at_level(logging.INFO, logger="omnigent.server.smart_routing"),
        patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)),
    ):
        harness, _model, _verdict, _error = await route_session_harness("hello")
    assert harness is not None
    assert "secret prompt paraphrase" not in caplog.text


# ── Router source selection ─────────────────────────────────────────────────
#
# Gateway backing selects WHICH router answers, not whether Smart Routing is
# offered. Off the gateway the external client's ``databricks-*`` picks are
# unreachable from the pane, so the built-in judge answers instead — and the
# static ``infer_models`` table, which is nothing but ``databricks-*`` ids, must
# not be offered as candidates at all.


def _both_backends(external: FakeRoutingClient, local: FakeRoutingClient) -> RoutingBackends:
    return RoutingBackends(external=external, local=local)


@pytest.mark.asyncio
async def test_route_session_harness_declines_rather_than_offer_gateway_ids_off_gateway() -> None:
    local = FakeRoutingClient(
        RoutingResult(model="databricks-claude-opus-4-8", rationale="big", harness="claude-native")
    )
    caps = FakeCaps(routing_client=local, routing_backends=RoutingBackends(local=local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "refactor everything",
            harness_candidates=("claude-native", "codex-native"),
            catalog={},
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert (harness, model, verdict) == (None, None, None)
    assert error is not None
    # The router was never called, so no databricks-* id could have been offered.
    assert local.offered == []


@pytest.mark.asyncio
async def test_route_turn_declines_rather_than_offer_gateway_ids_off_gateway() -> None:
    local = FakeRoutingClient(RoutingResult(model="databricks-claude-opus-4-8", rationale="big"))
    caps = FakeCaps(routing_client=local, routing_backends=RoutingBackends(local=local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn(
            "claude-native",
            "refactor everything",
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert (model, verdict) == (None, None)
    assert local.offered == []


@pytest.mark.asyncio
async def test_route_session_harness_skips_the_static_top_up_off_gateway() -> None:
    """A partial pre-session catalog is offered as-is, not topped up."""
    local = FakeRoutingClient(
        RoutingResult(model="claude-sonnet-4-6", rationale="mid", harness="claude-native")
    )
    caps = FakeCaps(routing_client=local, routing_backends=RoutingBackends(local=local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, _verdict, error = await route_session_harness(
            "hello",
            harness_candidates=("claude-native", "codex-native"),
            catalog={"claude-native": ["claude-sonnet-4-6", "claude-opus-4-8"]},
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert error is None
    assert (harness, model) == ("claude-native", "claude-sonnet-4-6")
    # Only the harness the caller could answer for; codex-native was NOT topped
    # up from the static table.
    assert list(local.offered[0]) == ["claude-native"]
    assert not any(m.startswith("databricks-") for m in local.offered[0]["claude-native"])


@pytest.mark.asyncio
async def test_route_turn_skips_the_static_table_off_gateway_but_takes_the_catalog() -> None:
    local = FakeRoutingClient(RoutingResult(model="claude-sonnet-4-6", rationale="mid"))
    caps = FakeCaps(routing_client=local, routing_backends=RoutingBackends(local=local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn(
            "claude-native",
            "hello",
            catalog=["claude-sonnet-4-6", "claude-opus-4-8"],
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert model == "claude-sonnet-4-6"
    assert verdict is not None
    assert local.offered == [{"claude-native": ["claude-sonnet-4-6", "claude-opus-4-8"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_backed", "expected_source"),
    [(True, "databricks-aigw"), (False, "oss-llm")],
)
async def test_route_turn_picks_the_router_gateway_backing_allows(
    gateway_backed: bool, expected_source: str
) -> None:
    external = FakeRoutingClient(RoutingResult(model="databricks-gpt-5-4", rationale="external"))
    local = FakeRoutingClient(RoutingResult(model="databricks-gpt-5-5", rationale="local"))
    caps = FakeCaps(routing_client=external, routing_backends=_both_backends(external, local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn("codex", "hello", gateway_backed=gateway_backed)
    assert verdict is not None
    assert verdict["router_source"] == expected_source
    if gateway_backed:
        assert (model, local.offered) == ("databricks-gpt-5-4", [])
    else:
        assert (model, external.offered) == ("databricks-gpt-5-5", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_backed", "expected_source"),
    [(True, "databricks-aigw"), (False, "oss-llm")],
)
async def test_route_session_harness_picks_the_router_gateway_backing_allows(
    gateway_backed: bool, expected_source: str
) -> None:
    external = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-4", rationale="external", harness="codex")
    )
    local = FakeRoutingClient(
        RoutingResult(model="databricks-gpt-5-5", rationale="local", harness="codex")
    )
    caps = FakeCaps(routing_client=external, routing_backends=_both_backends(external, local))
    with patch("omnigent.runtime._globals._caps", new=caps):
        _harness, model, verdict, error = await route_session_harness(
            "hello", gateway_backed=gateway_backed
        )
    assert error is None
    assert verdict is not None
    assert verdict["router_source"] == expected_source
    assert model == ("databricks-gpt-5-4" if gateway_backed else "databricks-gpt-5-5")


@pytest.mark.asyncio
async def test_routing_is_unavailable_when_neither_backend_is_configured() -> None:
    caps = FakeCaps(routing_client=None, routing_backends=RoutingBackends())
    with patch("omnigent.runtime._globals._caps", new=caps):
        assert await route_turn("codex", "hello") == (None, None)
        harness, model, verdict, error = await route_session_harness("hello")
    assert (harness, model, verdict) == (None, None, None)
    assert error == "Smart routing is not configured on this server."


@pytest.mark.asyncio
async def test_external_only_deployment_cannot_route_off_the_gateway() -> None:
    """No built-in judge means an off-gateway harness has no source at all."""
    external = FakeRoutingClient(RoutingResult(model="databricks-gpt-5-4", rationale="x"))
    caps = FakeCaps(routing_client=external, routing_backends=RoutingBackends(external=external))
    with patch("omnigent.runtime._globals._caps", new=caps):
        assert await route_turn("codex", "hello", gateway_backed=False) == (None, None)
    assert external.offered == []


@pytest.mark.asyncio
async def test_a_legacy_single_routing_client_is_classified_as_the_oss_judge() -> None:
    """``routing_backends`` unset derives the pair from ``routing_client``."""
    client = FakeRoutingClient(RoutingResult(model="databricks-gpt-5-4", rationale="x"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        _model, verdict = await route_turn("codex", "hello", gateway_backed=True)
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"


@pytest.mark.asyncio
async def test_llm_routing_client_serves_a_cross_harness_verdict_without_a_raw_model() -> None:
    """The built-in judge picks from the menu, so its pick needs no resolution."""
    llm = _FakeLLMClient({"model": "databricks-gpt-5-5", "harness": "codex-native"})
    client = LLMRoutingClient(llm)
    caps = FakeCaps(routing_client=client, routing_backends=RoutingBackends(local=client))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "hello",
            harness_candidates=("claude-native", "codex-native"),
            catalog={
                "claude-native": ["databricks-claude-sonnet-5"],
                "codex-native": ["databricks-gpt-5-5"],
            },
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert error is None
    assert (harness, model) == ("codex-native", "databricks-gpt-5-5")
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    assert "raw_model" not in verdict


# ── Fail open, fail fast: every failure shape a routing call presents ──────
#
# The owner's ruling is that a routing failure must never block the user's
# work, and must not block it slowly either. These cover the two clients and
# the turn-path boundary against the five failure modes a real call produces.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "handler"),
    [
        ("gateway_500", lambda _r: __import__("httpx").Response(500, text="upstream down")),
        (
            "unauthorized",
            lambda _r: __import__("httpx").Response(
                401, json={"error_code": 401, "message": "invalid access token"}
            ),
        ),
        ("malformed_body", lambda _r: __import__("httpx").Response(200, text="<html>nope</html>")),
        (
            "not_a_verdict",
            lambda _r: __import__("httpx").Response(200, json={"unexpected": "shape"}),
        ),
    ],
)
async def test_external_router_declines_with_a_reason_on_every_failure(
    label: str,
    handler: Any,
) -> None:
    """
    Each failure returns ``None`` AND names itself in ``last_error``.

    A bare ``None`` reads identically to "the router had no opinion", so the
    reason is what lets the caller show the user why nothing was routed
    instead of silently ignoring the toggle they turned on.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None
    assert client.last_error, label


@pytest.mark.asyncio
async def test_external_router_declines_when_the_transport_never_connects() -> None:
    """An unreachable router is a decline, not an exception out of ``route``."""
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None
    assert client.last_error is not None
    assert "router request failed" in client.last_error


@pytest.mark.asyncio
async def test_external_router_names_a_timeout_that_carries_no_message() -> None:
    """A messageless failure still names its cause on the decision card.

    httpx's timeouts stringify to ``""``, so the chip read "Routing unavailable
    (router request failed: )" — a dangling colon with the reason missing,
    exactly on the fail-open path a 5s budget makes the common one.
    """
    import httpx

    from omnigent.server.smart_routing import ExternalRoutingClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with _patch_httpx(httpx.MockTransport(handler)):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None
    assert client.last_error == "router request failed: ReadTimeout"


def test_failure_detail_prefers_the_message_and_falls_back_to_the_class() -> None:
    from omnigent.server.smart_routing import failure_detail

    assert failure_detail(ValueError("no route options")) == "no route options"
    # Whitespace is no reason either, and every httpx timeout arrives blank.
    assert failure_detail(TimeoutError("  ")) == "TimeoutError"


@pytest.mark.asyncio
async def test_external_router_passes_its_budget_to_the_transport() -> None:
    """
    The 5s budget reaches ``httpx``, rather than only being documented.

    Asserted against the constant, not a wall clock: the point is that the
    number the ladder is built on is the number the socket gets.
    """
    import httpx

    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S, ExternalRoutingClient

    seen: list[Any] = []
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("timeout"))
        kwargs["transport"] = httpx.MockTransport(lambda _r: httpx.Response(503))
        return real(*args, **kwargs)

    client = ExternalRoutingClient(base_url="https://host/v1", router_name="task_v0")
    with patch("httpx.AsyncClient", factory):
        assert await client.route("hi", {"claude": ["claude-opus-4-8"]}) is None
    assert seen == [ROUTING_REQUEST_TIMEOUT_S]


@pytest.mark.asyncio
async def test_the_judge_declines_rather_than_hang_on_a_stalled_model() -> None:
    """
    A judge that never answers is a decline inside the routing budget.

    Regression: the judge inherited the server ``llm:`` block's request
    timeout (300s by default, times every fallback model), so choosing the
    built-in router as the source turned every fail-open on the hazard paths
    into a multi-minute hang. Driven with a patched ``wait_for`` so the
    assertion is against the constant rather than real elapsed time.
    """
    import asyncio

    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S, LLMRoutingClient

    class _Stalled:
        async def create(self, **kwargs: Any) -> Any:
            await asyncio.sleep(3600)

    budgets: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(awaitable: Any, timeout: float | None) -> Any:
        budgets.append(timeout)
        # Zero, so the test never actually waits out the budget.
        return await real_wait_for(awaitable, 0)

    client = LLMRoutingClient(_Stalled())
    with patch("asyncio.wait_for", new=_recording_wait_for):
        assert await client.route("hi", {"claude-sdk": ["databricks-claude-haiku-4-5"]}) is None
    assert budgets == [ROUTING_REQUEST_TIMEOUT_S]
    assert client.last_error is not None


@pytest.mark.asyncio
async def test_the_judge_is_capped_by_its_own_request_timeout_too() -> None:
    """The adapter's per-call ``timeout`` carries the routing budget as well.

    ``wait_for`` alone would abandon the coroutine but leave the underlying
    HTTP request running against a 300s socket budget, so both bounds are set.
    """
    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S, LLMRoutingClient

    seen: dict[str, Any] = {}

    class _Recording:
        async def create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise RuntimeError("no verdict today")

    client = LLMRoutingClient(_Recording())
    assert await client.route("hi", {"claude-sdk": ["databricks-claude-haiku-4-5"]}) is None
    assert seen["timeout"] == ROUTING_REQUEST_TIMEOUT_S


@pytest.mark.asyncio
async def test_route_turn_or_decline_never_raises_at_the_turn_boundary() -> None:
    """
    The turn path's fail-open boundary converts a raise into an ``error``.

    Regression: ``route_turn`` let the failure through and the events POST
    answered 500, so a router outage cost the user a message that had already
    been persisted.
    """
    from omnigent.server.smart_routing import route_turn_or_decline

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("router down")

    with patch("omnigent.server.smart_routing.route_turn", new=_boom):
        model, verdict, error = await route_turn_or_decline("claude-native", "refactor auth")
    assert model is None
    assert verdict is None
    assert error is not None
    assert "Routing call failed" in error


@pytest.mark.asyncio
async def test_route_turn_or_decline_reports_an_ordinary_no_verdict_as_no_error() -> None:
    """
    "The router had no opinion" is a decline, not a failure.

    Only a raised failure earns the ``error`` string (and with it the
    "unavailable" card): a router that answered and declined is the ordinary
    unrouted path, which already says nothing.
    """
    from omnigent.server.smart_routing import route_turn_or_decline

    async def _no_verdict(*_args: Any, **_kwargs: Any) -> Any:
        return None, None

    with patch("omnigent.server.smart_routing.route_turn", new=_no_verdict):
        model, verdict, error = await route_turn_or_decline("claude-native", "refactor auth")
    assert (model, verdict, error) == (None, None, None)
