"""Schema and tail reader for the Claude Code mod's turn events.

The mod runs inside the Claude Code process as a function-hooks plugin. The
serve-mcp server rewrites ``<bridge_dir>/mod.json`` on every mod heartbeat and
appends one JSON record per event to ``<bridge_dir>/mod_events.jsonl``. This
module is the consumer side of that contract. It imports nothing from the bridge,
so the producer can import the same records without an import cycle.

Records with a ``v`` other than 1 and records of unknown kinds are ignored, and
unknown fields are ignored. Fields the consumer acts on are required; diagnostic
fields are optional.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from omnigent.harnesses.claude_native.status_file import _read_json_file

MOD_SCHEMA_VERSION = 1
MOD_STATE_FILE_NAME = "mod.json"
MOD_EVENTS_FILE_NAME = "mod_events.jsonl"
SERVER_STATE_FILE_NAME = "server.json"
# Three 30-second heartbeat intervals. An older mod.json means the mod is gone.
MOD_HEARTBEAT_MAX_AGE_S = 90.0
# Writer and reader share the host clock, so only a write racing the read looks newer.
MOD_HEARTBEAT_FUTURE_SKEW_S = 5.0
# Bytes sampled before a cursor. A mismatch means the file was truncated or recreated.
_CURSOR_FINGERPRINT_BYTES = 256
_TURN_START_KIND = "turn.start"
_TURN_COMPLETE_KIND = "turn.complete"


@dataclass(frozen=True)
class ModHeartbeat:
    """The latest ``mod.json`` heartbeat.

    :param mod_version: Mod build version. Diagnostic only.
    :param claude_version: Claude Code version. Diagnostic only.
    :param claude_session_id: Claude session uuid the mod is attached to.
    :param main_turn_id: Main-loop turn the mod believes is running, or ``None``
        when it believes none is.
    :param server_pid: Pid of the serve-mcp server that wrote the heartbeat.
    :param updated_at: Server clock (unix seconds) of the heartbeat.
    """

    mod_version: str | None
    claude_version: str | None
    claude_session_id: str | None
    main_turn_id: str | None
    server_pid: int
    updated_at: float


@dataclass(frozen=True)
class ModTurnStart:
    """A turn began.

    :param at: Server clock (unix seconds) when the server received the event.
    :param claude_session_id: Claude session uuid, when the record carries one.
    :param turn_id: Identifier of the turn.
    :param agent_id: Subagent or teammate id, or ``None`` for the main loop.
    """

    at: float
    claude_session_id: str | None
    turn_id: str
    agent_id: str | None


@dataclass(frozen=True)
class ModTurnComplete:
    """A turn ended.

    :param at: Server clock (unix seconds) when the server received the event.
    :param claude_session_id: Claude session uuid, when the record carries one.
    :param turn_id: Identifier of the turn.
    :param agent_id: Subagent or teammate id, or ``None`` for the main loop.
    :param reason: Why the turn ended, e.g. ``"answer"`` or ``"aborted"``.
    """

    at: float
    claude_session_id: str | None
    turn_id: str
    agent_id: str | None
    reason: str | None


ModEvent = ModTurnStart | ModTurnComplete


@dataclass(frozen=True)
class ModEventsCursor:
    """Position in ``mod_events.jsonl`` just past the last consumed record.

    :param byte_offset: Byte offset of the next unread record, e.g. ``0``.
    :param fingerprint: Hash of the bytes before ``byte_offset``. ``None`` when
        the cursor is at the start.
    """

    byte_offset: int = 0
    fingerprint: str | None = None


@dataclass(frozen=True)
class ModEventsRead:
    """Result of one incremental read of ``mod_events.jsonl``.

    :param cursor: Cursor to pass to the next read.
    :param events: Recognized events appended since the previous cursor, in
        file order.
    """

    cursor: ModEventsCursor
    events: list[ModEvent]


def parse_mod_heartbeat(payload: object) -> ModHeartbeat | None:
    """Validate a decoded ``mod.json`` payload.

    :param payload: Decoded JSON value, e.g. ``{"v": 1, "server_pid": 42, ...}``.
    :returns: The heartbeat, or ``None`` when the payload is not a v1 heartbeat
        with an integer ``server_pid``, a finite ``updated_at``, and a
        ``main_turn_id`` that is a string or null. A heartbeat without
        ``main_turn_id`` is not usable.
    """
    if not isinstance(payload, dict) or _as_int(payload.get("v")) != MOD_SCHEMA_VERSION:
        return None
    if "main_turn_id" not in payload:
        return None
    main_turn_id = payload["main_turn_id"]
    if main_turn_id is not None and not isinstance(main_turn_id, str):
        return None
    server_pid = _as_int(payload.get("server_pid"))
    updated_at = _finite_seconds(payload.get("updated_at"))
    if server_pid is None or updated_at is None:
        return None
    return ModHeartbeat(
        mod_version=_optional_text(payload.get("mod_version")),
        claude_version=_optional_text(payload.get("claude_version")),
        claude_session_id=_optional_text(payload.get("claude_session_id")),
        main_turn_id=main_turn_id,
        server_pid=server_pid,
        updated_at=updated_at,
    )


def parse_mod_event(payload: object) -> ModEvent | None:
    """Validate one decoded ``mod_events.jsonl`` record.

    :param payload: Decoded JSON value, e.g. ``{"v": 1, "kind": "turn.start", ...}``.
    :returns: The typed event, or ``None`` for an incompatible version, an unknown
        kind, or a known kind that lacks a required field.
    """
    if not isinstance(payload, dict) or _as_int(payload.get("v")) != MOD_SCHEMA_VERSION:
        return None
    turn_id = payload.get("turn_id")
    if "agent_id" not in payload or not isinstance(turn_id, str):
        return None
    agent_id = payload["agent_id"]
    if agent_id is not None and not isinstance(agent_id, str):
        return None
    at = _finite_seconds(payload.get("at"))
    if at is None:
        return None
    claude_session_id = _optional_text(payload.get("claude_session_id"))
    kind = payload.get("kind")
    if kind == _TURN_START_KIND:
        return ModTurnStart(
            at=at,
            claude_session_id=claude_session_id,
            turn_id=turn_id,
            agent_id=agent_id,
        )
    if kind == _TURN_COMPLETE_KIND:
        return ModTurnComplete(
            at=at,
            claude_session_id=claude_session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            reason=_optional_text(payload.get("reason")),
        )
    return None


def heartbeat_is_current(
    heartbeat: ModHeartbeat | None,
    *,
    server_pid: int | None,
    now: float,
) -> bool:
    """Decide whether the mod's heartbeat may drive session status.

    :param heartbeat: Parsed ``mod.json``, or ``None`` when absent or invalid.
    :param server_pid: ``pid`` from ``server.json``, or ``None`` when unreadable.
    :param now: Server-clock time (unix seconds) to judge freshness against.
    :returns: ``True`` only when the heartbeat is at most ``MOD_HEARTBEAT_MAX_AGE_S``
        old, at most ``MOD_HEARTBEAT_FUTURE_SKEW_S`` ahead of ``now``, and was
        written by the server that ``server.json`` names.
    """
    if heartbeat is None or server_pid is None or heartbeat.server_pid != server_pid:
        return False
    age_s = now - heartbeat.updated_at
    return -MOD_HEARTBEAT_FUTURE_SKEW_S <= age_s <= MOD_HEARTBEAT_MAX_AGE_S


def read_mod_heartbeat(bridge_dir: Path) -> ModHeartbeat | None:
    """Read and validate ``<bridge_dir>/mod.json``.

    :param bridge_dir: Bridge directory, e.g. ``Path("/tmp/omnigent-1000/claude-native/ab12")``.
    :returns: The heartbeat, or ``None`` when the file is missing, unreadable, or invalid.
    """
    return parse_mod_heartbeat(_read_json_file(bridge_dir / MOD_STATE_FILE_NAME))


def read_server_pid(bridge_dir: Path) -> int | None:
    """Return the ``pid`` recorded in ``<bridge_dir>/server.json``.

    :param bridge_dir: Bridge directory.
    :returns: The server pid, or ``None`` when the file is missing or has no
        integer ``pid``.
    """
    payload = _read_json_file(bridge_dir / SERVER_STATE_FILE_NAME)
    if payload is None:
        return None
    return _as_int(payload.get("pid"))


def read_mod_events(path: Path, cursor: ModEventsCursor) -> ModEventsRead:
    """Read the records appended after ``cursor``.

    Only newline-terminated records are consumed, so a partial trailing record
    is retried on the next call. A cursor whose fingerprint no longer matches
    the file, or that points past its end, restarts from the beginning. That
    covers truncation and recreation.

    :param path: Path to ``mod_events.jsonl``.
    :param cursor: Cursor from the previous read, or ``ModEventsCursor()`` to
        start from the beginning.
    :returns: The new cursor and the recognized events in file order. A missing
        file yields no events and a reset cursor.
    """
    if cursor.byte_offset and (
        cursor.fingerprint is None
        or _cursor_fingerprint(path, cursor.byte_offset) != cursor.fingerprint
    ):
        cursor = ModEventsCursor()
    try:
        return _read_complete_records(path, cursor)
    except OSError:
        return ModEventsRead(cursor=cursor, events=[])


def _read_complete_records(path: Path, cursor: ModEventsCursor) -> ModEventsRead:
    """Parse the newline-terminated records from ``cursor`` to the end of the file.

    :param path: Path to ``mod_events.jsonl``.
    :param cursor: Validated cursor to read from.
    :returns: The new cursor and the recognized events.
    """
    events: list[ModEvent] = []
    position = cursor.byte_offset
    with path.open("rb") as handle:
        handle.seek(position)
        for raw_line in iter(handle.readline, b""):
            if not raw_line.endswith(b"\n"):
                break
            position += len(raw_line)
            event = _parse_record_line(raw_line)
            if event is not None:
                events.append(event)
    return ModEventsRead(
        cursor=ModEventsCursor(
            byte_offset=position,
            fingerprint=_cursor_fingerprint(path, position),
        ),
        events=events,
    )


def _parse_record_line(raw_line: bytes) -> ModEvent | None:
    """Decode and validate one complete record line.

    :param raw_line: Newline-terminated record bytes.
    :returns: The typed event, or ``None`` for malformed or unrecognized records.
    """
    try:
        payload = json.loads(raw_line.decode("utf-8"))
    except ValueError:
        return None
    return parse_mod_event(payload)


def _cursor_fingerprint(path: Path, byte_offset: int) -> str | None:
    """Hash the bytes just before a cursor, to detect a replaced file.

    :param path: Path to ``mod_events.jsonl``.
    :param byte_offset: Cursor position, e.g. ``512``.
    :returns: A SHA-256 digest, or ``None`` when the file is missing or shorter
        than ``byte_offset``.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if byte_offset > handle.tell():
                return None
            sample_start = max(0, byte_offset - _CURSOR_FINGERPRINT_BYTES)
            handle.seek(sample_start)
            sample = handle.read(byte_offset - sample_start)
    except OSError:
        return None
    return hashlib.sha256(byte_offset.to_bytes(8, "big") + sample).hexdigest()


def _as_int(value: object) -> int | None:
    """Return ``value`` when it is an integer that is not a boolean.

    :param value: Decoded JSON value.
    :returns: The integer, or ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _finite_seconds(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    :param value: Decoded JSON number, e.g. ``1785480000.5``.
    :returns: The float, or ``None`` for non-numbers, booleans, and non-finite values.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _optional_text(value: object) -> str | None:
    """Return ``value`` when it is a string, else ``None``.

    :param value: Decoded JSON value.
    :returns: The string, or ``None``.
    """
    return value if isinstance(value, str) else None
