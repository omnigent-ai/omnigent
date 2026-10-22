"""Subscription tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.harnesses.codex_native.bridge import (
    read_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _elicitation_tracker,
    _expected_status_data,
    _FakeCodexAppServerClient,
    _recording_forwarder_client,
    _usage_coalescer,
    _write_forwarder_bridge,
)


class _FakeCodexWebSocket:
    """
    Minimal websocket for Codex app-server handshake tests.

    It immediately responds to the ``initialize`` request and records
    every outbound payload.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.responses: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        """
        Capture an outbound websocket text frame.

        :param payload: JSON-RPC text frame.
        :returns: None.
        """
        self.sent.append(payload)
        message = json.loads(payload)
        if message.get("method") == "initialize":
            await self.responses.put(json.dumps({"id": message["id"], "result": {}}))

    def __aiter__(self) -> _FakeCodexWebSocket:
        """
        Return the async iterator used by the client reader task.

        :returns: This websocket.
        """
        return self

    async def __anext__(self) -> str:
        """
        Yield the next queued inbound websocket text frame.

        :returns: JSON-RPC text frame.
        """
        return await self.responses.get()

    async def close(self) -> None:
        """
        Mark the fake websocket closed.

        :returns: None.
        """
        self.closed = True


def test_codex_app_server_client_uses_codex_remote_handshake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The Python client matches Codex's Unix-socket websocket transport
    and completes the initialize/initialized handshake.
    """
    fake_websocket = _FakeCodexWebSocket()
    captured_kwargs: dict[str, Any] = {}

    async def fake_unix_connect(**kwargs: Any) -> _FakeCodexWebSocket:
        """
        Capture the websocket connection arguments.

        :param kwargs: ``websockets.unix_connect`` keyword arguments.
        :returns: Fake websocket.
        """
        captured_kwargs.update(kwargs)
        return fake_websocket

    monkeypatch.setattr(
        codex_native_app_server.websockets,
        "unix_connect",
        fake_unix_connect,
    )

    async def run() -> None:
        """
        Open and close one Codex app-server client.

        :returns: None.
        """
        client = codex_native_app_server.CodexAppServerClient(
            tmp_path / "app-server.sock",
            client_name="test-client",
        )
        await client.connect()
        await client.close()

    asyncio.run(run())

    assert captured_kwargs == {
        "path": str(tmp_path / "app-server.sock"),
        "uri": "ws://localhost/rpc",
        "max_size": 128 << 20,
        "compression": None,
    }
    assert [json.loads(payload) for payload in fake_websocket.sent] == [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "test-client", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
    ]
    assert fake_websocket.closed


def test_codex_app_server_client_responds_to_server_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The Codex websocket client can answer server-to-client requests.

    Native Codex elicitations arrive as JSON-RPC requests from the
    app-server to the Omnigent client. After AP/web resolves the
    prompt, the forwarder must send a result envelope with the same
    request id; otherwise Codex never observes the answer.
    """
    fake_websocket = _FakeCodexWebSocket()

    async def fake_unix_connect(**_kwargs: Any) -> _FakeCodexWebSocket:
        """
        Return the fake websocket.

        :returns: Fake websocket.
        """
        return fake_websocket

    monkeypatch.setattr(
        codex_native_app_server.websockets,
        "unix_connect",
        fake_unix_connect,
    )

    async def run() -> None:
        """
        Connect, send one server-request response, and close.

        :returns: None.
        """
        client = codex_native_app_server.CodexAppServerClient(
            tmp_path / "app-server.sock",
            client_name="test-client",
        )
        await client.connect()
        await client.respond("req_7", {"answers": {"framework": {"answers": ["React"]}}})
        await client.close()

    asyncio.run(run())

    assert json.loads(fake_websocket.sent[-1]) == {
        "id": "req_7",
        "result": {"answers": {"framework": {"answers": ["React"]}}},
    }
    assert fake_websocket.closed


def test_thread_id_from_started_event_ignores_unrelated_events() -> None:
    """
    Thread discovery only accepts well-formed Codex ``thread/started``
    notifications.
    """
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "remoteControl/status/changed"}
        )
        is None
    )
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "thread/started", "params": {"thread": {}}}
        )
        is None
    )
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "thread/started", "params": {"thread": {"id": "thread_123"}}}
        )
        == "thread_123"
    )


def test_wait_for_thread_started_uses_tui_created_thread() -> None:
    """
    Fresh Codex sessions let the TUI create the remote app-server
    thread, then discover that id from the broadcast ``thread/started``
    notification.
    """
    fake_client = _FakeCodexAppServerClient(
        events=[
            {"method": "remoteControl/status/changed", "params": {"status": "disabled"}},
            {"method": "thread/started", "params": {"thread": {"id": "thread_123"}}},
        ]
    )

    thread_id = asyncio.run(codex_native._wait_for_thread_started(fake_client))  # type: ignore[arg-type]

    assert thread_id == "thread_123"
    assert fake_client.requests == []


def test_wait_for_thread_started_fails_when_stream_ends() -> None:
    """
    A Codex TUI that exits before creating a thread fails loudly instead
    of leaving the Omnigent session without a bridge state.
    """
    fake_client = _FakeCodexAppServerClient(events=[])

    with pytest.raises(click.ClickException, match="event stream ended"):
        asyncio.run(codex_native._wait_for_thread_started(fake_client))  # type: ignore[arg-type]


def test_wait_for_thread_started_times_out_when_no_thread_event() -> None:
    """
    A Codex TUI that connects but never emits ``thread/started`` makes
    discovery time out rather than hang forever.

    The host-spawned runner runs ``wait_for_thread_started`` in a background
    task; without the timeout a TUI that starts its remote connection but
    never creates a thread would wedge that task (and leak the listener)
    indefinitely. A regression removing the ``asyncio.timeout`` guard would
    hang this test instead of raising.
    """

    class _NeverEmitsClient:
        """App-server client whose event stream blocks without ever yielding."""

        async def iter_events(self) -> Any:
            await asyncio.sleep(3600)
            yield {}  # pragma: no cover - unreachable; the await blocks first

    with pytest.raises(TimeoutError):
        asyncio.run(
            codex_native_forwarder.wait_for_thread_started(
                _NeverEmitsClient(),  # type: ignore[arg-type]
                timeout=0.05,
            )
        )


def test_supervise_forwarder_subscribes_without_replaying_dead_letters(
    tmp_path: Path,
) -> None:
    """
    Fresh Codex sessions pass the listener used to discover
    ``thread/started``. Once the thread id is known, the forwarder
    subscribes that connection so TUI-originated turn/item events are
    mirrored into the web session. Dead-letter recovery belongs to cold-resume
    rollout reconstruction, so starting the live forwarder must not replay it
    again after that snapshot has already been built.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_native_forwarder.append_dead_letter(
        tmp_path,
        session_id="conv_123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="http 503",
        http_status=503,
    )
    ap_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ap_requests.append(request)
        return httpx.Response(200)

    async def run() -> None:
        """
        Run the forwarder against an empty event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            client=fake_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )

    asyncio.run(run())

    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    ]
    assert fake_client.closed
    assert ap_requests == []
    assert (tmp_path / "dead_letter.jsonl").exists()


def test_supervise_forwarder_resumes_when_it_opens_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume paths open a new app-server client and subscribe via
    ``thread/resume``.
    """
    fake_client = _FakeCodexAppServerClient()

    def fake_client_factory(*_args: Any, **_kwargs: Any) -> _FakeCodexAppServerClient:
        """
        Return the fake app-server client.

        :returns: Fake client.
        """
        return fake_client

    # Patch at the source: the forwarder builds its fallback client via
    # client_for_transport, which constructs the app_server module's class.
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient", fake_client_factory
    )

    async def run() -> None:
        """
        Run the forwarder against an empty event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
        )

    asyncio.run(run())

    assert fake_client.connected
    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    ]
    assert fake_client.closed


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Rollout file missing — the first transient fresh-thread state.
        ("{'code': -32600, 'message': 'no rollout found for thread id thread_123'}", True),
        # Rollout file present but EMPTY — the second transient state the
        # fresh host-spawned TUI exposes. Previously treated as fatal, which
        # made the forwarder give up subscribing and stop syncing chat.
        (
            "{'code': -32603, 'message': 'failed to read thread: thread-store "
            "internal error: failed to read thread /x/rollout.jsonl: rollout at "
            "/x/rollout.jsonl is empty'}",
            True,
        ),
        # A real app-server error must stay fatal (not retried forever).
        ("{'code': -32000, 'message': 'permission denied'}", False),
    ],
)
def test_is_thread_not_ready_error_matches_no_rollout_and_empty_rollout(
    message: str, expected: bool
) -> None:
    """
    ``_is_thread_not_ready_error`` treats BOTH fresh-thread not-ready states
    (missing rollout, present-but-empty rollout) as retryable, and leaves
    unrelated errors fatal. If the empty-rollout case regressed to fatal, the
    host-spawned forwarder would give up subscribing and chat would not sync.
    """
    assert codex_native_forwarder._is_thread_not_ready_error(RuntimeError(message)) is expected


def test_subscribe_until_ready_retries_no_rollout_and_replays_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh TUI threads can reject ``thread/resume`` until the first
    rollout exists; the forwarder retries and backfills resume items.
    """
    fake_client = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "turns": [
                        {
                            "id": "turn_123",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "item_user",
                                    "content": [{"type": "text", "text": "first"}],
                                },
                                {
                                    "type": "agentMessage",
                                    "id": "item_agent",
                                    "text": "reply",
                                },
                            ],
                        }
                    ]
                }
            }
        },
        error=RuntimeError(
            "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
        ),
    )
    calls = 0

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        fake_client.requests.append((method, params))
        calls += 1
        if calls == 1:
            raise RuntimeError(
                "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
            )
        return fake_client.response

    async def fake_sleep(_delay: float) -> None:
        return None

    posted: list[dict[str, Any]] = []

    monkeypatch.setattr(fake_client, "request", fake_request)
    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                thread_id="thread_123",
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
            )

    asyncio.run(run())

    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True}),
        ("thread/resume", {"threadId": "thread_123"}),
    ]
    assert [payload["data"]["item_data"]["role"] for payload in posted] == [
        "user",
        "assistant",
    ]


def test_subscribe_until_ready_replays_completed_turn_status(
    tmp_path: Path,
) -> None:
    """
    Resume replay closes a completed turn when live terminal events were missed.

    Host-spawned codex-native suppresses the runner's injection-task ``idle``
    edge, so a reconnect that misses both ``turn/started`` and
    ``turn/completed`` must recover the terminal status from explicit resume
    turn state instead of leaving the Omnigent session running forever.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "id": "thread_123",
                    "turns": [
                        {
                            "id": "turn_122",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "item_old_user",
                                    "content": [{"type": "text", "text": "already synced"}],
                                },
                                {
                                    "type": "agentMessage",
                                    "id": "item_old_agent",
                                    "text": "already synced reply",
                                },
                            ],
                        },
                        {
                            "id": "turn_123",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "item_user",
                                    "content": [{"type": "text", "text": "first"}],
                                },
                                {
                                    "type": "agentMessage",
                                    "id": "item_agent",
                                    "text": "reply",
                                },
                            ],
                        },
                    ],
                }
            }
        }
    )
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Subscribe and replay a completed turn.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                thread_id="thread_123",
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
            )

    asyncio.run(run())

    assert fake_client.requests == [("thread/resume", {"threadId": "thread_123"})]
    assert [payload["type"] for payload in posted] == [
        "external_conversation_item",
        "external_conversation_item",
        "external_session_status",
    ]
    assert [payload["data"]["item_data"]["role"] for payload in posted[:2]] == [
        "user",
        "assistant",
    ]
    assert posted[2]["data"] == _expected_status_data("idle", "turn_123")
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"method": "turn/started", "params": {}}, True),
        ({"method": "item/agentMessage/delta", "params": {}}, True),
        (
            {"method": "thread/status/changed", "params": {"status": {"type": "active"}}},
            True,
        ),
        # Idle status / fresh-thread announce / control noise do NOT imply a
        # rollout exists yet — they must NOT release the parked subscribe.
        (
            {"method": "thread/status/changed", "params": {"status": {"type": "idle"}}},
            False,
        ),
        ({"method": "thread/started", "params": {"thread": {"id": "t1"}}}, False),
        ({"method": "remoteControl/status/changed", "params": {"status": "disabled"}}, False),
        ({"result": {}, "id": 1}, False),
    ],
)
def test_event_indicates_thread_active(event: dict[str, Any], expected: bool) -> None:
    """Only turn/item/active-status events imply the thread's rollout exists.

    This predicate gates releasing the deferred subscription. A false
    positive (e.g. on the ``idle`` status codex emits at thread creation)
    would resume too early and reintroduce the no-rollout retry churn; a
    false negative would leave the forwarder parked through a real turn.
    """
    assert codex_native_forwarder._event_indicates_thread_active(event) is expected


def test_subscribe_until_ready_parks_until_signal_then_resumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fresh, not-ready thread parks on the signal instead of polling.

    Reproduces the fresh-session path: ``thread/resume`` fails with ``no
    rollout found`` until the thread becomes active. With a ``ready_signal``
    provided, the subscribe must NOT busy-poll — it waits for the signal
    (set by the caller when the live stream shows the thread active), then
    retries and succeeds. ``_sleep`` is stubbed to raise so a regression
    back to blind-polling turns the task red instead of silently hammering
    the app-server.
    """
    fake_client = _FakeCodexAppServerClient(response={"result": {"thread": {"turns": []}}})
    not_ready = RuntimeError(
        "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
    )

    async def run() -> int:
        ready = asyncio.Event()
        attempts = 0

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            # Not-ready until the thread is "active" (signal set) — mirrors
            # codex deferring rollout materialization until the first turn.
            if not ready.is_set():
                raise not_ready
            return fake_client.response

        async def fake_sleep(_delay: float) -> None:
            raise AssertionError(
                "subscribe must park on ready_signal for a not-ready thread, not poll"
            )

        monkeypatch.setattr(fake_client, "request", fake_request)
        monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda _req: httpx.Response(202, json={})),
        ) as client:
            task = asyncio.create_task(
                codex_native_forwarder._subscribe_until_ready(
                    fake_client,  # type: ignore[arg-type]
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    thread_id="thread_123",
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    ready_signal=ready,
                )
            )
            # Let the task make its first resume attempt and park. If it had
            # polled instead, fake_sleep would have raised and the task would
            # be done with an exception — so an un-done task proves it parked.
            for _ in range(8):
                await asyncio.sleep(0)
            assert not task.done(), "subscribe should still be parked on the signal"
            assert attempts >= 1

            # Caller observed the thread go active → release the wait.
            ready.set()
            await task
            return attempts

    attempts = asyncio.run(run())
    # ≥2: the initial not-ready attempt plus at least one post-signal retry
    # that succeeds. If it were still polling we'd never reach here (fake_sleep
    # raises); if it never retried after the signal it would hang on await task.
    assert attempts >= 2, attempts


def test_supervise_forwarder_continues_after_event_handler_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    One malformed Codex notification must not stop transcript mirroring.

    The native forwarder is long-lived. If one event raises during
    handling, later events from the same app-server stream still need
    to be processed or Codex responses stop syncing back to AP.
    """
    fake_client = _FakeCodexAppServerClient(
        events=[
            {"method": "turn/started", "params": {"turn": {"id": "turn_bad"}}},
            {"method": "turn/started", "params": {"turn": {"id": "turn_after"}}},
        ]
    )
    handled: list[str] = []

    async def fake_handle_event(
        _client: httpx.AsyncClient,
        *,
        session_id: str,
        bridge_dir: Path,
        event: dict[str, Any],
        delta_coalescer: codex_native_forwarder._OutputTextDeltaCoalescer | None = None,
        usage_coalescer: codex_native_forwarder._SessionUsageCoalescer | None = None,
        elicitation_tracker: codex_native_forwarder._CodexElicitationTaskTracker | None = None,
        expected_thread_id: str | None = None,
        codex_client: codex_native_app_server.CodexAppServerClient | None = None,
        forwarder_state: codex_native_forwarder._CodexForwarderState | None = None,
    ) -> None:
        """
        Fail the first event and record subsequent events.

        :param _client: Omnigent HTTP client.
        :param session_id: Omnigent session id, e.g. ``"conv_123"``.
        :param bridge_dir: Native Codex bridge directory.
        :param event: Codex event payload.
        :param delta_coalescer: Optional text-delta coalescer.
        :param usage_coalescer: Optional usage coalescer.
        :param elicitation_tracker: Optional elicitation tracker.
        :param expected_thread_id: Active Codex thread id.
        :param codex_client: Optional Codex app-server client.
        :param forwarder_state: Optional forwarder state.
        :returns: None.
        """
        del (
            session_id,
            bridge_dir,
            delta_coalescer,
            usage_coalescer,
            elicitation_tracker,
            expected_thread_id,
            codex_client,
            forwarder_state,
        )
        turn_id = event["params"]["turn"]["id"]
        if turn_id == "turn_bad":
            raise RuntimeError("bad event")
        handled.append(turn_id)

    monkeypatch.setattr(codex_native_forwarder, "_handle_event", fake_handle_event)

    async def run() -> None:
        """
        Run the supervisor against a two-event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            client=fake_client,  # type: ignore[arg-type]
        )

    asyncio.run(run())

    assert handled == ["turn_after"]
    assert "Codex forwarder event handling failed" in caplog.text


def test_codex_discover_thread_and_forward_writes_routing_summary_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A startup timeout records the launch routing summary in the bridge error (#2745)."""
    from omnigent.harnesses.codex_native import forwarder as _fwd
    from omnigent.harnesses.codex_native.bridge import read_bridge_startup_error
    from omnigent.runner.native import orchestration as native_orch

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    async def _timeout(_client: object) -> str:
        raise TimeoutError("no thread event")

    monkeypatch.setattr(_fwd, "wait_for_thread_started", _timeout)

    class _FakeClient:
        async def close(self) -> None:
            return None

    asyncio.run(
        native_orch._codex_discover_thread_and_forward(
            session_id="conv_test",
            bridge_dir=bridge_dir,
            codex_ws_url="ws://127.0.0.1:9999",
            codex_home=tmp_path / "codex-home",
            workspace=str(tmp_path / "workspace"),
            event_client=_FakeClient(),
            routing_summary="Codex CLI login (no provider configured) -- SENTINEL",
        )
    )

    err = read_bridge_startup_error(bridge_dir)
    assert err is not None
    assert "Launch routing: Codex CLI login (no provider configured) -- SENTINEL" in err
    assert "startup timed out" in err


# --- headless login-fallback fail-fast: no credential can start the TUI thread ---


def test_codex_discover_thread_login_required_records_error_before_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A login-doomed launch records the failure up front, before any wait.

    When routing resolved to Codex's own login with no usable credential, the
    TUI can only render the sign-in screen. The turn-facing error must exist
    *before* thread discovery starts waiting, so a headless chat turn fails
    immediately with an actionable message instead of burning the 30s
    thread-start timeout (the "Codex TUI never started a thread" hang).
    """
    from omnigent.harnesses.codex_native import forwarder as _fwd
    from omnigent.harnesses.codex_native.bridge import read_bridge_startup_error
    from omnigent.runner.native import orchestration as native_orch

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    error_at_wait_time: list[str | None] = []

    async def _wait(_client: object, *, timeout: float | None = 30.0) -> str:
        # The fail-fast contract: the turn-facing error is already on disk
        # when the (now unbounded) wait begins.
        error_at_wait_time.append(read_bridge_startup_error(bridge_dir))
        assert timeout is None, "login-gated discovery must wait without a deadline"
        raise RuntimeError("stream ended")

    monkeypatch.setattr(_fwd, "wait_for_thread_started", _wait)

    class _FakeClient:
        async def close(self) -> None:
            return None

    asyncio.run(
        native_orch._codex_discover_thread_and_forward(
            session_id="conv_test",
            bridge_dir=bridge_dir,
            codex_ws_url="ws://127.0.0.1:9999",
            codex_home=tmp_path / "codex-home",
            workspace=str(tmp_path / "workspace"),
            event_client=_FakeClient(),
            routing_summary="Codex CLI login (no provider configured) -- SENTINEL",
            login_required=True,
        )
    )

    assert error_at_wait_time and error_at_wait_time[0] is not None
    recorded = error_at_wait_time[0]
    assert "not signed in" in recorded
    assert "Launch routing: Codex CLI login (no provider configured) -- SENTINEL" in recorded
    # The pre-recorded error must not carry the timeout markers: the whole
    # point is a clear, non-timeout failure.
    assert "startup timed out" not in recorded
    assert "never started a thread" not in recorded


def test_codex_discover_thread_login_required_clears_error_on_thread_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An interactive sign-in that starts the thread clears the pre-recorded error.

    The login-gated launch stays recoverable: a user signing in from the
    attached terminal starts the thread, and the stale fail-fast cause must
    not shadow the now-working bridge state.
    """
    from omnigent.harnesses.codex_native import forwarder as _fwd
    from omnigent.harnesses.codex_native.bridge import (
        read_bridge_startup_error,
        read_bridge_state,
    )
    from omnigent.runner.native import orchestration as native_orch

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    record_at_forwarding: list[str | None] = []

    async def _wait(_client: object, *, timeout: float | None = 30.0) -> str:
        return "thread_after_signin"

    async def _forward(**_kwargs: object) -> None:
        record_at_forwarding.append(read_bridge_startup_error(bridge_dir))

    monkeypatch.setattr(_fwd, "wait_for_thread_started", _wait)
    monkeypatch.setattr(_fwd, "supervise_forwarder", _forward)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:1")

    class _FakeClient:
        async def close(self) -> None:
            return None

    asyncio.run(
        native_orch._codex_discover_thread_and_forward(
            session_id="conv_test",
            bridge_dir=bridge_dir,
            codex_ws_url="ws://127.0.0.1:9999",
            codex_home=tmp_path / "codex-home",
            workspace=str(tmp_path / "workspace"),
            event_client=_FakeClient(),
            routing_summary="Codex CLI login (no provider configured)",
            login_required=True,
        )
    )

    assert record_at_forwarding == [None]
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.thread_id == "thread_after_signin"
