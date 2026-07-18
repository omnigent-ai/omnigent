"""Tests for create-time SMART ROUTE harness selection.

Covers the sentinel helpers in ``omnigent.server.routes.sessions``:
harness eligibility (config ∩ host.configured_harnesses), the first-message
extractor, and ``_smart_route_harness`` (cross-harness catalog assembly +
routing-client dispatch, with graceful fallback).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnigent.server.routes.sessions import (
    SMART_ROUTE_HARNESS,
    _first_user_text,
    _smart_route_eligible_harnesses,
    _smart_route_harness,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.server.smart_routing import RoutingResult
from omnigent.stores.host_store import Host


def _host(configured: dict[str, Any] | None) -> Host:
    return Host(
        host_id="h",
        name="n",
        owner="o",
        status="online",
        created_at=0,
        updated_at=0,
        configured_harnesses=configured,
    )


def _caps(harnesses: list[str], routing_client: Any = None) -> Any:
    return MagicMock(smart_route_harnesses=harnesses, routing_client=routing_client)


class _Listing:
    def __init__(self, ids: list[str]) -> None:
        self.models = [MagicMock(id=i) for i in ids]


def test_sentinel_value() -> None:
    assert SMART_ROUTE_HARNESS == "__smart_route__"


def test_eligible_intersects_configured_harnesses() -> None:
    host = _host({"claude-native": True, "codex": False})
    with patch(
        "omnigent.runtime.get_caps", return_value=_caps(["claude-native", "codex", "cursor"])
    ):
        # codex is configured-off, cursor is absent -> only claude-native survives.
        assert _smart_route_eligible_harnesses(host) == ["claude-native"]


def test_eligible_trusts_config_when_host_unreported() -> None:
    with patch("omnigent.runtime.get_caps", return_value=_caps(["claude-native", "codex"])):
        assert _smart_route_eligible_harnesses(_host(None)) == ["claude-native", "codex"]
        assert _smart_route_eligible_harnesses(None) == ["claude-native", "codex"]


def test_first_user_text() -> None:
    items = [
        SessionEventInput(type="message", data={"content": [{"type": "input_text", "text": "hi"}]})
    ]
    assert _first_user_text(items) == "hi"
    assert _first_user_text([]) == ""


@pytest.mark.asyncio
async def test_smart_route_harness_picks_across_harnesses() -> None:
    host = _host({"claude-native": True, "codex": True})
    seen: dict[str, Any] = {}

    async def fake_route(message: str, available: dict[str, list[str]]) -> RoutingResult:
        seen["available"] = available
        return RoutingResult(model="databricks-gpt-5-5", rationale="codey", harness="codex")

    caps = _caps(["claude-native", "codex"], routing_client=MagicMock(route=fake_route))

    def fake_listing(spec: Any, harness: str) -> _Listing:
        return _Listing(
            {
                "claude-native": ["databricks-claude-opus-4-8"],
                "codex": ["databricks-gpt-5-5", "databricks-gpt-5-4-mini"],
            }[harness]
        )

    with (
        patch("omnigent.runtime.get_caps", return_value=caps),
        patch("omnigent.model_catalog.list_models_for_worker", side_effect=fake_listing),
    ):
        harness, model = await _smart_route_harness(
            spec=object(), first_message="write a codex thing", host=host
        )

    # The router saw BOTH harnesses' models (cross-harness candidate set).
    assert set(seen["available"]) == {"claude-native", "codex"}
    assert harness == "codex"
    assert model == "databricks-gpt-5-5"


@pytest.mark.asyncio
async def test_smart_route_harness_no_routing_client() -> None:
    caps = _caps(["claude-native"], routing_client=None)
    with patch("omnigent.runtime.get_caps", return_value=caps):
        assert await _smart_route_harness(
            spec=object(), first_message="x", host=_host({"claude-native": True})
        ) == (None, None)


@pytest.mark.asyncio
async def test_smart_route_harness_empty_message() -> None:
    caps = _caps(["claude-native"], routing_client=MagicMock())
    with patch("omnigent.runtime.get_caps", return_value=caps):
        assert await _smart_route_harness(
            spec=object(), first_message="   ", host=_host({"claude-native": True})
        ) == (None, None)


@pytest.mark.asyncio
async def test_smart_route_harness_no_eligible_models() -> None:
    """No eligible harness yields models -> fall back, router not called."""
    route = MagicMock()
    caps = _caps(["claude-native"], routing_client=MagicMock(route=route))
    with (
        patch("omnigent.runtime.get_caps", return_value=caps),
        patch("omnigent.model_catalog.list_models_for_worker", return_value=_Listing([])),
    ):
        result = await _smart_route_harness(
            spec=object(), first_message="x", host=_host({"claude-native": True})
        )
    assert result == (None, None)
    route.assert_not_called()


@pytest.mark.asyncio
async def test_smart_route_harness_router_declines() -> None:
    """Router returns None (or no harness) -> fall back to agent default."""

    async def fake_route(message: str, available: dict[str, list[str]]) -> None:
        return None

    caps = _caps(["claude-native"], routing_client=MagicMock(route=fake_route))
    with (
        patch("omnigent.runtime.get_caps", return_value=caps),
        patch(
            "omnigent.model_catalog.list_models_for_worker",
            return_value=_Listing(["databricks-claude-opus-4-8"]),
        ),
    ):
        result = await _smart_route_harness(
            spec=object(), first_message="x", host=_host({"claude-native": True})
        )
    assert result == (None, None)
