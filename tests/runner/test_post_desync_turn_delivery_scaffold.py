"""Post-desync turn delivery, driven against the real harness scaffold.

When a runner<->harness turn stream drops mid-turn (``connection_error``), the
runner marks the session desynced. The harness may still hold that turn's
``_active_turn_ctx`` (its stream teardown never ran): a fresh delivery with no
``previous_response_id`` is then treated as sessions-native steering and
answered ``204``, the runner logs ``harness rejected turn delivery ... with
status 204`` and surfaces ``turn failed (status 204)``. Or the harness may
already have unwound that turn (it finished on its own, a runner-side recovery
interrupted it, or the process was respawned): an interrupt then finds no
in-flight turn and the harness answers ``404``, which must count as "nothing
left to reconcile" rather than fail the user's message.

These tests drive the real runner ``proxy_stream`` boundary against a harness
client that delegates start-vs-inject and interrupt handling to the real
:class:`~omnigent.runtime.harnesses._scaffold.HarnessApp`, so the ``204``, the
interrupt-clears-context behaviour and the ``404`` for an interrupt with no
in-flight turn all come from product code. Only the transport is substituted:
the in-process stream is severed deterministically, which the browser/mock-LLM
stack cannot do without respawning a fresh, context-free harness.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

from omnigent.errors import OmnigentError
from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses._scaffold import (
    HarnessApp,
    MessageEvent,
    TurnContext,
    _format_sse_event,
)
from omnigent.server.schemas import CreateResponseRequest, OutputTextDeltaEvent
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager
from tests.runner.helpers import NullServerClient

_CONV_ID = "ac1dbeef245985f541fd686eb2a32b73"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_RETRY_HINT = "Please retry your message"


class _EchoHarness(HarnessApp):
    """Minimal harness: a started turn emits one delta and completes."""

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="ok"))


class _ScaffoldBackedHarnessClient:
    """Runner-facing harness client backed by a real ``HarnessApp`` scaffold.

    ``proxy_stream`` delivers turns via ``.stream()`` and forwards interrupts
    via ``.post()``. Both route into the real scaffold so the start-vs-inject
    (``204``) decision, interrupt reconciliation and the ``404`` for an
    interrupt with nothing in flight are product behaviour, not a stand-in.
    The first delivered turn is dropped mid-stream to model the
    runner<->harness transport loss that leaves ``_active_turn_ctx`` set.
    """

    def __init__(self, scaffold: HarnessApp, conv_id: str) -> None:
        self._scaffold = scaffold
        self._conv_id = conv_id
        self._drop_next_stream = True
        # The harness-side stream of the dropped turn, so a test can let the
        # harness finish that turn after the runner stopped listening.
        self.dropped_stream: StreamingResponse | None = None
        # When set, interrupts answer this status without reaching the
        # scaffold (a live harness that fails to process the interrupt).
        self.interrupt_status_override: int | None = None
        self.interrupt_statuses: list[int] = []
        self.completed_turns = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        scaffold = self._scaffold
        conv_id = self._conv_id
        client = self
        body = json

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> _ScaffoldBackedHarnessClient._Handle:
                create_req = MessageEvent.model_validate(body).to_create_request()
                result = await scaffold._start_or_inject_turn(create_req, session_id=conv_id)
                if isinstance(result, StreamingResponse):
                    if client._drop_next_stream:
                        client._drop_next_stream = False
                        client.dropped_stream = result
                        ctx = scaffold._active_turn_ctx
                        assert ctx is not None
                        # Never iterate the turn stream: leaving _active_turn_ctx
                        # set is exactly the stale-context state the drop produces.
                        frames = [
                            _format_sse_event(e).decode()
                            for e in scaffold._initial_envelope_events(
                                ctx, model=create_req.model or "plain-agent", start_seq=0
                            )
                        ]
                        return _ScaffoldBackedHarnessClient._Handle(200, frames, drop=True)
                    frames: list[str] = []
                    async for chunk in result.body_iterator:
                        frames.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
                    if any('"response.completed"' in frame for frame in frames):
                        client.completed_turns += 1
                    return _ScaffoldBackedHarnessClient._Handle(200, frames, drop=False)
                return _ScaffoldBackedHarnessClient._Handle(
                    getattr(result, "status_code", 200), [], drop=False
                )

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        def __init__(self, status_code: int, frames: list[str], *, drop: bool) -> None:
            self.status_code = status_code
            self._frames = frames
            self._drop = drop

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            if self._drop:
                raise httpx.ReadError("runner<->harness transport dropped mid-turn")

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        del timeout
        request = httpx.Request("POST", url)
        if json.get("type") != "interrupt":
            return httpx.Response(200, request=request)
        if self.interrupt_status_override is not None:
            status = self.interrupt_status_override
        else:
            try:
                await self._scaffold._handle_interrupt_event()
                status = 204
            except OmnigentError as exc:
                # The scaffold's exception handler renders errors with their http_status.
                status = exc.http_status
        self.interrupt_statuses.append(status)
        return httpx.Response(status, request=request)


def _make_app(client: _ScaffoldBackedHarnessClient) -> Any:
    pm = _FakeProcessManager(client)  # type: ignore[arg-type]
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _message_body(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "agent_id": _AGENT_ID,
        "model": "plain-agent",
        "content": [{"type": "input_text", "text": text}],
        "harness": "openai-agents",
    }


def _parse_sse(buf: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in buf.split("\n\n"):
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("data:"):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _drive_turn(app: Any, text: str) -> list[dict[str, Any]]:
    """Deliver one streamed (``?stream=true``) turn; return the runner's SSE events."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "POST", f"/v1/sessions/{_CONV_ID}/events?stream=true", json=_message_body(text)
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
    return _parse_sse(buf)


async def _post_streamed_turn(app: Any, text: str) -> httpx.Response:
    """Post a ``?stream=true`` turn and return the raw response, whatever its status."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        return await client.post(
            f"/v1/sessions/{_CONV_ID}/events?stream=true", json=_message_body(text)
        )


async def _run_background_turn(app: Any, text: str) -> None:
    """Post a turn on the background (``_run_turn_bg``) path and wait for it to finish."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(f"/v1/sessions/{_CONV_ID}/events", json=_message_body(text))
        assert resp.status_code == 202, resp.text
    for _ in range(300):
        if _CONV_ID not in app.state.active_turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background turn never released its slot")


def _published_statuses(app: Any) -> list[dict[str, Any]]:
    """Drain the session's published ``session.status`` events."""
    queue = app.state.session_event_queues.get(_CONV_ID)
    statuses: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            statuses.append(event)
    return statuses


def _failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "response.failed"]


def _completed(events: list[dict[str, Any]]) -> bool:
    return any(e.get("type") == "response.completed" for e in events)


async def _desync_via_dropped_turn(app: Any, scaffold: HarnessApp) -> None:
    """Turn 1: the mid-stream transport drop that leaves the harness context stale."""
    turn1 = await _drive_turn(app, "first message")
    assert any(e.get("error", {}).get("code") == "connection_error" for e in _failures(turn1)), (
        f"turn 1 should drop with connection_error, got {turn1}"
    )
    assert _CONV_ID in app.state.desynced_sessions
    assert scaffold._active_turn_ctx is not None


async def _let_harness_finish_dropped_turn(
    client: _ScaffoldBackedHarnessClient, scaffold: HarnessApp
) -> None:
    """The harness completes the dropped turn on its own after the runner went away."""
    assert client.dropped_stream is not None
    async for _ in client.dropped_stream.body_iterator:
        pass
    assert scaffold._active_turn_ctx is None
    assert not scaffold._in_flight


@pytest.mark.asyncio
async def test_next_message_after_midturn_drop_is_not_rejected_204() -> None:
    scaffold = _EchoHarness()
    client = _ScaffoldBackedHarnessClient(scaffold, _CONV_ID)
    app = _make_app(client)

    await _desync_via_dropped_turn(app, scaffold)

    turn2 = await _drive_turn(app, "second message")

    rejected_204 = [e for e in _failures(turn2) if e.get("error", {}).get("status") == 204]
    assert not rejected_204, (
        "the next message after a mid-turn drop was rejected with status 204: "
        "the runner delivered it into the harness's stale active-turn context "
        f"without reconciling the desync first. turn 2 events: {turn2}"
    )
    assert _completed(turn2), f"turn 2 should start a fresh turn and complete, got {turn2}"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "background"])
async def test_next_message_after_harness_finished_dropped_turn_completes(
    streaming: bool,
) -> None:
    """With nothing left to interrupt (the harness answers 404), the message still runs."""
    scaffold = _EchoHarness()
    client = _ScaffoldBackedHarnessClient(scaffold, _CONV_ID)
    app = _make_app(client)

    await _desync_via_dropped_turn(app, scaffold)
    await _let_harness_finish_dropped_turn(client, scaffold)
    _published_statuses(app)

    if streaming:
        turn2 = await _drive_turn(app, "second message")
        assert not _failures(turn2), f"turn 2 must not fail: {turn2}"
        assert _completed(turn2), turn2
    else:
        await _run_background_turn(app, "second message")
        statuses = _published_statuses(app)
        assert statuses and statuses[-1]["status"] != "failed", statuses
    assert client.completed_turns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "background"])
async def test_unacknowledged_interrupt_fails_readably_and_stays_recoverable(
    streaming: bool,
) -> None:
    """A live harness that fails the interrupt: readable failure, recovery kept, retry works."""
    scaffold = _EchoHarness()
    client = _ScaffoldBackedHarnessClient(scaffold, _CONV_ID)
    app = _make_app(client)

    await _desync_via_dropped_turn(app, scaffold)
    _published_statuses(app)
    client.interrupt_status_override = 503

    if streaming:
        resp = await _post_streamed_turn(app, "second message")
        assert resp.status_code == 503, resp.text
    else:
        await _run_background_turn(app, "second message")
    statuses = _published_statuses(app)
    assert statuses and statuses[-1]["status"] == "failed", statuses
    message = statuses[-1]["error"]["message"]
    assert _RETRY_HINT in message, message
    assert not message.lstrip().startswith("{"), f"raw error body shown to the user: {message}"
    assert client.interrupt_statuses == [503]
    # Nothing was delivered into the stale context, and recovery is still pending.
    assert scaffold._active_turn_ctx is not None
    assert _CONV_ID in app.state.desynced_sessions

    client.interrupt_status_override = None
    turn3 = await _drive_turn(app, "third message")
    assert _completed(turn3), turn3
    assert client.interrupt_statuses == [503, 204]
    assert _CONV_ID not in app.state.desynced_sessions
