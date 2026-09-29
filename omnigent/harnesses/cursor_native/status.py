"""Authoritative turn outcome from the cursor-native ``stop`` hook.

cursor-agent fires its ``stop`` hook when a turn completes, aborts, or fails (see
:func:`omnigent.harnesses.cursor_native.bridge.build_hooks_config`, which registers the
:mod:`omnigent.harnesses.cursor_native.usage` recorder). That hook is the ONLY
authoritative "this turn just finished" signal cursor-agent exposes: its SQLite
chat store (tailed by :mod:`omnigent.harnesses.cursor_native.forwarder`) carries no
turn-boundary marker, and the runner's PTY-activity watcher only drives the web
"Working…" spinner (a ``session.status`` edge) — which never wakes a parent
orchestrator.

So the stop hook ALSO appends a turn-end marker line here (alongside its usage
line), and the runner-owned
:func:`omnigent.harnesses.cursor_native.forwarder.forward_cursor_store_to_session` poll
loop tails it and POSTs an ``external_session_status`` event with its outcome — the SAME
server contract claude-/codex-/opencode-native use to mark a sub-agent turn
terminal and wake its parent's inbox. Without this a cursor-native sub-agent
finished silently and never notified the orchestrator.

Stdlib-only (no httpx) so importing it from the stop hook keeps the hook fast —
cursor blocks the turn end on the hook.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

#: Append-only log of per-turn outcomes written by the cursor ``stop`` hook.
TURN_END_FILE = "cursor_turn_end.jsonl"

#: Durable poster state: how many turn-end markers the forwarder has already
#: turned into an ``external_session_status`` post. Persisted so a
#: supervisor restart never re-wakes the parent for a turn it already reported.
_STATE_FILE = "cursor_status_forwarder.json"


def record_turn_end(bridge_dir: Path, payload: object | None = None) -> None:
    """Append one turn-outcome marker (called from the cursor ``stop`` hook).

    Fires on EVERY ended turn — including turns with no billable token usage,
    which :func:`omnigent.harnesses.cursor_native.usage.record_usage_payload` skips — so the
    parent wake never depends on a turn having produced usage. Best-effort and
    stdlib-only; the caller swallows failures so usage/idle capture never breaks
    the agent turn.

    :param bridge_dir: The cursor-native bridge dir (where the forwarder reads).
    :param payload: The cursor ``stop`` hook payload, if any — its
        ``generation_id`` and authoritative ``status`` are retained when present.
    """
    line: dict[str, object] = {"ts": time.time()}
    if isinstance(payload, dict):
        gen_id = payload.get("generation_id") or payload.get("conversation_id")
        if isinstance(gen_id, str) and gen_id:
            line["generation_id"] = gen_id
        hook_status = payload.get("status")
        if hook_status in ("completed", "aborted", "error"):
            line["status"] = hook_status
    bridge_dir.mkdir(parents=True, exist_ok=True)
    # A leading separator isolates this record from a previous short write.
    # Blank lines are ignored by readers, including older forwarders.
    with open(bridge_dir / TURN_END_FILE, "a", encoding="utf-8") as handle:
        handle.write("\n" + json.dumps(line, sort_keys=True) + "\n")


def count_turn_ends(bridge_dir: Path) -> int:
    """Return how many turn-end markers have been recorded (0 if none/unreadable).

    The marker file is append-only, so the line count is the number of turns that
    have ended since the terminal was (re-)created.
    """
    try:
        text = (bridge_dir / TURN_END_FILE).read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for raw in text.splitlines() if raw.strip())


def read_turn_outcome(bridge_dir: Path, turn_end: int) -> str:
    """Read the outcome at a snapshotted marker count, ignoring later appends.

    Legacy markers without a status retain their completed-turn behavior.
    Unreadable or corrupt markers raise so the forwarder can retry delivery.
    """
    if turn_end <= 0:
        raise ValueError("turn-end marker count must be positive")
    lines = (bridge_dir / TURN_END_FILE).read_text(encoding="utf-8").splitlines()
    markers = [raw for raw in lines if raw.strip()]
    marker = json.loads(markers[turn_end - 1])
    if not isinstance(marker, dict):
        raise ValueError("turn-end marker must be an object")
    hook_status = marker.get("status", "completed")
    if hook_status == "aborted":
        return "cancelled"
    if hook_status == "error":
        return "failed"
    if hook_status == "completed":
        return "completed"
    raise ValueError(f"unknown cursor turn outcome: {hook_status!r}")


def read_posted_count(bridge_dir: Path) -> int:
    """Load the count of turn-ends already POSTed (0 on cold/unreadable)."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    posted = data.get("posted") if isinstance(data, dict) else None
    return posted if isinstance(posted, int) and posted >= 0 else 0


def write_posted_count(bridge_dir: Path, posted: int) -> None:
    """Atomically persist the count of turn-ends already POSTed.

    Persisted only AFTER a successful status POST so a failed flush is retried (the
    unreported turn-ends stay unreported until the post lands).
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps({"posted": posted}), encoding="utf-8")
    os.replace(tmp, bridge_dir / _STATE_FILE)


def clear_cursor_status_state(bridge_dir: Path) -> None:
    """Remove the turn-end marker + poster state so a re-created terminal starts clean.

    Sibling of :func:`omnigent.harnesses.cursor_native.usage.clear_cursor_usage_state`: the
    runner calls this when it re-creates a cursor terminal so a stale marker count
    from a prior terminal can't make the new forwarder skip (or re-fire) idle.
    """
    for name in (TURN_END_FILE, _STATE_FILE):
        with contextlib.suppress(OSError):
            (bridge_dir / name).unlink()
