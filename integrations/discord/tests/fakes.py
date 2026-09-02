"""Shared fakes for the Discord integration tests.

Three halves:

- **Discord stand-ins** (:class:`RecordingChannel`, :class:`FakeMessage`,
  :class:`IncomingMessage`) that record every outbound call — sends, edits,
  deletes, thread creation — so a test can assert exactly what the bot showed
  the user. discord.py itself is bypassed: tests call ``service.handle_message``
  / ``setup.run_config`` directly.
- :class:`FakeOmnigent` — an in-memory Omnigent client for the fast unit tests,
  scripted with a list of SSE-shaped events.
- :class:`FakeOmnigentServer` — a ``respx`` router standing in for the Omnigent
  HTTP API, so the integration tests drive a REAL ``OmnigentClient`` (real
  ``httpx``) and assert both sides of the seam.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import respx
from omnigent_bot_core.events import SessionActivity, SessionInfo

# The placeholder text the bot streams its reply into. Kept in sync with the
# service module so a recorded placeholder is recognizable.
try:  # pragma: no cover - import shape only
    from omnigent_discord.service import _ACK_TEXT
except Exception:  # pragma: no cover
    _ACK_TEXT = "_Working on it…_"


class FakeUser:
    def __init__(self, user_id: str, *, bot: bool = False, name: str = "someone") -> None:
        self.id = int(user_id)
        self.bot = bot
        self.name = name


class FakeMessage:
    """A message the bot sent, recording every later edit and its deletion."""

    _next_id = 1

    def __init__(
        self,
        channel: RecordingChannel,
        content: str | None,
        embed: Any = None,
        view: Any = None,
        delete_after: float | None = None,
    ) -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.content = content
        self.embed = embed
        self.view = view
        self.delete_after = delete_after
        self.deleted = False
        # Every content value this message has shown, oldest first.
        self.history: list[str | None] = [content]
        self.edits = 0

    async def edit(self, **kwargs: Any) -> FakeMessage:
        if self.deleted:
            raise RuntimeError("edited a deleted message")
        self.edits += 1
        if "content" in kwargs:
            self.content = kwargs["content"]
            self.history.append(self.content)
        if "embed" in kwargs:
            self.embed = kwargs["embed"]
        if "view" in kwargs:
            self.view = kwargs["view"]
        return self

    async def delete(self) -> None:
        self.deleted = True
        self.channel.deleted.append(self)


class RecordingChannel:
    """A ``MessageableProtocol`` stand-in that records what the bot sent."""

    def __init__(self, channel_id: str = "500", parent_id: str | None = None) -> None:
        self.id = int(channel_id)
        # Present (non-None) on a thread; absent on a plain channel or DM.
        self.parent_id = int(parent_id) if parent_id is not None else None
        self.sent: list[FakeMessage] = []
        self.deleted: list[FakeMessage] = []
        # Set to raise on the next send, to exercise the failure paths.
        self.fail_next_send = False

    async def send(
        self,
        content: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
        delete_after: float | None = None,
        allowed_mentions: Any = None,
        **_kwargs: Any,
    ) -> FakeMessage:
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("send failed")
        message = FakeMessage(self, content, embed, view, delete_after)
        # Recorded so a test can assert the one deliberate ping is scoped and
        # that nothing else is allowed to mention anyone.
        message.allowed_mentions = allowed_mentions
        self.sent.append(message)
        return message

    # ── assertion helpers ────────────────────────────────────────────────
    @property
    def live(self) -> list[FakeMessage]:
        """Messages still on screen (not deleted)."""
        return [m for m in self.sent if not m.deleted]

    @property
    def texts(self) -> list[str]:
        """Final content of every message still on screen."""
        return [m.content for m in self.live if m.content is not None]

    @property
    def answer(self) -> str:
        """The streamed answer: content of every non-placeholder text message."""
        return "".join(
            m.content
            for m in self.live
            if m.content is not None and m.content != _ACK_TEXT and m.embed is None
        )

    def containing(self, needle: str) -> list[FakeMessage]:
        return [m for m in self.sent if m.content and needle in m.content]


class FakeThread(RecordingChannel):
    """A thread the bot created, with the starter message that spawned it."""

    def __init__(self, channel_id: str, parent_id: str, starter: Any | None = None) -> None:
        super().__init__(channel_id, parent_id=parent_id)
        self.starter_message = starter
        # Discord puts the thread creator on the gateway payload, so ownership
        # is answerable without an API call.
        author = getattr(starter, "author", None)
        self.owner_id: int | None = getattr(author, "id", None)


class IncomingMessage:
    """A gateway message the bot receives."""

    _next_id = 1000

    def __init__(
        self,
        *,
        content: str,
        author: FakeUser,
        channel: RecordingChannel,
        guild: Any = None,
        mentions: list[FakeUser] | None = None,
        role_mentions: list[FakeRole] | None = None,
        message_id: str | None = None,
    ) -> None:
        if message_id is None:
            IncomingMessage._next_id += 1
            message_id = str(IncomingMessage._next_id)
        self.id = int(message_id)
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []
        self.jump_url = f"https://discord.com/channels/x/{channel.id}/{self.id}"
        # The thread ``create_thread`` will return, and whether it should fail.
        self.thread: FakeThread | None = None
        self.thread_error: Exception | None = None

    async def create_thread(self, *, name: str, **_kwargs: Any) -> FakeThread:
        if self.thread_error is not None:
            raise self.thread_error
        self.thread = FakeThread(str(self.id + 1), parent_id=str(self.channel.id), starter=self)
        self.thread.name = name  # type: ignore[attr-defined]
        return self.thread


class FakeRole:
    """A guild role. ``managed`` + ``tags.bot_id`` mark a bot's own integration
    role — the one Discord auto-creates and offers in autocomplete."""

    def __init__(self, role_id: str, *, bot_id: str | None = None) -> None:
        self.id = int(role_id)
        self.managed = bot_id is not None
        self.tags = type("RoleTags", (), {"bot_id": int(bot_id)})() if bot_id else None


class FakeGuild:
    def __init__(self, guild_id: str = "900", name: str = "Acme Guild") -> None:
        self.id = int(guild_id)
        self.name = name


# ── Omnigent client stand-ins ─────────────────────────────────────────────


class FakeOmnigent:
    """An in-memory ``OmnigentClient`` scripted with a list of stream events."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.session_id = "conv_1"
        self.activity = SessionActivity(status="idle", pending_elicitation=False)
        self.info = SessionInfo(harness="claude-native", agent_name="debby")
        # What ``latest_assistant_message`` reports before any turn has
        # streamed (a PRIOR turn's answer — the baseline the service compares
        # against), and after one has (an answer THIS turn committed without
        # streaming). Keeping them separate is what lets a test tell the
        # no-delta recovery apart from resurrecting an old reply.
        self.latest: tuple[str | None, str] | None = None
        self.latest_after_turn: tuple[str | None, str] | None = None
        self._turns = 0
        self.created: list[tuple[str, str]] = []
        self.launched: list[dict[str, Any]] = []
        self.submitted: list[str] = []
        self.resolved: list[dict[str, Any]] = []
        # Raise this instead of streaming, to exercise the error paths.
        self.turn_error: Exception | None = None
        self.create_error: Exception | None = None
        self.resolve_error: Exception | None = None
        # Awaited between yielded events, so a test can interleave assertions.
        self.on_event: Any = None
        # Set the moment a turn begins streaming, so a test can wait for the
        # channel reservation to be held rather than racing the scheduler.
        self.turn_started = asyncio.Event()

    async def create_session(self, agent_id: str, title: str) -> str:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((agent_id, title))
        return self.session_id

    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        self.launched.append(
            {"session_id": session_id, "workspace": workspace, "host_id": host_id}
        )
        return "runner_1"

    async def get_session_activity(self, session_id: str) -> SessionActivity:
        return self.activity

    async def get_session_info(self, session_id: str) -> SessionInfo:
        return self.info

    async def latest_assistant_message(self, session_id: str) -> tuple[str | None, str] | None:
        if self._turns > 0 and self.latest_after_turn is not None:
            return self.latest_after_turn
        return self.latest

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        if self.resolve_error is not None:
            raise self.resolve_error
        self.resolved.append(
            {
                "session_id": session_id,
                "elicitation_id": elicitation_id,
                "accepted": accepted,
                "content": content,
            }
        )

    async def run_turn(self, session_id: str, text: str, **_kwargs: Any) -> Any:
        self.submitted.append(text)
        self._turns += 1
        self.turn_started.set()
        if self.turn_error is not None:
            raise self.turn_error
        for event in self.events:
            yield event
            if self.on_event is not None:
                await self.on_event(event)


class FakePool:
    """A client pool that hands out one :class:`FakeOmnigent` for everyone."""

    def __init__(self, client: FakeOmnigent) -> None:
        self.client = client
        self.invalidated: list[tuple[str, str]] = []

    async def get(self, server_url: str, user_id: str = "") -> FakeOmnigent:
        return self.client

    async def invalidate(self, server_url: str, user_id: str) -> None:
        self.invalidated.append((server_url, user_id))

    async def invalidate_user(self, user_id: str) -> None:
        self.invalidated.append(("*", user_id))


# ── SSE frame builders ────────────────────────────────────────────────────


def status_event(status: str, response_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "session.status", "status": status}
    if response_id is not None:
        payload["response_id"] = response_id
    return payload


def delta_event(text: str, message_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "response.output_text.delta", "delta": text}
    if message_id is not None:
        payload["message_id"] = message_id
    return payload


def elicitation_event(elicitation_id: str = "el_1", **params: Any) -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"message": "Approve this action?", **params},
    }


def resolved_event(elicitation_id: str = "el_1") -> dict[str, Any]:
    return {"type": "response.elicitation_resolved", "elicitation_id": elicitation_id}


def sse_status(status: str, response_id: str | None = None) -> str:
    """One ``session.status`` SSE frame (id-bearing when ``response_id`` given)."""
    return f"data: {json.dumps(status_event(status, response_id))}\n\n"


def sse_delta(text: str, message_id: str | None = None) -> str:
    """One ``response.output_text.delta`` SSE frame."""
    return f"data: {json.dumps(delta_event(text, message_id))}\n\n"


# The bot's SSE turn-end shape reused across streaming scenarios: a running
# edge, one answer delta, then the id-bearing idle that ends the turn.
DEFAULT_SSE_BODY = (
    sse_status("running", "resp_1")
    + sse_delta("Here is the answer.", "m1")
    + 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    + sse_status("idle", "resp_1")
)


async def _dropping_stream(body: str):
    """Yield ``body`` then raise a mid-stream drop the client treats as a proxy
    severing a long-lived response (reconnect, not "server down")."""
    yield body.encode()
    raise httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)")


# ── Omnigent API contract the Discord client depends on ───────────────────
#
# The single source of truth for which server endpoints the bot calls, and which
# of those are part of the server's PUBLIC (schema-documented) surface.
# ``test_integration.py``'s drift test reconciles this catalog against the
# committed ``openapi.json`` so a server-side rename/removal of a
# ``documented=True`` endpoint fails a Discord test — surfacing the break here
# rather than silently at runtime against a deployed server.
#
# Each entry: (method, path_template, documented). ``documented=False`` marks an
# endpoint the server intentionally hides from its OpenAPI schema
# (``include_in_schema=False`` — the ``/oauth/device/*`` login routes and the
# internal ``/events`` + elicitation-resolve routes); the drift test asserts
# those are ABSENT from the schema so a future decision to publish one is a
# deliberate, noticed change.
#
# Keep in sync with ``OmnigentClient`` (integrations/discord/src/omnigent_discord/
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
# schemas. If the server renames one of these, the client silently degrades (a
# None harness, an empty agent list), so the drift test pins them.
OMNIGENT_RESPONSE_FIELDS: dict[str, tuple[str, ...]] = {
    # GET /v1/sessions/{session_id} → SessionResponse (get_session_info).
    "SessionResponse": ("harness", "agent_name"),
    # GET /v1/agents → PaginatedList (list_agents reads .data).
    "PaginatedList": ("data",),
}


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
        # First message-submit (POST /events) returns 503 runner_unavailable,
        # then succeeds — models a session whose bound runner died.
        self.first_submit_runner_unavailable = False
        # Launch responds with this status (404/409 → host-unavailable).
        self.launch_status: int | None = None
        # Every request to a session path responds 412 harness_not_configured
        # with this curated message when set.
        self.harness_not_configured_message: str | None = None
        # SSE bodies served on successive /stream connections: a body ending in
        # "<DROP>" drops mid-tail so the client reconnects to the next.
        self.stream_legs: list[str] = []
        self._launch_calls = 0
        self._stream_calls = 0
        self._submit_calls = 0
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

        respx_mock.get(b + "/v1/agents").mock(
            side_effect=self._auth_wall_or({"data": self.agents})
        )
        respx_mock.get(b + "/v1/hosts").mock(side_effect=self._auth_wall_or({"hosts": self.hosts}))

        def _me(request: httpx.Request) -> httpx.Response:
            self._record(request)
            # 401 with login_url "/login" → accounts (device grant) mode.
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

        def _filesystem(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(
                200,
                json={"data": [{"name": ".bashrc", "path": "/home/bot/.bashrc", "type": "file"}]},
            )

        respx_mock.get(url__regex=rf"{b}/v1/hosts/[^/]+/filesystem").mock(side_effect=_filesystem)

        def _create_session(request: httpx.Request) -> httpx.Response:
            self._record(request)
            if self.auth_required:
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
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

        def _resolve(request: httpx.Request) -> httpx.Response:
            self._record(request)
            return httpx.Response(202, json={})

        respx_mock.post(url__regex=rf"{b}/v1/sessions/[^/]+/elicitations/[^/]+/resolve").mock(
            side_effect=_resolve
        )

        # Session snapshot. Serves the config-summary fields (harness/agent) and
        # the rolled-up ``status``, which gates two different decisions: the
        # route-time busy check (must read idle so a follow-up message runs) and
        # the mid-stream-drop reconnect check (must read running so the client
        # reconnects rather than treating the drop as a finished turn). Only the
        # ``stream_legs`` scenario exercises the latter, so the session reads
        # busy exactly there and idle everywhere else.
        def _snapshot(request: httpx.Request) -> httpx.Response:
            self._record(request)
            status = "running" if self.stream_legs and self._stream_calls > 0 else "idle"
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
        # this BEFORE the turn (baseline) and AFTER (fallback), recovering only a
        # message that differs from the baseline — so ``latest_items`` is treated
        # as produced BY the turn: empty until the stream has run, then present.
        def _items(request: httpx.Request) -> httpx.Response:
            self._record(request)
            items = self.latest_items if self._stream_calls > 0 else []
            return httpx.Response(200, json={"data": items})

        respx_mock.get(url__regex=rf"{b}/v1/sessions/[^/]+/items").mock(side_effect=_items)

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
        assert entry is not None, f"expected {method} {path}; saw {self.paths()}"
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
