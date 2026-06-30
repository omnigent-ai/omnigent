"""Client-side helpers for launching runners through the connect daemon.

These are the CLI-side counterpart to the host-runner protocol: the CLI
(``run`` / ``claude`` / ``codex``) asks the Omnigent server to launch
a runner on this machine's daemon via
``POST /v1/hosts/{host_id}/runners``; the server forwards a launch frame to
the daemon, which spawns the runner subprocess and binds it to the session.
The daemon owns the runner lifecycle — the CLI only connects.

Harness-agnostic on purpose: the same launch path serves headless
``run`` agents and the ``claude``/``codex`` terminal wrappers.
"""

from __future__ import annotations

import asyncio
import json

import click
import httpx

from omnigent.claude_native_bridge import url_component

# Poll cadence while waiting for a daemon-spawned runner to connect its
# tunnel or for a resource to appear.
DAEMON_POLL_INTERVAL_S = 0.5


def _json_body(resp: httpx.Response) -> dict[str, object]:
    """Decode a host/runner status response body, tolerating non-JSON.

    The host + runner status endpoints (``GET /v1/hosts/{id}``,
    ``GET /v1/runners/{id}/status``) are expected to answer JSON. But a
    server reached over ``--server`` that does not mount the host router
    (e.g. an API-only deployment, or a misconfigured server) lets these
    paths fall through to the SPA HTML5-history fallback, which answers
    ``200 text/html`` with ``index.html``. Calling ``resp.json()`` on that
    raised an opaque ``json.JSONDecodeError`` that crashed the REPL before
    it ever became ready. Treat any non-dict / non-JSON 200 body as "no
    status yet" so the caller keeps polling and ultimately fails with the
    actionable timeout message instead.

    :param resp: A ``200`` host/runner status response.
    :returns: The decoded JSON object, or an empty dict when the body is
        not a JSON object (e.g. the SPA HTML fallback).
    """
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def wait_for_host_online(
    client: httpx.AsyncClient,
    host_id: str,
    *,
    timeout_s: float,
) -> None:
    """
    Poll the host status endpoint until the daemon is registered.

    ``_ensure_host_daemon`` spawns the connect daemon as a subprocess that
    connects to the server asynchronously, so the runner-launch endpoint
    would 409 ("host offline") until that WebSocket is up. This waits for it.

    Transient transport errors (connection refused while a local server
    is still binding its socket, a dropped keepalive, etc.) are treated
    as "not online yet" and polled through; only the deadline fails the
    wait, with the last transport error included in the message.

    :param client: HTTP client pointed at the Omnigent server.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``30.0``.
    :returns: None once the host reports ``status == "online"``.
    :raises click.ClickException: If the host is not online in time.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_error: httpx.TransportError | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get(f"/v1/hosts/{url_component(host_id)}")
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if resp.status_code == 200 and _json_body(resp).get("status") == "online":
                return
        await asyncio.sleep(DAEMON_POLL_INTERVAL_S)
    message = (
        f"The connect daemon for host {host_id!r} did not come online within {timeout_s:.0f}s."
    )
    if last_error is not None:
        message += f" Last connection error: {last_error!r}."
    message += f" Check that this machine can reach {client.base_url}."
    raise click.ClickException(message)


async def runner_is_online(client: httpx.AsyncClient, runner_id: str) -> bool:
    """
    Return whether a runner currently has an open tunnel to the server.

    :param client: HTTP client pointed at the Omnigent server.
    :param runner_id: Runner id, e.g. ``"runner_abc123"``.
    :returns: ``True`` when the status endpoint reports ``online``.
    """
    resp = await client.get(f"/v1/runners/{url_component(runner_id)}/status")
    return resp.status_code == 200 and bool(_json_body(resp).get("online"))


async def wait_for_runner_online(
    client: httpx.AsyncClient,
    runner_id: str,
    *,
    timeout_s: float,
) -> None:
    """
    Poll until a daemon-spawned runner has connected its tunnel.

    Fails fast when the status endpoint reports the runner process
    died (the host daemon watches its spawned runners and reports
    ``host.runner_exited`` with the exit code and log tail) — a dead
    process can never connect, so waiting out the full timeout would
    only hide the cause.

    Transient transport errors are treated as "not online yet" and
    polled through (same rationale as :func:`wait_for_host_online`);
    only the deadline fails the wait, with the last transport error
    included in the message.

    :param client: HTTP client pointed at the Omnigent server.
    :param runner_id: Runner id the host was asked to spawn, e.g.
        ``"runner_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``60.0``.
    :returns: None once the runner is online.
    :raises click.ClickException: If the runner process died, or if it
        does not connect in time.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_error: httpx.TransportError | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get(f"/v1/runners/{url_component(runner_id)}/status")
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if resp.status_code == 200:
                body = _json_body(resp)
                if body.get("online"):
                    return
                exit_error = body.get("error")
                if isinstance(exit_error, str) and exit_error:
                    # The runner process is dead — it can never come
                    # online. Surface the daemon-composed cause (exit
                    # code + log tail) instead of polling to a timeout.
                    raise click.ClickException(
                        f"Runner {runner_id!r} failed to start: {exit_error}"
                    )
        await asyncio.sleep(DAEMON_POLL_INTERVAL_S)
    message = f"Runner {runner_id!r} did not connect within {timeout_s:.0f}s."
    if last_error is not None:
        message += f" Last connection error: {last_error!r}."
    message += " Check the host-runner logs under ~/.omnigent/logs/host-runner/."
    raise click.ClickException(message)


async def launch_or_reuse_daemon_runner(
    client: httpx.AsyncClient,
    *,
    host_id: str,
    session_id: str,
    workspace: str,
) -> str:
    """
    Ensure the session is bound to a daemon-spawned runner; return its id.

    Reuses the session's currently-bound runner when it is still online
    (resume into a live session). Otherwise clears any stale binding and
    asks the server to launch a fresh runner on the host via
    ``POST /v1/hosts/{host_id}/runners`` (which atomically binds it).

    :param client: HTTP client pointed at the Omnigent server.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param session_id: Session to bind, e.g. ``"conv_abc123"``.
    :param workspace: Absolute host path for the runner cwd, e.g.
        ``"/Users/me/proj"``.
    :returns: The bound runner id, e.g. ``"runner_abc123"``.
    :raises click.ClickException: If the launch request fails.
    """
    snap = await client.get(f"/v1/sessions/{url_component(session_id)}")
    existing = _json_body(snap).get("runner_id") if snap.status_code == 200 else None
    if isinstance(existing, str) and existing:
        if await runner_is_online(client, existing):
            return existing
        # Stale binding (offline runner): clear it so the launch
        # endpoint's atomic ``UPDATE ... WHERE runner_id IS NULL`` can
        # bind the freshly-spawned runner. "" is the clear sentinel.
        await client.patch(
            f"/v1/sessions/{url_component(session_id)}",
            json={"runner_id": ""},
        )
    # The host tunnel can be briefly absent from the server's in-memory
    # registry while it (re)connects — e.g. just after `omnigent host`
    # restarts, after a server restart/redeploy, or under a flapping tunnel.
    # During that window the launch 409s "host is offline" even though the
    # host is online per the cross-replica DB, and the whole session start
    # fails. Retry transient 409s across the reconnect window so
    # high-latency / reconnecting setups start reliably. Bounded, so a
    # genuinely-offline host still fails reasonably fast.
    _RETRY_DELAYS_S = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.0, 3.0)  # ~16.5s budget
    for attempt in range(len(_RETRY_DELAYS_S) + 1):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS_S[attempt - 1])
        resp = await client.post(
            f"/v1/hosts/{url_component(host_id)}/runners",
            json={"session_id": session_id, "workspace": workspace},
            timeout=60.0,
        )
        if resp.status_code < 400:
            break
        transient = resp.status_code == 409 and "offline" in error_text(resp).lower()
        if not (transient and attempt < len(_RETRY_DELAYS_S)):
            raise click.ClickException(
                f"Failed to launch a runner on host {host_id!r} "
                f"({resp.status_code}): {error_text(resp)}"
            )
    runner_id = resp.json().get("runner_id")
    if not isinstance(runner_id, str) or not runner_id:
        raise click.ClickException("Host launch response did not include a runner_id.")
    return runner_id


def error_text(resp: httpx.Response) -> str:
    """
    Extract a concise server error message from an HTTP response.

    :param resp: HTTP response returned by AP.
    :returns: Human-readable error text.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:400]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    return json.dumps(body)[:400]
