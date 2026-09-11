"""Post-desync turn delivery must reconcile the harness's stale turn ctx.

Guards the ``omnigent.runner.app`` / ``proxy_stream`` failure whose log
signature is::

    harness rejected turn delivery for <conv> with status 204

and whose user-visible symptom is a failed turn (a ``response.failed`` event
with ``source == "harness"`` and ``error == {"status": 204}``) in the web chat.

Root cause (a runner<->harness turn-boundary desync)
----------------------------------------------------
The runner delivers a turn to its per-conversation harness subprocess over
``POST /v1/sessions/{id}/events`` (``proxy_stream`` in ``omnigent/runner/app.py``)
and expects a 200 SSE stream. When a turn's runner<->harness transport drops
mid-stream (a disconnect/teardown), the runner catches the ``httpx`` error,
publishes a ``connection_error`` ``response.failed``, and in
``_on_proxy_stream_end`` pops its own ``_active_turns`` slot and marks the
session *desynced* -- but it never forwards an ``interrupt`` to the harness.
The harness therefore keeps its ``_active_turn_ctx`` set and uncancelled
(``omnigent/runtime/harnesses/_scaffold.py``).

When the user sends the next message the runner -- seeing an empty slot --
delivers it as a *fresh* turn (``previous_response_id`` absent). The harness,
still holding an active uncancelled turn ctx, applies its sessions-native
steering rule and treats the message as an in-band *injection*, answering
``204 No Content`` instead of opening a 200 SSE stream (the contract pinned by
``tests/runtime/harnesses/test_scaffold.py::
test_session_message_event_without_previous_response_id_injects_active_turn``).
``proxy_stream`` sees ``status_code != 200``, logs ``harness rejected turn
delivery for ... with status 204`` and fails the turn with ``source: harness``,
``error: {"status": 204}``.

What these tests guard
----------------------
They assert the *correct* post-desync behavior: before delivering the next
fresh turn into a desynced session, the runner reconciles the harness's
lingering turn ctx (forwarding an ``interrupt``, which clears
``_active_turn_ctx`` -- see the scaffold's
``test_interrupt_then_message_without_prev_id_starts_fresh_turn``), so the
follow-up turn is never hard-rejected with 204, no ``source=harness`` /
``{"status": 204}`` failure reaches the user, and the runner logs no
``harness rejected turn delivery ... with status 204`` line. Both delivery
paths are covered: the direct-stream path (``?stream=true``) and the
background ``_run_turn_bg`` path, plus the dead-harness edge where no live
process is left to interrupt.

Faithfulness
------------
These drive the *real* runner app (``create_runner_app``) and the *real*
``proxy_stream`` code path over the runner's HTTP boundary. The harness is a
faithful in-memory mirror of the scaffold's documented ``_active_turn_ctx``
state machine: it answers 204 to a fresh-turn delivery *because* its ctx is
still active (a rule copied from ``_scaffold.py``, not a hard-coded reply), and
it clears that ctx on the standard desync-reconciliation primitives -- a
forwarded ``interrupt`` or a ``release``/respawn. So the 204 emerges organically
from the same rule the production scaffold applies.

The runner<->harness transport is an in-process Unix-socket stream that the
mock-LLM e2e stack cannot sever without killing the whole harness subprocess
(which respawns a fresh, ctx-free process), so this internal desync is not
deterministically reachable from a browser journey; the runner HTTP boundary
is the surface where the failing state can be driven, and where the sibling
``proxy_stream`` regression tests (``test_stream_failure_diagnostics.py``) live.

Run::

    pytest tests/runner/test_post_desync_turn_delivery.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager, _sse
from tests.runner.helpers import NullServerClient

# 32-hex conversation / agent ids, matching the runner's id shape.
_CONV_ID = "feedfacecafebeeffeedfacecafe0001"
_AGENT_ID = "feedfacecafebeeffeedfacecafe0002"
_HARNESS = "openai-agents"

# The exact runner log prefix the rejection produces.
_REJECTION_LOG_PREFIX = "harness rejected turn delivery for"


class _StaleCtxHarnessClient:
    """Faithful in-memory mirror of the scaffold's ``_active_turn_ctx`` contract.

    Mirrors ``omnigent/runtime/harnesses/_scaffold.py::_start_or_inject_turn``:

    * A ``message`` turn delivered while an active, uncancelled turn ctx exists
      is treated as a sessions-native steering *injection* and answered
      ``204 No Content`` -- NOT a fresh 200 SSE stream.
    * A ``message`` turn delivered with no active ctx opens a fresh 200 SSE
      turn and marks the ctx active.
    * An ``interrupt`` clears the active ctx (204), so the next fresh turn is
      allowed to open a 200 stream (mirrors ``_handle_interrupt_event``).

    The first fresh turn's stream drops mid-stream (a runner<->harness
    transport disconnect) *without* the runner forwarding an interrupt, so the
    ctx stays set -- reproducing the stale-ctx window in which the next turn is
    rejected with 204.
    """

    def __init__(self) -> None:
        # Mirrors HarnessApp._active_turn_ctx being set-and-uncancelled.
        self._active_ctx = False
        # The first fresh turn drops mid-stream; later fresh turns complete.
        self._dropped_once = False
        # Observability for assertions.
        self.stream_calls: list[dict[str, Any]] = []
        self.interrupts_forwarded = 0
        self.rejected_with_204 = 0
        self.patched_events: list[dict[str, Any]] = []

    def reset_ctx(self) -> None:
        """Clear the active ctx the way a respawn (release + fresh process) does."""
        self._active_ctx = False

    # -- turn delivery: POST /v1/sessions/{id}/events (proxy_stream uses .stream) --
    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.stream_calls.append(json)
        if self._active_ctx:
            # Scaffold: fresh-turn message (no previous_response_id) while a
            # turn ctx is active/uncancelled => in-band injection => 204.
            self.rejected_with_204 += 1
            return _NoContentCtx()
        # Fresh turn: open a 200 SSE stream and mark the ctx active.
        self._active_ctx = True
        if not self._dropped_once:
            # The primary failure: transport drops mid-stream. The runner never
            # forwards an interrupt, so _active_ctx stays set afterwards.
            self._dropped_once = True
            return _DroppingStreamCtx(
                [_sse({"type": "response.created", "response": {"id": "resp_drop_a"}})],
                cause="runner<->harness stream dropped mid-turn",
            )
        # A reconciled (fixed-path) fresh turn completes cleanly.
        return _CompletingStreamCtx(
            [
                _sse({"type": "response.created", "response": {"id": "resp_fresh_b"}}),
                _sse({"type": "response.output_text.delta", "delta": "ok"}),
                _sse({"type": "response.completed", "response": {"id": "resp_fresh_b"}}),
            ]
        )

    # -- interrupt / result posts: POST /v1/sessions/{id}/events (uses .post) --
    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        del url, timeout
        if json.get("type") == "interrupt":
            # Scaffold._handle_interrupt_event clears _active_turn_ctx.
            self.interrupts_forwarded += 1
            self._active_ctx = False
            return _PlainResponse(204)
        self.patched_events.append(json)
        return _PlainResponse(200)


class _RespawnAwareProcessManager(_FakeProcessManager):
    """``_FakeProcessManager`` whose ``release`` clears the harness turn ctx.

    A real release tears the harness subprocess down; the next ``get_client``
    spawns a fresh, ctx-free process. Modelling that here keeps the regression
    guard robust to either reconciliation strategy a fix might take -- forward
    an ``interrupt`` (handled on the client) or release/respawn the harness.
    """

    def __init__(self, client: _StaleCtxHarnessClient) -> None:
        super().__init__(client)  # type: ignore[arg-type]
        self._stale_client = client

    async def release(self, conversation_id: str, *, only_if_idle_cutoff: float | None = None):
        await super().release(conversation_id, only_if_idle_cutoff=only_if_idle_cutoff)
        if only_if_idle_cutoff is None or conversation_id not in self._active_turns:
            self._stale_client.reset_ctx()


class _DeadHarnessProcessManager(_RespawnAwareProcessManager):
    """Manager for a harness whose subprocess died with the dropped stream.

    Mirrors the real ``HarnessProcessManager``: ``get_client(conv, "any")``
    raises ``NoLiveHarnessError`` when no live subprocess exists, while a
    named-harness ``get_client`` respawns a fresh, ctx-free process.
    """

    def __init__(self, client: _StaleCtxHarnessClient) -> None:
        super().__init__(client)
        self.process_dead = False

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        if self.process_dead:
            if harness == "any":
                raise NoLiveHarnessError(
                    f"no live harness subprocess for conversation {conversation_id!r}"
                )
            # Named-harness call: crash respawn -- a fresh process has no ctx.
            self.process_dead = False
            self._stale_client.reset_ctx()
        return await super().get_client(conversation_id, harness, env)


class _PlainResponse:
    """Minimal non-streaming httpx-like response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b""

    def raise_for_status(self) -> None:
        return None


class _NoContentCtx:
    """Stream ctx whose handle reports 204 (the harness's injection reply)."""

    async def __aenter__(self) -> _NoContentCtx._Handle:
        return self._Handle()

    async def __aexit__(self, *_: Any) -> None:
        return None

    class _Handle:
        status_code = 204

        async def aiter_text(self) -> AsyncIterator[str]:
            # 204 carries no body; the runner rejects before iterating.
            if False:  # pragma: no cover - keeps this an async generator
                yield ""


class _DroppingStreamCtx:
    """200 stream that yields its frames then drops with an httpx ReadError."""

    def __init__(self, frames: list[str], *, cause: str) -> None:
        self._frames = frames
        self._cause = cause

    async def __aenter__(self) -> _DroppingStreamCtx._Handle:
        return self._Handle(self._frames, self._cause)

    async def __aexit__(self, *_: Any) -> None:
        return None

    class _Handle:
        status_code = 200

        def __init__(self, frames: list[str], cause: str) -> None:
            self._frames = frames
            self._cause = cause

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            raise httpx.ReadError(self._cause)


class _CompletingStreamCtx:
    """200 stream that yields its frames and completes cleanly."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def __aenter__(self) -> _CompletingStreamCtx._Handle:
        return self._Handle(self._frames)

    async def __aexit__(self, *_: Any) -> None:
        return None

    class _Handle:
        status_code = 200

        def __init__(self, frames: list[str]) -> None:
            self._frames = frames

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame


def _build_app(
    manager_cls: type[_RespawnAwareProcessManager] = _RespawnAwareProcessManager,
) -> tuple[Any, _StaleCtxHarnessClient, _RespawnAwareProcessManager]:
    """Build a real runner app over the faithful stale-ctx harness client."""
    harness_client = _StaleCtxHarnessClient()
    pm = manager_cls(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, harness_client, pm


def _message_body(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "agent_id": _AGENT_ID,
        "model": "plain-agent",
        "harness": _HARNESS,
        "content": [{"type": "input_text", "text": text}],
    }


async def _drive_turn(client: httpx.AsyncClient, text: str) -> list[dict[str, Any]]:
    """Deliver one streamed turn; return the parsed SSE events the runner emits."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        f"/v1/sessions/{_CONV_ID}/events?stream=true",
        json=_message_body(text),
    ) as resp:
        assert resp.status_code == 200, resp.status_code
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
    for block in buf.split("\n\n"):
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[len("data:") :].strip()))
                except json.JSONDecodeError:
                    continue
    return events


async def _desync_via_dropped_turn(client: httpx.AsyncClient, app: Any) -> None:
    """Turn 1: the mid-stream transport drop that desyncs the session."""
    turn1 = await _drive_turn(client, "first message")
    t1_failed = [e for e in turn1 if e.get("type") == "response.failed"]
    assert len(t1_failed) == 1, f"turn 1 should fail on the transport drop: {turn1}"
    assert t1_failed[0].get("error", {}).get("code") == "connection_error", t1_failed
    assert _CONV_ID in app.state.desynced_sessions
    assert _CONV_ID not in app.state.active_turns


def _assert_no_204_rejection(
    harness: _StaleCtxHarnessClient,
    turn_events: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shared regression assertions: the follow-up turn was not 204-rejected."""
    assert harness.rejected_with_204 == 0, (
        "the runner delivered the post-disconnect follow-up as a fresh turn into "
        "a harness whose turn ctx was never reconciled, so the harness rejected "
        f"it with 204 (harness.rejected_with_204 == {harness.rejected_with_204})"
    )

    harness_204_failures = [
        e
        for e in turn_events
        if e.get("type") == "response.failed"
        and e.get("source") == "harness"
        and e.get("error") == {"status": 204}
    ]
    assert harness_204_failures == [], (
        "the follow-up turn surfaced a harness-sourced 204 failure to the user: "
        f"{harness_204_failures}"
    )

    rejection_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == "omnigent.runner.app" and _REJECTION_LOG_PREFIX in r.getMessage()
    ]
    assert rejection_logs == [], f"runner logged the 204 turn-delivery rejection: {rejection_logs}"


@pytest.mark.asyncio
async def test_post_desync_turn_is_not_rejected_by_harness_with_204(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A follow-up turn after a runner<->harness disconnect must not 204-fail.

    Journey (what a user does):
      1. Send a message (turn 1). The harness opens a stream, then the
         runner<->harness transport drops mid-turn -- the runner records a
         ``connection_error`` failure and marks the session desynced.
      2. Send the next message (turn 2).

    Correct behavior (asserted here): turn 2 is delivered as a normal turn --
    the runner reconciles the harness's lingering turn ctx first -- so the
    harness never answers 204, no ``source=harness`` / ``{"status": 204}``
    failure reaches the user, and the runner logs no
    ``harness rejected turn delivery ... with status 204`` line.
    """
    app, harness, _pm = _build_app()
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            await _desync_via_dropped_turn(client, app)

            # Turn 2: the user's next message, on the direct-stream path.
            turn2 = await _drive_turn(client, "second message")

    _assert_no_204_rejection(harness, turn2, caplog)
    # The reconciliation cleared the desync marker for the fresh turn.
    assert _CONV_ID not in app.state.desynced_sessions
    # Turn 2 ran as a real fresh turn and completed.
    completed = [e for e in turn2 if e.get("type") == "response.completed"]
    assert completed, f"turn 2 should complete cleanly: {turn2}"


@pytest.mark.asyncio
async def test_post_desync_background_turn_reconciles_before_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The background (non-stream) delivery path reconciles the stale ctx too.

    Same journey as the streamed variant, but turn 2 is posted without
    ``?stream=true`` so it runs through ``_run_turn_bg``. The runner must
    unwind the harness's stale turn ctx (one forwarded interrupt) before
    delivering, so the turn opens a fresh 200 stream instead of a 204.
    """
    app, harness, _pm = _build_app()
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            await _desync_via_dropped_turn(client, app)

            # Turn 2 on the background path: accepted, then runs as a task.
            resp = await client.post(
                f"/v1/sessions/{_CONV_ID}/events",
                json=_message_body("second message"),
            )
            assert resp.status_code == 202, resp.text

            # Wait for the background turn to finish.
            for _ in range(200):
                if _CONV_ID not in app.state.active_turns:
                    break
                await asyncio.sleep(0.01)
            assert _CONV_ID not in app.state.active_turns, "background turn never finished"

    _assert_no_204_rejection(harness, [], caplog)
    assert harness.interrupts_forwarded == 1
    assert _CONV_ID not in app.state.desynced_sessions
    # The follow-up was delivered as a fresh turn (drop + clean completion).
    assert len(harness.stream_calls) == 2, harness.stream_calls


@pytest.mark.asyncio
async def test_post_desync_turn_proceeds_when_harness_process_is_dead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconciliation must not fail the turn when no live harness remains.

    When the transport drop killed the harness subprocess, there is nothing to
    interrupt (``get_client(conv, "any")`` raises ``NoLiveHarnessError``) and a
    named-harness respawn starts ctx-free. The reconciliation must swallow the
    dead-harness case, still clear the desync marker, and let the follow-up
    turn run cleanly.
    """
    app, harness, pm = _build_app(manager_cls=_DeadHarnessProcessManager)
    assert isinstance(pm, _DeadHarnessProcessManager)
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            await _desync_via_dropped_turn(client, app)

            # The dropped stream took the harness subprocess down with it.
            pm.process_dead = True

            turn2 = await _drive_turn(client, "second message")

    _assert_no_204_rejection(harness, turn2, caplog)
    # No live process to interrupt -- reconciliation skipped the forward.
    assert harness.interrupts_forwarded == 0
    assert _CONV_ID not in app.state.desynced_sessions
    completed = [e for e in turn2 if e.get("type") == "response.completed"]
    assert completed, f"turn 2 should complete cleanly after a respawn: {turn2}"
