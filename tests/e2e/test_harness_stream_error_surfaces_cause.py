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
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

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


class _StreamErrorHarnessClient(_ScriptedHarnessClient):
    """Harness client that emits its scripted frames, then drops mid-stream.

    Mirrors the production transport failure: after ``response.created`` the
    per-conversation client is force-closed and ``aiter_text`` raises
    ``httpx.ReadError`` carrying the real cause -- which ``proxy_stream`` catches
    in its ``(httpx.HTTPError, RuntimeError)`` arm.
    """

    def __init__(self, sse_frames: list[str], *, cause: str) -> None:
        super().__init__(sse_frames)
        self._cause = cause

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream errors after the frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames
        cause = self._cause

        class _ErrCtx:
            status_code = 200

            async def __aenter__(self) -> _StreamErrorHarnessClient._ErrHandle:
                return _StreamErrorHarnessClient._ErrHandle(frames, cause)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ErrCtx()

    class _ErrHandle:
        """Stream handle that raises ``ReadError`` after yielding its frames."""

        status_code = 200

        def __init__(self, frames: list[str], cause: str) -> None:
            self._frames = frames
            self._cause = cause

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
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
