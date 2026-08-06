"""Which router answers a routing call — the AI Gateway's, or the built-in judge's.

Smart Routing has two possible backends. The external ``task_v1`` client rewrites
a launch's model to a Databricks AI Gateway catalog id, so it can only serve a
harness whose inference is gateway-backed. The built-in OSS judge
(:class:`~omnigent.server.smart_routing.LLMRoutingClient`) names models from the
candidate menu it is handed, so it serves any harness.

Gateway backing therefore selects the SOURCE rather than hiding the surface: an
off-gateway pane still routes, just with the built-in judge, and the decision
records which router answered so the UI can say so.

The same holds when the preferred router FAILS: :func:`route_with_fallback`
asks the judge behind it rather than declining the call, which is what keeps a
deployment whose workspace has no routing API on Smart Routing at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from omnigent.server.smart_routing import RoutingClient, RoutingResult

_logger = logging.getLogger(__name__)

#: Which router produced a decision. ``"databricks-aigw"`` is the external
#: ``task_v1`` service; ``"oss-llm"`` is the built-in judge.
RouterSource = Literal["databricks-aigw", "oss-llm"]


@dataclass(frozen=True)
class RoutingBackends:
    """The routing clients this deployment has, by source.

    Both may be configured at once — that is the normal Databricks posture, and
    it is what lets an off-gateway pane fall back to the judge instead of losing
    Smart Routing.

    :param external: The external ``routes:select`` client, or ``None``.
    :param local: The built-in judge, or ``None``.
    """

    external: RoutingClient | None = None
    local: RoutingClient | None = None

    def any(self) -> RoutingClient | None:
        """The client to use when nothing is known about gateway backing.

        The external client is preferred: it is the richer router, and every
        legacy consumer that reads a single ``routing_client`` off the caps
        expects the deployment's primary.

        :returns: The external client, else the local one, else ``None``.
        """
        return self.external or self.local


def usable_external(backends: RoutingBackends) -> RoutingClient | None:
    """The external client, unless it has latched a permanent failure.

    A workspace whose routing API is not enabled answers every
    ``routes:select`` the same way, so the client latches that and this reads
    as "no external router" from then on — the deployment's real posture for
    the rest of the process.

    :param backends: The deployment's configured clients.
    :returns: The external client when it can still answer, else ``None``.
    """
    client = backends.external
    if client is None or getattr(client, "permanently_unavailable", False):
        return None
    return client


@dataclass(frozen=True)
class RouterChoice:
    """A selected router and the source name to stamp on its decision.

    :param client: The routing client to call.
    :param source: Which source *client* is, e.g. ``"databricks-aigw"``.
    """

    client: RoutingClient
    source: RouterSource


def select_router(
    backends: RoutingBackends,
    *,
    gateway_backed: bool,
) -> RouterChoice | None:
    """Pick the router that can actually serve this call.

    :param backends: The deployment's configured clients.
    :param gateway_backed: Whether EVERY harness family involved in this call
        resolves AI-Gateway-backed inference. False takes the external client
        out of play — its picks are gateway catalog ids the pane cannot reach.
    :returns: The chosen client plus its source, or ``None`` when neither
        backend can serve (today's "routing unavailable").
    """
    external = usable_external(backends)
    if gateway_backed and external is not None:
        return RouterChoice(client=external, source="databricks-aigw")
    if backends.local is not None:
        return RouterChoice(client=backends.local, source="oss-llm")
    return None


@dataclass(frozen=True)
class RoutedCall:
    """The answer one routing call got, and who gave it.

    :param result: The verdict, or ``None`` when nothing could answer.
    :param source: Which router answered (or was asked last).
    :param client: The client asked last, so the caller can read its
        ``last_error`` for the reason behind a ``None`` *result*.
    """

    result: RoutingResult | None
    source: RouterSource
    client: RoutingClient


async def route_with_fallback(
    backends: RoutingBackends,
    message: str,
    available_models: dict[str, list[str]],
    *,
    gateway_backed: bool,
) -> RoutedCall | None:
    """Call the deployment's router, falling back to the judge when it fails.

    The external service is still preferred wherever it can serve — that is the
    Databricks posture and this does not change it. What it adds is that its
    failure is no longer the end of the call: a deployment that also configures
    the built-in judge routes with the judge instead of declining, and the
    decision records ``oss-llm`` so the chip says who answered.

    :param backends: The deployment's configured clients.
    :param message: The task text to route on.
    :param available_models: Harness id → candidate model ids.
    :param gateway_backed: Whether every harness family involved resolves
        AI-Gateway-backed inference (see :func:`select_router`).
    :returns: The answer, or ``None`` when no backend is configured at all.
    :raises Exception: Whatever the client that answered last raised — the
        external one only when there is no judge to fall back to, so each
        caller's existing fail-open handling still sees it.
    """
    choice = select_router(backends, gateway_backed=gateway_backed)
    if choice is None:
        return None
    if choice.source != "databricks-aigw" or backends.local is None:
        return RoutedCall(
            result=await choice.client.route(message, available_models),
            source=choice.source,
            client=choice.client,
        )
    try:
        result = await choice.client.route(message, available_models)
    except Exception:  # noqa: BLE001 — the judge is the fallback for any failure
        _logger.warning(
            "routing: the external router raised; falling back to the built-in judge",
            exc_info=True,
        )
    else:
        if result is not None:
            return RoutedCall(result=result, source="databricks-aigw", client=choice.client)
        _logger.info(
            "routing: the external router gave no verdict (%s); "
            "falling back to the built-in judge",
            getattr(choice.client, "last_error", None),
        )
    return RoutedCall(
        result=await backends.local.route(message, available_models),
        source="oss-llm",
        client=backends.local,
    )


def reported_gateway_inference(
    host: Any,  # type: ignore[explicit-any]  # a Host row, or None for a sandbox
) -> dict[str, bool] | None:
    """The gateway-inference map *host* last reported over its tunnel.

    Read from this replica's live :class:`~omnigent.server.host_registry.HostRegistry`,
    which the host fills on its connect handshake — nothing about gateway
    backing is persisted, so a replica that has not seen the host (a fresh
    process, another replica) simply reads unknown until it reconnects and
    re-reports.

    :param host: The session's target host row, or ``None``.
    :returns: The reported map, or ``None`` when nothing has been reported here.
    """
    host_id = getattr(host, "host_id", None) if host is not None else None
    if not isinstance(host_id, str):
        return None
    from omnigent.server.routes._sessions.common import get_server_host_registry

    registry = get_server_host_registry()
    if registry is None:
        return None
    return registry.gateway_inference(host_id)


def gateway_backs_all(
    host: Any,  # type: ignore[explicit-any]  # a Host row, or None for a sandbox
    harnesses: Iterable[str],
) -> bool:
    """Whether *host* backs every one of *harnesses* with the workspace AI gateway.

    Unknown reads as backed: a host that reports nothing, an older build, one
    bound to another replica, or none bound at all all land here, and
    withholding the external router there would downgrade every deployment that
    cannot yet answer.

    :param host: The session's target host, or ``None``.
    :param harnesses: Harness ids to check, e.g. ``("claude-native",)``.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    from omnigent.gateway_inference import not_gateway_backed

    return not not_gateway_backed(reported_gateway_inference(host), harnesses)


def backends_from_caps(caps: Any) -> RoutingBackends:  # type: ignore[explicit-any]  # RuntimeCaps-shaped
    """Read a deployment's routing backends off its caps.

    Prefers the explicit :attr:`~omnigent.runtime.caps.RuntimeCaps.routing_backends`.
    Absent it, the legacy single ``routing_client`` is classified by type — an
    :class:`~omnigent.server.smart_routing.ExternalRoutingClient` is the external
    source, anything else is treated as the OSS judge. Misclassifying a custom
    client as OSS costs only a badge on the chip, whereas the reverse would
    promise gateway reachability nobody verified.

    :param caps: A ``RuntimeCaps`` (or structural equivalent), or ``None``.
    :returns: The configured backends; all-``None`` when nothing is set.
    """
    if caps is None:
        return RoutingBackends()
    backends = getattr(caps, "routing_backends", None)
    if isinstance(backends, RoutingBackends):
        return backends
    client = getattr(caps, "routing_client", None)
    if client is None:
        return RoutingBackends()
    from omnigent.server.smart_routing import ExternalRoutingClient

    if isinstance(client, ExternalRoutingClient):
        return RoutingBackends(external=client)
    return RoutingBackends(local=client)


def _managed_llm_capability(caps: Any) -> bool:  # type: ignore[explicit-any]  # RuntimeCaps-shaped
    """Whether a managed deployment registered its policy-LLM factory.

    The factory means the deployment has LLM capability and supplies its own
    :class:`~omnigent.server.smart_routing.RoutingClient` later, so routing is
    available even though no client is on the caps yet.
    """
    return caps is not None and getattr(caps, "policy_llm_connection_factory", None) is not None


def routing_sources(caps: Any) -> dict[str, bool]:  # type: ignore[explicit-any]  # RuntimeCaps
    """Report which routers this deployment can answer a routing call with.

    Reports what can answer NOW, not what was configured: an external client
    that has latched a permanent failure (its workspace has no routing API)
    reads as absent, so ``/v1/info`` stops advertising a router that will only
    decline. Nothing about that is persisted — a restart re-probes.

    :param caps: A ``RuntimeCaps`` (or structural equivalent), or ``None``.
    :returns: ``{"external": ..., "oss": ...}`` — ``"external"`` is the
        workspace AI-Gateway ``task_v1`` client, ``"oss"`` the built-in judge
        (or a managed factory that will supply one).
    """
    backends = backends_from_caps(caps)
    return {
        "external": usable_external(backends) is not None,
        "oss": backends.local is not None or _managed_llm_capability(caps),
    }


def routing_available(caps: Any) -> bool:  # type: ignore[explicit-any]  # RuntimeCaps-shaped
    """Whether this deployment can route at all, from ANY source.

    The single "is routing configured" gate. It is exactly "some source can
    answer", so it can never disagree with :func:`routing_sources` — the drift
    that let a deployment configuring only ``routing_backends`` report routing
    off while the server routed anyway.

    :param caps: A ``RuntimeCaps`` (or structural equivalent), or ``None``.
    :returns: ``True`` when at least one router can answer.
    """
    return any(routing_sources(caps).values())
