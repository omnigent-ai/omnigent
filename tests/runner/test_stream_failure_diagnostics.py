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
  exit, while an exit recorded before the turn is not blamed for its drop.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import TerminalExitEvent, TerminalLifecycle
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
        else [_sse({"type": "response.created", "response": {"id": "resp_drop"}})],
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


async def _failed_event_message(app: Any, conv_id: str) -> tuple[dict[str, Any], str]:
    """Drive one failing streamed turn; return (failed event, error message)."""
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


def _required_terminal_exit(session_was_idle: bool) -> TerminalExitEvent:
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


@pytest.mark.asyncio
async def test_earlier_terminal_exit_does_not_relabel_a_later_stream_drop() -> None:
    """An exit recorded before the turn started is not blamed for its drop."""
    from omnigent.runner.app import _session_event_queues_ref

    app = _make_app(cause="stream dropped mid-turn")
    app.state.session_resource_registry._terminal_exit_publisher(_required_terminal_exit(True))
    try:
        failed, message = await _failed_event_message(app, _CONV_ID)
    finally:
        _session_event_queues_ref.pop(_CONV_ID, None)

    assert failed["error"]["code"] == "connection_error", failed
    assert "stream dropped mid-turn" in message, message
