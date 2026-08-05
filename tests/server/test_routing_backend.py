"""Tests for the routing-backend selector.

Gateway backing selects WHICH router answers a routing call, so the truth table
here is the whole contract: the external client only serves gateway-backed
harnesses, the built-in judge serves any, and neither means unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server.routing_backend import (
    RoutingBackends,
    backends_from_caps,
    gateway_backs_all,
    select_router,
)


@dataclass
class _Client:
    """Stand-in routing client; identity is all the selector reads."""

    name: str


_EXTERNAL = _Client("external")
_LOCAL = _Client("local")


# ── select_router: the eight-combination truth table ────────────────────────


@pytest.mark.parametrize(
    ("external", "local", "gateway_backed", "expected"),
    [
        (True, True, True, ("external", "databricks-aigw")),
        (True, True, False, ("local", "oss-llm")),
        (True, False, True, ("external", "databricks-aigw")),
        # An external-only deployment cannot serve an off-gateway harness: its
        # picks are gateway catalog ids the pane can't reach.
        (True, False, False, None),
        (False, True, True, ("local", "oss-llm")),
        (False, True, False, ("local", "oss-llm")),
        (False, False, True, None),
        (False, False, False, None),
    ],
)
def test_select_router_truth_table(
    external: bool,
    local: bool,
    gateway_backed: bool,
    expected: tuple[str, str] | None,
) -> None:
    backends = RoutingBackends(
        external=_EXTERNAL if external else None,
        local=_LOCAL if local else None,
    )
    choice = select_router(backends, gateway_backed=gateway_backed)
    if expected is None:
        assert choice is None
        return
    assert choice is not None
    assert (choice.client.name, choice.source) == expected


def test_any_prefers_the_external_client() -> None:
    assert RoutingBackends(external=_EXTERNAL, local=_LOCAL).any() is _EXTERNAL
    assert RoutingBackends(local=_LOCAL).any() is _LOCAL
    assert RoutingBackends(external=_EXTERNAL).any() is _EXTERNAL
    assert RoutingBackends().any() is None


# ── gateway_backs_all: both families must be backed ─────────────────────────


@pytest.mark.parametrize(
    ("gateway", "harnesses", "expected"),
    [
        # Both explicitly backed.
        ({"claude-native": True, "codex-native": True}, ("claude-native", "codex-native"), True),
        # One family off the gateway takes the whole two-family call away.
        ({"claude-native": True, "codex-native": False}, ("claude-native", "codex-native"), False),
        ({"claude-native": False, "codex-native": True}, ("claude-native", "codex-native"), False),
        # …but the backed family on its own still routes externally.
        ({"claude-native": True, "codex-native": False}, ("claude-native",), True),
        ({"claude-native": True, "codex-native": False}, ("codex-native",), False),
        # Unknown is backed, not unavailable: an older host reports nothing.
        ({}, ("claude-native", "codex-native"), True),
        ({"claude-native": True}, ("claude-native", "codex-native"), True),
    ],
)
def test_gateway_backs_all(
    gateway: dict[str, bool], harnesses: tuple[str, ...], expected: bool
) -> None:
    host = SimpleNamespace(gateway_inference=gateway)
    assert gateway_backs_all(host, harnesses) is expected


def test_gateway_backs_all_without_a_host_row() -> None:
    assert gateway_backs_all(None, ("claude-native", "codex-native")) is True
    assert gateway_backs_all(SimpleNamespace(), ("claude-native",)) is True
    assert gateway_backs_all(SimpleNamespace(gateway_inference=None), ("claude-native",)) is True


# ── backends_from_caps: explicit pair wins, else derive by type ──────────────


def test_backends_from_caps_prefers_the_explicit_pair() -> None:
    pair = RoutingBackends(external=_EXTERNAL, local=_LOCAL)
    caps: Any = SimpleNamespace(routing_backends=pair, routing_client=_LOCAL)
    assert backends_from_caps(caps) is pair


def test_backends_from_caps_derives_an_external_client_by_type() -> None:
    from omnigent.server.smart_routing import ExternalRoutingClient

    client = ExternalRoutingClient(base_url="https://example.invalid", router_name="task_v1")
    caps: Any = SimpleNamespace(routing_backends=None, routing_client=client)
    backends = backends_from_caps(caps)
    assert (backends.external, backends.local) == (client, None)


def test_backends_from_caps_derives_an_unknown_client_as_the_oss_judge() -> None:
    """Misclassifying a custom client as OSS costs only a badge on the chip."""
    caps: Any = SimpleNamespace(routing_backends=None, routing_client=_LOCAL)
    backends = backends_from_caps(caps)
    assert (backends.external, backends.local) == (None, _LOCAL)


def test_backends_from_caps_yields_an_empty_pair_when_nothing_is_configured() -> None:
    assert backends_from_caps(None) == RoutingBackends()
    caps: Any = SimpleNamespace(routing_backends=None, routing_client=None)
    assert backends_from_caps(caps) == RoutingBackends()
    # A caps object that predates both fields still reads as unconfigured.
    assert backends_from_caps(SimpleNamespace()) == RoutingBackends()
