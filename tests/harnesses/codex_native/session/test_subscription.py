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
    _capture_handler,
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


def _list_turns_refusal() -> codex_native_app_server.CodexAppServerResponseError:
    """Build the ``thread/resume`` refusal Codex returned for the worker thread."""
    return codex_native_app_server.CodexAppServerResponseError(
        {"code": -32601, "message": "list_turns is not supported yet"}
    )


def _failed_statuses(posted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the posted ``failed`` session-status payloads in order."""
    return [
        payload["data"]
        for payload in posted
        if payload["type"] == "external_session_status" and payload["data"]["status"] == "failed"
    ]


def test_subscribe_until_ready_fails_each_refused_turn_until_resume_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each refusal interrupts and fails the running turn; the next activation retries."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    resume_errors: list[Exception | None] = [_list_turns_refusal()] * 4 + [None]
    forwarder_state = codex_native_forwarder._CodexForwarderState()
    posted: list[dict[str, Any]] = []

    async def scripted_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        fake_client.requests.append((method, params))
        if method == "thread/resume":
            error = resume_errors.pop(0)
            if error is not None:
                raise error
            return {"result": {"thread": {"turns": []}}}
        return {"result": {}}

    monkeypatch.setattr(fake_client, "request", scripted_request)

    async def wait_for_failures(task: asyncio.Task[None], count: int) -> None:
        while len(_failed_statuses(posted)) < count and not task.done():
            await asyncio.sleep(0)

    async def run() -> None:
        ready = asyncio.Event()
        ready.set()
        async with _recording_forwarder_client(posted) as client:
            task = asyncio.create_task(
                codex_native_forwarder._subscribe_until_ready(
                    fake_client,  # type: ignore[arg-type]
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    thread_id="thread_123",
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    forwarder_state=forwarder_state,
                    ready_signal=ready,
                )
            )
            await asyncio.wait_for(wait_for_failures(task, 1), timeout=5.0)
            assert _failed_statuses(posted), (
                f"refused thread/resume left turn_123 running with nothing surfaced: {posted}"
            )
            state = read_bridge_state(tmp_path)
            assert state is not None
            assert state.active_turn_id is None
            assert not task.done(), "a refused subscription should wait for the next activation"
            _write_forwarder_bridge(tmp_path, active_turn_id="turn_456")
            ready.set()
            await asyncio.wait_for(wait_for_failures(task, 2), timeout=5.0)
            ready.set()
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())

    resume_with_turns = ("thread/resume", {"threadId": "thread_123"})
    resume_without_turns = ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    assert fake_client.requests == [
        resume_with_turns,
        resume_without_turns,
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_123"}),
        resume_with_turns,
        resume_without_turns,
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_456"}),
        resume_without_turns,
    ]
    failures = _failed_statuses(posted)
    assert [failure["response_id"] for failure in failures] == ["codex_turn_123", "codex_turn_456"]
    assert all("list_turns is not supported yet" in failure["output"] for failure in failures)
    assert forwarder_state.surfaced_terminal_error_turns == {"turn_123", "turn_456"}


def test_supervise_forwarder_retries_subscription_on_activity_during_refused_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Thread activity seen while a refused turn is failing re-arms the subscription retry."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    resume_errors: list[Exception | None] = [_list_turns_refusal()] * 2 + [None]
    interrupted = asyncio.Event()
    activity_handled = asyncio.Event()
    resumed = asyncio.Event()
    posted: list[dict[str, Any]] = []

    async def refuse_first_subscription(method: str, params: dict[str, Any]) -> dict[str, Any]:
        fake_client.requests.append((method, params))
        if method == "turn/interrupt":
            interrupted.set()
            await activity_handled.wait()
            return {"result": {}}
        error = resume_errors.pop(0)
        if error is not None:
            raise error
        resumed.set()
        return {"result": {"thread": {"turns": []}}}

    async def thread_activity_during_interrupt() -> Any:
        await interrupted.wait()
        yield {
            "method": "thread/status/changed",
            "params": {"threadId": "thread_123", "status": {"type": "active"}},
        }
        activity_handled.set()
        await resumed.wait()

    monkeypatch.setattr(fake_client, "request", refuse_first_subscription)
    monkeypatch.setattr(fake_client, "iter_events", thread_activity_during_interrupt)

    async def run() -> None:
        await asyncio.wait_for(
            codex_native_forwarder.supervise_forwarder(
                base_url="http://127.0.0.1:1",
                headers={},
                session_id="conv_123",
                bridge_dir=tmp_path,
                app_server_url=str(tmp_path / "app-server.sock"),
                thread_id="thread_123",
                client=fake_client,  # type: ignore[arg-type]
                ap_transport=httpx.MockTransport(_capture_handler(posted)),
            ),
            timeout=5.0,
        )

    asyncio.run(run())

    resume_without_turns = ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123"}),
        resume_without_turns,
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_123"}),
        resume_without_turns,
    ]
    assert [failure["response_id"] for failure in _failed_statuses(posted)] == ["codex_turn_123"]


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the forwarder's sleep with one that records each delay."""
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", recording_sleep)
    return delays


def _subscribe_without_signal(
    fake_client: _FakeCodexAppServerClient,
    bridge_dir: Path,
    *,
    forwarder_state: codex_native_forwarder._CodexForwarderState | None = None,
) -> list[dict[str, Any]]:
    """Run one subscription attempt with no ready signal; return the posted events."""
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        async with _recording_forwarder_client(posted) as client:
            await asyncio.wait_for(
                codex_native_forwarder._subscribe_until_ready(
                    fake_client,  # type: ignore[arg-type]
                    client,
                    session_id="conv_123",
                    bridge_dir=bridge_dir,
                    thread_id="thread_123",
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    forwarder_state=forwarder_state,
                ),
                timeout=5.0,
            )

    asyncio.run(run())
    return posted


def test_subscribe_until_ready_fails_turn_recorded_after_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A refusal that beats ``turn/start`` fails its turn; a failed interrupt keeps its id."""
    _write_forwarder_bridge(tmp_path, active_turn_id=None)
    fake_client = _FakeCodexAppServerClient()
    polls = 0

    async def refuse_resume_and_drop_interrupt(
        method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        fake_client.requests.append((method, params))
        if method == "thread/resume":
            raise _list_turns_refusal()
        raise ConnectionError("Codex app-server disconnected before responding to turn/interrupt")

    monkeypatch.setattr(fake_client, "request", refuse_resume_and_drop_interrupt)

    async def record_turn_on_third_poll(_delay: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
        await asyncio.sleep(0)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", record_turn_on_third_poll)

    posted = _subscribe_without_signal(fake_client, tmp_path)

    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True}),
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_123"}),
    ]
    failures = _failed_statuses(posted)
    assert [failure["response_id"] for failure in failures] == ["codex_turn_123"]
    assert "may still be running: list_turns is not supported yet" in failures[0]["output"]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_123"


@pytest.mark.parametrize(
    ("active_turn_id", "thread_ready"),
    [
        pytest.param("turn_123", True, id="replaying-active-turn"),
        pytest.param(None, False, id="fresh-thread-first-turn"),
    ],
)
def test_subscribe_until_ready_subscribes_without_turns_codex_cannot_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_turn_id: str | None,
    thread_ready: bool,
) -> None:
    """A resume refused only for its turns subscribes without them and leaves the turn running."""
    _write_forwarder_bridge(tmp_path, active_turn_id=active_turn_id)
    fake_client = _FakeCodexAppServerClient()

    async def refuse_listing_turns(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal thread_ready
        fake_client.requests.append((method, params))
        if not thread_ready:
            thread_ready = True
            raise codex_native_app_server.CodexAppServerResponseError(
                {"code": -32600, "message": "no rollout found for thread id thread_123"}
            )
        if not params.get("excludeTurns"):
            raise _list_turns_refusal()
        return {"result": {"thread": {"id": "thread_123", "turns": []}}}

    monkeypatch.setattr(fake_client, "request", refuse_listing_turns)
    _record_sleeps(monkeypatch)

    posted = _subscribe_without_signal(fake_client, tmp_path)

    resume_without_turns = ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    expected = [("thread/resume", {"threadId": "thread_123"}), resume_without_turns]
    if active_turn_id is None:
        expected.insert(0, resume_without_turns)
    assert fake_client.requests == expected
    assert posted == []
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == active_turn_id


@pytest.mark.parametrize(
    ("startup_rejections", "outcome"),
    [
        pytest.param(2, "was stopped", id="turn-still-starting"),
        pytest.param(None, "had already ended", id="turn-never-interruptible"),
    ],
)
def test_subscribe_until_ready_retries_interrupt_while_refused_turn_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    startup_rejections: int | None,
    outcome: str,
) -> None:
    """Codex's "no active turn" answer during turn startup is retried within the wait."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    interrupts = 0

    async def refuse_resume_while_turn_starts(
        method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal interrupts
        fake_client.requests.append((method, params))
        if method == "thread/resume":
            raise _list_turns_refusal()
        interrupts += 1
        if startup_rejections is None or interrupts <= startup_rejections:
            raise codex_native_app_server.CodexAppServerResponseError(
                {"code": -32600, "message": "no active turn to interrupt"}
            )
        return {"result": {}}

    monkeypatch.setattr(fake_client, "request", refuse_resume_while_turn_starts)
    delays = _record_sleeps(monkeypatch)

    posted = _subscribe_without_signal(fake_client, tmp_path)

    wait = codex_native_forwarder._REFUSED_SUBSCRIPTION_TURN_WAIT_SECONDS
    retry_delay = codex_native_forwarder._SUBSCRIBE_RETRY_DELAY_SECONDS
    retries = int(wait / retry_delay) if startup_rejections is None else startup_rejections
    assert interrupts == retries + 1
    assert delays == [retry_delay] * retries
    failures = _failed_statuses(posted)
    assert [failure["response_id"] for failure in failures] == ["codex_turn_123"]
    assert f"{outcome}: list_turns is not supported yet" in failures[0]["output"]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.parametrize(
    ("already_failed", "turn_after_interrupt", "interrupt_error"),
    [
        pytest.param(False, "turn_456", None, id="replaced-by-newer-turn"),
        pytest.param(
            False,
            "turn_456",
            codex_native_app_server.CodexAppServerResponseError(
                {"code": -32600, "message": "expected active turn id turn_123 but found turn_456"}
            ),
            id="superseded-before-interrupt",
        ),
        pytest.param(False, None, None, id="cleared-by-another-owner"),
        pytest.param(True, None, None, id="already-failed"),
    ],
)
def test_subscribe_until_ready_skips_a_replaced_or_already_failed_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    already_failed: bool,
    turn_after_interrupt: str | None,
    interrupt_error: Exception | None,
) -> None:
    """A refusal posts nothing for a turn already failed, or cleared or replaced meanwhile."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState()
    if already_failed:
        forwarder_state.surfaced_terminal_error_turns.add("turn_123")

    async def refuse_resume_then_change_turn(
        method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        fake_client.requests.append((method, params))
        if method == "thread/resume":
            raise _list_turns_refusal()
        _write_forwarder_bridge(tmp_path, active_turn_id=turn_after_interrupt)
        if interrupt_error is not None:
            raise interrupt_error
        return {"result": {}}

    monkeypatch.setattr(fake_client, "request", refuse_resume_then_change_turn)

    posted = _subscribe_without_signal(fake_client, tmp_path, forwarder_state=forwarder_state)

    expected_methods = ["thread/resume", "thread/resume"]
    if not already_failed:
        expected_methods.append("turn/interrupt")
    assert [method for method, _params in fake_client.requests] == expected_methods
    assert posted == []
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == ("turn_123" if already_failed else turn_after_interrupt)


@pytest.mark.parametrize(
    ("error", "active_turn_id", "waits_for_turn"),
    [
        pytest.param(
            ConnectionError("Codex app-server disconnected before responding to thread/resume"),
            "turn_123",
            False,
            id="transport-error",
        ),
        pytest.param(
            codex_native_app_server.CodexAppServerResponseError(
                {"code": -32600, "message": "Server is draining; retry after reconnecting"}
            ),
            "turn_123",
            False,
            id="supported-request-error",
        ),
        pytest.param(_list_turns_refusal(), None, True, id="refusal-without-active-turn"),
    ],
)
def test_subscribe_until_ready_leaves_turn_state_without_refused_active_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    active_turn_id: str | None,
    waits_for_turn: bool,
) -> None:
    """Only an unsupported-request refusal of a thread with a running turn fails that turn."""
    _write_forwarder_bridge(tmp_path, active_turn_id=active_turn_id)
    fake_client = _FakeCodexAppServerClient(error=error)
    delays = _record_sleeps(monkeypatch)

    posted = _subscribe_without_signal(fake_client, tmp_path)

    assert [method for method, _params in fake_client.requests] == ["thread/resume"]
    assert posted == []
    assert sum(delays) == pytest.approx(
        codex_native_forwarder._REFUSED_SUBSCRIPTION_TURN_WAIT_SECONDS if waits_for_turn else 0.0
    )
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == active_turn_id


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
