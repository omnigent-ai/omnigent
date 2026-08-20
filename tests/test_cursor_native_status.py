"""Unit + loop tests for the cursor-native turn-completion ("idle") signal.

Covers the three layers of the cursor parent-wake path:

1. :mod:`omnigent.cursor_native_status` — the turn-end marker store + poster
   state (record/count markers, read/write/clear the posted-count).
2. :func:`omnigent.cursor_native_usage._cli_record_usage` — the cursor ``stop``
   hook entrypoint, which must record a turn-end marker on EVERY firing (even a
   turn with no billable usage) so the parent wake never depends on usage.
3. :func:`omnigent.cursor_native_forwarder.forward_cursor_store_to_session` —
   the poll loop must POST ``external_session_status: idle`` exactly once per
   completed turn, deduped against the persisted posted-count and restart-safe.

The loop tests deliberately let store discovery return ``None`` so the idle path
is exercised in isolation from the chat-mirroring machinery — the idle check runs
every poll independent of store binding.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent import cursor_native_forwarder as fwd
from omnigent import cursor_native_status as status

# --- status store: markers + poster state ------------------------------------


def test_record_and_count_turn_ends(tmp_path: Path) -> None:
    bridge = tmp_path / "cursor-native" / "sess"
    assert status.count_turn_ends(bridge) == 0  # nothing / unreadable -> 0
    status.record_turn_end(bridge)
    status.record_turn_end(bridge, {"generation_id": "gen-2"})
    assert status.count_turn_ends(bridge) == 2


def test_record_turn_end_fires_without_usage(tmp_path: Path) -> None:
    """A turn-end marker is recorded even with no/empty payload (no billable usage)."""
    bridge = tmp_path / "b"
    status.record_turn_end(bridge, None)
    status.record_turn_end(bridge, {})
    assert status.count_turn_ends(bridge) == 2


def test_posted_count_roundtrip_and_clear(tmp_path: Path) -> None:
    bridge = tmp_path / "b"
    assert status.read_posted_count(bridge) == 0
    status.write_posted_count(bridge, 3)
    assert status.read_posted_count(bridge) == 3
    # A re-created terminal clears BOTH the marker file and the poster state.
    status.record_turn_end(bridge)
    status.clear_cursor_status_state(bridge)
    assert status.count_turn_ends(bridge) == 0
    assert status.read_posted_count(bridge) == 0


def test_read_posted_count_ignores_corrupt_state(tmp_path: Path) -> None:
    bridge = tmp_path / "b"
    bridge.mkdir(parents=True)
    (bridge / "cursor_status_forwarder.json").write_text("not json", encoding="utf-8")
    assert status.read_posted_count(bridge) == 0


# --- stop-hook wiring: usage recorder also records a turn-end marker ----------


def test_cli_record_usage_records_turn_end(tmp_path: Path, monkeypatch) -> None:
    """The cursor ``stop`` hook entrypoint records a turn-end marker per firing."""
    from omnigent import cursor_native_usage

    bridge = tmp_path / "cursor-native" / "sess"
    bridge.mkdir(parents=True)
    # The hook reads its JSON payload from stdin and writes ``{}`` to stdout.
    monkeypatch.setattr("sys.stdin.read", lambda: json.dumps({"generation_id": "g1"}))
    rc = cursor_native_usage._cli_record_usage(bridge)
    assert rc == 0
    assert status.count_turn_ends(bridge) == 1


def test_cli_record_usage_records_turn_end_on_empty_stdin(tmp_path: Path, monkeypatch) -> None:
    """Even an empty hook payload (no usage) still records the turn-end marker."""
    from omnigent import cursor_native_usage

    bridge = tmp_path / "b"
    bridge.mkdir(parents=True)
    monkeypatch.setattr("sys.stdin.read", lambda: "")
    assert cursor_native_usage._cli_record_usage(bridge) == 0
    assert status.count_turn_ends(bridge) == 1


# --- forwarder loop: idle POST is once-per-turn, deduped, restart-safe --------


class _StatusRecorder:
    """Async stub for ``_post_external_session_status`` capturing posted statuses."""

    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def __call__(self, client: object, *, session_id: str, status: str) -> None:
        self.statuses.append(status)


async def _wait_until(predicate, *, max_ticks: int = 2000) -> None:
    """Poll *predicate* on the event loop until true, or fail if it never holds."""
    for _ in range(max_ticks):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("forwarder never reached the expected state (wedged?)")


async def _drive_idle_loop(
    monkeypatch: pytest.MonkeyPatch,
    bridge_dir: Path,
    recorder: _StatusRecorder,
    *,
    until,
    max_ticks: int = 2000,
) -> None:
    """Run the real poll loop with store discovery disabled so only the idle path runs."""
    monkeypatch.setattr(fwd, "_discover_store", lambda *a, **k: None)
    monkeypatch.setattr(fwd, "_post_external_session_status", recorder)
    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge_dir,
            agent_name="cursor-native-ui",
            workspace="/ws",
            launch_epoch_ms=1_000,
            poll_interval_s=0.001,
        )
    )
    try:
        for _ in range(max_ticks):
            if until():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("forwarder never reached the expected state (wedged?)")
        # Let a few more polls run so a (buggy) duplicate post would surface.
        await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_posted_once_per_completed_turn(tmp_path: Path, monkeypatch) -> None:
    bridge = tmp_path / "cursor-native" / "sess"
    bridge.mkdir(parents=True)
    status.record_turn_end(bridge)  # one completed turn
    recorder = _StatusRecorder()
    await _drive_idle_loop(
        monkeypatch, bridge, recorder, until=lambda: status.read_posted_count(bridge) >= 1
    )
    assert recorder.statuses == ["idle"]
    assert status.read_posted_count(bridge) == 1


@pytest.mark.asyncio
async def test_idle_dedupes_and_posts_per_new_turn(tmp_path: Path, monkeypatch) -> None:
    """No duplicate idle while quiescent; a later turn-end posts exactly one more."""
    bridge = tmp_path / "cursor-native" / "sess"
    bridge.mkdir(parents=True)
    recorder = _StatusRecorder()
    monkeypatch.setattr(fwd, "_discover_store", lambda *a, **k: None)
    monkeypatch.setattr(fwd, "_post_external_session_status", recorder)
    status.record_turn_end(bridge)  # turn 1 completes
    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge,
            agent_name="cursor-native-ui",
            workspace="/ws",
            launch_epoch_ms=1_000,
            poll_interval_s=0.001,
        )
    )
    try:
        await _wait_until(lambda: status.read_posted_count(bridge) >= 1)
        await asyncio.sleep(0.02)  # quiescent: a one-per-poll bug would post again
        assert recorder.statuses == ["idle"]
        status.record_turn_end(bridge)  # turn 2 completes later
        await _wait_until(lambda: status.read_posted_count(bridge) >= 2)
        await asyncio.sleep(0.02)
        assert recorder.statuses == ["idle", "idle"]
        assert status.read_posted_count(bridge) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_restart_safe_does_not_rewake(tmp_path: Path, monkeypatch) -> None:
    """A restart whose posted-count already covers every marker posts no idle."""
    bridge = tmp_path / "cursor-native" / "sess"
    bridge.mkdir(parents=True)
    status.record_turn_end(bridge)
    status.write_posted_count(bridge, 1)  # already reported this turn before the "restart"
    recorder = _StatusRecorder()
    monkeypatch.setattr(fwd, "_discover_store", lambda *a, **k: None)
    monkeypatch.setattr(fwd, "_post_external_session_status", recorder)
    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge,
            agent_name="cursor-native-ui",
            workspace="/ws",
            launch_epoch_ms=1_000,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.sleep(0.05)  # let several polls run
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert recorder.statuses == []
    assert status.read_posted_count(bridge) == 1
