"""Automatic native routing must respect built-in tool-search compatibility."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnigent.server.smart_routing import RoutingResult, route_session_harness, route_turn
from tests.server.helpers import FakeCaps, FakeRoutingClient

_COMPATIBLE = "databricks-gpt-5-6-luna"
_UNSUPPORTED = "databricks-gpt-5-4-nano"


@pytest.mark.parametrize(
    "model",
    [
        _UNSUPPORTED,
        "gpt-5.4-nano",
        "system.ai.databricks-gpt-5-4-nano",
    ],
)
async def test_native_turn_filters_and_vetoes_unsupported_tool_search_models(model: str) -> None:
    router = FakeRoutingClient(RoutingResult(model=model, rationale="short task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=router)):
        selected, verdict = await route_turn(
            "codex-native", "Continue the work.", catalog=[model, _COMPATIBLE]
        )
    assert router.offered == [{"codex-native": [_COMPATIBLE]}]
    assert selected == _COMPATIBLE
    assert verdict is not None and verdict["raw_model"] == model


async def test_native_turn_without_a_compatible_candidate_declines_before_routing() -> None:
    router = FakeRoutingClient(RoutingResult(model=_UNSUPPORTED, rationale="short task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=router)):
        selected = await route_turn("codex-native", "Continue.", catalog=[_UNSUPPORTED])
    assert selected == (None, None)
    assert router.offered == []


async def test_native_turn_declines_an_unsupported_verdict_without_a_family_fallback() -> None:
    router = FakeRoutingClient(RoutingResult(model=_UNSUPPORTED, rationale="short task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=router)):
        selected = await route_turn(
            "codex-native", "Continue.", catalog=[_UNSUPPORTED, "databricks-gpt-5-4"]
        )
    assert selected == (None, None)
    assert router.offered == [{"codex-native": ["databricks-gpt-5-4"]}]


async def test_native_create_substitutes_a_compatible_model_from_the_offered_harness() -> None:
    router = FakeRoutingClient(
        RoutingResult(
            model=_UNSUPPORTED,
            harness="codex-native",
            rationale="short task",
        )
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=router)):
        harness, model, verdict, error = await route_session_harness(
            "Start the work.",
            harness_candidates=("codex-native",),
            catalog={"codex-native": [_UNSUPPORTED, _COMPATIBLE]},
        )
    assert error is None
    assert (harness, model) == ("codex-native", _COMPATIBLE)
    assert verdict is not None and verdict["raw_model"] == _UNSUPPORTED


async def test_sdk_routing_keeps_models_without_native_tool_search() -> None:
    router = FakeRoutingClient(RoutingResult(model=_UNSUPPORTED, rationale="short task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=router)):
        model, _ = await route_turn(
            "openai-agents", "Answer briefly.", catalog=[_UNSUPPORTED, _COMPATIBLE]
        )
    assert model == _UNSUPPORTED
    assert router.offered == [{"openai-agents": [_UNSUPPORTED, _COMPATIBLE]}]
