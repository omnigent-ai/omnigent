"""A person quitting their native agent settles the session idle instead of failing it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.app import _response_cancelled_payload, _session_event_queues_ref
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.server.schemas import CancelledEvent
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_READY = {"terminal_input_ready_at": "1759766400.5"}
# A tmux capture pads the screen with blank rows, so the tail can lack Claude's exit banner.
_PADDED_TAIL = (
    "PRIVATE PANE TEXT\n" + "\n" * 120 + "Pane is dead (status 0, Mon Oct  5 16:37:57 2026)"
)
_COMPOSER_PANE = "────────────────────\n❯ \n────────────────────"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_HARNESS = {"claude": "claude-native", "pi": "pi-native"}


class _RecordingServerClient(NullServerClient):
    """Server client that keeps every conversation item the runner persists.

    A voluntary exit must persist none: an ``error``-type item is counted as a failure
    whatever its level.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            self.items.append(body["data"]["item_data"])
        return self._Response()


@dataclass
class _Outcome:
    statuses: list[dict[str, Any]]
    events: list[dict[str, Any]]
    items: list[dict[str, Any]]
    released: list[str]
    classified: list[logging.LogRecord] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return {"type": "session.status", "status": "idle"} in self.statuses

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [status for status in self.statuses if status.get("status") == "failed"]


def _exit_event(
    terminal_name: str,
    *,
    exit_status: int | None = None,
    exit_signal: str | None = None,
    session_end_reason: str | None = None,
    session_end_signal: str | None = None,
    session_end_evidence: str | None = None,
    interactive: bool = True,
    last_output: str | None = _PADDED_TAIL,
    session_was_idle: bool = False,
    session_id: str | None = None,
    command: str | None = None,
) -> TerminalExitEvent:
    context = dict(_READY) if interactive else {}
    for key, value in (
        ("terminal_exit_signal", exit_signal),
        ("claude_session_end_reason", session_end_reason),
        ("claude_session_end_signal", session_end_signal),
        ("claude_session_end_evidence", session_end_evidence),
    ):
        if value is not None:
            context[key] = value
    return TerminalExitEvent(
        session_id=session_id or uuid.uuid4().hex,
        terminal_id=f"terminal_{terminal_name}_main",
        terminal_name=terminal_name,
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        command=command or terminal_name,
        last_output=last_output,
        exit_status=exit_status,
        session_was_idle=session_was_idle,
        lifecycle_context=context,
    )


@contextlib.contextmanager
def _tracked_subagent(child_id: str) -> Iterator[asyncio.Queue[dict[str, Any]]]:
    """Register *child_id* as a dispatched sub-agent; yield its parent's inbox."""
    parent_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    subagent_work.register_child_session(
        child_id, parent_session_id=parent_id, title="pi:main", tool="pi", session_name="main"
    )
    subagent_work.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="pi", title="main"
    )
    try:
        yield inbox
    finally:
        _session_event_queues_ref.pop(parent_id, None)
        subagent_work.unregister_subagent_work(child_id)
        subagent_work.unregister_child_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


async def _publish_exit(event: TerminalExitEvent, caplog: pytest.LogCaptureFixture) -> _Outcome:
    """Drive the runner's terminal-exit publisher and collect what it emitted."""
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    conv_id = event.session_id
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    process_manager._sessions.add(conv_id)
    server = _RecordingServerClient()
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )
    publish_exit = app.state.session_resource_registry._terminal_exit_publisher
    assert callable(publish_exit)
    events: list[dict[str, Any]] = []
    try:
        publish_exit(event)
        for _ in range(1000):
            events.extend(_drain_session_event_queue(_session_event_queues_ref.get(conv_id)))
            if process_manager.released:
                break
            await asyncio.sleep(0)
        # Let any fire-and-forget work the exit scheduled land.
        for _ in range(50):
            await asyncio.sleep(0)
        events.extend(_drain_session_event_queue(_session_event_queues_ref.get(conv_id)))
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        subagent_work.unregister_child_session(conv_id)
    return _Outcome(
        statuses=[item for item in events if item.get("type") == "session.status"],
        events=events,
        items=server.items,
        released=process_manager.released,
        classified=[
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "native_terminal_exit_classified"
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "rule"),
    [
        pytest.param(
            _exit_event("claude", session_end_reason="prompt_input_exit"),
            "session_end_reason",
            id="padded-tail-without-banner-and-prompt-input-exit",
        ),
        pytest.param(_exit_event("claude", exit_status=0), "exit_zero", id="claude-exit-0"),
        pytest.param(_exit_event("pi", exit_status=0), "exit_zero", id="pi-exit-0"),
        pytest.param(_exit_event("claude", exit_signal="SIGINT"), "user_signal", id="ctrl-c"),
        pytest.param(_exit_event("pi", exit_signal="SIGHUP"), "user_signal", id="hangup"),
        pytest.param(
            _exit_event("claude", exit_status=0, session_was_idle=True),
            "exit_zero",
            id="already-idle",
        ),
    ],
)
async def test_person_quitting_goes_idle_without_persisting_an_item(
    event: TerminalExitEvent, rule: str, caplog: pytest.LogCaptureFixture
) -> None:
    outcome = await _publish_exit(event, caplog)

    assert outcome.idle
    assert outcome.failed == []
    assert outcome.released == [event.session_id]
    assert {
        "type": "session.resource.deleted",
        "resource_id": event.terminal_id,
        "resource_type": "terminal",
        "session_id": event.session_id,
    } in outcome.events
    # No conversation item at all: an error-type one would be counted as a failure.
    assert outcome.items == []
    [record] = outcome.classified
    assert record.attributes["decision"] == "voluntary"
    assert record.attributes["rule"] == rule
    assert record.session_id == event.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "rule"),
    [
        pytest.param(_exit_event("claude", exit_signal="SIGKILL"), "fatal_signal", id="sigkill"),
        pytest.param(_exit_event("pi", exit_signal="SIGSEGV"), "fatal_signal", id="sigsegv"),
        pytest.param(_exit_event("pi", exit_status=1), "nonzero_exit", id="pi-exit-1"),
        pytest.param(
            _exit_event("claude", exit_status=129), "nonzero_exit", id="claude-sighup-129"
        ),
        pytest.param(
            _exit_event("claude", exit_status=143), "nonzero_exit", id="claude-sigterm-143"
        ),
        pytest.param(
            _exit_event("claude", exit_status=1, session_end_reason="prompt_input_exit"),
            "nonzero_exit",
            id="quit-reason-with-failing-status",
        ),
        pytest.param(
            _exit_event("claude", exit_status=0, session_end_signal="SIGTERM"),
            "external_signal",
            id="hook-sigterm-with-exit-0",
        ),
        pytest.param(
            _exit_event("claude", exit_status=0, session_end_signal="SIGQUIT"),
            "external_signal",
            id="hook-sigquit-with-exit-0",
        ),
        pytest.param(
            _exit_event("claude", session_end_reason="session_close"),
            "no_quit_evidence",
            id="session-close-without-status",
        ),
        pytest.param(
            _exit_event("claude", session_end_reason="signal"),
            "no_quit_evidence",
            id="signal-reason-without-status",
        ),
        pytest.param(
            _exit_event("claude", exit_status=0, interactive=False),
            "not_interactive",
            id="claude-exit-0-before-interactive",
        ),
        pytest.param(
            _exit_event("pi", exit_status=0, interactive=False),
            "not_interactive",
            id="pi-exit-0-before-interactive",
        ),
        pytest.param(
            _exit_event("claude", exit_signal="SIGINT", interactive=False),
            "not_interactive",
            id="ctrl-c-before-interactive",
        ),
        pytest.param(_exit_event("claude"), "no_quit_evidence", id="no-evidence"),
    ],
)
async def test_crash_or_launch_time_exit_still_fails_the_turn(
    event: TerminalExitEvent, rule: str, caplog: pytest.LogCaptureFixture
) -> None:
    outcome = await _publish_exit(event, caplog)

    [failed] = outcome.failed
    assert failed["error"]["code"] == "required_terminal_exited"
    assert not outcome.idle
    assert outcome.items == []
    assert outcome.released == [event.session_id]
    [record] = outcome.classified
    assert record.attributes["decision"] == "failed"
    assert record.attributes["rule"] == rule


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "code", "title"),
    [
        pytest.param(
            " Warning: No models available. Use /login to log into a provider via OAuth.",
            "pi_no_models",
            "Pi has no model to use",
            id="no-models",
        ),
        pytest.param(
            'ui.js": Failed to load extension: registry is unavailable\n'
            'Hint: Start without extensions using "pi -ne".',
            "pi_extension_load_failed",
            "Pi couldn't load an extension",
            id="extension",
        ),
        pytest.param(
            'Error: Model "acme/model-x" not found. Use --list-models to see available models.',
            "pi_model_not_found",
            "Pi doesn't know the selected model",
            id="model-not-found",
        ),
    ],
)
async def test_pi_configuration_exit_fails_with_a_specific_diagnosis(
    output: str, code: str, title: str, caplog: pytest.LogCaptureFixture
) -> None:
    event = _exit_event("pi", exit_status=1, last_output=output)

    outcome = await _publish_exit(event, caplog)

    [failed] = outcome.failed
    error = failed["error"]
    assert error["code"] == "required_terminal_exited"
    assert error["title"] == title
    assert error["remediation"]
    assert error["message"].startswith(title)
    [exit_record] = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "required_terminal_exited"
    ]
    assert exit_record.attributes["error_category"] == "config"
    assert exit_record.attributes["diagnosis_code"] == code


@pytest.mark.asyncio
async def test_already_idle_exit_without_quit_evidence_stays_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _exit_event("claude", session_was_idle=True)

    outcome = await _publish_exit(event, caplog)

    assert outcome.failed == []
    assert not outcome.idle
    assert outcome.items == []
    assert outcome.released == [event.session_id]


@pytest.mark.asyncio
async def test_classification_event_names_the_evidence_but_not_the_pane(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _exit_event(
        "claude",
        session_end_reason="prompt_input_exit",
        session_end_evidence="claude_hook",
        exit_signal="SIGINT",
        command="/usr/bin/env",
    )

    outcome = await _publish_exit(event, caplog)

    [record] = outcome.classified
    assert {k: v for k, v in record.attributes.items() if v is not None} == {
        "harness": "claude-native",
        # Basename only: the launcher wrapper is the clue, its path is not.
        "command": "env",
        "signal": "SIGINT",
        "session_end_reason": "prompt_input_exit",
        "session_end_evidence": "claude_hook",
        "banner_seen": False,
        "interactive": True,
        "decision": "voluntary",
        "rule": "session_end_reason",
    }
    assert "PRIVATE PANE TEXT" not in record.getMessage()
    assert "PRIVATE PANE TEXT" not in str(record.attributes)


@pytest.mark.asyncio
async def test_runner_shutdown_exit_is_neither_a_quit_nor_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    event = _exit_event("claude", exit_status=0)
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    process_manager._sessions.add(event.session_id)
    server = _RecordingServerClient()
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )
    app.state.shutting_down.set()
    try:
        app.state.session_resource_registry._terminal_exit_publisher(event)
        for _ in range(1000):
            if process_manager.released:
                break
            await asyncio.sleep(0)
        events = _drain_session_event_queue(_session_event_queues_ref.get(event.session_id))
    finally:
        _session_event_queues_ref.pop(event.session_id, None)

    assert [e for e in events if e.get("type") == "session.status"] == []
    assert server.items == []
    assert process_manager.released == [event.session_id]


# --- a turn stream that is still open when the person quits -------------------


class _OpenTurnClient(_ScriptedHarnessClient):
    """Harness client whose turn stream stays open until the runner releases the harness."""

    def __init__(self, released: Callable[[], bool]) -> None:
        super().__init__([_sse({"type": "response.created", "response": {"id": "resp_live"}})])
        self._released = released

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        frames, released = self._sse_frames, self._released

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                for frame in frames:
                    yield frame
                # Releasing the harness is what severs the stream the runner reads.
                while not released():
                    await asyncio.sleep(0)
                raise httpx.ReadError("harness released")

        class _Context:
            status_code = 200

            async def __aenter__(self) -> _Handle:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Context()


class _DroppedTurnClient(_ScriptedHarnessClient):
    """Harness client whose turn stream drops with an unrelated transport error."""

    def __init__(self) -> None:
        super().__init__([_sse({"type": "response.created", "response": {"id": "resp_next"}})])

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                for frame in frames:
                    yield frame
                raise httpx.ReadError("connection reset by peer")

        class _Context:
            status_code = 200

            async def __aenter__(self) -> _Handle:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Context()


async def _plain_spec(agent_id: str, session_id: str | None = None) -> AgentSpec:
    del agent_id, session_id
    return AgentSpec(spec_version=1, name="plain-agent")


async def _stream_turn(app: Any, conv_id: str, harness: str) -> list[dict[str, Any]]:
    """POST one streamed turn, run on *harness*, and return the SSE events it produced."""
    body = ""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        async with client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": harness,
                "harness_override": harness,
            },
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            async for chunk in resp.aiter_text():
                body += chunk
    return [
        json.loads(line[len("data:") :].strip())
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:")
    ]


async def _wait_for_live_turn(app: Any, conv_id: str) -> None:
    for _ in range(2000):
        if conv_id in app.state.live_response_id:
            return
        await asyncio.sleep(0)
    raise AssertionError("the turn stream never went live")


@dataclass
class _LiveTurnOutcome:
    stream_events: list[dict[str, Any]]
    published: list[dict[str, Any]]
    items: list[dict[str, Any]]
    released: list[str]
    live_markers_left: bool
    records: list[logging.LogRecord]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.published

    def types(self, events: list[dict[str, Any]]) -> list[str]:
        return [str(event.get("type")) for event in events]

    def statuses(self) -> list[str]:
        return [
            str(event.get("status"))
            for event in self.published
            if event["type"] == "session.status"
        ]

    def failed_errors(self) -> list[dict[str, Any]]:
        return [
            event["error"]
            for event in self.published
            if event["type"] == "session.status" and event.get("status") == "failed"
        ]

    def logged(self, event_name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event_name", None) == event_name]


async def _quit_during_live_turn(
    event: TerminalExitEvent, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> _LiveTurnOutcome:
    """Publish *event* while a native-harness turn stream is open; the release severs it."""
    monkeypatch.setattr("omnigent.runner.app._TERMINAL_EXIT_RELEASE_GRACE_S", 0.05)
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    conv_id = event.session_id
    released: list[str] = []
    process_manager = _FakeProcessManager(_OpenTurnClient(lambda: bool(process_manager.released)))
    process_manager._sessions.add(conv_id)
    server = _RecordingServerClient()
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        spec_resolver=_plain_spec,
        server_client=server,  # type: ignore[arg-type]
    )
    _session_event_queues_ref.pop(conv_id, None)
    try:
        turn = asyncio.create_task(_stream_turn(app, conv_id, _HARNESS[event.terminal_name]))
        await _wait_for_live_turn(app, conv_id)
        app.state.session_resource_registry._terminal_exit_publisher(event)
        stream_events = await asyncio.wait_for(turn, timeout=10)
        for _ in range(50):
            await asyncio.sleep(0)
        published = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
        live_markers_left = (
            conv_id in app.state.live_response_id or conv_id in app.state.active_turns
        )
        released = list(process_manager.released)
    finally:
        _session_event_queues_ref.pop(conv_id, None)
    return _LiveTurnOutcome(
        stream_events=stream_events,
        published=published,
        items=server.items,
        released=released,
        live_markers_left=live_markers_left,
        records=list(caplog.records),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_exit_event("claude", exit_signal="SIGINT"), id="ctrl-c"),
        pytest.param(
            _exit_event("claude", session_end_reason="prompt_input_exit"), id="prompt-input-exit"
        ),
        pytest.param(_exit_event("pi", exit_status=0), id="pi-exit-0"),
        pytest.param(_exit_event("pi", exit_signal="SIGHUP"), id="pi-hangup"),
    ],
)
async def test_quitting_during_a_live_turn_stream_ends_it_cancelled_not_failed(
    event: TerminalExitEvent, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = await _quit_during_live_turn(event, caplog, monkeypatch)

    # The stream the caller reads ends cancelled, not failed.
    assert outcome.types(outcome.stream_events)[-1] == "response.cancelled"
    assert "response.failed" not in outcome.types(outcome.stream_events)
    [cancelled] = [e for e in outcome.stream_events if e["type"] == "response.cancelled"]
    assert CancelledEvent.model_validate(cancelled).response.id == "resp_live"
    # Nothing that would read as a failure reaches the server relay.
    assert "response.failed" not in outcome.types(outcome.published)
    assert outcome.types(outcome.published).count("response.cancelled") == 1
    assert "failed" not in outcome.statuses()
    # One idle, from the exit: a native harness's stream end publishes none of its own
    # (a non-native harness would add a second), so this ran the native branch.
    assert outcome.statuses().count("idle") == 1
    assert outcome.logged("runner_turn_failed") == []
    assert outcome.logged("harness_stream_ended_by_terminal_exit") == []
    [ended] = outcome.logged("harness_stream_ended_by_voluntary_exit")
    assert ended.attributes["exception_type"] == "ReadError"
    # No conversation item: no error-type one, and no synthetic "[System: interrupted]" message.
    assert outcome.items == []
    assert outcome.released == [event.session_id]
    assert not outcome.live_markers_left


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_exit_event("claude", exit_signal="SIGKILL"), id="sigkill"),
        pytest.param(_exit_event("claude", exit_signal="SIGSEGV"), id="sigsegv"),
        pytest.param(_exit_event("pi", exit_status=1), id="pi-exit-1"),
        pytest.param(_exit_event("claude", exit_status=143), id="claude-exit-143"),
        pytest.param(
            _exit_event("claude", exit_status=0, session_end_signal="SIGTERM"), id="hook-sigterm"
        ),
        pytest.param(_exit_event("claude", exit_status=0, interactive=False), id="launch-time"),
    ],
)
async def test_crash_during_a_live_turn_stream_still_fails_the_turn(
    event: TerminalExitEvent, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = await _quit_during_live_turn(event, caplog, monkeypatch)

    [failed] = [e for e in outcome.stream_events if e["type"] == "response.failed"]
    assert failed["error"]["code"] == "required_terminal_exited"
    assert "response.cancelled" not in outcome.types(outcome.stream_events)
    assert "response.cancelled" not in outcome.types(outcome.published)
    # The exit handler and the stream end each publish the failed edge; both name the exit.
    assert {error["code"] for error in outcome.failed_errors()} == {"required_terminal_exited"}
    assert "idle" not in outcome.statuses()
    assert len(outcome.logged("runner_turn_failed")) == 1
    assert len(outcome.logged("harness_stream_ended_by_terminal_exit")) == 1
    assert outcome.logged("harness_stream_ended_by_voluntary_exit") == []
    assert outcome.items == []
    assert not outcome.live_markers_left


@pytest.mark.parametrize("response_id", ["resp_live", None])
def test_cancelled_envelope_validates_as_the_servers_cancelled_event(
    response_id: str | None,
) -> None:
    payload = _response_cancelled_payload(response_id)

    event = CancelledEvent.model_validate(payload)

    assert event.response.status == "cancelled"
    assert event.response.id == (response_id or "")


@pytest.mark.asyncio
async def test_stale_voluntary_marker_does_not_relabel_the_next_turns_transport_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quit with no stream open leaves its marker until the next turn starts."""
    monkeypatch.setattr("omnigent.runner.app._TERMINAL_EXIT_RELEASE_GRACE_S", 0.05)
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    event = _exit_event("claude", exit_status=0)
    conv_id = event.session_id
    process_manager = _FakeProcessManager(_DroppedTurnClient())
    process_manager._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        spec_resolver=_plain_spec,
        server_client=_RecordingServerClient(),  # type: ignore[arg-type]
    )
    try:
        app.state.session_resource_registry._terminal_exit_publisher(event)
        for _ in range(1000):
            if process_manager.released:
                break
            await asyncio.sleep(0)
        stream_events = await asyncio.wait_for(
            _stream_turn(app, conv_id, "claude-native"), timeout=10
        )
    finally:
        _session_event_queues_ref.pop(conv_id, None)

    [failed] = [e for e in stream_events if e["type"] == "response.failed"]
    assert failed["error"]["code"] == "connection_error"
    assert "response.cancelled" not in [e["type"] for e in stream_events]
    assert [r for r in caplog.records if getattr(r, "event_name", None) == "harness_stream_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("live_stream", [False, True], ids=["no-stream", "live-stream"])
@pytest.mark.parametrize("was_idle", [False, True], ids=["mid-turn", "was-idle"])
@pytest.mark.parametrize("strong", [True, False], ids=["strong-evidence", "bare-exit-zero"])
async def test_quitting_a_sub_agent_settles_its_dispatch_by_evidence(
    strong: bool,
    was_idle: bool,
    live_stream: bool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent hears of a quit unless the turn had already finished and was delivered.

    Strong evidence of the person's action cancels the dispatch; a bare zero exit
    fails it, since a launcher wrapper can hide a crash behind exit 0 and the parent
    should retry.
    """
    event = (
        _exit_event("claude", session_end_reason="prompt_input_exit", session_was_idle=was_idle)
        if strong
        else _exit_event("pi", exit_status=0, session_was_idle=was_idle)
    )
    with _tracked_subagent(event.session_id) as parent_inbox:
        outcome: _Outcome | _LiveTurnOutcome = (
            await _quit_during_live_turn(event, caplog, monkeypatch)
            if live_stream
            else await _publish_exit(event, caplog)
        )
        entry = subagent_work.get_subagent_work(event.session_id)

    assert not [
        e for e in outcome.events if e.get("type") == "session.status" and e["status"] == "failed"
    ]
    assert entry is not None
    if was_idle and not live_stream:
        # The turn already finished and was delivered; the quit settles nothing.
        assert parent_inbox.empty()
        assert entry.status not in ("cancelled", "failed", "completed")
        return
    item = parent_inbox.get_nowait()
    assert item["conversation_id"] == event.session_id
    assert item["status"] == ("cancelled" if strong else "failed")
    assert entry.status == item["status"]
    if not strong:
        assert "Required terminal exited unexpectedly" in item["output"]
    assert parent_inbox.empty()


class _SessionServer(_RecordingServerClient):
    """Serves the session snapshot the server keeps after the first launch."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        super().__init__()
        self._snapshot = snapshot

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        del kwargs
        payload: dict[str, Any] = {"labels": {}} if url.endswith("/labels") else self._snapshot

        class _Response(NullServerClient._Response):
            def json(self) -> dict[str, Any]:
                return payload

        return _Response()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_turn", [False, True], ids=["idle-session", "live-turn-stream"])
async def test_next_message_after_a_voluntary_exit_cold_resumes_the_session(
    live_turn: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The person's quit must leave the session resumable exactly like a failed exit.

    The exit goes through the real watcher wiring (composer seen, pane dies,
    exit 0), with or without a turn stream open at that moment. The ensure route
    a message triggers then re-launches Claude with ``--resume`` on the session's
    prior Claude id.
    """
    monkeypatch.setattr("omnigent.runner.app._TERMINAL_EXIT_RELEASE_GRACE_S", 0.05)
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    session_id = "5cdbea97a2fb0c659bc09605401e2bb2"
    prior_claude_sid = "3d10247d-c3c0-4689-8cbd-862d7453bf70"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    async def _no_forwarder(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder", _no_forwarder
    )
    transcripts: list[str] = []

    async def _fake_transcript(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
        bridge_dir: Path,
    ) -> Path:
        del client, session_id, workspace, bridge_dir
        transcripts.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ensure_local_claude_resume_transcript",
        _fake_transcript,
    )
    server = _SessionServer({"external_session_id": prior_claude_sid, "agent_id": _AGENT_ID})
    terminals = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminals._by_conversation[session_id] = {("claude", "main"): instance}
    callbacks: dict[str, Any] = {}

    def _capture_watcher(*_args: Any, **kwargs: Any) -> None:
        callbacks.update(kwargs)

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    process_manager = _FakeProcessManager(_OpenTurnClient(lambda: bool(process_manager.released)))
    process_manager._sessions.add(session_id)
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        spec_resolver=_plain_spec,
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminals,
    )
    resources = app.state.session_resource_registry
    monkeypatch.setattr(instance, "_tmux", AsyncMock())
    monkeypatch.setattr(resources, "_build_claude_native_status_poller", lambda **_kw: None)
    await resources.observe_required_terminal(
        session_id, "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )

    try:
        turn = None
        if live_turn:
            turn = asyncio.create_task(_stream_turn(app, session_id, "claude-native"))
            await _wait_for_live_turn(app, session_id)
        # The composer appears, then the person types /exit and the pane dies with status 0.
        instance._remember_pane_snapshot(_COMPOSER_PANE)
        callbacks["on_tick"]()
        instance._remember_exit_status("1 0")
        instance._remember_pane_snapshot(_PADDED_TAIL)
        instance.running = False
        callbacks["on_exit"]()
        await asyncio.wait_for(resources.wait_for_terminal_exit_cleanup(), timeout=2)
        stream_events = await asyncio.wait_for(turn, timeout=10) if turn is not None else []
        for _ in range(50):
            await asyncio.sleep(0)
        exit_events = _drain_session_event_queue(_session_event_queues_ref.get(session_id))

        assert {"type": "session.status", "status": "idle"} in exit_events
        assert not [e for e in exit_events if e.get("status") == "failed"]
        assert not [e for e in exit_events if e.get("type") == "response.failed"]
        if live_turn:
            assert stream_events[-1]["type"] == "response.cancelled"
            assert "response.failed" not in [e["type"] for e in stream_events]
        assert server.items == []
        assert terminals.get(session_id, "claude", "main") is None
        assert process_manager.released == [session_id]

        launched: list[str] = []

        async def _record_launch(**kwargs: Any) -> SessionResourceView:
            launched.extend(kwargs["spec"].args)
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

        monkeypatch.setattr(resources, "launch_required_terminal", _record_launch)
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            )
    finally:
        _session_event_queues_ref.pop(session_id, None)

    assert resp.status_code == 200, resp.text
    assert launched[launched.index("--resume") + 1] == prior_claude_sid
    assert transcripts == [prior_claude_sid]


@pytest.mark.asyncio
async def test_next_message_after_a_pi_quit_cold_resumes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Pi quit (exit 0 after its extension armed) leaves ``pi --session <id>`` resumable."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge
    import omnigent.harnesses.pi_native.credentials as pi_credentials
    import omnigent.harnesses.pi_native.resume as pi_resume

    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    session_id = "7c1f0a9e5b3d4a2c8e6f1b0d9a7c5e3f"
    prior_pi_sid = "019efdb8-54c8-7c02-be27-875eb2620635"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = tmp_path / "pi-bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        "omnigent.harnesses.pi_native.main.resolve_pi_executable", lambda: "/usr/bin/pi"
    )
    monkeypatch.setattr(pi_credentials, "resolve_pi_native_provider", lambda **_kw: None)
    resumed: list[str] = []

    async def _fake_resume_session(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        session_dir: Path,
        workspace: Path,
        model: str,
    ) -> Path:
        del client, session_id, workspace, model
        resumed.append(external_session_id)
        return session_dir / f"{external_session_id}.jsonl"

    monkeypatch.setattr(pi_resume, "ensure_local_pi_resume_session", _fake_resume_session)

    server = _SessionServer(
        {
            "workspace": str(workspace),
            "terminal_launch_args": None,
            "external_session_id": prior_pi_sid,
            "agent_id": _AGENT_ID,
        }
    )
    _extension, config = pi_bridge.write_extension_files(
        bridge_dir,
        session_id=session_id,
        server_url="http://ap.example",
        conversation_url=f"http://ap.example/c/{session_id}",
    )
    terminals = TerminalRegistry()
    instance = make_test_terminal_instance("pi", "main", tmp_path)
    instance.env = {pi_bridge.PI_NATIVE_CONFIG_ENV_VAR: str(config)}
    terminals._by_conversation[session_id] = {("pi", "main"): instance}
    callbacks: dict[str, Any] = {}

    def _capture_watcher(*_args: Any, **kwargs: Any) -> None:
        callbacks.update(kwargs)

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    process_manager._sessions.add(session_id)
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        spec_resolver=_plain_spec,
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminals,
    )
    resources = app.state.session_resource_registry
    monkeypatch.setattr(instance, "_tmux", AsyncMock())
    await resources.observe_required_terminal(
        session_id, "pi", "main", instance, resource_role=PI_NATIVE_TERMINAL_ROLE
    )

    try:
        # Pi's extension arms its inbox poller, then the person quits Pi with status 0.
        (bridge_dir / "input_ready").write_text("", encoding="utf-8")
        callbacks["on_tick"]()
        instance._remember_exit_status("1 0")
        instance._remember_pane_snapshot("Goodbye")
        instance.running = False
        callbacks["on_exit"]()
        await asyncio.wait_for(resources.wait_for_terminal_exit_cleanup(), timeout=2)
        for _ in range(50):
            await asyncio.sleep(0)
        exit_events = _drain_session_event_queue(_session_event_queues_ref.get(session_id))

        assert {"type": "session.status", "status": "idle"} in exit_events
        assert not [e for e in exit_events if e.get("status") == "failed"]
        assert server.items == []
        assert terminals.get(session_id, "pi", "main") is None

        launched: list[str] = []

        async def _record_launch(**kwargs: Any) -> SessionResourceView:
            launched.extend(kwargs["spec"].args)
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

        monkeypatch.setattr(resources, "launch_required_terminal", _record_launch)
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "pi", "session_key": "main", "ensure_native_terminal": True},
            )
    finally:
        _session_event_queues_ref.pop(session_id, None)

    assert resp.status_code == 200, resp.text
    assert launched[launched.index("--session") + 1] == prior_pi_sid
    assert resumed == [prior_pi_sid]
