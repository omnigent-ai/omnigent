"""Unit tests for :func:`_stream_live_events` disconnect / completion cleanup.

Pins the contract that ``finally`` is cleanup-only (presence + nested
subscriber teardown) and that ``data: [DONE]`` is emitted only on
normal stream completion — never during ``aclose`` / ``GeneratorExit``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runtime import session_stream
from omnigent.server import presence
from omnigent.server.routes.sessions import _stream_live_events

pytestmark = pytest.mark.asyncio

SESSION_ID = "conv_stream_live_aclose"
USER_ID = "alice@example.com"


class _ConnectedRequest:
    """Minimal request stand-in: client stays connected."""

    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _reset_presence_and_subscribers() -> Any:
    """Isolate module-global presence + session_stream state per test."""
    presence.reset_for_tests()
    session_stream._subscribers.clear()
    yield
    presence.reset_for_tests()
    session_stream._subscribers.clear()


async def test_aclose_cleans_presence_and_subscribers_without_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client ``aclose`` must not raise, and must tear down presence + slots.

    Regression: yielding ``[DONE]`` from the generator ``finally`` during
    ``aclose`` raised ``RuntimeError: async generator ignored GeneratorExit``,
    which could skip or obscure cleanup.
    """
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)

    gen = _stream_live_events(
        _ConnectedRequest(),  # type: ignore[arg-type]
        SESSION_ID,
        viewer_user_id=USER_ID,
        viewer_idle=False,
        presence_root_id=SESSION_ID,
    )
    # Ready heartbeat proves the subscribe slot is registered before aclose.
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert "session.heartbeat" in first
    assert SESSION_ID in session_stream._subscribers
    assert [v["user_id"] for v in presence.snapshot(SESSION_ID, SESSION_ID)["viewers"]] == [
        USER_ID
    ]

    # Direct close — the path StreamingResponse takes on client disconnect.
    await gen.aclose()

    assert SESSION_ID not in session_stream._subscribers, (
        "subscribe finally must drop the subscriber slot on aclose"
    )
    # Disconnect schedules leave after grace; wait past the shrunken window.
    for _ in range(50):
        if presence.snapshot(SESSION_ID, SESSION_ID)["viewers"] == []:
            break
        await asyncio.sleep(0.02)
    assert presence.snapshot(SESSION_ID, SESSION_ID)["viewers"] == [], (
        "presence.disconnect in finally must clear the viewer after grace"
    )


async def test_normal_completion_emits_done_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribe end-of-stream still yields ``[DONE]`` then cleans up."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)

    gen = _stream_live_events(
        _ConnectedRequest(),  # type: ignore[arg-type]
        SESSION_ID,
        viewer_user_id=USER_ID,
        viewer_idle=False,
        presence_root_id=SESSION_ID,
    )
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert "session.heartbeat" in first
    assert SESSION_ID in session_stream._subscribers

    session_stream.close(SESSION_ID)
    chunks: list[str] = []
    async for chunk in gen:
        chunks.append(chunk)

    assert chunks[-1] == "data: [DONE]\n\n", (
        f"normal completion must emit [DONE]; got trailing {chunks[-1]!r}"
    )
    assert SESSION_ID not in session_stream._subscribers
    for _ in range(50):
        if presence.snapshot(SESSION_ID, SESSION_ID)["viewers"] == []:
            break
        await asyncio.sleep(0.02)
    assert presence.snapshot(SESSION_ID, SESSION_ID)["viewers"] == []


async def _drain_available(gen: Any, count: int, timeout: float = 2.0) -> list[str]:
    """Pull exactly *count* frames off a live stream generator.

    :param gen: The ``_stream_live_events`` async generator.
    :param count: How many frames to pull.
    :param timeout: Per-frame timeout.
    :returns: The frames, in order.
    """
    return [await asyncio.wait_for(gen.__anext__(), timeout=timeout) for _ in range(count)]


async def test_worktree_setup_events_reach_the_wire() -> None:
    """All three ``session.worktree_setup.*`` edges serialize onto the stream.

    Regression: the events were published but absent from the
    ``ServerStreamEvent`` union, so the writer's boundary validation
    raised inside this generator. The client's connection died mid-setup
    with no ``[DONE]``, and because the "running setup script" band is
    hydrated from the session label rather than the live event, the
    reconnect left it stuck until the user navigated away and back.

    Before the union entries existed this test fails on the FIRST
    publish — the generator raises ``ValidationError`` instead of
    yielding a frame.
    """
    from omnigent.server.routes._sessions.helpers import (
        _publish_worktree_setup_completed,
        _publish_worktree_setup_failed,
        _publish_worktree_setup_in_progress,
    )

    gen = _stream_live_events(_ConnectedRequest(), SESSION_ID)  # type: ignore[arg-type]
    assert "session.heartbeat" in await asyncio.wait_for(gen.__anext__(), timeout=2.0)

    _publish_worktree_setup_in_progress(SESSION_ID, "bun install")
    _publish_worktree_setup_completed(SESSION_ID)
    _publish_worktree_setup_failed(SESSION_ID, "exited 1", "boom\n")
    frames = await _drain_available(gen, 3)
    await gen.aclose()

    assert frames[0].startswith("event: session.worktree_setup.in_progress\n")
    # The command has to survive onto the wire — it is what the band names.
    assert '"command": "bun install"' in frames[0]
    assert frames[1].startswith("event: session.worktree_setup.completed\n")
    assert frames[2].startswith("event: session.worktree_setup.failed\n")
    assert '"reason": "exited 1"' in frames[2]


async def test_unmodelled_event_drops_one_frame_not_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An event outside the union costs its own frame and nothing else.

    The failure mode this guards is not a dropped frame — it's a dead
    socket. Raising out of the streaming generator ends the response
    without ``[DONE]``, so every later event for that session is lost
    and the client sits on stale state until it reconnects and
    reconciles. The skip must stay LOUD (an ``ERROR`` naming the
    offending type) so a genuinely unmodelled event still gets fixed.
    """
    gen = _stream_live_events(_ConnectedRequest(), SESSION_ID)  # type: ignore[arg-type]
    assert "session.heartbeat" in await asyncio.wait_for(gen.__anext__(), timeout=2.0)

    with caplog.at_level("ERROR"):
        session_stream.publish(SESSION_ID, {"type": "session.not_a_real_event", "x": 1})
        session_stream.publish(
            SESSION_ID,
            {"type": "session.status", "conversation_id": SESSION_ID, "status": "idle"},
        )
        # Right shape, wrong payload: a modelled type whose fields don't
        # validate must be skipped the same way.
        session_stream.publish(
            SESSION_ID,
            {"type": "session.status", "conversation_id": SESSION_ID, "status": "bogus"},
        )
        session_stream.publish(SESSION_ID, {"type": "session.heartbeat"})
        frames = await _drain_available(gen, 2)

    # Only the two valid events crossed the wire, in order — the stream
    # survived both bad frames.
    assert frames[0].startswith("event: session.status\n")
    assert frames[1].startswith("event: session.heartbeat\n")
    assert "session.not_a_real_event" in caplog.text, (
        "the skipped frame must be logged loudly, naming the offending type"
    )

    # And the stream still terminates cleanly rather than as a drop.
    session_stream.close(SESSION_ID)
    tail = [chunk async for chunk in gen]
    assert tail[-1] == "data: [DONE]\n\n"


async def test_snapshot_on_connect_replays_the_settled_setup_edge() -> None:
    """A late subscriber is told the setup command already finished.

    ``session_stream`` has no replay and setup starts inside the create
    request, so a client that subscribes a moment later never sees the
    terminal edge. Without this the band, hydrated from the session
    label, would hold "running setup script" for the whole session.
    """
    from omnigent.server.routes.sessions import _worktree_setup_snapshot_event

    class _Store:
        """Conversation store stub returning one label map."""

        def __init__(self, labels: dict[str, str] | None) -> None:
            self._labels = labels

        def get_conversation(self, _session_id: str) -> Any:
            """Return a row carrying the configured labels."""
            return SimpleNamespace(labels=self._labels)

    running = await _worktree_setup_snapshot_event(
        _Store({"omnigent.worktree_setup": "running"}),  # type: ignore[arg-type]
        SESSION_ID,
    )
    assert running == {"type": "session.worktree_setup.in_progress", "command": None}
    done = await _worktree_setup_snapshot_event(
        _Store({"omnigent.worktree_setup": "done"}),  # type: ignore[arg-type]
        SESSION_ID,
    )
    assert done == {"type": "session.worktree_setup.completed"}
    failed = await _worktree_setup_snapshot_event(
        _Store({"omnigent.worktree_setup": "failed"}),  # type: ignore[arg-type]
        SESSION_ID,
    )
    assert failed == {
        "type": "session.worktree_setup.failed",
        "reason": None,
        "output_tail": None,
    }
    # Sessions with no setup command (the overwhelming majority) must not
    # gain a snapshot frame at all.
    assert await _worktree_setup_snapshot_event(_Store({}), SESSION_ID) is None  # type: ignore[arg-type]
    assert await _worktree_setup_snapshot_event(_Store(None), SESSION_ID) is None  # type: ignore[arg-type]


async def test_snapshot_hook_events_are_validated_and_serialized() -> None:
    """The snapshot-on-connect replay goes through the same wire boundary.

    Proves the late-subscriber path actually reaches the client: the
    ``on_subscribed`` events must validate against the union too, or the
    hardening above would silently drop them.
    """

    async def _snapshot() -> list[dict[str, Any]]:
        """Replay a settled setup edge, as the stream route now does."""
        return [{"type": "session.worktree_setup.completed"}]

    gen = _stream_live_events(
        _ConnectedRequest(),  # type: ignore[arg-type]
        SESSION_ID,
        _snapshot,
    )
    assert "session.heartbeat" in await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    await gen.aclose()

    assert frame.startswith("event: session.worktree_setup.completed\n")
