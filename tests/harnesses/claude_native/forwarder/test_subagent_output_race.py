"""A Stop hook must not be forwarded ahead of the reply it ends.

If Claude writes its final reply and fires Stop between the forwarder's
transcript read and hook read in one poll, the idle edge used to reach the
server before the reply was mirrored, so the parent saw "produced no output".
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import record_hook_event
from tests.harnesses.claude_native.forwarder._support import (
    _start_recording_server_with_responses,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_snapshot", ["normal", "slow", "missing"])
async def test_subagent_output_item_before_idle_status_prevents_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_snapshot: str,
) -> None:
    """The final reply is posted before the Stop idle edge, and idle posts once."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    # The turn starts with only the user message; the reply lands mid-poll.
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-1",
                "message": {"role": "user", "content": "verify sources"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    final_reply = json.dumps(
        {
            "type": "assistant",
            "uuid": "asst-final",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "VERIFIED_FINAL_VERDICT"}],
            },
        }
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-race-test",
            "transcript_path": str(transcript_path),
        },
    )

    # Track requests and inject Stop hook after items are read.
    request_bodies: list[Any] = []
    original_items = forwarder._forward_available_items
    stop_hook_injected = False

    async def mock_items(**kw: Any) -> Any:
        """Call real items, then inject Stop hook once (T1 → T2 window)."""
        result = await original_items(**kw)
        nonlocal stop_hook_injected
        if not stop_hook_injected and result.line_cursor > 0:
            # Claude writes its final reply, then fires Stop, after this poll's
            # transcript read but before its hook read.
            with transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(final_reply + "\n")
            record_hook_event(
                bridge_dir,
                {
                    "hook_event_name": "Stop",
                    "session_id": "claude-race-test",
                    "transcript_path": str(transcript_path),
                },
            )
            stop_hook_injected = True
        return result

    monkeypatch.setattr(forwarder, "_forward_available_items", mock_items)

    snapshot_entered = asyncio.Event()
    snapshot_released = threading.Event()
    snapshot_finished = threading.Event()
    loop = asyncio.get_running_loop()
    snapshot_pending = False
    original_prescan = forwarder._prescan_precompact_edges
    original_stat = Path.stat
    original_open = Path.open

    async def prescan(*args: Any, **kwargs: Any) -> Any:
        nonlocal snapshot_pending
        result = await original_prescan(*args, **kwargs)
        if hook_snapshot == "missing" and not stop_hook_injected:
            (bridge_dir / forwarder._HOOKS_FILE).unlink()
        snapshot_pending = True
        return result

    def hold_snapshot(path: Path) -> None:
        if (
            hook_snapshot == "slow"
            and snapshot_pending
            and path == bridge_dir / forwarder._HOOKS_FILE
            and not snapshot_finished.is_set()
        ):
            loop.call_soon_threadsafe(snapshot_entered.set)
            snapshot_released.wait(timeout=5.0)
            snapshot_finished.set()

    def stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        hold_snapshot(path)
        return original_stat(path, *args, **kwargs)

    def open_file(path: Path, *args: Any, **kwargs: Any) -> Any:
        hold_snapshot(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(forwarder, "_prescan_precompact_edges", prescan)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "open", open_file)

    def recording_handler(body: Any) -> dict[str, Any]:
        """Record POSTs."""
        request_bodies.append(body)
        if isinstance(body, dict):
            if body.get("type") == "external_conversation_item":
                return {"queued": False, "item_id": body.get("data", {}).get("source_id")}
            if body.get("type") == "external_session_status":
                return {"status": "ok"}
        if isinstance(body, list):
            return [{"queued": False, "item_id": r.get("data", {}).get("source_id")} for r in body]
        return {}

    server, thread, base_url = _start_recording_server_with_responses(recording_handler)

    try:
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_race_test",
                bridge_dir=bridge_dir,
                agent_name="claude-native-test",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )

        if hook_snapshot == "slow":
            await asyncio.wait_for(snapshot_entered.wait(), timeout=10.0)
            assert not snapshot_finished.is_set(), "hook snapshot I/O blocked the event loop"
            snapshot_released.set()

        # Wait for item + idle to appear.
        item_idx = None
        idle_idx = None
        for _ in range(200):
            for i, b in enumerate(request_bodies):
                if (
                    isinstance(b, dict)
                    and b.get("type") == "external_conversation_item"
                    and "VERIFIED_FINAL_VERDICT" in str(b)
                ):
                    item_idx = i
                if (
                    isinstance(b, dict)
                    and b.get("type") == "external_session_status"
                    and b.get("data", {}).get("status") == "idle"
                ):
                    idle_idx = i
            if item_idx is not None and idle_idx is not None:
                break
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert item_idx is not None, (
            f"Item not posted; requests: {[str(b)[:80] for b in request_bodies[-5:]]}"
        )
        assert idle_idx is not None, f"Idle not posted; got {len(request_bodies)} requests"
        assert item_idx < idle_idx, (
            f"idle ({idle_idx}) was posted before the final reply ({item_idx})"
        )

        # Idle posted exactly once.
        idle_count = sum(
            1
            for b in request_bodies
            if isinstance(b, dict) and b.get("data", {}).get("status") == "idle"
        )
        assert idle_count == 1, f"Expected 1 idle, got {idle_count}"

    finally:
        snapshot_released.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
