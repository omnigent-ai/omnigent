"""Tests for the server-side intelligent model routing module.

Covers model inference, the RoutingClient protocol, the default
LLMRoutingClient, and the public ``route_turn`` entry point.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def test_models_fixture_claude_sdk() -> None:
    """claude-sdk returns the claude model list."""
    models = _models_for("claude-sdk")
    assert models is not None
    assert any("haiku" in m for m in models)
    assert any("opus" in m for m in models)
    # Ordered cheapest → most powerful
    haiku_idx = next(i for i, m in enumerate(models) if "haiku" in m)
    opus_idx = next(i for i, m in enumerate(models) if "opus" in m)
    assert haiku_idx < opus_idx


def test_models_fixture_native_harnesses() -> None:
    assert _models_for("claude-native") is not None
    assert _models_for("codex-native") is not None


def test_models_fixture_codex() -> None:
    models = _models_for("codex")
    assert models is not None
    assert any("gpt" in m for m in models)


def test_models_fixture_openai_agents() -> None:
    assert _models_for("openai-agents") is not None


def test_models_fixture_pi() -> None:
    """pi is multi-model — both Claude and GPT."""
    models = _models_for("pi")
    assert models is not None
    assert any("haiku" in m for m in models)
    assert any("gpt" in m for m in models)


def test_model_lists_cover_current_claude_generations() -> None:
    """A stale list turns a live model into an unservable arm (sonnet-5 → haiku)."""
    models = infer_models("claude-sdk")
    assert models is not None
    assert "databricks-claude-sonnet-5" in models


def test_every_router_arm_has_a_substitution_chain() -> None:
    """An arm with no chain can only be applied where its own endpoint exists."""
    from omnigent.server.smart_routing import _ARM_SUBSTITUTES, TASK_V1_MENUS

    for scenario, arms in TASK_V1_MENUS.items():
        for arm in arms:
            assert arm in _ARM_SUBSTITUTES, f"{scenario}: {arm} has no substitution chain"
            # The arm itself leads, so a workspace serving it applies it exactly.
            assert _ARM_SUBSTITUTES[arm][0] == arm


def test_substitution_chains_stay_in_the_arms_family() -> None:
    """A chain crossing families would route a turn onto an unrunnable model."""
    from omnigent.server.smart_routing import _ARM_SUBSTITUTES, _model_family

    for arm, chain in _ARM_SUBSTITUTES.items():
        assert {_model_family(m) for m in chain} == {_model_family(arm)}, arm


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


@pytest.mark.asyncio
async def test_llm_routing_client_returns_result() -> None:
    verdict = {
        "harness": "claude-sdk",
        "model": "databricks-claude-opus-4-8",
        "rationale": "hard refactor",
    }
    client = LLMRoutingClient(_FakeLLMClient(verdict))
    models = _models_for("claude-sdk")
    assert models is not None
    result = await client.route("refactor auth", {"claude-sdk": models})
    assert result is not None
    assert result.model == "databricks-claude-opus-4-8"
    assert result.rationale == "hard refactor"
    assert result.harness == "claude-sdk"


@pytest.mark.asyncio
async def test_llm_routing_client_harness_mismatch_re_resolves() -> None:
    """If the judge picks a harness that doesn't own the model, fall back."""
    claude_models = _models_for("claude-sdk")
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
    models = _models_for("claude-sdk")
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
    ("router_name", "catalog", "pick", "present", "absent", "exact_menu", "first_offered"),
    [
        # A codex-only session offers the codex menu and none of the claude
        # arms — the router must not be able to pick an unspawnable harness.
        (
            "task_v1",
            _CODEX_CATALOG_ONLY,
            {"model": "glm-5-2", "harness": "codex"},
            _CODEX_ARMS,
            _CLAUDE_ARMS,
            None,
            # The caller's own catalog id leads; the router tolerates the extra.
            "gpt-5-4",
        ),
        (
            "task_v1",
            _CLAUDE_CATALOG_ONLY,
            {"model": "claude-sonnet-5", "harness": "claude-sdk"},
            _CLAUDE_ARMS,
            _CODEX_ARMS,
            None,
            None,
        ),
        # Both harnesses spawnable: every arm of both menus is on offer.
        (
            "task_v1",
            {**_CLAUDE_CATALOG_ONLY, **_CODEX_CATALOG_ONLY},
            {"model": "claude-opus-4-8", "harness": "claude-sdk"},
            _CLAUDE_ARMS + _CODEX_ARMS,
            (),
            None,
            None,
        ),
        # A router version with no menu entry gets exactly the caller's catalog.
        (
            "task_v0",
            _CODEX_CATALOG_ONLY,
            {"model": "gpt-5-4", "harness": "codex"},
            (),
            (),
            ["gpt-5-4"],
            None,
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
    assert result is not None
    assert result.harness == pick["harness"]


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
    """gpt-5-6-sol has no endpoint here; the pick lands on the nearest one."""
    import httpx

    captured: dict[str, Any] = {}
    client = _task_v1_client()
    with _patch_httpx(
        httpx.MockTransport(
            _capturing_handler(captured, {"model": "gpt-5-6-sol", "harness": "codex"})
        )
    ):
        result = await client.route("hi", {"codex": ["databricks-gpt-5-4", "databricks-gpt-5-5"]})

    assert result is not None
    assert result.model == "databricks-gpt-5-5"
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
                "claude-sdk": ["databricks-claude-haiku-4-5"],
                "codex": ["databricks-gpt-5-4"],
            },
        )

    assert result is not None
    assert result.harness == "claude-sdk"
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
    """pi alone on offer: a barred pick is substituted, never redirected away.

    The redirect targets (codex / claude-sdk) are not candidates here, so
    choosing one would hand a child a harness its parent's family forbids.
    """
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource()
    catalog = {"pi": list(infer_models("pi") or ())}
    for pick in ("databricks-claude-haiku-4-5", "gpt-5-6-luna"):
        resolved = source.resolve_selection(RoutePick(model=pick), ["pi"], catalog)
        assert resolved is not None, pick
        assert resolved.harness == "pi", pick
        assert not harness_bars_model("pi", resolved.model), (pick, resolved.model)


# ── Substitution for unservable arms ───────────────────────────────────────
#
# A live ucode workspace lists codex endpoints roughly alphabetically, so the
# substitution must come from the arm's own table, not from catalog position.

_UCODE_CODEX_CATALOG: tuple[str, ...] = (
    "gpt-5-1-codex-max",
    "gpt-5-1-codex-mini",
    "gpt-5-2",
    "gpt-5-3-codex",
    "gpt-5-5",
    "gpt-5-mini",
    "gpt-5-nano",
)


def _substitute(arm: str, models: Sequence[str], harness: str = "codex") -> str:
    from omnigent.server.smart_routing import RoutePick, TaskV1RouteOptionSource

    source = TaskV1RouteOptionSource(model_prefixes=["databricks-", "system.ai."])
    resolved = source.resolve_selection(RoutePick(model=arm), [harness], {harness: list(models)})
    assert resolved is not None, f"{arm} resolved to nothing"
    assert resolved.raw_model == arm
    return resolved.model


_CLAUDE_TIERS = (
    "databricks-claude-haiku-4-5",
    "databricks-claude-sonnet-4-6",
    "databricks-claude-opus-4-8",
)


@pytest.mark.parametrize(
    ("arm", "catalog", "harness", "expected"),
    [
        # sol is task_v1's anchor arm, so it must not land on a nano endpoint.
        ("gpt-5-6-sol", _UCODE_CODEX_CATALOG, "codex", "gpt-5-5"),
        # luna is the cheap arm: a mini endpoint, not gpt-5-5 and not nano.
        ("gpt-5-6-luna", _UCODE_CODEX_CATALOG, "codex", "gpt-5-mini"),
        # Its chain names nothing on offer: the strongest same-family id wins.
        ("gpt-5-6-luna", ("gpt-5-nano",), "codex", "gpt-5-nano"),
        # glm-5-2 has no local endpoint, and its chain escalates like sol's.
        ("glm-5-2", _UCODE_CODEX_CATALOG, "codex", "gpt-5-5"),
        # A workspace that serves GLM resolves the glm arm to that endpoint.
        (
            "glm-5-2",
            (*_UCODE_CODEX_CATALOG, "databricks-glm-5-2"),
            "codex",
            "databricks-glm-5-2",
        ),
        # The prefix-restore path is untouched: a servable arm is applied as-is.
        (
            "gpt-5-5",
            ("databricks-gpt-5-nano", "databricks-gpt-5-5", "databricks-gpt-5-mini"),
            "codex",
            "databricks-gpt-5-5",
        ),
        # Claude arms are unaffected when the workspace serves them exactly.
        (
            "claude-sonnet-5",
            ("databricks-claude-sonnet-5", "databricks-claude-opus-4-8"),
            "claude-sdk",
            "databricks-claude-sonnet-5",
        ),
        (
            "claude-opus-4-8",
            ("databricks-claude-sonnet-5", "databricks-claude-opus-4-8"),
            "claude-sdk",
            "databricks-claude-opus-4-8",
        ),
        # An unservable opus takes the most capable Claude on offer.
        (
            "claude-opus-4-8",
            (
                "databricks-claude-haiku-4-5",
                "databricks-claude-sonnet-4-6",
                "databricks-claude-opus-4-7",
            ),
            "claude-sdk",
            "databricks-claude-opus-4-7",
        ),
        # An unservable cheap Claude arm avoids the flagship.
        (
            "claude-sonnet-5",
            ("databricks-claude-opus-4-7", "databricks-claude-sonnet-4-6"),
            "claude-sdk",
            "databricks-claude-sonnet-4-6",
        ),
        # A sonnet-class arm takes the older sonnet, not the cheapest real model.
        ("claude-sonnet-5", _CLAUDE_TIERS, "claude-sdk", "databricks-claude-sonnet-4-6"),
        # Nothing cheaper on offer: the chain ends on the older flagship.
        (
            "claude-sonnet-5",
            ("databricks-claude-opus-4-7", "databricks-claude-opus-4-8"),
            "claude-sdk",
            "databricks-claude-opus-4-7",
        ),
        # The live-catalog case: exact prefix restore, no substitution at all.
        (
            "claude-sonnet-5",
            (
                "databricks-claude-haiku-4-5",
                "databricks-claude-sonnet-4-6",
                "databricks-claude-sonnet-5",
                "databricks-claude-opus-4-8",
            ),
            "claude-sdk",
            "databricks-claude-sonnet-5",
        ),
    ],
)
def test_unservable_arm_substitutes_from_its_own_chain(
    arm: str, catalog: Sequence[str], harness: str, expected: str
) -> None:
    """An arm the workspace can't serve resolves down its substitution chain."""
    assert _substitute(arm, catalog, harness) == expected


def test_substitution_is_independent_of_catalog_order() -> None:
    import random

    shuffled = list(_UCODE_CODEX_CATALOG)
    picks = set()
    for seed in range(25):
        random.Random(seed).shuffle(shuffled)
        picks.add((_substitute("gpt-5-6-sol", shuffled), _substitute("gpt-5-6-luna", shuffled)))
    assert picks == {("gpt-5-5", "gpt-5-mini")}


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
                        "profile": "eng-ml-inference",
                        "default": True,
                    },
                }
            },
            None,
            "eng-ml-inference",
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


@pytest.mark.asyncio
async def test_route_session_harness_keeps_the_clients_harness() -> None:
    """A client-resolved verdict still yields a harness, not a routing failure."""
    expected = RoutingResult(
        model="databricks-claude-opus-4-8",
        rationale="escalate up to claude-opus-4-8",
        harness="claude-native",
        raw_model="claude-opus-4-8",
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    catalog = {"claude-native": ["databricks-claude-opus-4-8", "databricks-claude-sonnet-5"]}
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "add a --dry-run flag",
            harness_candidates=("claude-native",),
            catalog=catalog,
        )
    assert harness == "claude-native"
    assert model == "databricks-claude-opus-4-8"
    # Same model, only spelled bare by the router: not a divergent raw pick.
    assert verdict is not None and "raw_model" not in verdict
    assert error is None


@pytest.mark.asyncio
async def test_route_session_harness_applies_the_clients_model_verbatim() -> None:
    """The client owns resolution; the server must not re-resolve its pick.

    A second pass with a different prefix list downgraded the pick on the
    zero-config Databricks path (routed gpt-5-6-luna applying gpt-5-4-mini).
    """
    expected = RoutingResult(
        model="databricks-gpt-5-6-luna",
        rationale="cheap arm",
        harness="codex-native",
        raw_model="gpt-5-6-luna",
    )
    caps = FakeCaps(routing_client=FakeRoutingClient(expected))
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "rename a variable",
            harness_candidates=("codex-native",),
            catalog={"codex-native": ["databricks-gpt-5-4-mini", "databricks-gpt-5-6-luna"]},
        )
    assert (harness, model) == ("codex-native", "databricks-gpt-5-6-luna")
    assert error is None
    # Applied exactly, so the card shows no divergent raw pick.
    assert verdict is not None and "raw_model" not in verdict


@pytest.mark.asyncio
async def test_route_turn_substitutes_a_model_the_harness_gateway_bars() -> None:
    """A turn cannot change harness, so a barred pick moves to a runnable id."""
    client = FakeRoutingClient(
        RoutingResult(
            model="databricks-gpt-5-6-luna",
            rationale="cheap arm",
            harness="pi",
            raw_model="gpt-5-6-luna",
        )
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        model, verdict = await route_turn("pi", "quick lookup")
    # luna 400s on pi's completions path; the next id in its chain does not.
    assert model == "databricks-gpt-5-4-mini"
    assert verdict is not None and verdict["raw_model"] == "gpt-5-6-luna"


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


# ── A barred pick never escalates cost ─────────────────────────────────────
#
# The substitution fallback used to take the most capable same-family candidate,
# so every SIMPLE pi turn (haiku is barred on pi) escalated to opus.


def test_a_barred_cheap_pick_does_not_become_the_most_expensive_candidate() -> None:
    from omnigent.server.smart_routing import _HARNESS_EXCLUDED_MODELS, substitute_model

    candidates = infer_models("pi") or []
    substitute = substitute_model(
        "databricks-claude-haiku-4-5",
        candidates,
        barred=_HARNESS_EXCLUDED_MODELS["pi"],
    )
    assert substitute == "databricks-claude-sonnet-4-6", (
        f"the cheapest Claude on pi must step to the next tier up, not to {substitute}"
    )


@pytest.mark.parametrize(
    ("pick", "expected"),
    [
        # The nearest tier, biased down — never the flagship.
        ("databricks-gpt-5-4-nano", "databricks-gpt-5-4-mini"),
        ("databricks-claude-haiku-4-5", "databricks-claude-sonnet-4-6"),
    ],
)
def test_chain_miss_substitutes_the_nearest_candidate(pick: str, expected: str) -> None:
    from omnigent.server.smart_routing import substitute_model

    candidates = infer_models("pi") or []
    assert substitute_model(pick, candidates, barred=[pick]) == expected


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
        # glm has no local endpoint; its chain names sol, which this pane serves.
        ("glm-5-2", "gpt-5.6-sol"),
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
    client = FakeRoutingClient(RoutingResult(model=model, rationale="x", harness="pi"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
        harness, routed, _verdict, error = await route_session_harness(
            "do it", allowed_family="pi"
        )
    assert harness == "pi", f"a pi child must stay on pi, got {harness!r}"
    if routed is not None:
        assert not harness_bars_model("pi", routed), routed
    assert error is None


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
