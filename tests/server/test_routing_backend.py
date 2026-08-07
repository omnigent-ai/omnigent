"""Tests for the routing-backend selector.

Gateway backing selects WHICH router answers a routing call, so the truth table
here is the whole contract: the external client only serves gateway-backed
harnesses, the built-in judge serves any, and neither means unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.common import set_server_host_registry
from omnigent.server.routing_backend import (
    RoutingBackends,
    backends_from_caps,
    gateway_backs_all,
    route_with_fallback,
    routing_available,
    routing_sources,
    select_router,
    usable_external,
)

_HOST_ID = "aabbccddeeff00112233445566778899"


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
#
# The map is read from the live host registry — reported on the host's connect
# handshake, never persisted — so these set it up the way the tunnel does.


@pytest.fixture()
def host_registry() -> Iterator[HostRegistry]:
    """Install a fresh process-global host registry for the duration of a test."""
    registry = HostRegistry()
    set_server_host_registry(registry)
    try:
        yield registry
    finally:
        set_server_host_registry(None)


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
    gateway: dict[str, bool],
    harnesses: tuple[str, ...],
    expected: bool,
    host_registry: HostRegistry,
) -> None:
    host_registry.record_gateway_inference(_HOST_ID, gateway)
    host = SimpleNamespace(host_id=_HOST_ID)
    assert gateway_backs_all(host, harnesses) is expected


def test_gateway_backs_all_without_a_report(host_registry: HostRegistry) -> None:
    # No host at all, a host id nothing has been reported for, and a row with no
    # id to key on: all unknown, and unknown is backed.
    assert gateway_backs_all(None, ("claude-native", "codex-native")) is True
    assert gateway_backs_all(SimpleNamespace(host_id=_HOST_ID), ("claude-native",)) is True
    assert gateway_backs_all(SimpleNamespace(), ("claude-native",)) is True


def test_gateway_backs_all_without_a_registry() -> None:
    # No server registry installed (a runner-side or embedding process): the
    # read degrades to unknown rather than raising.
    set_server_host_registry(None)
    assert gateway_backs_all(SimpleNamespace(host_id=_HOST_ID), ("claude-native",)) is True


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


# ── routing_available / routing_sources: one "is routing configured" gate ────
#
# The two answers come from one place because they used to disagree: the
# availability flag read the legacy single ``routing_client`` while everything
# else read the backends pair, so a deployment setting only ``routing_backends``
# (the documented managed override point) reported routing off — and the SPA hid
# a surface the server would happily have served.


def _caps(**fields: Any) -> Any:
    return SimpleNamespace(**fields)


def test_routing_available_from_the_backends_pair_alone() -> None:
    """The regression: only ``routing_backends`` set still reports available."""
    caps = _caps(
        routing_backends=RoutingBackends(external=_EXTERNAL),
        routing_client=None,
        policy_llm_connection_factory=None,
    )
    assert routing_available(caps) is True
    assert routing_sources(caps) == {"external": True, "oss": False}


def test_routing_available_from_a_local_only_backends_pair() -> None:
    caps = _caps(
        routing_backends=RoutingBackends(local=_LOCAL),
        routing_client=None,
        policy_llm_connection_factory=None,
    )
    assert routing_available(caps) is True
    assert routing_sources(caps) == {"external": False, "oss": True}


def test_routing_available_from_the_managed_policy_llm_factory() -> None:
    """A managed deployment supplies its own client later; that counts as OSS."""
    caps = _caps(
        routing_backends=None,
        routing_client=None,
        policy_llm_connection_factory=lambda: None,
    )
    assert routing_available(caps) is True
    assert routing_sources(caps) == {"external": False, "oss": True}


def test_routing_available_from_a_legacy_single_client() -> None:
    caps = _caps(routing_backends=None, routing_client=_LOCAL, policy_llm_connection_factory=None)
    assert routing_available(caps) is True
    assert routing_sources(caps) == {"external": False, "oss": True}


@pytest.mark.parametrize(
    "caps",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(
            routing_backends=RoutingBackends(),
            routing_client=None,
            policy_llm_connection_factory=None,
        ),
    ],
)
def test_routing_unavailable_when_nothing_is_configured(caps: Any) -> None:
    assert routing_available(caps) is False
    assert routing_sources(caps) == {"external": False, "oss": False}


@pytest.mark.parametrize(
    "caps",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(routing_backends=RoutingBackends(external=_EXTERNAL, local=_LOCAL)),
        SimpleNamespace(routing_backends=None, routing_client=_LOCAL),
        SimpleNamespace(routing_backends=None, policy_llm_connection_factory=lambda: None),
    ],
)
def test_availability_is_exactly_some_source_can_answer(caps: Any) -> None:
    """The invariant that keeps the flag and the source map from drifting."""
    assert routing_available(caps) is any(routing_sources(caps).values())


# ── a latched permanent failure reads as "no external router" ───────────────
#
# A workspace whose account never had the routing API enabled answers every
# ``routes:select`` with the same 404. The client latches that, and this
# deployment is a judge-only one for as long as the process lives.


@dataclass
class _Tripped:
    """A client that has latched an account-level permanent failure."""

    name: str = "external"
    permanently_unavailable: bool = True


def test_select_router_skips_a_router_that_latched_a_permanent_failure() -> None:
    tripped = _Tripped()
    choice = select_router(RoutingBackends(external=tripped, local=_LOCAL), gateway_backed=True)
    assert choice is not None
    assert (choice.client, choice.source) == (_LOCAL, "oss-llm")
    # Nothing left to answer with once the judge is absent too.
    assert select_router(RoutingBackends(external=tripped), gateway_backed=True) is None


def test_usable_external_hides_a_tripped_client() -> None:
    assert usable_external(RoutingBackends(external=_EXTERNAL)) is _EXTERNAL
    assert usable_external(RoutingBackends(external=_Tripped())) is None
    assert usable_external(RoutingBackends(local=_LOCAL)) is None


def test_routing_sources_stops_advertising_a_tripped_router() -> None:
    caps = _caps(
        routing_backends=RoutingBackends(external=_Tripped(), local=_LOCAL),
        routing_client=None,
        policy_llm_connection_factory=None,
    )
    assert routing_sources(caps) == {"external": False, "oss": True}
    assert routing_available(caps) is True


# ── route_with_fallback: the external router first, the judge behind it ─────


class _Recording:
    """Routing-client double recording how many calls it took.

    :param result: What :meth:`route` returns.
    :param error: Raised from :meth:`route` instead of returning.
    :param last_error: The protocol's reason field for a ``None`` result.
    """

    def __init__(
        self,
        result: Any = None,  # type: ignore[explicit-any]  # a RoutingResult stand-in
        *,
        error: Exception | None = None,
        last_error: str | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.last_error = last_error
        self.calls = 0

    async def route(
        self,
        _message: str,
        _available: dict[str, list[str]],
    ) -> Any:  # type: ignore[explicit-any]  # a RoutingResult stand-in
        """Record the call and answer with the canned verdict (or raise)."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


_MENU = {"claude-sdk": ["databricks-claude-opus-4-8"]}
_VERDICT = "verdict"


async def test_route_with_fallback_prefers_the_external_router() -> None:
    external, local = _Recording(_VERDICT), _Recording(_VERDICT)
    call = await route_with_fallback(
        RoutingBackends(external=external, local=local), "hi", _MENU, gateway_backed=True
    )
    assert call is not None
    assert (call.result, call.source, call.client) == (_VERDICT, "databricks-aigw", external)
    assert local.calls == 0


async def test_route_with_fallback_asks_the_judge_when_the_router_declines() -> None:
    external = _Recording(None, last_error="router returned HTTP 404: not enabled")
    local = _Recording(_VERDICT)
    call = await route_with_fallback(
        RoutingBackends(external=external, local=local), "hi", _MENU, gateway_backed=True
    )
    assert call is not None
    assert (call.result, call.source, call.client) == (_VERDICT, "oss-llm", local)


async def test_route_with_fallback_asks_the_judge_when_the_router_raises() -> None:
    external = _Recording(error=RuntimeError("connection reset"))
    local = _Recording(_VERDICT)
    call = await route_with_fallback(
        RoutingBackends(external=external, local=local), "hi", _MENU, gateway_backed=True
    )
    assert call is not None
    assert (call.result, call.source) == (_VERDICT, "oss-llm")


async def test_route_with_fallback_reports_the_last_router_asked() -> None:
    """Both declined: the caller reads its reason off whoever answered last."""
    external = _Recording(None, last_error="router returned HTTP 404: not enabled")
    local = _Recording(None, last_error="the judge had no opinion")
    call = await route_with_fallback(
        RoutingBackends(external=external, local=local), "hi", _MENU, gateway_backed=True
    )
    assert call is not None
    assert (call.result, call.source, call.client) == (None, "oss-llm", local)


async def test_route_with_fallback_lets_a_lone_router_fail_the_way_it_always_did() -> None:
    """No judge behind it: the caller's own fail-open handling still sees the raise."""
    external = _Recording(error=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        await route_with_fallback(
            RoutingBackends(external=external), "hi", _MENU, gateway_backed=True
        )


async def test_route_with_fallback_uses_the_judge_off_the_gateway() -> None:
    external, local = _Recording(_VERDICT), _Recording(_VERDICT)
    call = await route_with_fallback(
        RoutingBackends(external=external, local=local), "hi", _MENU, gateway_backed=False
    )
    assert call is not None
    assert (call.source, external.calls) == ("oss-llm", 0)


async def test_route_with_fallback_reports_an_unconfigured_deployment() -> None:
    assert await route_with_fallback(RoutingBackends(), "hi", _MENU, gateway_backed=True) is None
