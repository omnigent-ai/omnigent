"""CLI-side Smart Routing: pick a harness/model *before* the TUI launches.

``omnigent claude --smart-routing -p "..."`` (tier 2) and
``omnigent run --smart-routing -p "..."`` (tier 3) both need a routing verdict
in hand before a native wrapper starts, because the harness pick is physical
(a session *is* a live ``claude``/``codex`` process) and the model is applied
as a launch flag. The web UI gets the same verdict server-side at session
create; the CLI takes the same path — it creates the session itself through the
standard JSON ``POST /v1/sessions`` with the routing contract fields, reads the
resolved ``harness`` / ``model_override`` back off the response, and attaches
the matching native wrapper to that session. One session, routed at create: the
row already carries the agent binding, the wrapper's presentation labels, the
routed model, and the routing decision card, so the launched session shows the
same chip and provenance the web UI gets.

Two rules shape everything here:

* **Preflight is a hard error.** Routing that the server cannot do, or a host
  whose inference is not AI-Gateway-backed, means the pick could not be
  applied — say so and stop (designs/INTELLIGENT_ROUTING_PLAN.md §10).
* **Routing itself fails open.** Once preflight passes, any router failure
  (missing verdict, HTTP error, unreachable server) returns a decision with a
  one-line notice and no pick. The launch always happens.
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
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_AGENT_NAME,
    native_coding_agent_for_harness,
)

#: Sentinel ``harness_override`` that asks the server to route the harness too.
AUTO_HARNESS = "auto"

#: Provenance label on a CLI-routed session. The server merges the wrapper's
#: own presentation labels (``omnigent.ui`` / ``omnigent.wrapper``) over it.
ROUTING_SESSION_LABELS = {"omnigent.smart_routing": "cli-route"}

_TIMEOUT = httpx.Timeout(10.0, read=60.0)


@dataclass(frozen=True)
class RoutingDecision:
    """
    The routed session the CLI attaches to, and what the router picked.

    :param session_id: The created session, e.g. ``"conv_abc123"``. The wrapper
        attaches to this instead of bundling its own. ``None`` when the create
        failed — the caller then launches a fresh wrapper session.
    :param harness: Canonical harness bound to the session, e.g.
        ``"codex-native"`` (for an ``"auto"`` create the server rebinds the
        agent to the wrapper it picked). ``None`` when it could not be read.
    :param model: Routed model id, e.g. ``"databricks-claude-sonnet-4-6"``.
        ``None`` means launch on the harness default.
    :param notice: One user-facing line explaining a missing pick, e.g.
        ``"omnigent: Smart Routing was unavailable (...)"``. ``None`` when the
        router answered.
    """

    session_id: str | None
    harness: str | None
    model: str | None
    notice: str | None


def smart_routing_families(harness: str | None) -> tuple[str, ...]:
    """
    Harness families whose inference must be gateway-backed for *harness*.

    A fixed-harness route only applies to that harness's pane. The auto route
    picks across the claude + codex arms, so it needs both — mirroring the web's
    per-surface gating (top-level Smart Routing needs both; a per-harness
    Model row needs only its own).

    :param harness: Canonical harness id, or ``None`` / :data:`AUTO_HARNESS`
        for the auto route.
    :returns: Harness ids to check, e.g. ``("claude-native", "codex-native")``.
    """
    if harness is None or harness == AUTO_HARNESS:
        return ("claude-native", "codex-native")
    return (harness,)


def check_smart_routing_available(
    *,
    base_url: str,
    harnesses: Sequence[str],
    host_id: str | None = None,
) -> None:
    """
    Fail loud when Smart Routing cannot be applied for *harnesses*.

    Two gates, both config-level (no liveness probing): the server must have a
    routing client (``GET /v1/info`` ``smart_routing_enabled``), and this
    machine's inference for each harness family must be AI-Gateway-backed
    (``GET /v1/hosts`` ``gateway_inference``). An absent ``gateway_inference``
    map — or an absent entry in it — is *unknown*, not unavailable: hosts on
    older builds keep every option.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param harnesses: Harness ids the route may pick, e.g.
        ``("claude-native",)``.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``. ``None``
        skips the per-host gate (nothing to look up).
    :returns: None when routing may proceed.
    :raises click.ClickException: When routing is unavailable, naming why.
    """
    info = _get_json(base_url=base_url, path="/v1/info")
    if not (isinstance(info, dict) and info.get("smart_routing_enabled") is True):
        raise click.ClickException(
            f"Smart Routing is not enabled on {base_url}: the server has no routing "
            "model configured. Re-run without --smart-routing, or pass --model to "
            "pick a model yourself."
        )
    if host_id is None:
        return
    gateway = _gateway_inference_for_host(base_url=base_url, host_id=host_id)
    if gateway is None:
        return
    for harness in harnesses:
        state = _gateway_state(gateway, harness)
        if state is None or state is True:
            continue
        reason = state if isinstance(state, str) else "not gateway-backed"
        raise click.ClickException(
            f"Smart Routing is unavailable for {harness} on this host: its inference "
            f"is not AI-Gateway-backed ({reason}), so a routed model would not be "
            "reachable from the pane. Re-run without --smart-routing, or point the "
            "harness at the workspace AI Gateway (`omnigent configure harnesses`)."
        )


def create_smart_routing_session(
    *,
    base_url: str,
    prompt: str,
    harness: str | None,
    host_id: str | None = None,
    workspace: str | None = None,
) -> RoutingDecision:
    """
    Create the routed session, and read the verdict back off the create.

    Sends the routing contract — ``cost_control_mode_override="on"``,
    ``smart_routing_message=<prompt>``, and (auto route only)
    ``harness_override="auto"``; a fixed harness comes from the bound wrapper
    agent, so it needs no override. The response carries the resolved
    ``harness`` and ``model_override``, with the session snapshot as a fallback.
    This is the session the wrapper attaches to — nothing is deleted.

    Never raises: a create the server rejects (including the auto route's
    "no native CLI on this host") yields a decision with no session and a
    notice, and the caller launches a fresh wrapper session instead.

    :param base_url: Omnigent server base URL.
    :param prompt: The user's ``-p`` text. Routed, not dispatched — the TUI
        delivers it as its own first input.
    :param harness: Canonical harness to pin, or ``None`` for the auto route.
    :param host_id: Host this session will run on, e.g. ``"host_abc123"``.
        Needed for a real verdict: the server builds the candidate model
        catalog by round-tripping the bound host's model-options frames, so a
        hostless create gives the router an empty menu. ``None`` (the server
        does not know this host yet) still routes, over whatever it can resolve
        without one.
    :param workspace: Absolute workspace path on *host_id* — the launch cwd.
        Required by the server whenever ``host_id`` is set (it is validated
        against the agent's cwd boundary), so it is sent only with *host_id*.
    :returns: The :class:`RoutingDecision` to launch on.
    """
    body: dict[str, Any] = {
        "agent_id": _routing_agent_id(harness),
        "host_type": "external",
        "labels": dict(ROUTING_SESSION_LABELS),
        "cost_control_mode_override": "on",
        "smart_routing_message": prompt,
    }
    if harness is None:
        # Auto route: the sentinel tells the server to pick the harness and
        # rebind the session's agent to that wrapper.
        body["harness_override"] = AUTO_HARNESS
    if host_id is not None:
        body["host_id"] = host_id
        # host_id without workspace is a 400 — the server stats the path on the
        # host to validate the agent's cwd boundary.
        body["workspace"] = workspace
    session_id: str | None = None
    picked_harness: str | None = None
    picked_model: str | None = None
    try:
        with httpx.Client(
            base_url=base_url, headers=_headers(base_url), timeout=_TIMEOUT
        ) as client:
            resp = client.post("/v1/sessions", json=body)
            if resp.status_code >= 400:
                return _unavailable(f"the server rejected the routed session ({resp.status_code})")
            payload = _json_object(resp)
            raw_id = payload.get("id") or payload.get("session_id")
            session_id = raw_id if isinstance(raw_id, str) and raw_id else None
            # ``harness`` (not ``harness_override``) is the resolved harness on
            # SessionResponse; native rows leave the override null on purpose.
            picked_harness = _clean_str(payload.get("harness"))
            picked_model = _clean_str(payload.get("model_override"))
            if (picked_model is None or picked_harness is None) and session_id is not None:
                snapshot = _json_object(client.get(f"/v1/sessions/{session_id}"))
                picked_harness = picked_harness or _clean_str(snapshot.get("harness"))
                picked_model = picked_model or _clean_str(snapshot.get("model_override"))
    except httpx.HTTPError as exc:
        return _unavailable(f"could not reach {base_url}: {exc}")
    if session_id is None:
        return _unavailable("the create returned no session id")
    notice = (
        None
        if picked_model is not None
        else (
            "omnigent: Smart Routing did not pick a model for this session; "
            "launching on the harness default."
        )
    )
    return RoutingDecision(
        session_id=session_id,
        harness=picked_harness,
        model=picked_model,
        notice=notice,
    )


def _unavailable(reason: str) -> RoutingDecision:
    """
    Build the fail-open decision for *reason*.

    :param reason: Short cause, e.g. ``"the create returned no session id"``.
    :returns: A decision with no session and one user-facing notice line.
    """
    return RoutingDecision(
        session_id=None,
        harness=None,
        model=None,
        notice=(
            f"omnigent: Smart Routing was unavailable ({reason}); "
            "launching on the default harness/model."
        ),
    )


def _routing_agent_id(harness: str | None) -> str:
    """
    Built-in agent to bind the routing session to.

    The bound agent only has to exist — the routing verdict rides on the
    session row, not the agent. A fixed harness uses its own ``*-native-ui``
    built-in; the auto route uses the claude-native built-in, which every
    server seeds.

    :param harness: Canonical harness id, or ``None`` for the auto route.
    :returns: A deterministic built-in agent id.
    """
    native = native_coding_agent_for_harness(harness) if harness else None
    name = native.agent_name if native is not None else CLAUDE_NATIVE_AGENT_NAME
    return builtin_agent_id(name)


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
    payload = _get_json(base_url=base_url, path="/v1/hosts")
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
    payload = _get_json(base_url=base_url, path="/v1/hosts")
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


def _headers(base_url: str) -> dict[str, str]:
    """
    Auth headers for *base_url*, matching every other CLI server call.

    :param base_url: Omnigent server base URL.
    :returns: Header mapping, possibly empty for a local server.
    """
    from omnigent.chat import _remote_headers

    return _remote_headers(server_url=base_url)


def _get_json(*, base_url: str, path: str) -> dict[str, Any]:
    """
    GET *path* and return its JSON object, or ``{}`` on any failure.

    Preflight reads treat an unreadable answer as "unknown" and let the
    caller's own defaults decide, so this never raises.

    :param base_url: Omnigent server base URL.
    :param path: Request path, e.g. ``"/v1/info"``.
    :returns: The decoded object, or ``{}``.
    """
    try:
        with httpx.Client(
            base_url=base_url, headers=_headers(base_url), timeout=_TIMEOUT
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


def _clean_str(value: Any) -> str | None:
    """
    Normalize a wire value to a non-empty string.

    :param value: Raw JSON value, e.g. ``"codex-native"`` or ``None``.
    :returns: The stripped string, or ``None`` when it is not usable.
    """
    return value.strip() if isinstance(value, str) and value.strip() else None
