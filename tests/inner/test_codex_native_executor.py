"""Tests for the native Codex TUI executor bridge."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_startup_error,
    write_bridge_state,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete

# A 1x1 transparent PNG, base64-encoded — a real decodable image small
# enough to embed, used to prove image blocks are materialized to disk
# rather than inlined as text.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


class _FakeCodexNativeClient:
    """
    Fake Codex app-server client for native executor tests.

    Accepts both call shapes ``client_for_transport`` produces — a
    positional unix ``socket_path`` Path or a ``ws_url`` keyword — so
    tests can drive either transport. ``created`` records
    ``(socket_path, ws_url, client_name)`` per construction so a test
    can assert which transport branch the executor took.

    :param socket_path: Unix app-server socket path, e.g.
        ``Path("/tmp/app-server.sock")``. ``None`` for ws transports.
    :param ws_url: Loopback WebSocket URL, e.g.
        ``"ws://127.0.0.1:9876"``. ``None`` for unix transports.
    :param client_name: JSON-RPC client name, e.g.
        ``"omnigent-codex-native"``.
    """

    requests: list[tuple[str, dict[str, Any]]] = []
    created: list[tuple[Path | None, str | None, str]] = []
    next_turn = 1

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        ws_url: str | None = None,
        client_name: str = "omnigent",
    ) -> None:
        """
        Initialize one fake client connection.

        :param socket_path: Unix app-server socket path, or ``None``.
        :param ws_url: Loopback WebSocket URL, or ``None``.
        :param client_name: JSON-RPC client name.
        """
        self.socket_path = socket_path
        self.ws_url = ws_url
        self.client_name = client_name
        self.connected = False
        self.closed = False
        type(self).created.append((socket_path, ws_url, client_name))

    async def connect(self) -> None:
        """
        Mark this fake client as connected.

        :returns: None.
        """
        self.connected = True

    async def close(self) -> None:
        """
        Mark this fake client as closed.

        :returns: None.
        """
        self.closed = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture a Codex JSON-RPC request and return a canned response.

        :param method: JSON-RPC method, e.g. ``"turn/start"``.
        :param params: JSON-RPC params.
        :returns: Codex-shaped response payload.
        """
        type(self).requests.append((method, params))
        if method == "turn/start":
            turn_id = f"turn_{type(self).next_turn}"
            type(self).next_turn += 1
            return {"result": {"turn": {"id": turn_id}}}
        if method == "turn/steer":
            return {"result": {"turnId": "turn_steered"}}
        return {"result": {}}

    async def iter_events(self) -> Any:
        """
        Fail if the executor waits on Codex terminal notifications.

        The native executor is only an injection bridge. The
        separate forwarder owns Codex status and transcript events.

        :returns: Async iterator that raises on first consumption.
        """
        raise AssertionError("native executor must not wait for Codex turn events")
        yield {}


def _collect_turn_events(executor: CodexNativeExecutor, text: str) -> list[Any]:
    """
    Run one native executor turn and collect its events.

    :param executor: Native Codex executor under test.
    :param text: User text to send, e.g. ``"hello"``.
    :returns: Events yielded by :meth:`CodexNativeExecutor.run_turn`.
    """

    async def run() -> list[Any]:
        """
        Collect the async turn iterator.

        :returns: Events yielded by the turn.
        """
        events: list[Any] = []
        async for event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            [],
            "",
        ):
            events.append(event)
        return events

    return asyncio.run(run())


def test_web_started_codex_turn_returns_without_waiting_for_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A web-started Codex turn returns after app-server accepts it.

    The terminal forwarder mirrors Codex completion/status events.
    Waiting for those events inside the harness turn can leave the
    runner permanently active after the first web message, so later
    web messages never reach the local Codex TUI as new dispatches.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    # Patch at the source so the executor's client_for_transport builds
    # the fake for either transport (ws:// or unix path).
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    events = _collect_turn_events(executor, "first")
    state = read_bridge_state(tmp_path)

    assert [type(event) for event in events] == [TurnComplete]
    assert state is not None
    assert state.active_turn_id == "turn_1"
    assert _FakeCodexNativeClient.requests == [
        (
            "turn/start",
            {
                "threadId": "thread_123",
                "input": [{"type": "text", "text": "first"}],
            },
        )
    ]


def test_system_prompt_does_not_override_collaboration_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Native startup config owns system prompts; turns preserve Codex defaults."""
    from omnigent.tools.builtins.session_rename import SESSION_RENAME_INSTRUCTION

    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    async def run() -> None:
        async for _event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            [],
            SESSION_RENAME_INSTRUCTION,
            None,
        ):
            pass

    asyncio.run(run())

    assert _FakeCodexNativeClient.requests == [
        (
            "turn/start",
            {
                "threadId": "thread_123",
                "input": [{"type": "text", "text": "hello"}],
            },
        ),
    ]


def test_image_block_is_sent_as_local_image_not_inline_base64(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image attachment is sent as a ``localImage`` item, not inline text.

    Regression for the ``input_too_large`` failure: the executor used to
    JSON-dump image blocks (their multi-megabyte base64 data URI) into
    the turn's text input, and the Codex app-server rejects any turn
    whose input text exceeds 1 MiB. The fix materializes the image to
    disk and references it by path. This pins three things: (1) the
    image becomes a ``localImage`` input item pointing at a real file
    holding the decoded PNG; (2) accompanying text is preserved as a
    separate ``text`` item; (3) the base64 payload appears in NO text
    item — its presence there would be the exact bug recurring.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    async def run() -> list[Any]:
        """
        Drive one turn carrying an image block plus a text block.

        :returns: Events yielded by the turn.
        """
        events: list[Any] = []
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": _PNG_DATA_URI},
                        {"type": "input_text", "text": "what is this?"},
                    ],
                }
            ],
            [],
            "",
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    assert [type(event) for event in events] == [TurnComplete]
    assert len(_FakeCodexNativeClient.requests) == 1
    method, params = _FakeCodexNativeClient.requests[0]
    assert method == "turn/start"
    items = params["input"]

    local_images = [item for item in items if item["type"] == "localImage"]
    texts = [item for item in items if item["type"] == "text"]
    # One localImage item — the image was routed to the image channel,
    # not flattened into text. Zero would mean the attachment was dropped.
    assert len(local_images) == 1, f"expected one localImage item, got {items}"
    # The accompanying prompt survives as its own text item.
    assert len(texts) == 1
    assert texts[0]["text"] == "what is this?"
    # The path points at a real file holding the decoded PNG bytes — so
    # the Codex app-server can open it. Mismatch means we wrote the wrong
    # bytes (e.g. the base64 text instead of the decoded image).
    image_path = Path(local_images[0]["path"])
    assert image_path.read_bytes() == base64.b64decode(_PNG_B64)
    # CRITICAL: the base64 payload must not appear in ANY text item. If it
    # does, the 11.7 M-char data URI is back in the text input and Codex
    # rejects the turn with input_too_large — the original bug.
    assert all(_PNG_B64 not in item.get("text", "") for item in items), (
        "base64 image payload leaked into a text input item — the "
        "input_too_large bug has regressed"
    )


def test_input_file_text_is_inlined_as_a_text_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A textual ``input_file`` is decoded and inlined as a ``text`` item.

    The Codex app-server has no file input item, so a ``text/*`` file is
    decoded from its data URI and sent inline as text. Proves the
    decoded content reaches the turn input verbatim and that NO
    ``localImage`` item is produced for a file. A failure means a text
    file was dropped or mis-routed to the image channel.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    file_text = "line one\nline two\n"
    data_uri = "data:text/plain;base64," + base64.b64encode(file_text.encode()).decode()

    async def run() -> None:
        """Drive one turn carrying a single text ``input_file`` block."""
        async for _event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_file", "file_data": data_uri}]}],
            [],
            "",
        ):
            pass

    asyncio.run(run())

    method, params = _FakeCodexNativeClient.requests[-1]
    assert method == "turn/start"
    items = params["input"]
    # The decoded file content is inlined as text, verbatim.
    assert items == [{"type": "text", "text": file_text}]
    # No image channel item for a file, and no uploads dir written.
    assert all(item["type"] != "localImage" for item in items)
    assert not (tmp_path / "uploads").exists()


def test_input_file_binary_is_materialized_and_referenced_by_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A binary ``input_file`` is written to disk and referenced by path.

    A non-text file (e.g. a PDF) can't be inlined as text, so it is
    materialized under ``uploads/`` and referenced via an
    ``[Attached file: <path>]`` text item — keeping the multi-megabyte
    base64 out of the turn input while still letting the model open it.
    Proves the file lands on disk with its decoded bytes and that the
    referenced path matches what was written. A failure means the binary
    was inlined as base64 (the input_too_large risk) or dropped.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    pdf_bytes = b"%PDF-1.4\n%binary\x00\xff bytes\n"
    data_uri = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
    block = {"type": "input_file", "file_data": data_uri, "filename": "report.pdf"}

    async def run() -> None:
        """Drive one turn carrying a single binary ``input_file`` block."""
        async for _event in executor.run_turn(
            [{"role": "user", "content": [block]}],
            [],
            "",
        ):
            pass

    asyncio.run(run())

    method, params = _FakeCodexNativeClient.requests[-1]
    assert method == "turn/start"
    items = params["input"]
    assert len(items) == 1
    assert items[0]["type"] == "text"
    # The text item references the materialized path, not inline base64.
    text = items[0]["text"]
    assert text.startswith("[Attached file: ")
    assert base64.b64encode(pdf_bytes).decode() not in text
    referenced = Path(text[len("[Attached file: ") : -len("]")])
    # The referenced file exists under uploads/ and holds the decoded bytes.
    assert referenced.parent == tmp_path / "uploads"
    assert referenced.read_bytes() == pdf_bytes


async def test_executor_reaches_app_server_over_ws_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Steering and interrupt reach the app-server over a ws:// transport.

    Host-spawned codex sessions persist a ``ws://`` ``socket_path`` in
    bridge state (the runner's app-server listens on a loopback ws
    port). Before the transport was routed through
    ``client_for_transport``, the executor wrapped it in ``Path(...)``
    and dialed a nonexistent unix socket, so steering and interrupt
    silently failed over the web UI. This pins the ws:// path: every
    client the executor builds must use the ``ws_url`` branch (never a
    unix Path), and the steer / interrupt RPCs must carry the active
    turn the executor read from bridge state.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    ws_url = "ws://127.0.0.1:9876"
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=ws_url,
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            # Non-None active turn → enqueue takes the turn/steer path
            # (not turn/start), exercising the steering connect site.
            active_turn_id="turn_active",
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    steered = await executor.enqueue_session_message("k", "steer me")
    interrupted = await executor.interrupt_session("k")

    assert steered is True
    assert interrupted is True
    # Both connect sites must have routed through the ws_url branch. A
    # regression to Path(state.socket_path) would record a non-None
    # socket_path of Path("ws:/127.0.0.1:9876") and ws_url=None.
    assert len(_FakeCodexNativeClient.created) == 2  # one per RPC (steer, interrupt)
    for socket_path, built_ws_url, _name in _FakeCodexNativeClient.created:
        assert socket_path is None
        assert built_ws_url == ws_url
    assert (
        "turn/steer",
        {
            "threadId": "thread_123",
            "expectedTurnId": "turn_active",
            "input": [{"type": "text", "text": "steer me"}],
        },
    ) in _FakeCodexNativeClient.requests
    # The steer advanced the active turn id to the fake's response
    # ("turn_steered"), persisted via update_active_turn_id; interrupt
    # then targets that updated turn, proving the steer write landed.
    assert (
        "turn/interrupt",
        {"threadId": "thread_123", "turnId": "turn_steered"},
    ) in _FakeCodexNativeClient.requests


def test_next_web_message_starts_new_codex_turn_after_forwarder_marks_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A later web message starts a fresh turn after Codex reports idle.

    The forwarder clears ``active_turn_id`` from bridge state on
    ``turn/completed``. Once that happens, the next Omnigent dispatch must
    call ``turn/start`` rather than steering a completed turn.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    # Patch at the source so the executor's client_for_transport builds
    # the fake for either transport (ws:// or unix path).
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    first_events = _collect_turn_events(executor, "first")
    state = read_bridge_state(tmp_path)
    assert state is not None
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=state.session_id,
            socket_path=state.socket_path,
            thread_id=state.thread_id,
            codex_home=state.codex_home,
            active_turn_id=None,
        ),
    )
    second_events = _collect_turn_events(executor, "second")

    assert [type(event) for event in first_events] == [TurnComplete]
    assert [type(event) for event in second_events] == [TurnComplete]
    assert [method for method, _params in _FakeCodexNativeClient.requests] == [
        "turn/start",
        "turn/start",
    ]


async def test_concurrent_steering_during_turn_start_is_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Steering that arrives while a turn is starting must steer that turn.

    ``run_turn`` (initiating message) and ``enqueue_session_message``
    (mid-turn steering) run as concurrent tasks against one cached
    executor. ``run_turn`` reads ``active_turn_id`` (``None``), issues
    ``turn/start``, then writes the new turn id. Without the injection
    lock, a steering injection that reads bridge state in that window
    sees "no active turn" and silently drops the message. The lock holds
    the steering call until ``run_turn`` has established the turn, so the
    steer lands on it.

    Deterministic race window: the fake's ``turn/start`` blocks on
    ``release``, so ``active_turn_id`` is provably still ``None`` for the
    whole window while the steering injection makes its decision.
    """
    # Per-test shared state, closed over by the fake below. Kept local
    # (not class-level) so it can't leak across instances or tests; the
    # executor instantiates the fake once per RPC client and all instances
    # coordinate through these.
    requests: list[tuple[str, dict[str, Any]]] = []
    start_entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingStartCodexClient:
        """Codex app-server fake whose ``turn/start`` blocks until released.

        Pins ``run_turn`` inside the ``turn/start`` RPC — after it read
        ``active_turn_id=None`` but before it writes the new turn id — so a
        concurrent steering injection races that window.
        """

        def __init__(
            self,
            socket_path: Path | None = None,
            *,
            ws_url: str | None = None,
            client_name: str,
        ) -> None:
            """Accept the real client's call shapes; state lives in closure."""
            del socket_path, ws_url, client_name

        async def connect(self) -> None:
            """No-op connect."""
            return

        async def close(self) -> None:
            """No-op close."""
            return

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            """Record the request; block inside ``turn/start`` until released."""
            requests.append((method, params))
            if method == "turn/start":
                start_entered.set()
                await release.wait()
                return {"result": {"turn": {"id": "turn_1"}}}
            if method == "turn/steer":
                return {"result": {"turnId": "turn_steered"}}
            return {"result": {}}

    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _BlockingStartCodexClient,
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    async def _drive_run_turn() -> list[Any]:
        """Consume run_turn (the initiating-message injection path).

        :returns: Events yielded by the turn.
        """
        events: list[Any] = []
        async for event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_text", "text": "first"}]}],
            [],
            "",
        ):
            events.append(event)
        return events

    run_turn_task = asyncio.create_task(_drive_run_turn())
    # run_turn is now parked inside turn/start, holding the injection lock,
    # having read active_turn_id=None but not yet written turn_1.
    await asyncio.wait_for(start_entered.wait(), timeout=5.0)

    # A steering message arrives in that window.
    enqueue_task = asyncio.create_task(executor.enqueue_session_message("k", "steer me"))
    # Let the steering injection reach its turn-vs-buffer decision. The
    # window stays open (run_turn is blocked in turn/start), so in the
    # un-serialized case it deterministically reads active_turn_id=None
    # and drops the message before we release.
    await asyncio.sleep(0.1)
    release.set()

    accepted = await asyncio.wait_for(enqueue_task, timeout=5.0)
    await asyncio.wait_for(run_turn_task, timeout=5.0)

    methods = [method for method, _params in requests]
    # The steering message was steered into the started turn, not dropped.
    # accepted=False / no turn/steer would mean enqueue read
    # active_turn_id=None and dropped the message (no serialization).
    assert accepted is True, (
        "steering during turn-start was dropped; the injection lock must hold "
        "enqueue_session_message until run_turn establishes the turn"
    )
    assert "turn/steer" in methods, (
        f"expected a turn/steer after turn/start; got requests {methods}. A "
        "missing steer means the steering message read active_turn_id=None and "
        "was dropped (no serialization with run_turn)."
    )
    # Exactly one turn was started — no double-start race.
    assert methods.count("turn/start") == 1, f"expected exactly one turn/start; got {methods}"


def _start_state(tmp_path: Path) -> None:
    """
    Write bridge state with no active turn so run_turn takes turn/start.

    :param tmp_path: Bridge directory.
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )


def _run_turn_with_config(
    executor: CodexNativeExecutor, text: str, config: ExecutorConfig
) -> None:
    """
    Drive one run_turn carrying a per-turn :class:`ExecutorConfig`.

    :param executor: Native Codex executor under test.
    :param text: User text to send.
    :param config: Per-turn config carrying model / reasoning effort.
    :returns: None.
    """

    async def run() -> None:
        """Consume the turn iterator, discarding events."""
        async for _event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            [],
            "",
            config,
        ):
            pass

    asyncio.run(run())


def test_web_model_pick_applied_via_thread_settings_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A web-picker model + reasoning effort apply via ``thread/settings/update``.

    A model/effort change made in the Omnigent web UI reaches the runner
    as ``ExecutorConfig.model`` / ``extra["reasoning_effort"]``. Codex's
    ``turn/start`` takes no model/effort (input/context only), so the
    override must ride a ``thread/settings/update`` request — whose
    ``ThreadSettingsUpdateParams`` carries ``model`` and ``effort`` — or the
    picker silently does nothing (#1256). The settings update precedes the
    bare turn so the change is in effect for it.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _start_state(tmp_path)
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    _run_turn_with_config(
        executor,
        "hello",
        ExecutorConfig(model="gpt-5.3-codex", extra={"reasoning_effort": "high"}),
    )

    assert _FakeCodexNativeClient.requests == [
        (
            "thread/settings/update",
            {
                "threadId": "thread_123",
                "model": "gpt-5.3-codex",
                "effort": "high",
            },
        ),
        (
            "turn/start",
            {
                "threadId": "thread_123",
                "input": [{"type": "text", "text": "hello"}],
            },
        ),
    ]


def test_no_settings_update_when_overrides_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    With no model/effort pinned, no ``thread/settings/update`` is sent.

    A native thread that never touches the web picker must keep its
    launch-pinned model — a stray ``thread/settings/update`` could
    clobber it. An empty/None config issues only the bare
    ``{threadId, input}`` ``turn/start``.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _start_state(tmp_path)
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    # A config with neither field set produces the bare turn/start params.
    _run_turn_with_config(executor, "a", ExecutorConfig())

    assert _FakeCodexNativeClient.requests == [
        ("turn/start", {"threadId": "thread_123", "input": [{"type": "text", "text": "a"}]}),
    ]


def test_settings_update_drops_invalid_effort_keeps_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An unsupported reasoning effort is dropped; the model still applies.

    A bad effort must not sink the turn (the bridge can't surface a
    validation error cleanly mid-dispatch), so it is logged and omitted
    while a valid model override still rides along on
    ``thread/settings/update``.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _start_state(tmp_path)
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    _run_turn_with_config(
        executor,
        "hi",
        ExecutorConfig(model="gpt-5.3-codex", extra={"reasoning_effort": "bogus"}),
    )

    method, params = _FakeCodexNativeClient.requests[0]
    assert method == "thread/settings/update"
    assert params["model"] == "gpt-5.3-codex"
    assert "effort" not in params


def test_run_turn_surfaces_recorded_startup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A missing bridge state surfaces the recorded startup cause, not the
    generic "bridge state is missing" (issue #59).
    """

    async def _no_sleep(_seconds: float) -> None:
        """No-op the poll backoff so the missing-state path is fast."""

    # asyncio.run does not depend on asyncio.sleep, so patching it is safe.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    write_bridge_startup_error(
        tmp_path,
        "Codex app-server never started a thread within the startup timeout.",
    )
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    events = _collect_turn_events(executor, "hello")

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert "never started" in error.message
    assert "startup timeout" in error.message
    assert error.message != "Codex native bridge state is missing"


# ── MCP startup: no client-side gate + Stop cancel (issue #2058) ────────


def _seed_bridge(tmp_path: Path, active_turn_id: str | None = None) -> None:
    """
    Write bridge state for the executor under test.

    :param tmp_path: Bridge directory.
    :param active_turn_id: Active turn id to seed, or ``None``.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=active_turn_id,
        ),
    )


def test_turn_start_is_not_gated_on_pending_mcp_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``turn/start`` dispatches immediately while MCP servers still boot.

    The Codex app-server accepts a mid-startup ``turn/start`` and defers
    its execution until the startup round settles (verified against codex
    0.142.5), so a client-side wait would only add latency — up to its
    full bound when a server hangs. The bounded ``asyncio.timeout`` fails
    this test if a gate sneaks back in.
    """
    from omnigent.codex_native_bridge import update_mcp_server_startup

    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _seed_bridge(tmp_path)
    update_mcp_server_startup(tmp_path, "storage-console", "starting")
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    async def run() -> list[Any]:
        """
        Drive one turn under a budget any startup gate would blow.

        :returns: Events yielded by the turn.
        """
        events: list[Any] = []
        async with asyncio.timeout(5.0):
            async for event in executor.run_turn(
                [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                [],
                "",
            ):
                events.append(event)
        return events

    events = asyncio.run(run())

    assert [type(event) for event in events] == [TurnComplete]
    assert [method for method, _ in _FakeCodexNativeClient.requests] == ["turn/start"]


def test_turn_error_names_pending_mcp_servers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A turn failure during MCP startup names the still-pending servers.

    An injection failure this early in the session's life is most often
    the startup itself; without the suffix the user sees a bare transport
    error and has no idea codex is still booting MCP servers.
    """
    from omnigent.codex_native_bridge import update_mcp_server_startup

    class _FailingTurnClient(_FakeCodexNativeClient):
        """Fake client whose ``turn/start`` fails like a mid-boot app-server."""

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            """
            Reject ``turn/start``; defer to the base fake otherwise.

            :param method: JSON-RPC method, e.g. ``"turn/start"``.
            :param params: JSON-RPC params.
            :returns: Codex-shaped response payload.
            """
            if method == "turn/start":
                raise RuntimeError("app-server hiccup")
            return await super().request(method, params)

    _FailingTurnClient.requests = []
    _FailingTurnClient.created = []
    _FailingTurnClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FailingTurnClient,
    )
    _seed_bridge(tmp_path)
    update_mcp_server_startup(tmp_path, "storage-console", "starting")
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    events = _collect_turn_events(executor, "hi")

    assert [type(event) for event in events] == [ExecutorError]
    assert "MCP startup still waiting on storage-console" in events[0].message


def test_interrupt_with_active_turn_and_pending_mcp_stops_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stop during a startup-deferred turn interrupts the turn AND the startup.

    Codex holds a mid-startup turn until the MCP round settles, so
    stopping only the turn would leave the user watching a startup they
    asked to stop. The startup interrupt (empty turn id) is sent first and
    best-effort, then the recorded turn is interrupted.
    """
    from omnigent.codex_native_bridge import read_mcp_startup, update_mcp_server_startup

    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _seed_bridge(tmp_path, active_turn_id="turn_active")
    update_mcp_server_startup(tmp_path, "storage-console", "starting")
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    interrupted = asyncio.run(executor.interrupt_session("key"))

    assert interrupted is True
    assert _FakeCodexNativeClient.requests == [
        ("turn/interrupt", {"threadId": "thread_123", "turnId": ""}),
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_active"}),
    ]
    assert read_mcp_startup(tmp_path)["storage-console"]["status"] == "cancelled"


def test_interrupt_with_no_active_turn_cancels_mcp_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stop during MCP startup cancels it instead of no-oping.

    With no active turn id recorded, ``interrupt_session`` used to return
    ``False`` and the user's Stop did nothing while Codex was wedged on a
    slow MCP server. It must flip the pending servers to ``cancelled``
    (unblocking the first-turn gate) and send Codex the TUI's startup
    interrupt: ``turn/interrupt`` with an empty turn id.
    """
    from omnigent.codex_native_bridge import read_mcp_startup, update_mcp_server_startup

    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _seed_bridge(tmp_path, active_turn_id=None)
    update_mcp_server_startup(tmp_path, "storage-console", "starting")
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    interrupted = asyncio.run(executor.interrupt_session("key"))

    assert interrupted is True
    assert _FakeCodexNativeClient.requests == [
        ("turn/interrupt", {"threadId": "thread_123", "turnId": ""})
    ]
    assert read_mcp_startup(tmp_path)["storage-console"]["status"] == "cancelled"


def test_interrupt_with_no_active_turn_and_no_pending_mcp_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stop with nothing running and nothing starting stays a no-op.

    An idle session must not send spurious ``turn/interrupt`` requests to
    the app-server on every Stop press.
    """
    _FakeCodexNativeClient.requests = []
    _FakeCodexNativeClient.created = []
    _FakeCodexNativeClient.next_turn = 1
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        _FakeCodexNativeClient,
    )
    _seed_bridge(tmp_path, active_turn_id=None)
    executor = CodexNativeExecutor(bridge_dir=tmp_path)

    interrupted = asyncio.run(executor.interrupt_session("key"))

    assert interrupted is False
    assert _FakeCodexNativeClient.requests == []
