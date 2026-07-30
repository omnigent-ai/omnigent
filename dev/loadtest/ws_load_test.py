"""WebSocket load test for the Omnigent server — one target, many sockets.

Opens **N concurrent WebSocket connections** to the client-facing live-updates
socket ``WS /v1/sessions/updates`` and holds them open, exactly as N browser
tabs would. No agent turns, no runner, no LLM — this isolates the server's
WebSocket fan-out: handshake, origin/auth gating, per-connection watch-set
diffing, and the idle heartbeat timer.

``-u N`` (locust ``--users``) is the number of concurrent sockets. Each user
opens ONE connection in ``on_start`` and keeps it open for the whole run; the
task loop drives ``watch`` round-trips and drains server-pushed frames on it.

Protocol (JSON text frames), from the server's ``session_updates`` handler
(``omnigent/server/routes/sessions.py``):

* client → server: ``{"type": "watch", "session_ids": [...]}`` — the ids the
  client is displaying. Fully replaces the prior watch-set; the server answers
  each ``watch`` with exactly one ``snapshot``.
* server → client: ``{"type": "snapshot", "items": [...]}`` per watch, then
  ``{"type": "changed", "items": [...]}`` / ``{"type": "removed", "ids": [...]}``
  deltas as watched sessions change, and ``{"type": "heartbeat"}`` every ~30s
  when idle.

Auth: a local single-user server needs none. Against an authenticated
deployment, pass a bearer token via ``-e AUTH_TOKEN`` (sent as
``Authorization: Bearer <token>`` on the handshake). The first-party sentinel
``Origin: omnigent://internal`` is always sent so the server's cross-origin
WebSocket (CSWSH) guard accepts a non-browser client regardless of mode.

Metrics (Locust ``WS`` request type):

* ``connect`` — handshake + first snapshot latency (a socket came up), fired
  from ``on_start``.
* ``watch roundtrip`` — send a ``watch`` on an open socket, time to its
  ``snapshot`` (server round-trip under connection load).
* ``frames drained`` — a bounded read that consumes pushed heartbeat/delta
  frames; ``response_length`` is the frame count.

Run against a local server (no auth):

    pip install -e '.[loadtest]'          # or: uv sync --extra loadtest
    omnigent server --port 8000 &         # a server to hit
    locust -f dev/loadtest/ws_load_test.py \
        --host http://localhost:8000 \
        --headless -u 50 -r 5 -t 60s

Against a remote deployment (bearer auth):

    locust -f dev/loadtest/ws_load_test.py \
        --host https://my-omnigent.example.com \
        --headless -u 50 -r 5 -t 5m \
        -e AUTH_TOKEN "$TOKEN"

Drop ``--headless`` to open Locust's web UI and drive users interactively.
"""

from __future__ import annotations

import contextlib
import json
import os
import time

import websocket  # websocket-client, from the `loadtest` extra
from locust import User, between, task

# The session-updates WS route path. The server mounts its routers at prefix
# "/v1" with no further mount, so this is the full path on a plain server.
SESSION_UPDATES_PATH = "/v1/sessions/updates"

# First-party non-browser sentinel Origin. The server's WebSocket origin guard
# (omnigent/server/ws_origin.py:origin_allowed) always accepts this value, so a
# non-browser client passes the CSWSH check in every server mode. Mirrors the
# OmnigentClient default header.
INTERNAL_ORIGIN = "omnigent://internal"

# Request-type label all samples report under, so the WS load is its own group
# in the Locust stats table.
WS_REQUEST_TYPE = "WS"

# Per-recv timeout. Above the server's 30s session-updates heartbeat so an idle
# socket still reads a frame (the heartbeat) within one recv.
DEFAULT_WS_READ_TIMEOUT_S = 35.0

# Handshake timeout for opening a connection.
CONNECT_TIMEOUT_S = 30.0

# Max frames one "frames drained" iteration consumes, so a user can't
# monopolize its greenlet draining a chatty socket.
MAX_FRAMES_PER_DRAIN = 10


def _ws_url(host: str, mount_prefix: str = "") -> str:
    """Derive the ``ws(s)://`` session-updates URL from an ``http(s)`` host.

    :param host: The server base URL (Locust ``--host``), e.g.
        ``"http://localhost:8000"``; its scheme is swapped to the WS scheme.
    :param mount_prefix: Path the Omnigent app is mounted under, e.g.
        ``"/omnigent"`` or ``"/api/2.0/omnigent"`` on a fronted deployment.
        Empty (default) for a plain server that serves the routes at root.
    :returns: The full WebSocket URL for ``WS /v1/sessions/updates``.
    """
    base = host.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    prefix = "/" + mount_prefix.strip("/") if mount_prefix.strip("/") else ""
    return f"{base}{prefix}{SESSION_UPDATES_PATH}"


class SessionUpdatesUser(User):
    """One simulated browser tab holding a live-updates WebSocket open.

    Each user opens a single connection in :meth:`on_start` and keeps it for the
    whole run; tasks reuse it. ``-u N`` gives N concurrent sockets against the
    one target host.
    """

    # Short think-time between iterations. The socket stays open across
    # iterations — this only paces how often each user re-watches / drains.
    wait_time = between(1.0, 3.0)

    def on_start(self) -> None:
        """Resolve config and open the connection, timing the first snapshot."""
        token = os.getenv("AUTH_TOKEN", "").strip()
        self.headers = [f"Authorization: Bearer {token}"] if token else []
        # The connected host the load is scoped to (set by run.py --host-id).
        # The session-updates socket is user-scoped, not host-scoped, so this is
        # recorded run context here; host-scoped scenarios read it to target a
        # specific host.
        self.host_id = os.getenv("HOST_ID", "").strip() or None
        # Path the Omnigent app is mounted under on a fronted deployment (e.g.
        # "/omnigent"); empty for a plain server serving routes at root.
        mount_prefix = os.getenv("MOUNT_PREFIX", "").strip()
        self.ws_url = _ws_url(self.host, mount_prefix)
        self.read_timeout = float(os.getenv("WS_READ_TIMEOUT", str(DEFAULT_WS_READ_TIMEOUT_S)))
        raw_ids = os.getenv("SESSION_IDS", "").strip()
        # Default: empty watch-set — a valid, dependency-free probe. The server
        # returns an empty snapshot, then heartbeats.
        self.session_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        self.ws: websocket.WebSocket | None = None
        self._connect()

    def on_stop(self) -> None:
        """Close the connection at the end of the run."""
        self._close()

    # ── connection lifecycle ─────────────────────────────────────────

    def _connect(self) -> None:
        """Open the socket, send the initial watch, await the first snapshot.

        Reported as the ``connect`` sample: TCP + TLS handshake, the WebSocket
        upgrade (with optional bearer auth), the initial ``watch`` send, and the
        server's first ``snapshot`` — the full "socket is live and serving"
        cost. On failure the sample is recorded as a failure and ``self.ws`` is
        left ``None`` so the task loop retries.
        """
        start = time.monotonic()
        try:
            ws = websocket.create_connection(
                self.ws_url,
                timeout=CONNECT_TIMEOUT_S,
                header=self.headers,
                origin=INTERNAL_ORIGIN,
                enable_multithread=True,
            )
            ws.send(json.dumps({"type": "watch", "session_ids": self.session_ids}))
            ws.settimeout(self.read_timeout)
            self._read_until_snapshot(ws)
        except Exception as exc:  # noqa: BLE001 — any failure is a failed connect sample
            self._fire("connect", start, exc=exc)
            self._close()
            return
        self.ws = ws
        self._fire("connect", start)

    def _close(self) -> None:
        """Close and drop the current connection (best-effort)."""
        if self.ws is not None:
            # Teardown must not raise — a socket already faulted / closed is fine.
            with contextlib.suppress(Exception):
                self.ws.close()
            self.ws = None

    def _read_until_snapshot(self, ws: websocket.WebSocket) -> None:
        """Read frames until a ``snapshot`` arrives (the reply to a ``watch``).

        The server answers each ``watch`` with exactly one ``snapshot``, but a
        heartbeat or delta can interleave, so non-snapshot frames are skipped.

        :param ws: The open connection to read from.
        :raises websocket.WebSocketTimeoutException: If no snapshot arrives
            within the socket's read timeout.
        """
        deadline = time.monotonic() + self.read_timeout
        while time.monotonic() < deadline:
            frame = ws.recv()
            if self._frame_type(frame) == "snapshot":
                return
        raise websocket.WebSocketTimeoutException("no snapshot within read timeout")

    @staticmethod
    def _frame_type(frame: object) -> str | None:
        """Return the ``type`` of a JSON text frame, or ``None`` if unparseable.

        :param frame: A raw frame from ``ws.recv()`` (str for text frames).
        :returns: The frame's ``type`` field, or ``None``.
        """
        if not isinstance(frame, str):
            return None
        try:
            parsed = json.loads(frame)
        except json.JSONDecodeError:
            return None
        return parsed.get("type") if isinstance(parsed, dict) else None

    def _fire(
        self,
        name: str,
        start: float,
        *,
        response_length: int = 0,
        exc: Exception | None = None,
    ) -> None:
        """Report one Locust request sample under the ``WS`` request type.

        :param name: Sample name, e.g. ``"connect"`` / ``"watch roundtrip"``.
        :param start: ``time.monotonic()`` captured before the timed operation.
        :param response_length: Bytes/frames to attribute to the sample.
        :param exc: Exception to record a failure, or ``None`` for success.
        """
        self.environment.events.request.fire(
            request_type=WS_REQUEST_TYPE,
            name=name,
            response_time=(time.monotonic() - start) * 1000,
            response_length=response_length,
            exception=exc,
            context={},
        )

    # ── tasks (run on the already-open socket) ───────────────────────

    @task(3)
    def watch_roundtrip(self) -> None:
        """Send a ``watch`` on the open socket and time the ``snapshot`` reply.

        The server round-trip under connection load. Reconnects first if the
        socket is down (e.g. after a prior failure), so a dropped connection
        self-heals rather than wedging the user.
        """
        if self.ws is None:
            self._connect()
            return
        start = time.monotonic()
        try:
            self.ws.send(json.dumps({"type": "watch", "session_ids": self.session_ids}))
            self._read_until_snapshot(self.ws)
        except Exception as exc:  # noqa: BLE001 — record + drop the socket for reconnect
            self._fire("watch roundtrip", start, exc=exc)
            self._close()
            return
        self._fire("watch roundtrip", start)

    @task(1)
    def frames_drained(self) -> None:
        """Consume up to a few server-pushed frames (heartbeats / deltas).

        Keeps the receive buffer from backing up on a long-held socket and
        records how many frames were drained (as ``response_length``). A recv
        timeout on an idle socket ends the drain cleanly and still counts as a
        success — idle is not a failure for a hold-open socket.
        """
        if self.ws is None:
            self._connect()
            return
        start = time.monotonic()
        count = 0
        try:
            for _ in range(MAX_FRAMES_PER_DRAIN):
                frame = self.ws.recv()
                if self._frame_type(frame) is not None:
                    count += 1
        except websocket.WebSocketTimeoutException:
            # Idle socket: no frame within the timeout. Expected for a quiet
            # watch-set — report what we drained (possibly zero) as a success.
            pass
        except Exception as exc:  # noqa: BLE001 — a real socket error drops the connection
            self._fire("frames drained", start, response_length=count, exc=exc)
            self._close()
            return
        self._fire("frames drained", start, response_length=count)
