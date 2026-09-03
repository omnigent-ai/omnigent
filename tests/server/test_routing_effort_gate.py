"""route_turn's effort gate: candidates must advertise the session's effort.

A codex-family native pane validates the (model, reasoning effort) pairing
itself and rejects an unsupported one with ``invalid_value``, so a session
with an explicit effort must only be routed onto live models that advertise
that effort — and must be left alone when none does or when live
capabilities cannot be read.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.server.smart_routing import (
    RoutingResult,
    _catalog_reasoning_efforts,
    invalidate_runner_catalog,
    route_turn,
)
from tests.server.helpers import FakeCaps, FakeRoutingClient

#: Cheap arm whose live ladder tops out below the session's effort.
XHIGH_CAPPED = "databricks-gpt-5-5"

#: Premium arm advertising the full ladder, including ``max``.
MAX_CAPABLE = "databricks-gpt-5-6-sol"

_LADDERS: dict[str, list[str]] = {
    XHIGH_CAPPED: ["low", "medium", "high", "xhigh"],
    MAX_CAPABLE: ["low", "medium", "high", "xhigh", "max", "ultra"],
}


def _catalog_row(model_id: str, *, with_efforts: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"id": model_id, "family": "gpt"}
    if with_efforts:
        row["supportedReasoningEfforts"] = [
            {"reasoningEffort": effort} for effort in _LADDERS[model_id]
        ]
    return row


def _runner_client(
    model_ids: list[str],
    *,
    catalog_efforts: bool = True,
    options_efforts: bool = False,
) -> httpx.AsyncClient:
    """A runner double serving /models and /codex-model-options."""

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            rows = [_catalog_row(m, with_efforts=catalog_efforts) for m in model_ids]
            return httpx.Response(
                200,
                json={
                    "workers": {"self": {"source": "catalog", "verified": True, "models": rows}}
                },
            )
        if path.endswith("/codex-model-options"):
            rows = (
                [_catalog_row(m, with_efforts=True) for m in model_ids] if options_efforts else []
            )
            return httpx.Response(200, json={"models": rows})
        return httpx.Response(404, json={})

    return httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    )


@pytest.fixture(autouse=True)
def _cold_catalog_cache() -> Any:
    invalidate_runner_catalog()
    yield
    invalidate_runner_catalog()


def _max_capable_pick() -> FakeRoutingClient:
    return FakeRoutingClient(
        RoutingResult(model=MAX_CAPABLE, rationale="needs deep reasoning", harness="codex")
    )


@pytest.mark.asyncio
async def test_effort_incompatible_models_are_not_offered() -> None:
    """With effort set, only live models advertising it reach the router."""
    client = _max_capable_pick()
    async with _runner_client([XHIGH_CAPPED, MAX_CAPABLE]) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_effort_mixed",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model == MAX_CAPABLE
    assert verdict is not None
    assert client.offered, "the router must have been consulted"
    offered = [m for models in client.offered[-1].values() for m in models]
    assert XHIGH_CAPPED not in offered, (
        f"an xhigh-capped model was offered to a 'max' session: {offered}"
    )


@pytest.mark.asyncio
async def test_declines_when_no_live_model_advertises_the_effort() -> None:
    """Every arm capped below the session's effort → no routing at all."""
    client = _max_capable_pick()
    async with _runner_client([XHIGH_CAPPED]) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_effort_none",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model is None and verdict is None
    assert not client.offered, "no candidate is compatible, so the router must not run"


@pytest.mark.asyncio
async def test_declines_when_live_capabilities_are_unavailable() -> None:
    """No effort ladders anywhere → decline rather than risk an invalid pairing."""
    client = _max_capable_pick()
    async with _runner_client([XHIGH_CAPPED, MAX_CAPABLE], catalog_efforts=False) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, _verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_effort_unknown",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model is None
    assert not client.offered


@pytest.mark.asyncio
async def test_falls_back_to_codex_model_options_for_ladders() -> None:
    """Ladders absent from the catalog are read from codex-model-options."""
    client = _max_capable_pick()
    async with _runner_client(
        [XHIGH_CAPPED, MAX_CAPABLE], catalog_efforts=False, options_efforts=True
    ) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, _verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_effort_options",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model == MAX_CAPABLE
    offered = [m for models in client.offered[-1].values() for m in models]
    assert XHIGH_CAPPED not in offered


@pytest.mark.asyncio
async def test_no_session_effort_leaves_routing_unchanged() -> None:
    """Without an explicit effort, the gate is inert and every arm is offered."""
    client = FakeRoutingClient(
        RoutingResult(model=XHIGH_CAPPED, rationale="trivial", harness="codex")
    )
    async with _runner_client([XHIGH_CAPPED, MAX_CAPABLE]) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, _verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_no_effort",
                runner_client=runner,
            )
    assert model == XHIGH_CAPPED
    offered = [m for models in client.offered[-1].values() for m in models]
    assert set(offered) == {XHIGH_CAPPED, MAX_CAPABLE}


@pytest.mark.asyncio
async def test_effort_gate_does_not_apply_to_non_codex_harnesses() -> None:
    """claude-sdk treats effort as a provider-clamped hint, so no gate."""
    expected = RoutingResult(
        model="databricks-claude-haiku-4-5", rationale="trivial", harness="claude-sdk"
    )
    client = FakeRoutingClient(expected)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "workers": {
                        "self": {
                            "source": "catalog",
                            "verified": True,
                            "models": [{"id": "databricks-claude-haiku-4-5", "family": "claude"}],
                        }
                    }
                },
            )
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, _verdict = await route_turn(
                "claude-sdk",
                "hello",
                session_id="conv_claude_effort",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_gate_matches_across_id_spellings() -> None:
    """A picker-spelled live row still gates its catalog-spelled candidate."""
    client = _max_capable_pick()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            rows = [
                {"id": XHIGH_CAPPED, "family": "gpt"},
                {"id": MAX_CAPABLE, "family": "gpt"},
            ]
            return httpx.Response(
                200,
                json={
                    "workers": {"self": {"source": "catalog", "verified": True, "models": rows}}
                },
            )
        if path.endswith("/codex-model-options"):
            # The live model list spells ids the picker's way (dots, no prefix).
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "gpt-5.5",
                            "model": "gpt-5.5",
                            "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
                        },
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "xhigh"},
                                {"reasoningEffort": "max"},
                            ],
                        },
                    ]
                },
            )
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner:
        with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=client)):
            model, _verdict = await route_turn(
                "codex-native",
                "rename one variable",
                session_id="conv_effort_spelling",
                runner_client=runner,
                reasoning_effort="max",
            )
    assert model == MAX_CAPABLE
    offered = [m for models in client.offered[-1].values() for m in models]
    assert XHIGH_CAPPED not in offered


def test_catalog_reasoning_efforts_parses_both_wire_shapes() -> None:
    row = {
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}],
        "reasoning": {"efforts": ["medium"]},
    }
    assert _catalog_reasoning_efforts(row) == frozenset({"low", "medium", "max"})
    assert _catalog_reasoning_efforts({}) == frozenset()
    assert _catalog_reasoning_efforts({"supportedReasoningEfforts": "bad"}) == frozenset()
    assert _catalog_reasoning_efforts({"reasoning": {"efforts": [1, "high"]}}) == frozenset(
        {"high"}
    )
