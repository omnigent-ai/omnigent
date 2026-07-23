"""Tests for native turn sequencing, message flow, and recovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent import (
    claude_native_bridge,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _build_lifecycle_app,
    _build_native_app,
    _FakeProcessManager,
    _interrupt_markers,
    _ordered_user_texts,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


def _build_blocking_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _BlockingHarnessClient]:
    """Build a runner app with a blocking harness for concurrency tests.

    :param gate: Event that unblocks the harness mid-stream.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": "hi"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _BlockingHarnessClient(sse_frames, gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_turn_sequencing_buffers_concurrent_message() -> None:
    """Second message during an active turn returns 202 (buffered)."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "49ed0bd1f0cae058f05f48057e9f98cf",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        async def _run_first_turn() -> None:
            """Start the first turn and drain its response."""
            resp = await client.post(
                "/v1/sessions/49ed0bd1f0cae058f05f48057e9f98cf/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [
                        {"type": "input_text", "text": "first"},
                    ],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        # Start the first turn as a background task — it will
        # block inside the harness stream until gate is set.
        turn_task = _aio.create_task(_run_first_turn())
        await _aio.sleep(0.05)

        # Second message while turn active → 202 buffered.
        resp2 = await client.post(
            "/v1/sessions/49ed0bd1f0cae058f05f48057e9f98cf/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [
                    {"type": "input_text", "text": "second"},
                ],
                "harness": "openai-agents",
            },
        )
        assert resp2.status_code == 202, (
            f"Expected 202 buffered, got {resp2.status_code}: {resp2.text}"
        )
        assert resp2.json()["status"] == "buffered"

        # Unblock the harness and let the first turn complete.
        gate.set()
        await _aio.wait_for(turn_task, timeout=5.0)


@pytest.mark.asyncio
async def test_turn_lifecycle_events() -> None:
    """Turn start/complete lifecycle events appear on the session stream."""
    app, _pm, _hc = _build_lifecycle_app()

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "490f8ac07b3f7ac2c9b265eec87eb0e8",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            """Collect events until [DONE]."""
            async with client.stream(
                "GET", "/v1/sessions/490f8ac07b3f7ac2c9b265eec87eb0e8/stream"
            ) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        task = asyncio.create_task(_sub())
        await asyncio.sleep(0.05)

        resp = await client.post(
            "/v1/sessions/490f8ac07b3f7ac2c9b265eec87eb0e8/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        async for _ in resp.aiter_text():
            pass
        await asyncio.sleep(0.05)

        await client.delete("/v1/sessions/490f8ac07b3f7ac2c9b265eec87eb0e8")
        await asyncio.wait_for(task, timeout=5.0)

    lifecycle_events = [e for e in collected if e.get("type") != "session.heartbeat"]
    types = [e.get("type") for e in lifecycle_events]
    assert lifecycle_events, (
        f"Expected turn lifecycle events after ready heartbeat, got {collected}"
    )
    # session.status=running must be the first non-heartbeat event.
    assert types[0] == "session.status", (
        f"First non-heartbeat event must be session.status, got {types[0]}"
    )
    assert lifecycle_events[0].get("status") == "running", (
        f"First session.status must be running, got {lifecycle_events[0].get('status')}"
    )
    # session.status=idle must appear after harness events.
    statuses = [e.get("status") for e in collected if e.get("type") == "session.status"]
    assert statuses[-1] in ("idle", "failed"), (
        f"last session.status must be idle or failed, got statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_delete_during_active_turn_cleans_state() -> None:
    """DELETE cancels the active turn and clears buffers."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "553a265445caf1cdb034abe0b449485d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        # Start a turn (don't drain — turn stays active).
        await client.post(
            "/v1/sessions/553a265445caf1cdb034abe0b449485d/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )

        # Buffer a second message.
        await client.post(
            "/v1/sessions/553a265445caf1cdb034abe0b449485d/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "bye"}],
                "harness": "openai-agents",
            },
        )

        # DELETE while turn active.
        del_resp = await client.delete("/v1/sessions/553a265445caf1cdb034abe0b449485d")
        assert del_resp.status_code == 200
        assert "553a265445caf1cdb034abe0b449485d" in pm.released


@pytest.mark.asyncio
async def test_post_turn_continuation() -> None:
    """Buffered messages are drained and sent to the harness after the first turn."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "68d532c6117d7c15ec58a38e9c7f4790",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        async def _first() -> None:
            """Run and drain the first turn."""
            resp = await client.post(
                "/v1/sessions/68d532c6117d7c15ec58a38e9c7f4790/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "first"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        task = _aio.create_task(_first())
        await _aio.sleep(0.05)

        # Buffer a second message while the first turn is active.
        resp2 = await client.post(
            "/v1/sessions/68d532c6117d7c15ec58a38e9c7f4790/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )
        assert resp2.status_code == 202

        # Unblock the first turn and wait for it to complete.
        gate.set()
        await _aio.wait_for(task, timeout=5.0)

        # Allow post-turn continuation to run (drains buffer,
        # starts background turn for the second message).
        await _aio.sleep(0.2)

    # The harness should have received both messages: the first
    # from the initial turn, the second from the continuation.
    # Each proxy_stream call posts one body to the harness.
    assert len(hc.posted_bodies) >= 2, (
        f"Expected harness to receive 2 messages (initial + "
        f"continuation), got {len(hc.posted_bodies)}"
    )


def _body_contains_text(body: dict[str, Any], needle: str) -> bool:
    """Return whether *needle* appears in any ``input_text`` block of *body*.

    The runner posts user text to the harness in two different content
    shapes: a flat list of content blocks (mid-turn injection forwards)
    and a nested list of ``message`` history items (turn-start streams).
    This walks both so a message is detected regardless of the channel
    that carried it.

    :param body: A harness request body (from ``posted_bodies`` or
        ``patched_events``).
    :param needle: Substring to search for in ``input_text`` blocks.
    :returns: ``True`` if any ``input_text`` block contains *needle*.
    """

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "input_text" and needle in (node.get("text") or ""):
                return True
            return any(_walk(v) for v in node.values())
        if isinstance(node, list):
            return any(_walk(v) for v in node)
        return False

    return _walk(body)


class _HandshakeHarnessClient(_ScriptedHarnessClient):
    """Blocking harness fake that emits ``injection.consumed`` for forwards.

    Simulates the real consumed-handshake (RUNNER_MESSAGE_INGEST.md
    Part B): when the runner forwards a mid-turn injection via ``post``,
    this captures the injection_id the runner stamped, and the active
    turn's stream emits a matching ``injection.consumed`` frame after the
    gate releases — exactly what the executor adapter emits on a real
    harness once it drains the injection into the running turn.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        """Initialize with the gate that unblocks the turn-1 stream."""
        super().__init__([])
        self._gate = gate
        self._consumed_ids: list[str] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Turn-1 stream: created → (gate) → consumed markers → completed."""
        del method, url, timeout
        self.posted_bodies.append(json)
        gate = self._gate
        consumed_ids = self._consumed_ids

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> Any:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": "resp_1"}})
                await gate.wait()
                # Mirror the executor adapter: once injections are consumed
                # into the running turn, echo each correlation id back.
                for inj_id in list(consumed_ids):
                    yield _sse({"type": "injection.consumed", "injection_id": inj_id})
                yield _sse({"type": "response.completed", "response": {"id": "resp_1"}})

        return _Ctx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Record a forwarded injection + capture its injection_id."""
        del url, timeout
        self.patched_events.append(json)
        inj_id = json.get("injection_id")
        if isinstance(inj_id, str) and inj_id:
            self._consumed_ids.append(inj_id)

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}
            content = b""

            def raise_for_status(self) -> None:
                pass

        return _Response()


def _build_handshake_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _HandshakeHarnessClient]:
    """Build a runner app whose harness emits the consumed-handshake.

    :param gate: Event that unblocks the turn-1 stream (after which the
        ``injection.consumed`` markers and ``response.completed`` flow).
    :returns: ``(app, process_manager, harness_client)``.
    """
    spec = AgentSpec(spec_version=1, name="t")
    harness_client = _HandshakeHarnessClient(gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_midturn_message_not_double_delivered_to_harness() -> None:
    """A message sent during an active turn must reach the harness once.

    Covers the web→TUI / claude-native duplication fix. When a user
    message arrives while a turn is in flight, ``post_session_events``
    forwards it as a live mid-turn injection (recorded in
    ``patched_events``) AND buffers it. With the consumed-handshake
    (RUNNER_MESSAGE_INGEST.md Part B), the harness echoes an
    ``injection.consumed`` marker once it consumes the injection, and the
    runner drops the buffered copy — so the message is NOT re-delivered in
    a continuation turn. Exactly-once: forwarded once, no continuation.

    The handshake harness here emits that marker on the turn-1 stream,
    mirroring the real executor adapter. Without the runner's dedup (the
    bug), the buffered "second" would still drain into a continuation
    turn — ``posted_bodies`` would grow to 2 and the assertions fail.
    """
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, hc = _build_handshake_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "ede98a0180773a70b1e81cc854ff7d8a",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        # Turn 1 starts fire-and-forget (202) and its background task
        # blocks inside the harness stream on `gate`. The 0.05s yield lets
        # the runner mark the turn active before the second message lands.
        resp1 = await client.post(
            "/v1/sessions/ede98a0180773a70b1e81cc854ff7d8a/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "first"}],
                "harness": "openai-agents",
            },
        )
        assert resp1.status_code == 202
        await _aio.sleep(0.05)

        # "second" arrives while turn 1 is provably still active (blocked
        # on the gate) → the runner buffers it AND forwards it as a live
        # mid-turn injection with a correlation id.
        resp2 = await client.post(
            "/v1/sessions/ede98a0180773a70b1e81cc854ff7d8a/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )
        # 202 "buffered" confirms turn 1 was still active — the precondition
        # for the handshake path. A 200/stream here would mean the race
        # window never opened and the test below would be vacuous.
        assert resp2.status_code == 202, (
            f"Expected 'second' to be buffered against the active turn, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        assert resp2.json()["status"] == "buffered"

        # Release turn 1; the stream then emits injection.consumed (for
        # "second") and response.completed. The runner drops the buffered
        # copy, so no continuation turn starts. Poll a bounded window: if a
        # continuation were (incorrectly) going to start, posted_bodies
        # would reach 2 within it.
        gate.set()
        for _ in range(100):
            if len(hc.posted_bodies) >= 2:
                break
            await _aio.sleep(0.01)

    # "second" was forwarded as a live injection (channel 1)...
    midturn_injections = [b for b in hc.patched_events if _body_contains_text(b, "second")]
    assert len(midturn_injections) == 1, (
        f"'second' should be forwarded as exactly one mid-turn injection; "
        f"got {len(midturn_injections)} ({hc.patched_events})"
    )
    # ...and must NOT also be re-sent in a continuation turn (channel 2).
    # Exactly one harness turn stream means no continuation was started.
    assert len(hc.posted_bodies) == 1, (
        f"'second' was double-delivered: a continuation turn started after "
        f"the injection was consumed. The runner must drop the buffered "
        f"copy on injection.consumed.\nposted_bodies={hc.posted_bodies}"
    )
    continuation_has_second = any(_body_contains_text(b, "second") for b in hc.posted_bodies[1:])
    assert not continuation_has_second


@pytest.mark.asyncio
async def test_native_buffered_messages_each_delivered_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-native: every buffered message is delivered once, in order.

    Repro for the observed ``1 2 4 4 5 6 7 8 9 0`` corruption. claude-native
    turns are instant and ``run_turn`` types only the *latest* user message,
    so the runner's LLM-oriented machinery mis-delivers for native sessions:
    the collapse-batch continuation (``next_body = all_bodies[-1]``) types
    only the last buffered message (dropping the rest), and the mid-turn
    forward races the instant turn's teardown (duplicating).

    The native path (RUNNER_MESSAGE_INGEST.md Part C) instead skips the
    forward and drains the buffer ONE message at a time, so each buffered
    message gets its own continuation turn — typed exactly once, in order.

    This test buffers 2, 3, 4 behind a blocked first turn and asserts:
    (a) no mid-turn forward POSTs happen (native skips them), and
    (b) the continuation starts a turn per buffered message carrying 2, 3,
    4 as the latest user text, in order — not a single collapsed "4" turn.
    """
    import asyncio as _aio

    def _skip_tools_changed_notification(*args: object, **kwargs: object) -> None:
        """
        Skip MCP tools/list notification in this fake native harness test.

        :param args: Positional notification arguments.
        :param kwargs: Keyword notification arguments.
        :returns: None.
        """
        del args, kwargs

    monkeypatch.setattr(
        claude_native_bridge,
        "post_tools_changed",
        _skip_tools_changed_notification,
    )

    gate = _aio.Event()
    app, _pm, hc = _build_native_app(gate)

    async def _post(text: str) -> httpx.Response:
        """POST one user message carrying an agent_id (drives spec resolve)."""
        return await client.post(
            "/v1/sessions/11b4a5755857531509b492c3f9a7a1a6/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "agent_id": "0c5de81a62eeb73d54466214cf37e5db",
                "content": [{"type": "input_text", "text": text}],
            },
        )

    async with _runner_client(app) as client:
        # Message "1" starts turn 0; its _run_turn_bg resolves + caches the
        # claude-native spec (before streaming), then blocks on the gate.
        assert (await _post("1")).status_code == 202
        # Wait until turn 0 has streamed (spec is cached → native detected)
        # and is now blocked on the gate, i.e. provably active.
        for _ in range(200):
            if hc.posted_bodies:
                break
            await _aio.sleep(0.01)

        # 2, 3, 4 arrive while turn 0 is active → buffered (native: no forward).
        for text in ("2", "3", "4"):
            resp = await _post(text)
            assert resp.status_code == 202, f"{text!r}: {resp.status_code} {resp.text}"
            assert resp.json()["status"] == "buffered"

        # Release turn 0; the buffer drains one-at-a-time. Each continuation
        # turn completes immediately, re-entering the drain for the next.
        gate.set()
        for _ in range(300):
            if len(hc.posted_bodies) >= 4:
                break
            await _aio.sleep(0.01)

    # (a) No mid-turn forward for a native session — the forward is the
    # unreliable injection race we removed for native harnesses.
    assert hc.patched_events == [], (
        f"native sessions must not forward mid-turn injections; got {hc.patched_events}"
    )
    # (b) One continuation turn per buffered message, each typing 2, 3, 4
    # as its latest user text, in order (snapshotted at stream time — see
    # turn_latest_texts). Collapse (the bug) would yield a single
    # continuation whose latest text is "4", dropping 2 and 3.
    continuation_latest = hc.turn_latest_texts[1:]
    assert continuation_latest == ["2", "3", "4"], (
        f"expected one continuation turn per buffered message delivering "
        f"2, 3, 4 in order; got {continuation_latest}. A collapsed ['4'] "
        f"means intermediate messages were dropped from the terminal.\n"
        f"turn_latest_texts={hc.turn_latest_texts}"
    )


class _GatedFileServerClient:
    """Server client that parks the gated file fetch until released.

    ``_resolve_forwarded_message_content`` awaits two GETs per
    ``file_id`` block (metadata, then content). Blocking the metadata
    GET parks the message that carries that block *inside* content
    resolution — before it reaches ``post_session_events``' turn-vs-buffer
    gate — so a later, plain-text message can claim the turn first. This
    is the deterministic trigger for the runner's arrival-order vs
    resolution-order defect.
    """

    def __init__(self) -> None:
        """Initialize the gate events and the call log."""
        self.meta_fetch_started = asyncio.Event()
        self.release = asyncio.Event()
        self.get_calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Return a file response; park on the gated file's metadata GET."""
        del kwargs
        self.get_calls.append(url)
        if url.endswith("/content"):
            return _GatedFileServerClient._Resp(body=b"png-bytes")
        # Metadata GET for the gated file: signal that the caller is now
        # parked inside resolution, then block until the test releases it.
        self.meta_fetch_started.set()
        await self.release.wait()
        return _GatedFileServerClient._Resp(
            payload={
                "id": "c531a3c97ad5fca15709d73d1f734a0c",
                "filename": "a.png",
                "content_type": "image/png",
            }
        )

    class _Resp:
        """Minimal httpx-Response stand-in for file metadata/content."""

        def __init__(self, *, body: bytes = b"", payload: dict[str, Any] | None = None) -> None:
            """Hold either raw bytes (content) or a metadata payload."""
            self.content = body
            self._payload = payload or {}
            self.headers = {"content-type": self._payload.get("content_type", "image/png")}
            self.status_code = 200

        def json(self) -> dict[str, Any]:
            """Return the metadata payload."""
            return self._payload

        def raise_for_status(self) -> None:
            """No-op: the gated client never returns error statuses."""
            return


@pytest.mark.asyncio
async def test_messages_reach_harness_in_submission_order() -> None:
    """Two messages must reach the harness in the order they were sent.

    Repro for the web→TUI / claude-native out-of-order symptom. In
    ``post_session_events`` (omnigent/runner/app.py) the turn-vs-buffer
    decision (the ``if conversation_id in _active_turns`` check at ~4237)
    runs *after* ``await _resolve_forwarded_message_content`` (~4230).
    A message with slow content resolution (e.g. a remote runner inlining
    an uploaded image) is therefore parked before it can claim the turn,
    letting a later plain-text message overtake it and start the first
    turn. The runner orders turns by resolution-completion, not arrival.

    The test makes this deterministic: "alpha-first" (submitted first,
    carries a gated image) is held inside resolution while "bravo-second"
    (submitted second, plain text) races ahead. The invariant under test
    is that the FIRST turn the harness sees carries the FIRST-submitted
    message.
    """
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    server = _GatedFileServerClient()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )

    # NB: post events directly (first event auto-creates session state),
    # mirroring test_sessions_native_resolves_file_id_before_harness. An
    # explicit POST /v1/sessions needs a spec_resolver this app omits.
    async with _runner_client(app) as client:

        async def _post_alpha() -> httpx.Response:
            """POST the first message (gated image + text)."""
            return await client.post(
                "/v1/sessions/ea532aed7642ec833ab31a5649c3495b/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [
                        {
                            "type": "input_image",
                            "file_id": "c531a3c97ad5fca15709d73d1f734a0c",
                            "filename": "a.png",
                        },
                        {"type": "input_text", "text": "alpha-first"},
                    ],
                    "harness": "openai-agents",
                },
            )

        # Submit alpha first; it takes arrival slot 0, passes the ingest
        # gate, and parks inside content resolution on the gated metadata
        # fetch — holding its slot open.
        alpha_task = asyncio.create_task(_post_alpha())
        await asyncio.wait_for(server.meta_fetch_started.wait(), timeout=5.0)

        # Submit bravo second, as a task: under the ordering fix it cannot
        # return until alpha's decision completes, so awaiting it inline
        # here would deadlock. Bravo takes arrival slot 1 and blocks at the
        # ingest gate behind alpha even though its plain-text content
        # resolves instantly.
        async def _post_bravo() -> httpx.Response:
            """POST the second message (plain text)."""
            return await client.post(
                "/v1/sessions/ea532aed7642ec833ab31a5649c3495b/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "bravo-second"}],
                    "harness": "openai-agents",
                },
            )

        bravo_task = asyncio.create_task(_post_bravo())
        # Let bravo reach its steady state: blocked at the gate (fixed) or
        # already racing into the turn-vs-buffer decision (buggy). This is
        # what makes the assertion below catch a regression — without the
        # gate, bravo's plain-text turn starts here while alpha is parked.
        await asyncio.sleep(0.05)

        # Release alpha; correct ordering requires alpha's decision (start
        # turn) to complete before bravo's (buffer behind the active turn).
        server.release.set()
        alpha_resp = await asyncio.wait_for(alpha_task, timeout=5.0)
        bravo_resp = await asyncio.wait_for(bravo_task, timeout=5.0)
        assert alpha_resp.status_code == 202
        assert bravo_resp.status_code == 202

        # Wait for the first turn to reach the harness.
        for _ in range(200):
            if hc.posted_bodies:
                break
            await asyncio.sleep(0.01)

    assert hc.posted_bodies, "harness never received a turn"
    # The harness builds each turn from session history, so the order of
    # user messages there reflects the order the runner accepted them.
    # Submission order was alpha → bravo, so "alpha-first" must precede
    # "bravo-second". They are reversed today: "bravo-second" reached the
    # runner's turn gate first (alpha was still parked in content
    # resolution) and so was appended to history first. Containment alone
    # is not enough to assert here — both texts are present — only order
    # distinguishes the bug.
    ordered = _ordered_user_texts(hc.posted_bodies[0])
    assert ordered.index("alpha-first") < ordered.index("bravo-second"), (
        "out-of-order delivery: 'alpha-first' was submitted before "
        "'bravo-second', but the harness sees them in the order "
        f"{ordered}. post_session_events gates turn-vs-buffer AFTER "
        "awaiting content resolution, so a message with slow resolution is "
        "overtaken by a later one."
    )


@pytest.mark.asyncio
async def test_buffered_continuation_skips_transient_idle() -> None:
    """End-of-turn `idle` is suppressed when a buffered message will start a new turn."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "daec53e4bab215026ded66b565924480",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            async with client.stream(
                "GET", "/v1/sessions/daec53e4bab215026ded66b565924480/stream"
            ) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = _aio.create_task(_sub())
        await _aio.sleep(0.05)

        async def _first() -> None:
            resp = await client.post(
                "/v1/sessions/daec53e4bab215026ded66b565924480/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "first"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        turn_task = _aio.create_task(_first())
        await _aio.sleep(0.05)

        await client.post(
            "/v1/sessions/daec53e4bab215026ded66b565924480/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )

        gate.set()
        await _aio.wait_for(turn_task, timeout=5.0)
        await _aio.sleep(0.3)

        await client.delete("/v1/sessions/daec53e4bab215026ded66b565924480")
        await _aio.wait_for(sub_task, timeout=5.0)

    statuses = [e["status"] for e in collected if e.get("type") == "session.status"]
    # Buffered continuation: the turn-1 idle must be skipped so the client
    # never sees a running → idle → running flicker that would hide the
    # Working indicator. Expected: running (turn 1), running (turn 2),
    # then a terminal idle once the buffer drains.
    assert "idle" not in statuses[:-1], (
        f"Expected no transient idle between turns; got statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_cancelled_turn_publishes_idle_so_client_unsticks() -> None:
    """CancelledError in `_drain_streaming_response` must publish idle.

    Without this, the client sits on stale ``running`` forever after DELETE.
    """
    import asyncio as _aio

    gate = _aio.Event()  # never set → harness stream blocks forever
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "1a6237b81972b420cfd54818b51d1e21",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            async with client.stream(
                "GET", "/v1/sessions/1a6237b81972b420cfd54818b51d1e21/stream"
            ) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = _aio.create_task(_sub())
        await _aio.sleep(0.05)

        async def _stuck() -> None:
            resp = await client.post(
                "/v1/sessions/1a6237b81972b420cfd54818b51d1e21/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "blocked"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        turn_task = _aio.create_task(_stuck())
        await _aio.sleep(0.1)

        # DELETE cancels the turn task — exercises `_drain_streaming_response`'s
        # CancelledError path.
        del_resp = await client.delete("/v1/sessions/1a6237b81972b420cfd54818b51d1e21")
        assert del_resp.status_code == 200

        gate.set()  # unblock the stuck stream so the test can finish
        with contextlib.suppress(Exception):
            await _aio.wait_for(turn_task, timeout=2.0)
        await _aio.wait_for(sub_task, timeout=2.0)

    statuses = [e["status"] for e in collected if e.get("type") == "session.status"]
    # Without the fix, the only emitted status is "running" — client stays stuck.
    assert "idle" in statuses, f"Cancelled turn must publish a terminal status; got: {statuses}"


class _FakeServerClient:
    """Fake server_client that returns paginated history items.

    Items must have an ``"id"`` field. Supports ``after`` cursor
    and ``limit`` params, returns ``has_more`` when more pages
    exist. Tracks GET calls for assertion.
    """

    def __init__(
        self, items: list[dict[str, Any]], *, session_snapshot: dict[str, Any] | None = None
    ) -> None:
        self._items = items
        self._session_snapshot: dict[str, Any] = session_snapshot or {}
        self.get_calls: list[dict[str, str]] = []

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, timeout: float = 10.0
    ) -> Any:
        del timeout
        params = params or {}
        self.get_calls.append(dict(params))

        # Session snapshot GET (e.g. /v1/sessions/{id}, no /items suffix).
        if "/items" not in url:
            snapshot = dict(self._session_snapshot)

            class _SnapshotResp:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return snapshot

            return _SnapshotResp()

        after = params.get("after")
        limit = int(params.get("limit", "100"))

        # Find start index based on after cursor.
        start = 0
        if after:
            for i, item in enumerate(self._items):
                if item.get("id") == after:
                    start = i + 1
                    break
        page = self._items[start : start + limit]
        has_more = (start + limit) < len(self._items)

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"data": page, "has_more": has_more}

        return _Resp()


def _build_recovery_app(
    history_items: list[dict[str, Any]],
    *,
    harness_name: str | None = None,
) -> tuple[FastAPI, _FakeProcessManager, _ScriptedHarnessClient]:
    """Build a runner app with a fake server_client returning history.

    :param history_items: Items returned by GET /v1/sessions/{id}/items.
    :param harness_name: Optional harness override for the resolved spec,
        e.g. ``"codex-native"``.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec_kwargs: dict[str, Any] = {
        "spec_version": 1,
        "name": "recovery-test",
    }
    if harness_name is not None:
        spec_kwargs["executor"] = ExecutorSpec(
            type="omnigent",
            config={"harness": harness_name},
        )
    spec = AgentSpec(**spec_kwargs)
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_r1"}}),
        _sse({"type": "response.output_text.delta", "delta": "recovered"}),
        _sse({"type": "response.completed", "response": {"id": "resp_r1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)
    server_client = _FakeServerClient(history_items)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, pm, hc


async def _fake_auto_create_codex_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Any,
    **kwargs: Any,
) -> SessionResourceView:
    """
    Return a fake Codex terminal without launching native processes.

    :param session_id: Session whose Codex terminal would be created.
    :param resource_registry: Runner resource registry passed by the
        production call site.
    :param publish_event: Event publisher passed by the production call
        site.
    :param kwargs: Auto-create keyword-only arguments.
    :returns: Fake Codex terminal resource for the requested session.
    """
    del resource_registry, publish_event, kwargs
    return SessionResourceView(
        id="terminal_codex_main",
        type="terminal",
        session_id=session_id,
        name="Codex",
    )


@pytest.mark.asyncio
async def test_session_creation_auto_starts_turn_for_unanswered_user_message() -> None:
    """POST /v1/sessions with history ending in a user message starts a recovery turn.

    Breakage this catches: if _run_turn_bg's incomplete-turn detection
    is removed, the session stays idle and the harness receives no POST.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "5f35011bda530550543bf0c329c309f1",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        # "running" proves the recovery turn was started during
        # session creation. "idle" would mean the incomplete-turn
        # detection didn't fire.
        assert resp.json()["status"] == "running"

        # Wait for the background turn to POST to harness.
        # The scripted harness completes instantly so 0.5s is
        # generous; event-driven sync isn't possible because the
        # turn runs in a fire-and-forget background task.
        await _aio.sleep(0.5)

    # 1 POST = the recovery turn sent full history to the harness.
    # 0 would mean _run_turn_bg never ran (detection broken).
    assert len(hc.posted_bodies) == 1, (
        f"Expected exactly 1 harness POST (recovery turn), "
        f"got {len(hc.posted_bodies)}. 0 = detection broken, "
        f">1 = duplicate turn started."
    )
    # The recovery turn's body must include the unanswered user
    # message in its content (loaded from server history).
    body_content = hc.posted_bodies[0].get("content", [])
    assert any(
        item.get("type") == "message" and item.get("role") == "user" for item in body_content
    ), (
        "Recovery turn body must contain the unanswered user message "
        "from history. Empty content means _load_history_as_input "
        "failed or _session_histories wasn't populated."
    )


@pytest.mark.asyncio
async def test_session_creation_does_not_replay_trailing_user_for_codex_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native startup must not replay a trailing user item as recovery.

    Native transcripts are mirrored from Codex. If a Codex turn errors before
    producing an assistant item, Omnigent history can end with the user prompt even
    though Codex already consumed it. Generic crash recovery would treat that
    as an unanswered Omnigent turn and resend the same prompt when ``omnigent
    codex`` reattaches.

    :param monkeypatch: Pytest monkeypatch fixture used to bypass real
        terminal auto-create.
    """
    import asyncio as _aio

    from omnigent.runner import app as runner_app_mod

    session_id = "c5bceafbef391eeff567c144d1d33f3f"
    runner_app_mod._session_histories_ref.pop(session_id, None)

    monkeypatch.setattr(
        runner_app_mod,
        "_auto_create_codex_terminal",
        _fake_auto_create_codex_terminal,
    )

    history = [
        {
            "id": "item_user_failed",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "errored prompt"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history, harness_name="codex-native")

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert resp.status_code == 201
            assert resp.json()["status"] == "idle"
            await _aio.sleep(0.1)
    finally:
        runner_app_mod._session_histories_ref.pop(session_id, None)

    assert hc.posted_bodies == [], (
        "Codex-native session startup must not POST a recovery turn for a "
        "mirrored trailing user item. A POST here means the previous failed "
        "prompt was resent to Codex."
    )


@pytest.mark.asyncio
async def test_catch_up_scan_skips_codex_native_history_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Catch-up scan must not replay mirrored Codex-native transcript items.

    Native sessions can enter ``_session_histories`` through normal turn
    processing, not only through ``POST /v1/sessions`` history recovery.
    If catch-up scan treats that native history like a runner-native
    conversation, a tunnel reconnect can fetch a mirrored trailing user
    item and dispatch a duplicate recovery turn to Codex.

    :param monkeypatch: Pytest monkeypatch fixture used to bypass real
        terminal auto-create.
    """
    import asyncio as _aio

    from omnigent.runner import app as runner_app_mod

    session_id = "97990a9c3b849bb4710a9fb1e9fdc6c8"
    saved_histories = dict(runner_app_mod._session_histories_ref)
    runner_app_mod._session_histories_ref.clear()
    missed_user_item = {
        "id": "item_missed_user",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "already typed natively"}],
    }
    server_client = _FakeServerClient([missed_user_item])
    spec = AgentSpec(
        spec_version=1,
        name="catchup-codex-native",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native"},
        ),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_catchup"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_catchup"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    monkeypatch.setattr(
        runner_app_mod,
        "_auto_create_codex_terminal",
        _fake_auto_create_codex_terminal,
    )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert resp.status_code == 201
            assert resp.json()["status"] == "idle"

            # Simulate the turn-processing paths that already populated
            # native in-memory history before a tunnel reconnect.
            runner_app_mod._session_histories_ref[session_id] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "prior native output"}],
                }
            ]

            server_client.get_calls.clear()
            await app.state.catch_up_scan()
            await _aio.sleep(0.1)
    finally:
        runner_app_mod._session_histories_ref.clear()
        runner_app_mod._session_histories_ref.update(saved_histories)

    assert server_client.get_calls == [], (
        "Catch-up scan must skip Codex-native sessions before fetching Omnigent "
        "items. A GET here means reconnect recovery can observe mirrored "
        "native transcript items and replay them."
    )
    assert hc.posted_bodies == [], (
        "Catch-up scan must not dispatch a recovery turn for a Codex-native "
        "session already present in _session_histories. A POST here means the "
        "mirrored native user item was resent to Codex."
    )


@pytest.mark.asyncio
async def test_session_creation_stays_idle_for_completed_conversation() -> None:
    """POST /v1/sessions with history ending in an assistant message stays idle.

    Breakage this catches: if incomplete-turn detection triggers on
    assistant messages, the runner would start spurious recovery turns.
    """
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "92613c24e132e95e80519e59b2134a38",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        # "idle" proves no recovery turn was started. "running"
        # would mean the detection falsely triggered on a completed
        # conversation.
        assert resp.json()["status"] == "idle"

    # 0 POSTs confirms the harness was never called.
    assert len(hc.posted_bodies) == 0, (
        f"Expected 0 harness POSTs for idle session, "
        f"got {len(hc.posted_bodies)}. >0 = spurious recovery turn."
    )


@pytest.mark.asyncio
async def test_session_creation_auto_starts_turn_for_pending_tool_call() -> None:
    """POST /v1/sessions with history ending in a function_call starts a recovery turn.

    Breakage this catches: if the detection only checks for user
    messages and misses pending tool calls, tool-interrupted sessions
    would stay stuck after crash recovery.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "run ls"}],
        },
        {
            "id": "item_2",
            "type": "function_call",
            "call_id": "call_1",
            "name": "sys_os_shell",
            "arguments": "{}",
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "f74b6cc12acef4605aa5808eb214b53c",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"

        await _aio.sleep(0.5)

    # 1 POST = recovery turn for the pending tool call.
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery for pending tool_call), "
        f"got {len(hc.posted_bodies)}. 0 = detection missed "
        f"function_call items."
    )


@pytest.mark.asyncio
async def test_session_creation_no_recovery_for_empty_history() -> None:
    """POST /v1/sessions with no history stays idle (fresh session).

    Breakage this catches: if the detection crashes on empty history
    (e.g. IndexError on last item), session creation would fail.
    """
    app, _pm, hc = _build_recovery_app([])

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "19f000886345b9519ac5977b97d3a795",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

    assert len(hc.posted_bodies) == 0, (
        "Fresh session with no history must not trigger a recovery turn."
    )


@pytest.mark.asyncio
async def test_history_load_paginates_beyond_100_items() -> None:
    """_load_history_as_input must paginate when history exceeds one page.

    Breakage this catches: if the history loader fetches only one page
    (limit=100) and doesn't follow has_more, conversations with >100
    items would silently lose early history.
    """
    import asyncio as _aio

    # 150 items — requires 2 pages at limit=100.
    history = [
        {
            "id": f"item_{i}",
            "type": "message",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "input_text", "text": f"msg {i}"}],
        }
        for i in range(150)
    ]
    # Last item is assistant (i=149, odd) so no recovery turn —
    # we're testing pagination, not recovery.
    server_client = _FakeServerClient(history)
    spec = AgentSpec(spec_version=1, name="paginate-test")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_p"}}),
        _sse({"type": "response.output_text.delta", "delta": "ok"}),
        _sse({"type": "response.completed", "response": {"id": "resp_p"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Create session — loads history via pagination.
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "1371f04fe2cf189fe4246131ddff016d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

        # Now send a message to trigger a turn — the turn uses
        # _session_histories which should have all 150 items.
        resp2 = await client.post(
            "/v1/sessions/1371f04fe2cf189fe4246131ddff016d/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test",
                "content": [{"type": "input_text", "text": "final"}],
            },
        )
        assert resp2.status_code == 202
        await _aio.sleep(0.5)

    # The server_client should have been called multiple times
    # (pagination). 2 pages for 150 items at limit=100.
    # get_calls[0] has no after cursor (first page).
    # get_calls[1] has after=item_99 (second page).
    assert len(server_client.get_calls) >= 2, (
        f"Expected at least 2 GET calls (pagination), "
        f"got {len(server_client.get_calls)}. "
        f"1 = pagination broken, loader only fetched first page."
    )
    # The harness received the turn with all history in content.
    assert len(hc.posted_bodies) >= 1
    body_content = hc.posted_bodies[0].get("content", [])
    # Must have at least 150 items from the server (the paginated
    # history) plus the new user message. If only 100, pagination
    # stopped at the first page.
    assert len(body_content) > 100, (
        f"Expected >100 history items (all 150 paginated + new "
        f"user msg), got {len(body_content)}. If <=100, pagination "
        f"broke and only one page was loaded."
    )
    assert len(body_content) >= 151, (
        f"Expected at least 151 items (150 server + 1 new), "
        f"got {len(body_content)}. Some server items were dropped."
    )


@pytest.mark.asyncio
async def test_resume_sends_full_history_plus_new_message_to_harness() -> None:
    """Resumed session sends prior history + new user message to the harness.

    Simulates the resume scenario: session was created with a completed
    conversation (user + assistant), then a new message is sent. The
    harness must receive ALL prior history items concatenated with the
    new user message so the LLM has full context.

    Breakage this catches: if _session_histories doesn't include the
    new user message from post_session_events, the harness only sees
    the stale server history (missing the new prompt). If the history
    load fails, the harness sees only the new message (no context).
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Preresume"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello from preresume"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "b76becb60586615cf61d0894efbbbfe0",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        # History ends with assistant — session stays idle (no recovery).
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

        # Now send a new message (simulating user typing after resume).
        resp2 = await client.post(
            "/v1/sessions/b76becb60586615cf61d0894efbbbfe0/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "Postresume"}],
            },
        )
        # 202 = turn started in background.
        assert resp2.status_code == 202

        await _aio.sleep(0.5)

    # Harness received exactly 1 POST (the new turn).
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (resume turn), got {len(hc.posted_bodies)}. "
        f"0 = turn never started, >1 = duplicate turn."
    )
    body = hc.posted_bodies[0]
    content = body.get("content", [])

    # Content must include at least 3 items: user("Preresume"),
    # assistant("Hello from preresume"), user("Postresume").
    # If only 1, history loading failed. If only 2, the new
    # message wasn't appended to _session_histories.
    # Note: the fake harness stores a dict reference, not a copy.
    # The proxy_stream appends the scripted assistant response to
    # the shared _session_histories list AFTER the harness call,
    # so posted_bodies may show 4 items instead of the 3 the
    # production harness actually received (httpx serializes at
    # call time). Assert >= 3 to cover both shapes.
    assert len(content) >= 3, (
        f"Expected >= 3 history items (2 prior + 1 new), got {len(content)}. Items: {content}"
    )
    # First item: original user message from server history.
    assert content[0].get("type") == "message"
    assert content[0].get("role") == "user"

    # Second item: assistant response from server history.
    assert content[1].get("type") == "message"
    assert content[1].get("role") == "assistant"

    # Third item: the new user message sent after resume.
    assert content[2].get("type") == "message"
    assert content[2].get("role") == "user"
    user_content = content[2].get("content", [])
    # Verify the new message text made it through.
    assert any(
        block.get("text") == "Postresume"
        for block in (user_content if isinstance(user_content, list) else [])
    ), f"New user message 'Postresume' not found in harness content. Content[2]: {content[2]}"


@pytest.mark.asyncio
async def test_compaction_item_in_history_expands_and_discards_prior() -> None:
    """History loading expands compaction items and discards pre-compaction items.

    Breakage this catches: if _convert_raw_items_to_input drops compaction
    items (the old behavior), the harness receives the full uncompacted
    history — context window overflow on long conversations. If it doesn't
    discard pre-compaction items, the summary is prepended but the original
    items remain — defeating the point of compaction.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old msg"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "old reply"}],
        },
        {
            "id": "item_3",
            "type": "compaction",
            "summary": "User asked about old stuff. Assistant replied.",
            "last_item_id": "item_2",
            "model": "test-model",
            "token_count": 20,
        },
        {
            "id": "item_4",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "new msg"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        # Session has a trailing user message → crash recovery fires.
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "f29d4764cd2a1682c103dff3562976eb",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"
        await _aio.sleep(0.5)

    # The harness received the recovery turn.
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery turn), got {len(hc.posted_bodies)}."
    )
    content = hc.posted_bodies[0].get("content", [])

    # 3 items expected: synthetic-user (compaction request),
    # synthetic-assistant (summary), post-compaction user msg.
    # If 5, pre-compaction items weren't discarded.
    # If 1, the compaction item was dropped entirely.
    assert len(content) >= 3, (
        f"Expected >= 3 items (2 synthetic + 1 post-compaction user), "
        f"got {len(content)}. If 1, compaction items are dropped. "
        f"Items: {content}"
    )
    # First item: synthetic user requesting summary.
    assert content[0]["role"] == "user"
    assert "summary" in content[0]["content"][0]["text"].lower()

    # Second item: synthetic assistant with the summary text.
    assert content[1]["role"] == "assistant"
    assert content[1]["content"][0]["text"] == (
        "User asked about old stuff. Assistant replied."
    ), "Summary text must match the compaction item's summary field."

    # Third item: the post-compaction user message.
    assert content[2]["role"] == "user"
    assert content[2]["content"] == [{"type": "input_text", "text": "new msg"}], (
        "Post-compaction items must be converted normally."
    )

    # Pre-compaction items ("old msg", "old reply") must NOT appear.
    all_texts = json.dumps(content)
    assert "old msg" not in all_texts, (
        "Pre-compaction user message leaked through — "
        "_convert_raw_items_to_input didn't discard items before the compaction boundary."
    )
    assert "old reply" not in all_texts, "Pre-compaction assistant message leaked through."


@pytest.mark.asyncio
async def test_error_item_in_history_is_surfaced_as_error_block_not_dropped() -> None:
    """History loading surfaces ``error`` items as typed ERROR blocks, not dropped (#1108).

    Breakage this catches: ``_convert_raw_items_to_input`` used to drop every
    item that wasn't message / function_call / function_call_output, so an
    ``error`` item recorded for a failed turn vanished on history reload — the
    next turn replayed as if the failure had never happened ("silent success").

    The converter now preserves each error item as a typed ``error`` item
    (the ``ErrorData`` shape: ``source`` / ``code`` / ``message``). The fix is
    specifically NOT a synthetic user-role ``input_text`` message: that would
    keep the text visible but mis-attribute the failure to the user's input
    and lose the error semantics. This test pins the typed-error shape and
    guards against a regression back to the user-message shim.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "do the thing"}],
        },
        {
            "id": "item_2",
            "type": "error",
            "response_id": "resp_failed",
            "source": "execution",
            "code": "codex_turn_error",
            "message": "401 Unauthorized: ChatGPT login expired",
        },
        {
            "id": "item_3",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "try again"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        # Trailing user message → crash recovery starts a turn, replaying history.
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "e65fe670b242ca2ea48eb8930779d5ff",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"
        await _aio.sleep(0.5)

    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery turn), got {len(hc.posted_bodies)}."
    )
    content = hc.posted_bodies[0].get("content", [])
    error_items = [
        item for item in content if isinstance(item, dict) and item.get("type") == "error"
    ]
    # The error item survived the converter as a typed ERROR block.
    assert len(error_items) == 1, (
        "Expected exactly one typed 'error' item in the converted history; "
        f"got {len(error_items)}. If 0, the error item was dropped (the "
        "silent-success regression) or wrongly mapped to another type."
    )
    error_item = error_items[0]
    assert error_item["message"] == "401 Unauthorized: ChatGPT login expired"
    # The stable code/source round-trip so the failure stays attributable.
    assert error_item["code"] == "codex_turn_error"
    assert error_item["source"] == "execution"
    # Crucially, the error is NOT mis-attributed as a user input_text message.
    user_texts = [
        block.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user"
        for block in (item.get("content") or [])
        if isinstance(block, dict)
    ]
    assert not any("401 Unauthorized" in text for text in user_texts), (
        "Error text leaked into a user message — it must be a typed error block, not user input."
    )


@pytest.mark.asyncio
async def test_crash_recovery_with_compaction_uses_post_compaction_history() -> None:
    """Crash recovery after compaction sees only post-compaction items.

    Breakage this catches: if crash recovery sees pre-compaction items,
    it might start a spurious recovery turn for an item that's already
    been summarized.
    """
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old"}],
        },
        {
            "id": "item_2",
            "type": "compaction",
            "summary": "Prior context summarized.",
            "last_item_id": "item_1",
            "model": "test-model",
            "token_count": 10,
        },
        {
            "id": "item_3",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "post-compaction reply"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "ebfd802a516c4aa95f33f58c894aad31",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert resp.status_code == 201
        # History ends with assistant message (post-compaction) → idle.
        # If crash recovery saw the pre-compaction user message ("old"),
        # it would incorrectly start a recovery turn.
        assert resp.json()["status"] == "idle", (
            "Session should be idle — history ends with an assistant message "
            "after the compaction boundary. 'running' would mean crash recovery "
            "looked at pre-compaction items."
        )

    # No harness POSTs — idle session.
    assert len(hc.posted_bodies) == 0, (
        f"Expected 0 harness POSTs for idle post-compaction session, got {len(hc.posted_bodies)}."
    )


class _OverflowThenSuccessHarnessClient:
    """Harness that returns context-window overflow on first call, success on second.

    Used by the reactive compaction test. The first POST returns a
    ``response.failed`` SSE event with ``context_length_exceeded``.
    The second POST returns normal ``response.completed``.

    :param success_frames: SSE frames to return on the second call.
    """

    def __init__(self, success_frames: list[str]) -> None:
        """Initialize with success frames for the retry."""
        self.posted_bodies: list[dict[str, Any]] = []
        self._success_frames = success_frames
        self.patched_events: list[dict[str, Any]] = []
        self._call_count = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """First call returns overflow; second returns success."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self._call_count += 1
        if self._call_count == 1:
            overflow_frames = [
                _sse(
                    {
                        "type": "response.failed",
                        "error": {
                            "message": (
                                "context_length_exceeded: 5000 tokens > 4096 "
                                "maximum context length"
                            ),
                            "code": "context_length_exceeded",
                        },
                    }
                ),
            ]
            frames = overflow_frames
        else:
            frames = self._success_frames

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self) -> Any:
                return _ScriptedHarnessClient._StreamHandle(frames, None)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Record PATCH events and return 200."""
        del url, timeout
        self.patched_events.append(json)

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

        return _Response()


class _ForwardBlockingHarnessClient(_BlockingHarnessClient):
    """Blocks the interrupt FORWARD (``.post``) so a test can assert it is awaited."""

    def __init__(
        self,
        sse_frames: list[str],
        gate: asyncio.Event,
        fwd_gate: asyncio.Event,
    ) -> None:
        """
        :param sse_frames: SSE frames returned by the harness stream.
        :param gate: Event that releases the stream after the first frame.
        :param fwd_gate: Event that releases a blocked interrupt forward.
        """
        super().__init__(sse_frames, gate)
        self._fwd_gate = fwd_gate
        self.fwd_seen: asyncio.Event = asyncio.Event()
        self.order: list[str] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Stream that records when the blocked turn is cancelled."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self.post_seen.set()
        frames = self._sse_frames
        gate = self._gate
        order = self.order

        class _ForwardBlockingCtx:
            status_code = 200

            async def __aenter__(self) -> _ForwardBlockingHarnessClient._ForwardBlockingHandle:
                return _ForwardBlockingHarnessClient._ForwardBlockingHandle(frames, gate, order)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ForwardBlockingCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Block an interrupt forward on ``fwd_gate``; pass other posts through."""
        if isinstance(json, dict) and json.get("type") == "interrupt":
            self.order.append("forward")
            self.fwd_seen.set()
            await self._fwd_gate.wait()
        return await super().post(url, json=json, timeout=timeout)

    class _ForwardBlockingHandle:
        """Stream handle that records cancellation while paused on the gate."""

        status_code = 200

        def __init__(
            self,
            frames: list[str],
            gate: asyncio.Event,
            order: list[str],
        ) -> None:
            """Initialize with frames, gate, and shared order log."""
            self._frames = frames
            self._gate = gate
            self._order = order

        async def aiter_text(self) -> AsyncIterator[str]:
            """Yield first frame, then record cancellation of the blocked turn."""
            try:
                for i, frame in enumerate(self._frames):
                    if i == 1:
                        await self._gate.wait()
                    yield frame
            except asyncio.CancelledError:
                self._order.append("cancel")
                raise


def _build_fwd_blocking_app(
    gate: asyncio.Event,
    fwd_gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _ForwardBlockingHarnessClient]:
    """Build a runner app whose harness stream AND interrupt forward both block.

    :param gate: Releases the harness stream (kept set-never so the turn blocks).
    :param fwd_gate: Releases a blocked interrupt forward.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_fwd"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_fwd"}}),
    ]
    harness_client = _ForwardBlockingHarnessClient(sse_frames, gate, fwd_gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_interrupt_forwards_to_harness_before_cancelling() -> None:
    """Forward-first: the interrupt is awaited to the harness BEFORE the cancel.

    The harness must receive the interrupt while its turn is still in-flight, so
    its handler engages (cancels the turn + drops the claude-sdk session).
    Cancel-first closed the runner's harness stream first, so the interrupt 404'd
    and the session was never dropped — the next message then resumed the
    abandoned turn and the agent ran one message behind. Here the harness's
    interrupt ``.post`` blocks; the interrupt route must NOT complete until the
    forward is released, proving the forward is awaited first. Cancel-first
    (backgrounded forward) would let the route return immediately.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # stream blocks forever
    fwd_gate = _aio.Event()  # interrupt forward blocks until released
    app, _pm, _hc = _build_fwd_blocking_app(gate, fwd_gate)

    async with _runner_client(app) as client:
        conv_id = "d741917a64f51f2d41226b88d53daf58"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "go"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Await these events / the interrupt task directly rather than through
        # asyncio.wait_for: a wall-clock timeout races task completion when the
        # loaded misc shard starves the event loop — the interrupt could return
        # 204 yet still raise TimeoutError because the timer fired first. pytest's
        # global --timeout guards against a genuine hang.
        await _hc.post_seen.wait()

        # The interrupt route must block on the (still-blocked) harness forward —
        # forward-first awaits it before cancelling. If it completes here, the
        # forward was backgrounded (cancel-first) and the harness never got the
        # interrupt in-flight.
        int_task = _aio.create_task(
            client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})
        )
        # Wait until the route is actually blocked on fwd_gate — deterministic
        # proof the forward is in-flight. This replaces a flaky 0.5 s sleep that
        # could race on loaded CI machines.
        await _hc.fwd_seen.wait()
        assert not int_task.done(), "interrupt must await the harness forward (forward-first)"
        assert _hc.order == ["forward"]

        # Release the forward → the harness gets the interrupt, then the cancel runs.
        fwd_gate.set()
        int_resp = await int_task
        assert int_resp.status_code == 204, int_resp.text
        assert _hc.order == ["forward", "cancel"]
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"interrupt must forward to the harness then finalize the turn with one "
        f"marker; got {len(markers)}."
    )
