"""A native harness stream failure surfaces as an opaque
``Harness stream connection error.`` with the real cause discarded.

User journey (native harness, e.g. kimi-native parking on ``Trust this folder?``):

1. Start a session on a native harness and send a turn.
2. The harness's HTTP stream dies mid-turn (transport error) -- here the CLI is
   still alive, sitting at a ``Trust this folder?`` prompt.
3. The chat shows a failure whose only detail is ``Harness stream connection
   error.`` -- the real transport exception text is thrown away, and the live
   terminal pane (the most diagnostic thing available) is never attached.

The runner's proxy exception handler (``omnigent/runner/app.py``, the
``except (httpx.HTTPError, RuntimeError)`` arm of ``proxy_stream``) logs the real
exception via ``_logger.exception()`` but builds a *fixed* user-visible payload::

    {"code": "connection_error", "message": "Harness stream connection error.",
     "type": "<exception class>"}

so ``str(exc)`` never reaches the user, and no ``Last captured terminal output``
block is attached even for a session with a live native terminal.

This is an e2e test of the runner's public HTTP contract
(``POST /v1/sessions/{id}/events``) -- the exact server<->runner boundary that
feeds the web chat -- driven in-process over ASGI with a harness whose stream
drops mid-flight, exactly as the reaper-kill / trust-prompt failure does in
production. It drives the *real* ``proxy_stream`` code; only the harness
subprocess and Omnigent server are test doubles.

Facet 1 (``test_stream_failure_discards_real_cause``): the user-facing
``response.failed`` event must carry the real transport cause, not only the
opaque headline. Reproduces today (cause discarded); the fix flips it.

Facet 2 (``test_stream_failure_omits_live_terminal_pane``): with a live native
terminal sitting at ``Trust this folder?``, the failure event must surface that
pane snapshot. Reproduces today (pane omitted); the fix flips it.

Facet 3 (``test_stream_severed_by_required_terminal_exit_reports_the_exit``): the
pane dies while the runner is still delivering the message and the harness never
reports back, so the runner's own required-terminal exit handling eventually
releases the harness and severs the stream it is reading. The failure must report
that exit, not a connection error.

Facet 4 (``test_required_terminal_exit_lets_the_harness_report_its_own_failure``):
the harness reports the failure that killed its pane (a prompt-readiness timeout)
on the very stream the exit would sever. The runner must let that stream converge
before releasing the harness, so the user sees the readiness diagnosis.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

# The real, knowable transport cause the harness stream dies with. Mirrors a
# kimi CLI parked at its workspace-trust prompt: the information exists (it is
# logged) but never reaches the user.
_REAL_CAUSE = (
    "kimi is waiting for workspace trust in /work/project-a: ReadError(ClosedResourceError())"
)

# What the CLI pane shows at the moment the stream drops -- a recoverable,
# self-describing prompt that the failure event should surface but does not.
_LIVE_PANE = "Trust this folder?\n1. Yes, proceed\n2. No, exit\n> "

_CONV_ID = "9217a860245985f541fd686eb2a32b73"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"

# What a claude-native pane shows when it dies before ever reaching a ready
# prompt -- the sub-agent dispatch failure behind OMNI-5626 / #5558.
_BOOTING_PANE = "Loading MCP servers... (3/7)\nWaiting for the API key helper\n"


class _StreamErrorHarnessClient(_ScriptedHarnessClient):
    """Harness client that emits its scripted frames, then drops mid-stream.

    Mirrors the production transport failure: after ``response.created`` the
    per-conversation client is force-closed and ``aiter_text`` raises
    ``httpx.ReadError`` carrying the real cause -- which ``proxy_stream`` catches
    in its ``(httpx.HTTPError, RuntimeError)`` arm.
    """

    def __init__(
        self,
        sse_frames: list[str],
        *,
        cause: str,
        sever: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(sse_frames)
        self._cause = cause
        self._sever = sever

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream errors after the frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames
        cause = self._cause
        sever = self._sever

        class _ErrCtx:
            status_code = 200

            async def __aenter__(self) -> _StreamErrorHarnessClient._ErrHandle:
                return _StreamErrorHarnessClient._ErrHandle(frames, cause, sever)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ErrCtx()

    class _ErrHandle:
        """Stream handle that raises ``ReadError`` after yielding its frames."""

        status_code = 200

        def __init__(
            self,
            frames: list[str],
            cause: str,
            sever: Callable[[], Awaitable[None]] | None,
        ) -> None:
            self._frames = frames
            self._cause = cause
            self._sever = sever

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            # Whatever kills the channel (e.g. the runner releasing the harness
            # subprocess) happens while the runner is parked on this read.
            if self._sever is not None:
                await self._sever()
            raise httpx.ReadError(self._cause)


def _parse_sse_events(buf: str) -> list[dict[str, Any]]:
    """Parse ``data:`` payloads out of an SSE byte stream (event: lines and all)."""
    events: list[dict[str, Any]] = []
    for block in buf.split("\n\n"):
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("data:"):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _drive_failing_turn(
    app: Any,
    conv_id: str,
    *,
    harness: str,
    model: str,
) -> list[dict[str, Any]]:
    """POST a streamed turn to the runner and collect the user-facing SSE events.

    Drains the live ``?stream=true`` response the SPA would consume, so the
    captured ``response.failed`` event is exactly what a user sees.
    """
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
                "model": model,
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": harness,
            },
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            # A mid-stream transport drop surfaces to the drain as an error too;
            # suppress it -- the failure event is emitted before the drop.
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
            events = _parse_sse_events(buf)
    return events


def _failed_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single user-facing ``response.failed`` event (asserting one)."""
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(failed) == 1, f"expected exactly one response.failed event, got {events}"
    return failed[0]


@pytest.mark.asyncio
async def test_stream_failure_discards_real_cause() -> None:
    """Facet 1: the real transport cause is discarded from the user-facing event.

    Drives the real ``proxy_stream`` with a harness whose stream dies carrying a
    distinctive, knowable cause. The user-facing ``response.failed`` event must
    let the user act on it -- it must carry the actual cause, not only the fixed
    ``Harness stream connection error.`` headline. Today it carries only the
    headline (bug reproduced); the fix preserves ``str(exc)`` and this flips.
    """
    harness_client = _StreamErrorHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_drop"}})],
        cause=_REAL_CAUSE,
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    events = await _drive_failing_turn(app, _CONV_ID, harness="openai-agents", model="plain-agent")
    failed = _failed_event(events)
    error = failed.get("error", {})

    # The failure is classified as a connection error, as expected.
    assert error.get("code") == "connection_error", failed
    # Regression assertion: the real cause the runner logged must reach the user.
    # Reproduces the bug today (the whole event is scrubbed of the cause); the
    # fix, which stops discarding ``str(exc)``, makes this pass.
    event_blob = json.dumps(failed)
    assert _REAL_CAUSE in event_blob, (
        "the real transport cause was discarded from the user-facing failure "
        f"event -- the user sees only the opaque headline {error.get('message')!r} "
        f"with nothing actionable. Full event: {failed}"
    )


@pytest.mark.asyncio
async def test_stream_failure_omits_live_terminal_pane(tmp_path: Path) -> None:
    """Facet 2: a live native terminal's pane is never attached to the failure.

    A native session whose CLI is alive at a ``Trust this folder?`` prompt is the
    trust-prompt class of failure: the harness HTTP stream dies while the terminal
    still sits at the prompt, so the required-terminal-*exit* diagnostics path
    never fires and the
    single most diagnostic thing (what is on the pane right now) is never
    collected. The user-facing failure event must surface that pane snapshot.
    Today it does not (bug reproduced); the fix attaches ``last_pane_text()``.
    """
    conv_id = "cafef00d245985f541fd686eb2a32b73"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("kimi", "main", tmp_path)
    # Seed the pane snapshot the user would want surfaced. Private-attr seed
    # matches the existing runner/resource-registry test convention (no tmux).
    instance._remember_pane_snapshot(_LIVE_PANE)
    assert instance.last_pane_text() == _LIVE_PANE.strip()
    terminal_registry._by_conversation.setdefault(conv_id, {})[("kimi", "main")] = instance

    native_spec = AgentSpec(
        spec_version=1,
        name="kimi-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kimi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    harness_client = _StreamErrorHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_kimi"}})],
        cause="ReadError(ClosedResourceError())",
    )
    pm = _FakeProcessManager(harness_client)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    # Register the session as native so the runner knows a native terminal exists.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": _AGENT_ID},
        )
        assert create_resp.status_code == 201, create_resp.text

    # Precondition: the live pane is present and self-describing before the turn.
    assert terminal_registry.get(conv_id, "kimi", "main") is not None

    events = await _drive_failing_turn(app, conv_id, harness="kimi-native", model="kimi-agent")
    failed = _failed_event(events)

    event_blob = json.dumps(failed)
    # Regression assertion: the live pane text (and a "Last captured terminal
    # output" block) must be surfaced so the blocked CLI self-describes. Both are
    # absent today (bug reproduced); the fix attaches the bounded pane snapshot.
    assert "Trust this folder?" in event_blob, (
        "the live native terminal's pane (sitting at 'Trust this folder?') was "
        "never attached to the stream-failure event, so a recoverable, knowable "
        f"condition is invisible to the user. Full event: {failed}"
    )
    assert "last captured" in event_blob.lower(), (
        "no 'Last captured terminal output' diagnostics block was attached to "
        f"the stream-failure event. Full event: {failed}"
    )


def _failed_statuses(conv_id: str) -> list[dict[str, Any]]:
    """Drain the ``session.status: failed`` events the runner published for *conv_id*."""
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
@pytest.mark.parametrize("pane_status_before_exit", [None, "idle"])
async def test_stream_severed_by_required_terminal_exit_reports_the_exit(
    tmp_path: Path, pane_status_before_exit: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Facet 3: a stream severed by the runner's own harness release names the exit.

    A claude-native sub-agent's pane dies while the runner is still delivering the
    first message (Claude Code never shows a ready prompt, or exits while booting).
    The tmux watcher reports the exit, the resource registry evicts the terminal,
    and the runner's exit handler releases the harness subprocess -- which closes
    the client ``proxy_stream`` is reading, surfacing there as ``ReadError``.
    That used to be reported as an opaque connection error. For a pane that
    never ran a turn (``idle`` at exit) the exit handler publishes nothing
    itself, so that connection error was the *only* failure the user ever saw
    (OMNI-5626 / #5558). The turn must instead fail as ``required_terminal_exited``
    carrying the pane's last output, and it must fail exactly once.
    """
    from omnigent.runner.app import _session_event_queues_ref

    # The harness is parked on its readiness wait and never reports back, so the
    # release proceeds once the (shortened) grace for the live stream expires.
    monkeypatch.setattr(runner_app, "_TERMINAL_EXIT_RELEASE_GRACE_S", 0.2)
    conv_id = "5626dead245985f541fd686eb2a32b73"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "claude"
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot(_BOOTING_PANE)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]

    native_spec = AgentSpec(
        spec_version=1,
        name="claude-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    state: dict[str, Any] = {}

    async def _pane_dies_under_the_stream() -> None:
        """The pane exits mid-delivery; the runner tears the harness down under its own read."""
        if pane_status_before_exit is not None:
            # The PTY watcher's last reading before the pane vanished.
            state["resource_registry"]._last_session_status[conv_id] = pane_status_before_exit
        callbacks["on_exit"]()
        await state["resource_registry"].wait_for_terminal_exit_cleanup()
        # The release waits out the (shortened) grace for this live stream first.
        for _ in range(300):
            if state["pm"].released == [conv_id]:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the required-terminal exit never released the harness")

    harness_client = _StreamErrorHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_claude"}})],
        cause="ReadError(ClosedResourceError())",
        sever=_pane_dies_under_the_stream,
    )
    pm = _FakeProcessManager(harness_client)
    state["pm"] = pm
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    state["resource_registry"] = resource_registry
    _session_event_queues_ref.pop(conv_id, None)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": _AGENT_ID},
            )
            assert create_resp.status_code == 201, create_resp.text
        await resource_registry.observe_required_terminal(conv_id, "claude", "main", instance)
        assert callable(callbacks.get("on_exit"))

        events = await _drive_failing_turn(
            app, conv_id, harness="claude-native", model="claude-agent"
        )
        statuses = _failed_statuses(conv_id)
    finally:
        _session_event_queues_ref.pop(conv_id, None)

    failed = _failed_event(events)
    error = failed.get("error", {})
    event_blob = json.dumps(failed)
    assert error.get("code") == "required_terminal_exited", failed
    assert "Harness stream connection error" not in event_blob, failed
    assert "Required terminal exited unexpectedly" in event_blob, failed
    assert "Waiting for the API key helper" in event_blob, failed
    assert "last captured terminal output" in event_blob.lower(), failed
    # The registry evicted the dead pane and the runner released its harness.
    assert terminal_registry.get(conv_id, "claude", "main") is None
    assert pm.released == [conv_id]
    # Every failure the session surfaced is the exit -- never the transport symptom.
    assert statuses, "the turn must surface as failed"
    assert {s["error"]["code"] for s in statuses} == {"required_terminal_exited"}, statuses
    if pane_status_before_exit == "idle":
        # The exit handler itself stays quiet for an idle pane; the stream's
        # failure is the one and only failure the user sees.
        assert len(statuses) == 1, statuses


class _ExitThenFailureHarnessClient(_ScriptedHarnessClient):
    """Harness whose pane dies mid-delivery and which then reports why.

    Emits ``response.created``, awaits *sever* (the pane exit reaching the
    runner), then emits the executor's own ``response.failed`` and ends cleanly --
    exactly what claude-native does when its prompt-readiness wait times out and
    it kills the pane before reporting.
    """

    def __init__(
        self,
        frames_before: list[str],
        *,
        sever: Callable[[], Awaitable[None]],
        frames_after: list[str],
    ) -> None:
        super().__init__(frames_before)
        self._sever = sever
        self._frames_after = frames_after

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream pauses for the pane exit mid-way."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames_before = self._sse_frames
        frames_after = self._frames_after
        sever = self._sever

        class _Handle:
            status_code = 200

            async def __aenter__(self) -> _Handle:
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def aiter_text(self) -> AsyncIterator[str]:
                for frame in frames_before:
                    yield frame
                await sever()
                for frame in frames_after:
                    yield frame

        return _Handle()


_READINESS_DIAGNOSIS = (
    "inner executor error: Claude Code's input box never became ready within 30s "
    "(200 polls, 0 empty captures); last capture: Loading MCP servers... (3/7)"
)


@pytest.mark.asyncio
async def test_required_terminal_exit_lets_the_harness_report_its_own_failure(
    tmp_path: Path,
) -> None:
    """Facet 4: the runner relays the harness's readiness diagnosis, then releases.

    claude-native kills its pane when the prompt-readiness wait times out and
    only then reports the failure on its turn stream. The runner sees the pane
    exit first; releasing the harness at that point would sever the stream and
    replace the diagnosis with a transport error. The exit handler must let the
    live stream converge, so the one failure the user sees is the harness's own
    readiness diagnosis, and the harness is released once the stream has ended.
    """
    from omnigent.runner.app import _session_event_queues_ref

    conv_id = "5626ead1245985f541fd686eb2a32b73"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "claude"
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot(_BOOTING_PANE)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]

    native_spec = AgentSpec(
        spec_version=1,
        name="claude-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    state: dict[str, Any] = {}

    async def _pane_killed_by_the_harness() -> None:
        """The exit reaches the runner before the harness's failure report does."""
        # A pane that never ran a turn reads as idle to the PTY watcher.
        state["resource_registry"]._last_session_status[conv_id] = "idle"
        callbacks["on_exit"]()
        await state["resource_registry"].wait_for_terminal_exit_cleanup()
        # The exit handler has run; give its release task a chance to (wrongly)
        # fire before the harness reports.
        for _ in range(20):
            await asyncio.sleep(0)
        assert state["pm"].released == [], "the harness was released under a live stream"

    failure = {
        "code": "inner_executor_error",
        "message": _READINESS_DIAGNOSIS,
        "type": "RuntimeError",
    }
    harness_client = _ExitThenFailureHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_ready"}})],
        sever=_pane_killed_by_the_harness,
        frames_after=[
            _sse(
                {
                    "type": "response.failed",
                    "response": {"id": "resp_ready", "status": "failed", "error": failure},
                    "error": failure,
                }
            )
        ],
    )
    pm = _FakeProcessManager(harness_client)
    state["pm"] = pm
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    state["resource_registry"] = resource_registry
    _session_event_queues_ref.pop(conv_id, None)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": _AGENT_ID},
            )
            assert create_resp.status_code == 201, create_resp.text
        await resource_registry.observe_required_terminal(conv_id, "claude", "main", instance)
        assert callable(callbacks.get("on_exit"))

        events = await _drive_failing_turn(
            app, conv_id, harness="claude-native", model="claude-agent"
        )
        # The stream has converged; the deferred release now goes through.
        for _ in range(200):
            if pm.released == [conv_id]:
                break
            await asyncio.sleep(0.01)
        statuses = _failed_statuses(conv_id)
    finally:
        _session_event_queues_ref.pop(conv_id, None)

    failed = _failed_event(events)
    event_blob = json.dumps(failed)
    # The user sees the harness's own diagnosis, not a symptom of the teardown.
    assert _READINESS_DIAGNOSIS in event_blob, failed
    assert "connection_error" not in event_blob, failed
    assert "required_terminal_exited" not in event_blob, failed
    assert pm.released == [conv_id]
    assert terminal_registry.get(conv_id, "claude", "main") is None
    # Exactly one failure reaches the session, and it carries the diagnosis.
    assert len(statuses) == 1, statuses
    assert _READINESS_DIAGNOSIS in statuses[0]["error"]["message"], statuses
