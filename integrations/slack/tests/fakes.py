"""Shared fakes for integration tests.

Two halves:

- :class:`RecordingSlackClient` — a Slack Web-API client stand-in that records
  every outbound call (streaming replies, modal open/update, DMs, ephemerals) so
  a test can assert what the bot showed the user. Bolt itself is bypassed: tests
  call ``service.handle_*`` / ``setup._handle_*`` directly, exactly as the unit
  tests do.
- :class:`FakeOmnigentServer` — a ``respx`` router that stands in for the
  Omnigent HTTP API. It owns the endpoint contract (paths, status codes, body
  shapes drawn from ``OmnigentClient``) in ONE place, exposes scenario knobs
  (``auth_required``, ``agents``, ``hosts``, ``sse_body`` …) rather than raw
  routes, and records every request so a test can assert the bot issued
  spec-correct calls (method, path, bearer header, JSON body).

The point of pairing them: drive a real ``OmnigentClient`` (real ``httpx``)
against the fake server, and assert both sides of the seam — the HTTP requests
the bot sent, and the Slack method it called in reaction to each response.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

# The placeholder text the bot posts as its "Working on it…" ack. Kept in sync
# with the service module so a recorded ack is recognizable.
try:  # pragma: no cover - import shape only
    from omnigent_slack.service import _ACK_TEXT
except Exception:  # pragma: no cover
    _ACK_TEXT = "Working on it…"


class FakeStream:
    """Records a ``chat_stream`` lifecycle: appended deltas and the stop tail.

    Mirrors the SDK's in-memory buffering — ``append`` accumulates text and only
    "flushes" (returns a response) once the buffer crosses ``buffer_size`` or a
    forced flush (``chunks`` set) arrives; until then it returns ``None`` like
    the real client. That buffering is what the reply's placeholder/flush logic
    depends on, so the fake reproduces it.
    """

    def __init__(self, client: RecordingSlackClient, start_kwargs: dict[str, Any]) -> None:
        self._client = client
        self.start_kwargs = start_kwargs
        self.appended: list[str] = []
        self.stopped = False
        self.stop_text: str | None = None
        self._buffer_size = 256
        self._pending = 0

    async def append(
        self, *, markdown_text: str | None = None, chunks: Any = None
    ) -> dict[str, Any] | None:
        if markdown_text is not None:
            self.appended.append(markdown_text)
            self._pending += len(markdown_text)
        if chunks is None and self._pending < self._buffer_size:
            return None
        if chunks is not None and self._pending == 0:
            return None
        self._pending = 0
        return {"ok": True}

    async def stop(self, *, markdown_text: str | None = None) -> dict[str, Any]:
        self.stopped = True
        self.stop_text = markdown_text
        return {"ok": True}

    @property
    def text(self) -> str:
        """The full delivered message: streamed deltas plus any stop tail."""
        return "".join(self.appended) + (self.stop_text or "")


class RecordingSlackClient:
    """Records every outbound Slack Web-API call the bot makes.

    Covers the surface both the turn path (streaming replies, posts, DMs) and
    the setup path (modal open/update, team/user lookups) exercise, so one
    client works for every integration scenario.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.deleted_ts: list[str] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.dm_opens: list[dict[str, Any]] = []
        self.opened_views: list[dict[str, Any]] = []
        self.updated_views: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self._next_ts = 0

    # ── turn path ────────────────────────────────────────────────────────
    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._next_ts += 1
        ts = f"bot-{self._next_ts}"
        entry = {**kwargs, "ts": ts}
        self.posts.append(entry)
        if kwargs.get("text") == _ACK_TEXT:
            self.acks.append(entry)
        return {"ok": True, "ts": ts}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.ephemerals.append({**kwargs})
        return {"ok": True, "message_ts": "ephemeral"}

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        self.dm_opens.append({**kwargs})
        return {"ok": True, "channel": {"id": f"D-{kwargs.get('users')}"}}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        ts = kwargs.get("ts")
        self.deleted_ts.append(str(ts))
        self.posts = [p for p in self.posts if p.get("ts") != ts]
        return {"ok": True}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        ts = kwargs.get("ts")
        for post in self.posts:
            if post.get("ts") == ts:
                post.update(kwargs)
        return {"ok": True, "ts": ts}

    async def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:
        channel = kwargs.get("channel")
        ts = kwargs.get("message_ts")
        return {"ok": True, "permalink": f"https://slack.test/archives/{channel}/p{ts}"}

    async def chat_stream(self, **kwargs: Any) -> FakeStream:
        stream = FakeStream(self, kwargs)
        self.streams.append(stream)
        return stream

    # ── setup path ───────────────────────────────────────────────────────
    async def views_open(self, **kwargs: Any) -> dict[str, Any]:
        self.opened_views.append(kwargs)
        return {"ok": True, "view": {"id": "V1"}}

    async def views_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated_views.append(kwargs)
        return {"ok": True}

    async def team_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "team": {"id": kwargs.get("team", "T1"), "name": "Acme Corp"}}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "user": {"profile": {"email": "user@example.com"}}}

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def stream(self) -> FakeStream:
        return self.streams[-1]

    @property
    def streamed_text(self) -> str:
        return "".join(s.text for s in self.streams)

    def last_view_text(self) -> str:
        """Concatenated text of the most recently opened/updated modal.

        Flattens the Block Kit blocks so a test can assert on user-visible copy
        (a login link, a failure reason) without walking the block structure.
        """
        source = self.updated_views[-1] if self.updated_views else self.opened_views[-1]
        view = source.get("view", source)
        return _flatten_blocks(view)


async def _dropping_stream(body: str):
    """Yield ``body`` then raise a mid-stream drop the client treats as a proxy
    severing a long-lived response (reconnect, not "server down")."""
    yield body.encode()
    raise httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)")


def _flatten_blocks(view: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in view.get("blocks", []) if isinstance(view, dict) else []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, dict) and isinstance(text.get("text"), str):
            parts.append(text["text"])
        elif isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


# ── Omnigent API contract the Slack client depends on ─────────────────────────
#
# The single source of truth for which server endpoints the bot calls, and
# which of those are part of the server's PUBLIC (schema-documented) surface.
# ``test_integration.py``'s drift test reconciles this catalog against the
# committed ``openapi.json`` so a server-side rename/removal of a
# ``documented=True`` endpoint fails a Slack test — surfacing the break here
# rather than silently at runtime against a deployed server.
#
# Each entry: (method, path_template, documented). ``documented=False`` marks
# an endpoint the server intentionally hides from its OpenAPI schema
# (``include_in_schema=False`` — the ``/oauth/device/*`` login routes and the
# internal ``/events`` + elicitation-resolve routes); the drift test asserts
# those are ABSENT from the schema so a future decision to publish one is a
# deliberate, noticed change.
#
# Keep in sync with ``OmnigentClient`` (integrations/slack/src/omnigent_slack/
# omnigent.py) and the login flow (oauth.py / auth_manager.py).
OMNIGENT_ENDPOINTS: list[tuple[str, str, bool]] = [
    # Setup / validation.
    ("GET", "/health", True),
    ("GET", "/v1/me", True),
    ("GET", "/v1/agents", True),
    ("GET", "/v1/hosts", True),
    ("GET", "/v1/hosts/{host_id}/filesystem", True),
    # Session lifecycle.
    ("POST", "/v1/sessions", True),
    ("GET", "/v1/sessions/{session_id}", True),
    ("GET", "/v1/sessions/{session_id}/items", True),
    ("GET", "/v1/sessions/{session_id}/stream", True),
    ("POST", "/v1/hosts/{host_id}/runners", True),
    ("GET", "/v1/runners/{runner_id}/status", True),
    # Internal (hidden from the public schema).
    ("POST", "/v1/sessions/{session_id}/events", False),
    ("POST", "/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve", False),
    # Device-grant login (oauth.py, accounts mode): authorize starts the grant;
    # /oauth/token both polls for the device-code token AND refreshes;
    # /oauth/revoke logs out.
    ("POST", "/oauth/device/authorize", False),
    ("POST", "/oauth/token", False),
    ("POST", "/oauth/revoke", False),
    # OIDC ticket login (oauth.py, oidc mode): start a CLI-login ticket, then poll.
    ("POST", "/auth/cli-login", False),
    ("GET", "/auth/cli-poll", False),
]

# Response fields the client actually reads off the two richest documented
# schemas. If the server renames one of these, the client silently degrades
# (a None harness, an empty agent list), so the drift test pins them.
OMNIGENT_RESPONSE_FIELDS: dict[str, tuple[str, ...]] = {
    # GET /v1/sessions/{session_id} → SessionResponse (get_session_info).
    "SessionResponse": ("harness", "agent_name"),
    # GET /v1/agents → PaginatedList (list_agents reads .data).
    "PaginatedList": ("data",),
}


def sse_status(status: str, response_id: str | None = None) -> str:
    """One ``session.status`` SSE frame (id-bearing when ``response_id`` given)."""
    payload: dict[str, Any] = {"type": "session.status", "status": status}
    if response_id is not None:
        payload["response_id"] = response_id
    return f"data: {json.dumps(payload)}\n\n"


def sse_delta(text: str, message_id: str | None = None) -> str:
    """One ``response.output_text.delta`` SSE frame."""
    payload: dict[str, Any] = {"type": "response.output_text.delta", "delta": text}
    if message_id is not None:
        payload["message_id"] = message_id
    return f"data: {json.dumps(payload)}\n\n"


# The bot's SSE turn-end shape reused across streaming scenarios: a running
# edge, one answer delta, then the id-bearing idle that ends the turn.
DEFAULT_SSE_BODY = (
    sse_status("running", "resp_1")
    + sse_delta("Here is the answer.", "m1")
    + 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    + sse_status("idle", "resp_1")
)


class FakeOmnigentServer:
    """A ``respx`` router standing in for the Omnigent HTTP API.

    Install it inside a ``respx.mock`` block with :meth:`install`. Tests set
    scenario knobs (``auth_required``, ``agents``, ``hosts``, ``sse_body`` …);
    the router owns the endpoint contract and records every request for
    assertions.
    """

    def __init__(self, base_url: str = "http://omnigent.test") -> None:
        self.base_url = base_url.rstrip("/")
        # Scenario knobs.
        self.auth_required = False  # auth-gated endpoints 401 when True
        self.agents: list[dict[str, Any]] = [{"id": "ag_1", "name": "debby"}]
        self.hosts: list[dict[str, Any]] = [
            {"host_id": "h1", "name": "Host One", "status": "online"}
        ]
        self.session_id = "conv_1"
        self.runner_id = "runner_1"
        self.harness = "claude-native"
        self.agent_name = "debby"
        self.sse_body = DEFAULT_SSE_BODY
        # Login (device grant) knobs — used when auth_required drives a login.
        self.user_code = "ABCD-2345"
        self.verification_url = self.base_url + "/oauth/device?user_code=ABCD-2345"
        # Assistant items returned by the no-delta fallback probe.
        self.latest_items: list[dict[str, Any]] = []
        # ── complex-scenario knobs ───────────────────────────────────────
        # First message-submit (POST /events) returns 503 runner_unavailable,
        # then succeeds — models a session whose bound runner died: run_turn
        # catches it, launches a fresh runner, and retries the turn once.
        self.first_submit_runner_unavailable = False
        # Launch responds with this status (404/409 → host-unavailable) instead
        # of 200. None means the normal success path.
        self.launch_status: int | None = None
        # Every request to a session path (submit/stream/…) responds 412
        # harness_not_configured with this curated message when set.
        self.harness_not_configured_message: str | None = None
        # A list of SSE bodies served on successive /stream connections: the
        # first drops mid-tail (StreamInterruptedError), the client reconnects
        # and gets the next. Overrides ``sse_body`` when non-empty.
        self.stream_legs: list[str] = []
        self._launch_calls = 0
        self._stream_calls = 0
        # Recorded requests: each is (method, path, headers, json|None).
        self.requests: list[tuple[str, str, httpx.Headers, Any]] = []

    # ── request recording ────────────────────────────────────────────────
    def _record(self, request: httpx.Request) -> None:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content
        self.requests.append((request.method, request.url.path, request.headers, body))

    # ── route side-effects ───────────────────────────────────────────────
    def _auth_wall_or(self, payload: dict[str, Any]):
        def _handler(request: httpx.Request) -> httpx.Response:
            self._record(request)
            if self.auth_required:
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
            return httpx.Response(200, json=payload)

        return _handler

    def install(self, respx_mock: respx.MockRouter) -> FakeOmnigentServer:
        b = self.base_url

        # Health is always reachable (setup probes it before the auth-gated list).
        def _health(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(200, json={"status": "ok"})

        respx_mock.get(b + "/health").mock(side_effect=_health)

        # Auth-gated listing endpoints.
        respx_mock.get(b + "/v1/agents").mock(
            side_effect=self._auth_wall_or({"data": self.agents})
        )
        respx_mock.get(b + "/v1/hosts").mock(side_effect=self._auth_wall_or({"hosts": self.hosts}))

        # Login-mode probe + device grant (only hit when a login starts).
        def _me(request: httpx.Request) -> httpx.Response:
            self._record(request)
            # 401 with a login_url that isn't /auth/login → accounts (device) mode.
            return httpx.Response(401, json={"login_url": "/login"})

        respx_mock.get(b + "/v1/me").mock(side_effect=_me)

        def _device_authorize(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": self.user_code,
                    "verification_uri": b + "/oauth/device",
                    "verification_uri_complete": self.verification_url,
                    "expires_in": 600,
                    "interval": 0,
                },
            )

        respx_mock.post(b + "/oauth/device/authorize").mock(side_effect=_device_authorize)

        # Host filesystem (workspace default) — parent of any absolute path.
        def _filesystem(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(
                200,
                json={"data": [{"name": ".bashrc", "path": "/home/bot/.bashrc", "type": "file"}]},
            )

        respx_mock.get(url__regex=rf"{b}/v1/hosts/[^/]+/filesystem").mock(side_effect=_filesystem)

        # Session lifecycle.
        def _create_session(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(201, json={"id": self.session_id})

        respx_mock.post(b + "/v1/sessions").mock(side_effect=_create_session)

        def _launch_runner(request: httpx.Request) -> httpx.Response:
            self._record(request)
            self._launch_calls += 1
            if self.launch_status in (404, 409):
                return httpx.Response(self.launch_status, json={"error": {"code": "host_offline"}})
            return httpx.Response(200, json={"runner_id": self.runner_id})

        respx_mock.post(url__regex=rf"{b}/v1/hosts/[^/]+/runners").mock(side_effect=_launch_runner)

        def _runner_status(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(200, json={"runner_id": self.runner_id, "online": True})

        respx_mock.get(url__regex=rf"{b}/v1/runners/[^/]+/status").mock(side_effect=_runner_status)

        self._submit_calls = 0

        def _submit(request: httpx.Request) -> httpx.Response:
            self._record(request)
            self._submit_calls += 1
            if self.harness_not_configured_message is not None:
                return httpx.Response(
                    412,
                    json={
                        "error": {
                            "code": "harness_not_configured",
                            "message": self.harness_not_configured_message,
                        }
                    },
                )
            if self.first_submit_runner_unavailable and self._submit_calls == 1:
                return httpx.Response(503, json={"error": {"code": "runner_unavailable"}})
            return httpx.Response(202, json={})

        respx_mock.post(url__regex=rf"{b}/v1/sessions/[^/]+/events").mock(side_effect=_submit)

        # Session snapshot. Serves the config-summary fields (harness/agent) and
        # the rolled-up ``status`` that gates two decisions:
        #   • the route-time busy check (must read idle so a follow-up runs); and
        #   • the mid-stream-drop reconnect check (must read running so the client
        #     reconnects rather than treating the drop as a finished turn).
        # These happen at different times, so the status is idle UNTIL the turn's
        # stream has opened, then running — matching a real session's lifecycle.
        def _snapshot(request: httpx.Request) -> httpx.Response:
            self._record(request)
            status = "running" if self._stream_calls > 0 else "idle"
            return httpx.Response(
                200,
                json={
                    "harness": self.harness,
                    "agent_name": self.agent_name,
                    "status": status,
                },
            )

        respx_mock.get(url__regex=rf"{b}/v1/sessions/[^/]+$").mock(side_effect=_snapshot)

        # Newest-assistant-message probe (no-delta fallback). The service reads
        # this BEFORE the turn (baseline) and AFTER (fallback), and only recovers a
        # message that differs from the baseline. So ``latest_items`` is treated as
        # produced BY the turn: empty until the stream has run, then present — a
        # blind pre-turn baseline would otherwise resurrect a prior answer.
        def _items(request: httpx.Request) -> httpx.Response:
            self._record(request)
            items = self.latest_items if self._stream_calls > 0 else []
            return httpx.Response(200, json={"data": items})

        respx_mock.get(url__regex=rf"{b}/v1/sessions/[^/]+/items").mock(side_effect=_items)

        # SSE stream. When auth_required, the tail 401s so a mid-turn auth wall
        # surfaces as AuthRequiredError (the re-login path). ``stream_legs`` (when
        # set) serves one body per successive connection so a leg can drop
        # mid-tail (a "…" sentinel raises a RemoteProtocolError) and the client
        # reconnects to the next leg.
        def _stream(request: httpx.Request) -> httpx.Response:
            self._record(request)
            if self.auth_required:
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
            if self.stream_legs:
                leg = self.stream_legs[min(self._stream_calls, len(self.stream_legs) - 1)]
                self._stream_calls += 1
                if leg.endswith("<DROP>"):
                    return httpx.Response(200, stream=_dropping_stream(leg[: -len("<DROP>")]))
                return httpx.Response(200, text=leg)
            self._stream_calls += 1
            return httpx.Response(200, text=self.sse_body)

        respx_mock.get(url__regex=rf"{b}/v1/sessions/[^/]+/stream").mock(side_effect=_stream)

        return self

    # ── assertion helpers ────────────────────────────────────────────────
    def paths(self, method: str | None = None) -> list[str]:
        """Recorded request paths, optionally filtered by HTTP method."""
        return [p for m, p, _h, _b in self.requests if method is None or m == method]

    def find(self, method: str, path: str) -> tuple[str, str, httpx.Headers, Any] | None:
        """The first recorded request matching ``method`` and exact ``path``."""
        for entry in self.requests:
            if entry[0] == method and entry[1] == path:
                return entry
        return None

    def assert_request(
        self, method: str, path: str, *, json_contains: dict[str, Any] | None = None
    ) -> tuple[str, str, httpx.Headers, Any]:
        """Assert a request was made, optionally with a superset JSON body."""
        entry = self.find(method, path)
        assert entry is not None, f"expected {method} {path}; saw {self.requests}"
        if json_contains is not None:
            body = entry[3]
            assert isinstance(body, dict), f"{method} {path} body not JSON: {body!r}"
            for key, value in json_contains.items():
                assert body.get(key) == value, (
                    f"{method} {path} body[{key!r}]={body.get(key)!r} != {value!r}"
                )
        return entry

    def assert_bearer(self, method: str, path: str, token: str | None = None) -> None:
        """Assert the request carried an Authorization: Bearer header."""
        entry = self.find(method, path)
        assert entry is not None, f"expected {method} {path}; saw {self.paths()}"
        auth = entry[2].get("authorization")
        assert auth is not None and auth.startswith("Bearer "), (
            f"{method} {path} missing bearer; headers={dict(entry[2])}"
        )
        if token is not None:
            assert auth == f"Bearer {token}", f"{method} {path} bearer={auth!r} != Bearer {token}"
