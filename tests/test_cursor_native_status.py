"""Unit + loop tests for the cursor-native turn-completion ("idle") signal.

Covers the three layers of the cursor parent-wake path:

1. :mod:`omnigent.harnesses.cursor_native.status` — the turn-end marker store + poster
   state (record/count markers, read/write/clear the posted-count).
2. :func:`omnigent.harnesses.cursor_native.usage._cli_record_usage` — the cursor ``stop``
   hook entrypoint, which must record a turn-end marker on EVERY firing (even a
   turn with no billable usage) so the parent wake never depends on usage.
3. :func:`omnigent.harnesses.cursor_native.forwarder.forward_cursor_store_to_session` —
   the poll loop must POST ``external_session_status: idle`` exactly once per
   completed turn, deduped against the persisted posted-count and restart-safe.

The loop tests deliberately let store discovery return ``None`` so the idle path
is exercised in isolation from the chat-mirroring machinery — the idle check runs
every poll independent of store binding.
"""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.cursor_native import forwarder as fwd
from omnigent.harnesses.cursor_native import status

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
    from omnigent.harnesses.cursor_native import usage as cursor_native_usage

    bridge = tmp_path / "cursor-native" / "sess"
    bridge.mkdir(parents=True)
    # The hook reads its JSON payload from stdin and writes ``{}`` to stdout.
    monkeypatch.setattr("sys.stdin.read", lambda: json.dumps({"generation_id": "g1"}))
    rc = cursor_native_usage._cli_record_usage(bridge)
    assert rc == 0
    assert status.count_turn_ends(bridge) == 1


def test_cli_record_usage_records_turn_end_on_empty_stdin(tmp_path: Path, monkeypatch) -> None:
    """Even an empty hook payload (no usage) still records the turn-end marker."""
    from omnigent.harnesses.cursor_native import usage as cursor_native_usage

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

    async def __call__(
        self, client: object, *, session_id: str, status: str, turn_outcome: str | None = None
    ) -> None:
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
    recorder: _StatusRecorder | None,
    *,
    until,
    max_ticks: int = 2000,
) -> None:
    """Run the real poll loop with store discovery disabled so only the idle path runs."""
    monkeypatch.setattr(fwd, "_discover_store", lambda *a, **k: None)
    if recorder is not None:
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
@pytest.mark.parametrize(
    ("hook_status", "wire_status", "outcome"),
    [
        (None, "idle", "completed"),
        ("completed", "idle", "completed"),
        ("aborted", "idle", "cancelled"),
        ("error", "failed", "failed"),
    ],
)
async def test_stop_hook_outcome_reaches_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_status: str | None,
    wire_status: str,
    outcome: str,
) -> None:
    """The stop hook distinguishes actual completion, cancellation, and failure."""
    status.record_turn_end(tmp_path, {"generation_id": "g1", "status": hook_status})
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        fwd.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    await _drive_idle_loop(
        monkeypatch, tmp_path, None, until=lambda: status.read_posted_count(tmp_path) == 1
    )
    assert posted == [
        {
            "type": "external_session_status",
            "data": {"status": wire_status, "turn_outcome": outcome},
        }
    ]


@pytest.mark.asyncio
async def test_partial_stop_marker_retries_before_reporting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that finishes on the next poll keeps its actual outcome."""
    marker_file = tmp_path / status.TURN_END_FILE
    marker_file.write_text('{"status": "abort', encoding="utf-8")
    read_outcome = status.read_turn_outcome
    reads = 0
    outcomes: list[str] = []

    def read_then_finish(bridge: Path, marker: int) -> str:
        nonlocal reads
        reads += 1
        try:
            return read_outcome(bridge, marker)
        finally:
            if reads == 1:
                with marker_file.open("a", encoding="utf-8") as handle:
                    handle.write('ed"}\n')

    async def post_status(client, *, session_id, status, turn_outcome):
        outcomes.append(turn_outcome)

    monkeypatch.setattr(status, "read_turn_outcome", read_then_finish)
    monkeypatch.setattr(fwd, "_post_external_session_status", post_status)
    await _drive_idle_loop(
        monkeypatch, tmp_path, None, until=lambda: status.read_posted_count(tmp_path) == 1
    )
    assert reads == 2
    assert outcomes == ["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "post"])
async def test_stop_outcome_snapshot_survives_append_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A newer marker cannot change the outcome of an earlier undelivered edge."""
    status.record_turn_end(tmp_path, {"status": "aborted"})
    count_turn_ends = status.count_turn_ends
    first_count = True

    def count_then_append(bridge: Path) -> int:
        nonlocal first_count
        count = count_turn_ends(bridge)
        if first_count:
            status.record_turn_end(bridge, {"status": "completed"})
            first_count = False
        return count

    outcomes: list[str] = []
    read_text = Path.read_text
    first_read = True

    def read_once_unavailable(path: Path, *args, **kwargs) -> str:
        nonlocal first_read
        if (
            path == tmp_path / status.TURN_END_FILE
            and not first_count
            and first_read
            and failure == "read"
        ):
            first_read = False
            raise OSError(errno.EMFILE, "too many open files")
        return read_text(path, *args, **kwargs)

    def handle(request: httpx.Request) -> httpx.Response:
        outcomes.append(json.loads(request.content)["data"]["turn_outcome"])
        return httpx.Response(503 if failure == "post" and len(outcomes) == 1 else 200)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(status, "count_turn_ends", count_then_append)
    monkeypatch.setattr(Path, "read_text", read_once_unavailable)
    monkeypatch.setattr(
        fwd.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    await _drive_idle_loop(
        monkeypatch, tmp_path, None, until=lambda: status.read_posted_count(tmp_path) == 2
    )
    assert outcomes == (["cancelled"] if failure == "post" else []) + ["cancelled", "completed"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["finishes_after_read", "rejected_item", "connection_error"])
async def test_completion_waits_for_final_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """The parent's completion must follow delivery of the child's final reply."""
    bridge = tmp_path / "bridge"
    store = tmp_path / "store.db"
    with sqlite3.connect(store) as db:
        db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")

    def finish_turn() -> None:
        reply = {"role": "assistant", "content": [{"type": "text", "text": "pwd: /workspace"}]}
        with sqlite3.connect(store) as db:
            db.execute("INSERT INTO blobs VALUES (?, ?)", ("reply", json.dumps(reply)))
        status.record_turn_end(bridge)

    if race != "finishes_after_read":
        finish_turn()
    read_items = fwd._read_new_items
    first_read = True

    def read_then_finish(*args):
        nonlocal first_read
        items = read_items(*args)
        if first_read and race == "finishes_after_read":
            finish_turn()
        first_read = False
        return items

    delivered: list[str] = []
    attempts = 0

    async def post_item(client, *, session_id, item):
        nonlocal attempts
        attempts += 1
        if race == "rejected_item" and attempts == 1:
            response = httpx.Response(503, request=httpx.Request("POST", "http://test/events"))
            response.raise_for_status()
        if race == "connection_error" and attempts == 1:
            raise httpx.ConnectError("connection refused")
        delivered.append(item.item_data["content"][0]["text"])

    async def post_status(client, *, session_id, status, turn_outcome=None):
        delivered.append(status)

    async def ignore_patch(*args, **kwargs):
        pass

    monkeypatch.setattr(fwd, "_discover_store", lambda *args: store)
    monkeypatch.setattr(fwd, "_read_new_items", read_then_finish)
    monkeypatch.setattr(fwd, "_read_last_used_model", lambda path: None)
    monkeypatch.setattr(fwd, "_patch_external_session_id", ignore_patch)
    monkeypatch.setattr(fwd, "_post_conversation_item", post_item)
    monkeypatch.setattr(fwd, "_post_external_session_status", post_status)
    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="child",
            bridge_dir=bridge,
            agent_name="cursor-native-ui",
            workspace=str(tmp_path),
            launch_epoch_ms=1000,
            poll_interval_s=0.001,
        )
    )
    try:
        await _wait_until(lambda: status.read_posted_count(bridge) == 1)
        assert delivered == ["pwd: /workspace", "idle"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
