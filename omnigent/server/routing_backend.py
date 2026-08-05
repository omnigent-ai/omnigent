"""Which router answers a routing call — the AI Gateway's, or the built-in judge's.

Smart Routing has two possible backends. The external ``task_v1`` client rewrites
a launch's model to a Databricks AI Gateway catalog id, so it can only serve a
harness whose inference is gateway-backed. The built-in OSS judge
(:class:`~omnigent.server.smart_routing.LLMRoutingClient`) names models from the
candidate menu it is handed, so it serves any harness.

Gateway backing therefore selects the SOURCE rather than hiding the surface: an
off-gateway pane still routes, just with the built-in judge, and the decision
records which router answered so the UI can say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from omnigent.server.smart_routing import RoutingClient

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
    if gateway_backed and backends.external is not None:
        return RouterChoice(client=backends.external, source="databricks-aigw")
    if backends.local is not None:
        return RouterChoice(client=backends.local, source="oss-llm")
    return None


def gateway_backs_all(
    host: Any,  # type: ignore[explicit-any]  # a Host row, or None for a sandbox
    harnesses: Iterable[str],
) -> bool:
    """Whether *host* backs every one of *harnesses* with the workspace AI gateway.

    Unknown reads as backed: an older host build (or none bound at all) reports
    nothing, and withholding the external router there would downgrade every
    deployment that cannot yet answer.

    :param host: The session's target host, or ``None``.
    :param harnesses: Harness ids to check, e.g. ``("claude-native",)``.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    from omnigent.gateway_inference import not_gateway_backed

    gateway = getattr(host, "gateway_inference", None) if host is not None else None
    return not not_gateway_backed(gateway, harnesses)


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
