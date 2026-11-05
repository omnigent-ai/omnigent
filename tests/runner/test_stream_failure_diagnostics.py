"""Edge cases of the runner's composed harness stream-failure message.

When ``proxy_stream``'s harness stream dies mid-turn, the runner composes the
user-facing ``response.failed`` message from the real transport cause plus a
bounded live-terminal pane snapshot (a ``Last captured terminal output:``
block the web UI renders as diagnostics). These tests pin the composition's
edges at the runner's HTTP boundary (``POST /v1/sessions/{id}/events``):

- a cause-less exception keeps the plain headline (no dangling colon) while
  the pane snapshot still attaches;
- a blank pane snapshot attaches no diagnostics block;
- a pane read that raises never masks the failure event itself;
- an oversized pane snapshot is bounded by the shared trim budget;
- a drop caused by the runner's own required-terminal release reports that
  exit, while an exit recorded before the turn is not blamed for its drop;
- a pane Omnigent closed at its delivery timeout reports that timeout and the
  pane's output when the release severs the stream first, and the release
  waits for the turn's own error before it does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.resource_registry import TerminalExitEvent, TerminalLifecycle
from omnigent.server.schemas import CancelledEvent
from omnigent.spec.types import AgentSpec
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_CONV_ID = "ac1dbeef245985f541fd686eb2a32b73"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"


class _StreamErrorHarnessClient(_ScriptedHarnessClient):
    """Harness client that emits its scripted frames, then drops mid-stream."""

    def __init__(
        self,
        sse_frames: list[str],
        *,
        cause: str,
        before_drop: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(sse_frames)
        self._cause = cause
        self._before_drop = before_drop

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream errors after the frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames
        cause = self._cause
        before_drop = self._before_drop

        class _ErrCtx:
            status_code = 200

            async def __aenter__(self) -> _StreamErrorHarnessClient._ErrHandle:
                return _StreamErrorHarnessClient._ErrHandle(frames, cause, before_drop)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ErrCtx()

    class _ErrHandle:
        """Stream handle that raises ``ReadError`` after yielding its frames."""

        status_code = 200

        def __init__(
            self, frames: list[str], cause: str, before_drop: Callable[[], None] | None
        ) -> None:
            self._frames = frames
            self._cause = cause
            self._before_drop = before_drop

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            # What severs the channel (e.g. the runner releasing the harness)
            # happens while the runner is parked on this read.
            if self._before_drop is not None:
                self._before_drop()
            raise httpx.ReadError(self._cause)


def _make_app(
    *,
    cause: str,
    terminal_registry: TerminalRegistry | None = None,
    frames: list[str] | None = None,
    before_drop: Callable[[], None] | None = None,
) -> Any:
    """Build a runner app whose harness stream drops with *cause* mid-turn."""
    harness_client = _StreamErrorHarnessClient(
        frames
        if frames is not None
        else [
            _sse(
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_drop",
                        "model": "plain-agent",
                        "created_at": 1,
                        "status": "in_progress",
                    },
                }
            )
        ],
        cause=cause,
        before_drop=before_drop,
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )


def _register_terminal(registry: TerminalRegistry, conv_id: str, instance: Any) -> None:
    """Seed a live instance for *conv_id* (private-attr test convention, no tmux)."""
    registry._by_conversation.setdefault(conv_id, {})[("bash", "main")] = instance


async def _streamed_events(app: Any, conv_id: str) -> list[dict[str, Any]]:
    """Drive one streamed turn through the runner's HTTP boundary."""
    transport = httpx.ASGITransport(app=app)
    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
            for block in buf.split("\n\n"):
                for line in block.strip().splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        with contextlib.suppress(json.JSONDecodeError):
                            events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _failed_event_message(app: Any, conv_id: str) -> tuple[dict[str, Any], str]:
    """Drive one failing streamed turn; return (failed event, error message)."""
    events = await _streamed_events(app, conv_id)
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(failed) == 1, f"expected exactly one response.failed event, got {events}"
    message = failed[0].get("error", {}).get("message", "")
    assert isinstance(message, str)
    return failed[0], message


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [None, "resp_drop"])
async def test_stream_failure_logs_harness_and_response(
    response_id: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures identify their response only when the harness supplied one."""
    frames = (
        [_sse({"type": "response.created", "response": {"id": response_id}})]
        if response_id is not None
        else []
    )
    app = _make_app(cause="private transport detail", frames=frames)
    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        failed, _ = await _failed_event_message(app, _CONV_ID)

    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "harness_stream_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.session_id == _CONV_ID
    assert record.attributes == {
        "harness": "openai-agents",
        "response_id": response_id,
        # Carried so the transport cause is groupable even when the exception
        # itself has no message.
        "exception_type": "ReadError",
    }
    assert record.exc_info is not None
    assert failed["error"]["code"] == "connection_error"


@pytest.mark.asyncio
async def test_causeless_failure_names_the_exception_type_and_attaches_pane(
    tmp_path: Path,
) -> None:
    """An exception with empty text falls back to naming its type.

    The headline previously degraded to the bare sentence, so every messageless
    transport failure (httpx raises ``ReadError()`` with no text) collapsed into
    one indistinguishable signature. Naming the type keeps the original intent —
    never a dangling ``error: `` — while saying which transport failure it was.
    """
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    instance._remember_pane_snapshot("Trust this folder?\n> ")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="", terminal_registry=registry)
    _, message = await _failed_event_message(app, _CONV_ID)

    first_line = message.splitlines()[0]
    # No cause text, so the type stands in — never a dangling "error: ".
    assert first_line == "Harness stream connection error: ReadError", message
    # The live pane is still the most diagnostic thing available -- attached.
    assert "Last captured terminal output:" in message, message
    assert "Trust this folder?" in message, message


@pytest.mark.asyncio
async def test_blank_pane_snapshot_attaches_no_diagnostics_block(tmp_path: Path) -> None:
    """A whitespace-only pane adds no block; the cause still reaches the user."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    instance._remember_pane_snapshot("   \n\t\n ")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="ReadError(ClosedResourceError())", terminal_registry=registry)
    failed, message = await _failed_event_message(app, _CONV_ID)

    assert failed["error"].get("code") == "connection_error", failed
    assert "ReadError(ClosedResourceError())" in message, message
    assert "last captured" not in message.lower(), message


@pytest.mark.asyncio
async def test_pane_read_error_does_not_mask_the_failure_event(tmp_path: Path) -> None:
    """A raising pane read is swallowed; the failure event still carries the cause."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)

    def _boom() -> str:
        raise RuntimeError("tmux went away")

    instance.last_pane_text = _boom  # type: ignore[method-assign]
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="stream dropped mid-turn", terminal_registry=registry)
    failed, message = await _failed_event_message(app, _CONV_ID)

    assert failed["error"].get("code") == "connection_error", failed
    assert "stream dropped mid-turn" in message, message
    assert "last captured" not in message.lower(), message


@pytest.mark.asyncio
async def test_oversized_pane_snapshot_is_bounded(tmp_path: Path) -> None:
    """A huge pane is trimmed to the shared diagnostics budget, tail preserved."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    filler = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    instance._remember_pane_snapshot(filler + "\nfinal prompt line")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="stream dropped mid-turn", terminal_registry=registry)
    _, message = await _failed_event_message(app, _CONV_ID)

    assert "Last captured terminal output:" in message, message
    _, _, pane_block = message.partition("Last captured terminal output:\n")
    assert pane_block.startswith("... omitted "), pane_block[:120]
    assert pane_block.endswith("final prompt line"), pane_block[-120:]
    # Bounded by the shared trim budget (40 lines / 4000 chars + marker slack).
    assert len(pane_block) <= 4100, len(pane_block)


def _required_terminal_exit(
    session_was_idle: bool, expected_close: bool = False, closed_outside_omnigent: bool = False
) -> TerminalExitEvent:
    """Build the exit the registry publishes when the session's Claude pane dies."""
    return TerminalExitEvent(
        session_id=_CONV_ID,
        terminal_id="terminal_claude_main",
        terminal_name="claude",
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        command="claude",
        args_count=2,
        cwd="/work",
        last_output="Waiting for the API key helper...\n",
        session_was_idle=session_was_idle,
        expected_close=expected_close,
        closed_outside_omnigent=closed_outside_omnigent,
    )


def _failed_statuses(conv_id: str) -> list[dict[str, Any]]:
    """Drain and return the ``session.status: failed`` events published for *conv_id*."""
    from omnigent.runner.app import _session_event_queues_ref

    queue = _session_event_queues_ref.get(conv_id)
    statuses: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        item = queue.get_nowait()
        if (
            isinstance(item, dict)
            and item.get("type") == "session.status"
            and item.get("status") == "failed"
        ):
            statuses.append(item)
    return statuses


@pytest.mark.asyncio
async def test_expected_close_of_the_pane_publishes_no_terminal_failure() -> None:
    """A pane Omnigent closed to fail its turn adds no terminal-exit failure.

    The delivery timeout's own error fails the turn. The exit that the reap
    causes must not publish a second ``session.status: failed``, which would
    show the person two failure banners and make the terminal exit the
    first error.
    """
    from omnigent.runner.app import _session_event_queues_ref

    _session_event_queues_ref.pop(_CONV_ID, None)
    app = _make_app(cause="unused")
    publish = app.state.session_resource_registry._terminal_exit_publisher
    try:
        publish(_required_terminal_exit(session_was_idle=False, expected_close=True))
        statuses = _failed_statuses(_CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert statuses == []


@pytest.mark.asyncio
async def test_session_killed_outside_omnigent_names_that_cause() -> None:
    """A Claude session killed outside Omnigent fails with that cause, not a bare exit."""
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.launch_failure import TERMINAL_CLOSED_OUTSIDE_OMNIGENT

    _session_event_queues_ref.pop(_CONV_ID, None)
    app = _make_app(cause="unused")
    publish = app.state.session_resource_registry._terminal_exit_publisher
    try:
        publish(_required_terminal_exit(session_was_idle=False, closed_outside_omnigent=True))
        statuses = _failed_statuses(_CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert len(statuses) == 1
    error = statuses[0]["error"]
    assert error["code"] == "required_terminal_exited"
    assert error["title"] == TERMINAL_CLOSED_OUTSIDE_OMNIGENT.title
    assert error["message"].startswith(TERMINAL_CLOSED_OUTSIDE_OMNIGENT.title)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_was_idle", [False, True])
async def test_stream_drop_after_required_terminal_exit_reports_the_exit(
    session_was_idle: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A drop caused by the runner's own harness release names the terminal exit.

    When the pane dies mid-delivery, the terminal-exit handler releases the
    harness subprocess, which closes the client ``proxy_stream`` is reading and
    raises ``ReadError`` there. That surfaced as a generic connection error --
    the only failure a sub-agent whose pane never became ready ever showed, since
    a pane that never ran a turn exits with ``session_was_idle`` and the exit
    handler itself publishes nothing. The stream's failure must carry the exit's
    diagnostics instead, and the turn must still surface as failed exactly that way.
    """
    from omnigent.runner.app import _session_event_queues_ref

    # Earlier tests in this module publish to the same conversation's queue.
    _session_event_queues_ref.pop(_CONV_ID, None)
    publish: dict[str, Callable[[TerminalExitEvent], None]] = {}
    app = _make_app(
        cause="ReadError(ClosedResourceError())",
        before_drop=lambda: publish["exit"](_required_terminal_exit(session_was_idle)),
    )
    publish["exit"] = app.state.session_resource_registry._terminal_exit_publisher
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            failed, message = await _failed_event_message(app, _CONV_ID)
        statuses = _failed_statuses(_CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert failed["error"]["code"] == "required_terminal_exited", failed
    assert "Required terminal exited unexpectedly" in message, message
    assert "Waiting for the API key helper" in message, message
    assert "Harness stream connection error" not in message, message
    assert statuses, "the turn must still surface as failed"
    assert {s["error"]["code"] for s in statuses} == {"required_terminal_exited"}, statuses
    assert [
        r for r in caplog.records if getattr(r, "event_name", None) == "harness_stream_failed"
    ] == []
    ended = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "harness_stream_ended_by_terminal_exit"
    ]
    assert len(ended) == 1, caplog.text
    assert ended[0].attributes["exception_type"] == "ReadError"


class _ExitBeforeFailureHarnessClient(_ScriptedHarnessClient):
    """Harness client whose pane dies between its first frame and the rest."""

    def __init__(self, frames: list[str], on_exit: Callable[[], None]) -> None:
        super().__init__(frames)
        self._on_exit = on_exit

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Stream the first frame, run *on_exit*, then stream the rest."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames, on_exit = self._sse_frames, self._on_exit

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield frames[0]
                on_exit()
                for frame in frames[1:]:
                    yield frame

        class _Ctx:
            async def __aenter__(self) -> _Handle:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()


@pytest.mark.asyncio
async def test_pane_death_before_the_message_is_typed_fails_only_the_turn() -> None:
    """A Claude pane that dies while the message waits for its prompt reports one failure.

    The exit arrives while the turn is live; the turn then ends saying nothing
    was typed, and the server sends the message again. The terminal exit must
    not publish its own ``required_terminal_exited`` failure, which is the
    session failure the re-send exists to avoid.
    """
    from omnigent.native.input_diagnostics import TERMINAL_EXITED_BEFORE_DELIVERY_CODE
    from omnigent.runner.app import _session_event_queues_ref
    from tests._helpers.polling import poll_until_async

    _session_event_queues_ref.pop(_CONV_ID, None)
    publish: dict[str, Callable[[TerminalExitEvent], None]] = {}
    harness_client = _ExitBeforeFailureHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_waiting"}}),
            _sse(
                {
                    "type": "response.failed",
                    "response": {"id": "resp_waiting"},
                    "error": {
                        "code": TERMINAL_EXITED_BEFORE_DELIVERY_CODE,
                        "message": "The Claude Code terminal has exited before delivery.",
                        "undelivered": True,
                    },
                }
            ),
        ],
        on_exit=lambda: publish["exit"](_required_terminal_exit(session_was_idle=False)),
    )
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    publish["exit"] = app.state.session_resource_registry._terminal_exit_publisher
    try:
        failed, _ = await _failed_event_message(app, _CONV_ID)
        statuses: list[dict[str, Any]] = []

        def read_statuses() -> list[dict[str, Any]]:
            nonlocal statuses
            statuses = _failed_statuses(_CONV_ID)
            return statuses

        await poll_until_async(read_statuses, timeout_s=2)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert failed["error"]["code"] == TERMINAL_EXITED_BEFORE_DELIVERY_CODE
    assert [s["error"]["code"] for s in statuses] == [TERMINAL_EXITED_BEFORE_DELIVERY_CODE]


@pytest.mark.asyncio
async def test_stream_drop_after_an_expected_close_reports_the_delivery_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drop caused by releasing a pane Omnigent closed names that close, with the pane.

    The claude-native executor reaps a pane whose input box never rendered and
    then reports the readiness timeout. Releasing the harness subprocess 2 s
    after the exit could sever the stream before that report arrived, and the
    turn surfaced as ``connection_error`` / ``Harness stream connection error:
    ReadError`` with no pane: the launcher prompt that held the terminal was
    invisible to the person and to the failure's classification.
    """
    from omnigent.runner.app import _session_event_queues_ref

    _session_event_queues_ref.pop(_CONV_ID, None)
    publish: dict[str, Callable[[TerminalExitEvent], None]] = {}
    app = _make_app(
        cause="",
        before_drop=lambda: publish["exit"](
            _required_terminal_exit(session_was_idle=False, expected_close=True)
        ),
    )
    publish["exit"] = app.state.session_resource_registry._terminal_exit_publisher
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            failed, message = await _failed_event_message(app, _CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert failed["error"]["code"] == "RuntimeError", failed
    assert message.startswith(
        "Claude Code terminal did not become ready before the message delivery timed out"
    ), message
    assert "Last captured terminal output:\nWaiting for the API key helper" in message, message
    assert "Harness stream connection error" not in message, message
    assert [
        r for r in caplog.records if getattr(r, "event_name", None) == "harness_stream_failed"
    ] == []


class _LateFailureHarnessClient(_ScriptedHarnessClient):
    """Harness client whose own failure frame arrives a moment after the pane exit."""

    def __init__(self, order: list[str], on_exit: Callable[[], None], delay_s: float) -> None:
        super().__init__([])
        self._order = order
        self._on_exit = on_exit
        self._delay_s = delay_s

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        order, on_exit, delay_s = self._order, self._on_exit, self._delay_s

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> _LateFailureHarnessClient._Handle:
                return _LateFailureHarnessClient._Handle(order, on_exit, delay_s)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        status_code = 200

        def __init__(self, order: list[str], on_exit: Callable[[], None], delay_s: float) -> None:
            self._order, self._on_exit, self._delay_s = order, on_exit, delay_s

        async def aiter_text(self) -> AsyncIterator[str]:
            yield _sse({"type": "response.created", "response": {"id": "resp_late"}})
            self._on_exit()
            # The executor is still composing its readiness error after the reap.
            await asyncio.sleep(self._delay_s)
            self._order.append("failure_frame")
            yield _sse(
                {
                    "type": "response.failed",
                    "response": {"id": "resp_late", "status": "failed"},
                    "error": {"code": "RuntimeError", "message": "inner executor error: late"},
                }
            )


@pytest.mark.asyncio
async def test_expected_close_release_waits_for_the_turns_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness is released only once the turn's own failure has been streamed.

    The plain terminal-exit grace is short; a pane Omnigent closed itself waits
    longer, so the executor's readiness error, not the release, ends the stream.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref

    monkeypatch.setattr(runner_app, "_TERMINAL_EXIT_RELEASE_GRACE_S", 0.01)
    monkeypatch.setattr(runner_app, "_EXPECTED_CLOSE_RELEASE_GRACE_S", 5.0, raising=False)
    _session_event_queues_ref.pop(_CONV_ID, None)
    order: list[str] = []
    publish: dict[str, Callable[[TerminalExitEvent], None]] = {}
    client = _LateFailureHarnessClient(
        order,
        on_exit=lambda: publish["exit"](
            _required_terminal_exit(session_was_idle=False, expected_close=True)
        ),
        delay_s=0.3,
    )
    pm = _FakeProcessManager(client)
    original_release = pm.release

    async def _release(conversation_id: str, *args: Any, **kwargs: Any) -> Any:
        order.append("release")
        return await original_release(conversation_id, *args, **kwargs)

    pm.release = _release  # type: ignore[method-assign]
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    publish["exit"] = app.state.session_resource_registry._terminal_exit_publisher
    try:
        events = await _streamed_events(app, _CONV_ID)
        for _ in range(100):
            if "release" in order:
                break
            await asyncio.sleep(0.02)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    failed = [e for e in events if e.get("type") == "response.failed"]
    assert [e["error"]["message"] for e in failed] == ["inner executor error: late"], events
    assert order == ["failure_frame", "release"], order


@pytest.mark.asyncio
@pytest.mark.parametrize("voluntary", [False, True])
async def test_earlier_terminal_exit_does_not_relabel_a_later_stream_drop(
    voluntary: bool,
) -> None:
    """An exit recorded before the turn started is not blamed for its drop."""
    from omnigent.runner.app import _session_event_queues_ref

    app = _make_app(cause="stream dropped mid-turn")
    exit_event = _required_terminal_exit(True)
    if voluntary:
        exit_event = replace(
            exit_event,
            exit_status=0,
            last_output="Resume this session with:\nclaude --resume earlier-session",
        )
    app.state.session_resource_registry._terminal_exit_publisher(exit_event)
    try:
        failed, message = await _failed_event_message(app, _CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert failed["error"]["code"] == "connection_error", failed
    assert "stream dropped mid-turn" in message, message


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_name", ["claude", "qwen", "antigravity"])
@pytest.mark.parametrize("tracked_child", [False, True])
@pytest.mark.parametrize("response_created", [False, True])
async def test_clean_terminal_exit_cancels_its_live_stream(
    terminal_name: str, tracked_child: bool, response_created: bool
) -> None:
    """Releasing a voluntarily exited pane must not turn its stream into a failure."""
    from omnigent.runner.app import _session_event_queues_ref

    _session_event_queues_ref.pop(_CONV_ID, None)
    publish: dict[str, Callable[[TerminalExitEvent], None]] = {}
    exit_event = replace(
        _required_terminal_exit(session_was_idle=False),
        terminal_name=terminal_name,
        terminal_id=f"terminal_{terminal_name}_main",
        exit_status=0,
        last_output=(
            "Resume this session with:\nclaude --resume synthetic-session"
            if terminal_name == "claude"
            else "Goodbye!"
        ),
    )
    app = _make_app(
        cause="stream closed by harness release",
        frames=None if response_created else [],
        before_drop=lambda: publish["exit"](exit_event),
    )
    publish["exit"] = app.state.session_resource_registry._terminal_exit_publisher
    work = None
    if tracked_child:
        work = subagent_work.register_subagent_work(
            parent_session_id="synthetic-parent",
            child_session_id=_CONV_ID,
            agent="worker",
            title="worker",
        )
        subagent_work._session_inboxes_ref["synthetic-parent"] = asyncio.Queue()
    try:
        events = await _streamed_events(app, _CONV_ID)
        statuses = _failed_statuses(_CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)
        subagent_work.unregister_subagent_work(_CONV_ID)
        subagent_work._session_inboxes_ref.pop("synthetic-parent", None)

    assert [event for event in events if event.get("type") == "response.failed"] == []
    cancelled = [event for event in events if event.get("type") == "response.cancelled"]
    assert len(cancelled) == int(response_created)
    if response_created:
        response = CancelledEvent.model_validate(cancelled[0]).response
        assert response.id == "resp_drop"
        assert response.status == "cancelled"
        assert response.model == "plain-agent"
        assert response.created_at == 1
    assert statuses == []
    assert _CONV_ID not in app.state.live_response_id
    assert _CONV_ID not in app.state.required_terminal_exit_errors
    if work is not None:
        assert work.status == "cancelled"
