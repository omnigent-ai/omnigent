"""A person quitting their native agent settles the session idle instead of failing it."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.app import _session_event_queues_ref
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_READY = {"terminal_input_ready_at": "1759766400.5"}
# A tmux capture pads the screen with blank rows, so the tail can lack Claude's exit banner.
_PADDED_TAIL = (
    "PRIVATE PANE TEXT\n" + "\n" * 120 + "Pane is dead (status 0, Mon Oct  5 16:37:57 2026)"
)
_COMPOSER_PANE = "────────────────────\n❯ \n────────────────────"
_NOTICE = "{agent} exited in its terminal. Send a message to resume."


class _RecordingServerClient(NullServerClient):
    """Server client that keeps the ``external_conversation_item`` notices it is sent."""

    def __init__(self) -> None:
        self.notices: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            self.notices.append(body["data"]["item_data"])
        return self._Response()


@dataclass
class _Outcome:
    statuses: list[dict[str, Any]]
    events: list[dict[str, Any]]
    notices: list[dict[str, Any]]
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
    interactive: bool = True,
    last_output: str | None = _PADDED_TAIL,
    session_was_idle: bool = False,
    session_id: str | None = None,
) -> TerminalExitEvent:
    context = dict(_READY) if interactive else {}
    if exit_signal is not None:
        context["terminal_exit_signal"] = exit_signal
    if session_end_reason is not None:
        context["claude_session_end_reason"] = session_end_reason
    return TerminalExitEvent(
        session_id=session_id or uuid.uuid4().hex,
        terminal_id=f"terminal_{terminal_name}_main",
        terminal_name=terminal_name,
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        command=terminal_name,
        last_output=last_output,
        exit_status=exit_status,
        session_was_idle=session_was_idle,
        lifecycle_context=context,
    )


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
        # Let the fire-and-forget notice post land.
        for _ in range(50):
            await asyncio.sleep(0)
        events.extend(_drain_session_event_queue(_session_event_queues_ref.get(conv_id)))
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        subagent_work.unregister_child_session(conv_id)
    return _Outcome(
        statuses=[item for item in events if item.get("type") == "session.status"],
        events=events,
        notices=server.notices,
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
async def test_person_quitting_goes_idle_with_a_notice(
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
    agent = {"claude": "Claude", "pi": "Pi"}[event.terminal_name]
    assert outcome.notices == [
        {
            "source": "harness",
            "code": "native_terminal_exited",
            "title": _NOTICE.format(agent=agent),
            "message": _NOTICE.format(agent=agent),
            "level": "info",
        }
    ]
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
            _exit_event("claude", exit_status=1, session_end_reason="prompt_input_exit"),
            "nonzero_exit",
            id="quit-reason-with-failing-status",
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
    assert outcome.notices == []
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
            "to see available models.",
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
    assert outcome.notices == []
    assert outcome.released == [event.session_id]


@pytest.mark.asyncio
async def test_classification_event_names_the_evidence_but_not_the_pane(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _exit_event("claude", session_end_reason="prompt_input_exit", exit_signal="SIGINT")

    outcome = await _publish_exit(event, caplog)

    [record] = outcome.classified
    assert {k: v for k, v in record.attributes.items() if v is not None} == {
        "harness": "claude-native",
        "signal": "SIGINT",
        "session_end_reason": "prompt_input_exit",
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
    assert server.notices == []
    assert process_manager.released == [event.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("was_idle", [False, True])
async def test_quitting_a_sub_agent_mid_task_wakes_the_parent_as_cancelled(
    was_idle: bool, caplog: pytest.LogCaptureFixture
) -> None:
    parent_id = uuid.uuid4().hex
    event = _exit_event("pi", exit_status=0, session_was_idle=was_idle)
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = parent_inbox
    subagent_work.register_child_session(
        event.session_id,
        parent_session_id=parent_id,
        title="pi:main",
        tool="pi",
        session_name="main",
    )
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=event.session_id,
        agent="pi",
        title="main",
    )
    try:
        outcome = await _publish_exit(event, caplog)
    finally:
        _session_event_queues_ref.pop(parent_id, None)
        subagent_work.unregister_subagent_work(event.session_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)

    assert outcome.failed == []
    if was_idle:
        # The turn already finished and was delivered; the quit settles nothing.
        assert parent_inbox.empty()
    else:
        item = parent_inbox.get_nowait()
        assert item["status"] == "cancelled"
        assert item["conversation_id"] == event.session_id


@pytest.mark.asyncio
async def test_next_message_after_a_voluntary_exit_cold_resumes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The person's quit must leave the session resumable exactly like a failed exit.

    The exit goes through the real watcher wiring (composer seen, pane dies,
    exit 0), then the ensure route a message triggers re-launches Claude with
    ``--resume`` on the session's prior Claude id.
    """
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

    class _SessionServer(_RecordingServerClient):
        """Serves the session snapshot the server keeps after the first launch."""

        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            del kwargs
            payload: dict[str, Any] = (
                {"labels": {}}
                if url.endswith("/labels")
                else {"external_session_id": prior_claude_sid}
            )

            class _Response(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return payload

            return _Response()

    server = _SessionServer()
    terminals = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminals._by_conversation[session_id] = {("claude", "main"): instance}
    callbacks: dict[str, Any] = {}

    def _capture_watcher(*_args: Any, **kwargs: Any) -> None:
        callbacks.update(kwargs)

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    process_manager._sessions.add(session_id)
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
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
        # The composer appears, then the person types /exit and the pane dies with status 0.
        instance._remember_pane_snapshot(_COMPOSER_PANE)
        callbacks["on_tick"]()
        instance._remember_exit_status("1 0")
        instance._remember_pane_snapshot(_PADDED_TAIL)
        instance.running = False
        callbacks["on_exit"]()
        await asyncio.wait_for(resources.wait_for_terminal_exit_cleanup(), timeout=2)
        for _ in range(50):
            await asyncio.sleep(0)
        exit_events = _drain_session_event_queue(_session_event_queues_ref.get(session_id))

        assert {"type": "session.status", "status": "idle"} in exit_events
        assert not [e for e in exit_events if e.get("status") == "failed"]
        assert [notice["code"] for notice in server.notices] == ["native_terminal_exited"]
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
