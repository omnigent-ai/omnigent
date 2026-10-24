"""Writers for the Claude mod bridge files that the status source reads.

The records follow the mod turn-event contract. The reader, poller, and runner
tests share these helpers so that every test writes the same shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.claude_native.mod_events import (
    MOD_EVENTS_FILE_NAME,
    MOD_STATE_FILE_NAME,
    SERVER_STATE_FILE_NAME,
)


def write_server_state(bridge_dir: Path, *, pid: int) -> None:
    """Write ``server.json`` as the serve-mcp server does.

    :param bridge_dir: Bridge directory, created when missing.
    :param pid: Pid the server reports, e.g. ``77``.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    payload = {"token": "test-token", "pid": pid, "updated_at": 0.0, "socket": "/tmp/s.sock"}
    (bridge_dir / SERVER_STATE_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def write_mod_heartbeat(
    bridge_dir: Path,
    *,
    server_pid: int,
    updated_at: float,
    main_turn_id: str | None,
) -> None:
    """Write ``mod.json`` as the server does on each mod heartbeat.

    :param bridge_dir: Bridge directory, created when missing.
    :param server_pid: Pid of the serve-mcp server that wrote the heartbeat.
    :param updated_at: Server clock (unix seconds) of the heartbeat.
    :param main_turn_id: Main-loop turn the mod believes is running, e.g. ``"t1"``,
        or ``None`` when it believes none is.
    """
    write_raw_mod_heartbeat(
        bridge_dir,
        {
            "v": 1,
            "mod_version": "0.1.0",
            "claude_version": "2.1.301",
            "claude_session_id": "claude-mod",
            "main_turn_id": main_turn_id,
            "server_pid": server_pid,
            "updated_at": updated_at,
        },
    )


def write_raw_mod_heartbeat(bridge_dir: Path, payload: dict[str, object]) -> None:
    """Write an arbitrary ``mod.json`` payload, for incomplete or malformed heartbeats.

    :param bridge_dir: Bridge directory, created when missing.
    :param payload: The exact JSON object to write.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / MOD_STATE_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def turn_start_record(
    turn_id: str, *, at: float, agent_id: str | None = None
) -> dict[str, object]:
    """Build a ``turn.start`` record.

    :param turn_id: Turn identifier, e.g. ``"t1"``.
    :param at: Server clock (unix seconds) of receipt.
    :param agent_id: Subagent or teammate id, or ``None`` for the main loop.
    :returns: The record as a JSON-serializable mapping.
    """
    return {
        "v": 1,
        "kind": "turn.start",
        "at": at,
        "claude_session_id": "claude-mod",
        "turn_id": turn_id,
        "agent_id": agent_id,
    }


def turn_complete_record(
    turn_id: str, *, at: float, agent_id: str | None = None, reason: str = "answer"
) -> dict[str, object]:
    """Build a ``turn.complete`` record.

    :param turn_id: Turn identifier, e.g. ``"t1"``.
    :param at: Server clock (unix seconds) of receipt.
    :param agent_id: Subagent or teammate id, or ``None`` for the main loop.
    :param reason: One of ``"answer"``, ``"aborted"``, ``"refusal"``, ``"error"``.
    :returns: The record as a JSON-serializable mapping.
    """
    record = turn_start_record(turn_id, at=at, agent_id=agent_id)
    record["kind"] = "turn.complete"
    record["reason"] = reason
    return record


def append_event_text(bridge_dir: Path, text: str) -> None:
    """Append raw text to ``mod_events.jsonl``, for partial or malformed lines.

    :param bridge_dir: Bridge directory, created when missing.
    :param text: Exact text to append, with no newline added.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    with (bridge_dir / MOD_EVENTS_FILE_NAME).open("a", encoding="utf-8") as handle:
        handle.write(text)


def append_event_record(bridge_dir: Path, record: dict[str, object]) -> None:
    """Append one record as a newline-terminated JSON line.

    :param bridge_dir: Bridge directory, created when missing.
    :param record: Record to serialize, e.g. ``turn_start_record("t1", at=1.0)``.
    """
    append_event_text(bridge_dir, json.dumps(record) + "\n")
