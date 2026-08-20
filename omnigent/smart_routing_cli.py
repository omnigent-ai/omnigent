"""CLI-side Smart Routing: arm a session so its harness routes what you type.

``omnigent claude --smart-routing`` and ``omnigent codex --smart-routing`` are
the only CLI entry points. Neither routes anything before the TUI starts: the
CLI creates the session through the standard JSON ``POST /v1/sessions`` with
``cost_control_mode_override="on"`` and no ``smart_routing_message``, then
attaches the native wrapper to it. The harness's own first-message hook picks
the model once the user types. Routing a prompt at create time is the web UI's
job — it is the surface that can show and change the pick before the session
runs.

Because the session is created here rather than bundled by the wrapper, the row
already carries the agent binding, the wrapper's presentation labels, and the
Smart Routing mode the hook's server-side gate reads.

Two rules shape everything here:

* **Preflight is a hard error.** Routing that no source on the server can
  serve means the pick could not be applied — say so and stop. A family
  whose inference is not AI-Gateway-backed is not that case: it downgrades to the server's built-in
  router, and only fails when there is no built-in router either.
* **Arming itself fails open.** Once preflight passes, a create the server
  rejects (or cannot answer) returns a one-line notice and no session, and the
  wrapper launches its own. The launch always happens.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import click
import httpx

from omnigent.db.utils import builtin_agent_id
from omnigent.harness_aliases import canonicalize_harness
from omnigent.native_coding_agents import native_coding_agent_for_harness

#: Provenance label on a CLI-routed session. The server merges the wrapper's
#: own presentation labels (``omnigent.ui`` / ``omnigent.wrapper``) over it.
ROUTING_SESSION_LABELS = {"omnigent.smart_routing": "cli-route"}

#: Budget for the armed ``POST /v1/sessions``. Generous on the read because
#: this is a session CREATE, not a routing call: it validates the workspace on
#: the host, may cut a worktree, and resolves a pre-launch model catalog. A
#: short budget here would abandon creates that were merely slow and drop the
#: user into an unrouted session for no good reason.
_TIMEOUT = httpx.Timeout(10.0, read=60.0)

#: Budget for the preflight reads (``/v1/info``, ``/v1/hosts``). These ARE
#: routing calls, they answer in milliseconds on a healthy server, and every
#: failure already degrades to "unknown" — which does not gate — so there is
#: nothing to win by waiting. Keeping it short stops a wedged server from
#: stalling the launch before the session even exists.
_PREFLIGHT_TIMEOUT = httpx.Timeout(5.0)


@dataclass(frozen=True)
class ArmedSession:
    """
    The session the CLI armed for Smart Routing, and what to say about it.

    :param session_id: The created session, e.g. ``"conv_abc123"``. The wrapper
        attaches to this instead of bundling its own. ``None`` when the create
        failed — the caller then launches a fresh wrapper session.
    :param notice: One user-facing line explaining why the session was not
        armed, e.g. ``"omnigent: Smart Routing was unavailable (...)"``.
        ``None`` when the create succeeded.
    """

    session_id: str | None
    notice: str | None


def local_gateway_inference() -> dict[str, bool]:
    """
    This machine's own per-harness AI-Gateway-backed map, or ``{}``.

    A ``--smart-routing`` launch always runs its TUI on *this* machine, so the
    local config resolution is the authoritative answer — and it needs neither a
    registered host row nor a server round-trip. Never raises: an unevaluable
    map is "unknown", which does not gate.

    :returns: Harness spelling → gateway-backed flag, e.g.
        ``{"claude-native": True, "codex-native": False}``; ``{}`` when the
        check could not run at all.
    """
    from omnigent.gateway_inference import gateway_inference_map

    try:
        return gateway_inference_map()
    except Exception:  # noqa: BLE001 — an unevaluable map is unknown, not unavailable
        return {}


def check_smart_routing_available(
    *,
    base_url: str,
    harnesses: Sequence[str],
    host_id: str | None = None,
) -> None:
    """
    Fail loud when Smart Routing cannot be applied for *harnesses*.

    Gate 1 is availability: the server must have at least one routing source
    (``GET /v1/info`` ``smart_routing_sources``) — the external AI-Gateway
    router, the built-in one, or both. With neither, nothing can produce a pick.

    Gates 2 and 3 are *source selection*, not availability. A family whose
    inference is not AI-Gateway-backed cannot run a gateway-routed pick, so it
    downgrades to the server's built-in router (one informational line, then
    proceed) and only fails when the server has no built-in router either. The
    answer comes from this machine's own config first
    (:func:`local_gateway_inference`) — the launch happens here, so the local
    answer is authoritative and needs no host row — and failing that from the
    host row the server holds for this machine (``GET /v1/hosts``
    ``gateway_inference``), which is what an older CLI had to rely on.

    An absent ``gateway_inference`` map — or an absent entry in it — is
    *unknown*, not off-gateway: a family whose check could not run keeps every
    option. An older server that omits ``smart_routing_sources`` degrades to its
    ``smart_routing_enabled`` answer for both sources, so it blocks nothing new.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param harnesses: Harness ids the route may pick, e.g.
        ``("claude-native",)``.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``. ``None``
        skips the server-side per-host gate (nothing to look up).
    :returns: None when routing may proceed.
    :raises click.ClickException: When routing is unavailable, naming why.
    """
    info = _get_json(base_url=base_url, path="/v1/info")
    sources = _routing_sources(info)
    if not (sources["external"] or sources["oss"]):
        raise click.ClickException(
            f"Smart Routing is not enabled on {base_url}: the server has no routing "
            "model configured. Re-run without --smart-routing, or pass --model to "
            "pick a model yourself."
        )
    local = local_gateway_inference()
    remote: dict[str, Any] | None = None
    for harness in harnesses:
        state = _gateway_state(local, harness)
        if state is None:
            # The local check said nothing about this family; fall back to the
            # host row, which an older host build may still answer for.
            if remote is None and host_id is not None:
                remote = _gateway_inference_for_host(base_url=base_url, host_id=host_id) or {}
            state = _gateway_state(remote or {}, harness)
        if state is None or state is True:
            continue
        if sources["oss"]:
            # Off the gateway the AI Gateway's router cannot be applied here,
            # but the server's built-in one still answers.
            click.echo(
                f"{harness} is not AI-Gateway-backed on this host — routing with the "
                "built-in router instead",
                err=True,
            )
            continue
        reason = state if isinstance(state, str) else "not gateway-backed"
        raise click.ClickException(
            f"Smart Routing is unavailable for {harness} on this host: its inference "
            f"is not AI-Gateway-backed ({reason}), so a routed model would not be "
            "reachable from the pane. Re-run without --smart-routing, or point the "
            "harness at the workspace AI Gateway (`omnigent configure harnesses`)."
        )


def _routing_sources(info: dict[str, Any]) -> dict[str, bool]:
    """
    Which routing sources the server can serve, from ``GET /v1/info``.

    :param info: The decoded ``/v1/info`` payload, possibly ``{}``.
    :returns: ``{"external": bool, "oss": bool}`` — the AI-Gateway router and
        the built-in one. A server that omits (or garbles) the field degrades to
        its ``smart_routing_enabled`` answer for both: a server that can route
        is assumed able to serve either source, so nothing new is blocked. The
        web parser degrades the same way, so both surfaces read one older server
        alike.
    """
    enabled = info.get("smart_routing_enabled") is True
    raw = info.get("smart_routing_sources")
    if not isinstance(raw, dict):
        return {"external": enabled, "oss": enabled}
    return {"external": raw.get("external") is True, "oss": raw.get("oss") is True}


def arm_smart_routing_session(
    *,
    base_url: str,
    harness: str,
    host_id: str | None = None,
    workspace: str | None = None,
) -> ArmedSession:
    """
    Create the session with Smart Routing on, and nothing routed yet.

    Sends ``cost_control_mode_override="on"`` — the mode the harness hook's
    server-side gate reads — bound to *harness*'s own wrapper agent. No
    ``smart_routing_message``, so the create routes nothing: the model is picked
    in-session when the user types their first message. This is the session the
    wrapper attaches to; nothing is deleted.

    Never raises: a create the server rejects yields no session and one notice,
    and the caller launches a fresh wrapper session instead.

    :param base_url: Omnigent server base URL.
    :param harness: Canonical native harness to bind, e.g. ``"claude-native"``.
    :param host_id: Host this session will run on, e.g. ``"host_abc123"``.
        Binding it lets the server resolve the pane's model catalog for the
        in-session route. ``None`` (the server does not know this host yet)
        still creates the session.
    :param workspace: Absolute workspace path on *host_id* — the launch cwd.
        Required by the server whenever ``host_id`` is set (it is validated
        against the agent's cwd boundary), so it is sent only with *host_id*.
    :returns: The :class:`ArmedSession` to launch on.
    """
    body: dict[str, Any] = {
        "agent_id": _routing_agent_id(harness),
        "host_type": "external",
        "labels": dict(ROUTING_SESSION_LABELS),
        "cost_control_mode_override": "on",
    }
    if host_id is not None:
        body["host_id"] = host_id
        # host_id without workspace is a 400 — the server stats the path on the
        # host to validate the agent's cwd boundary.
        body["workspace"] = workspace
    try:
        with httpx.Client(
            base_url=base_url, headers=_headers(base_url, host_id=host_id), timeout=_TIMEOUT
        ) as client:
            resp = client.post("/v1/sessions", json=body)
            if resp.status_code >= 400:
                return _unavailable(f"the server rejected the routed session ({resp.status_code})")
            payload = _json_object(resp)
            raw_id = payload.get("id") or payload.get("session_id")
            session_id = raw_id if isinstance(raw_id, str) and raw_id else None
    except httpx.HTTPError as exc:
        return _unavailable(f"could not reach {base_url}: {exc}")
    if session_id is None:
        return _unavailable("the create returned no session id")
    return ArmedSession(session_id=session_id, notice=None)


def _unavailable(reason: str) -> ArmedSession:
    """
    Build the fail-open result for *reason*.

    :param reason: Short cause, e.g. ``"the create returned no session id"``.
    :returns: A result with no session and one user-facing notice line.
    """
    return ArmedSession(
        session_id=None,
        notice=(
            f"omnigent: Smart Routing was unavailable ({reason}); launching on the default model."
        ),
    )


def _routing_agent_id(harness: str) -> str:
    """
    Built-in agent to bind the armed session to.

    The bound agent only has to exist and carry the right wrapper — Smart
    Routing rides on the session row, not the agent.

    :param harness: Canonical harness id, e.g. ``"codex-native"``.
    :returns: A deterministic built-in agent id.
    :raises ValueError: When *harness* has no native wrapper agent.
    """
    native = native_coding_agent_for_harness(harness)
    if native is None:
        raise ValueError(f"{harness!r} has no native wrapper agent to bind")
    return builtin_agent_id(native.agent_name)


def known_host_id(*, base_url: str, host_id: str | None) -> str | None:
    """
    Return *host_id* only when the server already knows that host.

    Binding a routing session to a host the server has never seen would 4xx
    the create and cost us the verdict, so an unregistered host degrades to a
    hostless route instead.

    :param base_url: Omnigent server base URL.
    :param host_id: This machine's host id, or ``None``.
    :returns: *host_id* when it appears in ``GET /v1/hosts``, else ``None``.
    """
    if host_id is None:
        return None
    payload = _get_json(base_url=base_url, path="/v1/hosts", host_id=host_id)
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, list):
        return None
    for host in hosts:
        if isinstance(host, dict) and host.get("host_id") == host_id:
            return host_id
    return None


def _gateway_inference_for_host(*, base_url: str, host_id: str) -> dict[str, Any] | None:
    """
    Read this host's ``gateway_inference`` map from ``GET /v1/hosts``.

    :param base_url: Omnigent server base URL.
    :param host_id: Host id to match, e.g. ``"host_abc123"``.
    :returns: The map, or ``None`` when the host, the field, or the request is
        unavailable (all of which mean "unknown", which does not gate).
    """
    payload = _get_json(base_url=base_url, path="/v1/hosts", host_id=host_id)
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, list):
        return None
    for host in hosts:
        if not isinstance(host, dict) or host.get("host_id") != host_id:
            continue
        gateway = host.get("gateway_inference")
        return gateway if isinstance(gateway, dict) else None
    return None


def _gateway_state(gateway: dict[str, Any], harness: str) -> Any:
    """
    Look up *harness* in a ``gateway_inference`` map, tolerating spellings.

    The map is keyed by harness spellings (the ``configured_harnesses``
    convention: ``claude-native`` / ``native-claude``, ``codex`` /
    ``codex-native`` / ``native-codex``), never by a bare family name — so key
    off the canonical id, falling back to the spelling the caller passed.

    :param gateway: The host's ``gateway_inference`` map.
    :param harness: Harness id to look up, e.g. ``"codex-native"``.
    :returns: The stored value, or ``None`` when absent (= unknown).
    """
    canonical = canonicalize_harness(harness) or harness
    for key in (canonical, harness):
        if key in gateway:
            return gateway[key]
    return None


def _headers(base_url: str, *, host_id: str | None) -> dict[str, str]:
    """
    Auth headers for *base_url*, matching every other CLI server call.

    :param base_url: Omnigent server base URL.
    :param host_id: This machine's host id for the slice-key header, or
        ``None``. Required kwarg because ``_remote_headers`` makes it required
        (host_id sharding, commit 615383a) — a preflight read that omitted it
        used to crash before the routing decision ran.
    :returns: Header mapping, possibly empty for a local server.
    """
    from omnigent.chat import _remote_headers

    return _remote_headers(server_url=base_url, host_id=host_id)


def _get_json(*, base_url: str, path: str, host_id: str | None = None) -> dict[str, Any]:
    """
    GET *path* and return its JSON object, or ``{}`` on any failure.

    Preflight reads treat an unreadable answer as "unknown" and let the
    caller's own defaults decide, so this never raises — and a timeout is one
    of those unreadable answers, which is why the budget is
    :data:`_PREFLIGHT_TIMEOUT` rather than the create's.

    :param base_url: Omnigent server base URL.
    :param path: Request path, e.g. ``"/v1/info"``.
    :param host_id: This machine's host id for the slice-key header, or
        ``None`` (the ``/v1/info`` availability probe needs no host routing).
    :returns: The decoded object, or ``{}``.
    """
    try:
        with httpx.Client(
            base_url=base_url,
            headers=_headers(base_url, host_id=host_id),
            timeout=_PREFLIGHT_TIMEOUT,
        ) as client:
            resp = client.get(path)
            if resp.status_code >= 400:
                return {}
            return _json_object(resp)
    except httpx.HTTPError:
        return {}


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    """
    Decode *resp* as a JSON object.

    :param resp: The HTTP response.
    :returns: The decoded object, or ``{}`` when the body is not one.
    """
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
