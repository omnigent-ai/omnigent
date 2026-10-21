"""Durable, allowlisted Claude hook evidence, independent of debug-log capture."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import cast

from filelock import FileLock

from omnigent.inner.terminal_lifecycle import (
    TERMINAL_INSTANCE_ID_ENV,
    TERMINAL_LAUNCH_ID_ENV,
    TERMINAL_LAUNCH_SESSION_ID_ENV,
)

_EVENTS = {"SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "StopFailure", "PreCompact"}
_REASONS = {
    "prompt_input_exit",
    "clear",
    "logout",
    "session_close",
    "signal",
    "bypass_permissions_disabled",
    "other",
    "unknown",
}
_SOURCES = {"startup", "resume", "clear", "compact"}
_SIGNALS = {"SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"}
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_LAUNCH_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_EVENTS = 16
_MAX_BYTES = 32768
_MAX_OBSERVATIONS = 1_000_000


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _known(value: object, values: set[str]) -> str | None:
    return value if isinstance(value, str) and value in values else None


def _timestamp(value: object) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        with contextlib.suppress(OverflowError):
            if math.isfinite(value) and value > 0:
                return float(value)
    return None


def _path(bridge_dir: Path, launch_id: str) -> Path:
    if not _LAUNCH_ID.fullmatch(launch_id):
        raise ValueError("Invalid terminal launch identity")
    return bridge_dir / f"lifecycle-{launch_id}.json"


def _read(path: Path) -> dict[str, object] | None:
    """Bound input and reject links/devices before reading any persisted evidence."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Lifecycle input is not an owned regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise ValueError("Lifecycle input exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Lifecycle input is not a record")
    return value


def _event(value: object) -> dict[str, object] | None:
    """Project persisted records too, so arbitrary file fields never become logs."""
    if not isinstance(value, dict):
        return None
    name = _known(value.get("event_name"), _EVENTS)
    recorded_at = _timestamp(value.get("recorded_at"))
    if name is None or recorded_at is None:
        return None
    event: dict[str, object] = {
        "event_id": _identifier(value.get("event_id")),
        "event_name": name,
        "recorded_at": recorded_at,
        "timestamp_source": "hook_received",
        "claude_session_id": _identifier(value.get("claude_session_id")),
        "bridge_session_id": _identifier(value.get("bridge_session_id")),
    }
    if name == "SessionStart":
        event["source"] = _known(value.get("source"), _SOURCES) or "unknown"
        event["identity_started_at"] = min(
            _timestamp(value.get("identity_started_at")) or recorded_at, recorded_at
        )
    if name == "SessionEnd":
        event["reason"] = _known(value.get("reason"), _REASONS) or "unknown"
        event["reason_status"] = (
            _known(value.get("reason_status"), {"reported", "missing", "unrecognized"})
            or "unrecognized"
        )
        event["signal"] = _known(value.get("signal"), _SIGNALS)
        event["last_recorded_at"] = _timestamp(value.get("last_recorded_at")) or recorded_at
        count = value.get("observation_count")
        event["observation_count"] = (
            count if type(count) is int and 1 <= count <= _MAX_OBSERVATIONS else 1
        )
    return event


def _events(value: object, omitted: object) -> tuple[list[dict[str, object]], int]:
    """Bound valid history and count malformed and overflow records once."""
    omitted_count = max(0, omitted) if type(omitted) is int else 0
    if not isinstance(value, list):
        return [], omitted_count
    events = [event for item in value if (event := _event(item)) is not None]
    events.sort(key=lambda item: cast(float, item["recorded_at"]))
    retained = events[-_MAX_EVENTS:]
    return retained, omitted_count + len(value) - len(retained)


def record_hook_lifecycle(
    bridge_dir: Path, payload: dict[str, object], recorded_at: float
) -> None:
    """Persist source evidence before rotations/forwarding; never affect the hook result."""
    try:
        name = _known(payload.get("hook_event_name"), _EVENTS)
        if name is None or payload.get("agent_id"):
            return
        instance_id = os.environ.get(TERMINAL_INSTANCE_ID_ENV, "")
        launch_id = os.environ.get(TERMINAL_LAUNCH_ID_ENV, "")
        if not _LAUNCH_ID.fullmatch(instance_id) or not _LAUNCH_ID.fullmatch(launch_id):
            return
        from omnigent.harnesses.claude_native.bridge import (
            _ensure_secure_dir,
            _write_json_file,
            read_active_session_id,
        )

        _ensure_secure_dir(bridge_dir)
        path = _path(bridge_dir, launch_id)
        event: dict[str, object] = {
            "event_id": uuid.uuid4().hex,
            "event_name": name,
            "recorded_at": recorded_at,
            "claude_session_id": _identifier(payload.get("session_id")),
            "bridge_session_id": _identifier(read_active_session_id(bridge_dir)),
            "source": _known(payload.get("source"), _SOURCES),
        }
        if name == "SessionEnd":
            reason = _known(payload.get("reason"), _REASONS)
            event.update(
                reason=reason or "unknown",
                reason_status=(
                    "reported"
                    if reason
                    else "missing"
                    if payload.get("reason") is None
                    else "unrecognized"
                ),
                signal=_known(payload.get("signal"), _SIGNALS),
            )
        projected = _event(event)
        if projected is None:
            return
        # A stuck peer must not hold up a SessionEnd hook indefinitely.
        with FileLock(str(path) + ".lock", mode=0o600, timeout=0.5):
            previous = _read(path)
            if previous is not None and (
                previous.get("schema_version") != 1
                or previous.get("terminal_instance_id") != instance_id
                or previous.get("launch_id") != launch_id
            ):
                return
            previous = previous or {}
            events, omitted = _events(previous.get("events"), previous.get("events_omitted"))
            session_start = _event(previous.get("session_start"))
            if session_start is not None and session_start["event_name"] != "SessionStart":
                session_start = None
            if name == "SessionStart" and (
                session_start is None or recorded_at >= cast(float, session_start["recorded_at"])
            ):
                if (
                    session_start is not None
                    and projected["source"] == "compact"
                    and projected["claude_session_id"] is not None
                    and projected["claude_session_id"] == session_start["claude_session_id"]
                ):
                    # Compaction continues this identity's existing turn history.
                    projected["identity_started_at"] = session_start["identity_started_at"]
                session_start = projected
            if (
                name == "SessionEnd"
                and events
                and all(
                    events[-1].get(key) == projected.get(key)
                    for key in (
                        "event_name",
                        "claude_session_id",
                        "bridge_session_id",
                        "reason",
                        "reason_status",
                        "signal",
                    )
                )
            ):
                last = events[-1]
                last["recorded_at"] = min(cast(float, last["recorded_at"]), recorded_at)
                last["last_recorded_at"] = max(cast(float, last["last_recorded_at"]), recorded_at)
                last["observation_count"] = min(
                    cast(int, last["observation_count"]) + 1, _MAX_OBSERVATIONS
                )
            else:
                events.append(projected)
            events.sort(key=lambda item: cast(float, item["recorded_at"]))
            _write_json_file(
                path,
                {
                    "schema_version": 1,
                    "terminal_instance_id": instance_id,
                    "launch_id": launch_id,
                    "launch_session_id": _identifier(
                        os.environ.get(TERMINAL_LAUNCH_SESSION_ID_ENV)
                    ),
                    "session_start": session_start,
                    "events": events[-_MAX_EVENTS:],
                    "events_omitted": omitted + max(0, len(events) - _MAX_EVENTS),
                },
            )
    except Exception:  # noqa: BLE001 - telemetry must never block Claude's shutdown.
        # The exception or payload can contain paths or user input.
        with contextlib.suppress(Exception):
            print("omnigent claude lifecycle: could not record hook evidence", file=sys.stderr)


def read_lifecycle_snapshot(
    bridge_dir: Path | None, instance_id: str, launch_id: str
) -> dict[str, object]:
    """Read only this launch's evidence; missing hooks do not imply a voluntary exit."""
    result: dict[str, object] = {
        "session_end_reason": "unknown",
        "session_end_evidence": "not_observed",
        "session_end": None,
        "recent_hooks": [],
    }
    if bridge_dir is None:
        result["read_status"] = "bridge_unavailable"
        return result
    try:
        state = _read(_path(bridge_dir, launch_id))
        if state is None:
            result["read_status"] = "not_found"
            return result
        if state.get("schema_version") != 1:
            result["read_status"] = "unsupported_schema"
            return result
        if state.get("launch_id") != launch_id or state.get("terminal_instance_id") != instance_id:
            result["read_status"] = "identity_mismatch"
            return result
        events, omitted = _events(state.get("events"), state.get("events_omitted"))
        session_start = _event(state.get("session_start"))
        if session_start is not None and session_start["event_name"] != "SessionStart":
            session_start = None
        current_session = (
            _identifier(session_start["claude_session_id"]) if session_start else None
        )
        started_at = cast(float, session_start["identity_started_at"]) if session_start else 0.0
        identity_verified = current_session is not None
        current_end: dict[str, object] | None = None
        turn_in_progress: bool | None = None
        for event in events:
            if cast(float, event["recorded_at"]) < started_at:
                continue
            name = event["event_name"]
            if name == "SessionStart":
                continues_turn = (
                    event["source"] == "compact"
                    and current_session is not None
                    and event["claude_session_id"] == current_session
                )
                current_session = _identifier(event["claude_session_id"])
                current_end = None
                if not continues_turn:
                    turn_in_progress = None
                continue
            if current_session is None and session_start is None:
                current_session = _identifier(event["claude_session_id"])
            if event["claude_session_id"] == current_session and current_session is not None:
                if name == "UserPromptSubmit":
                    current_end = None
                    turn_in_progress = True
                elif name in {"Stop", "StopFailure"}:
                    turn_in_progress = False
                elif name == "SessionEnd" and current_end is None:
                    current_end = event
        result.update(
            read_status="ok",
            launch_session_id=_identifier(state.get("launch_session_id")),
            claude_session_id=current_session,
            session_start=session_start,
            hook_turn_in_progress=turn_in_progress,
            recent_hooks=events,
            events_omitted=omitted,
        )
        if current_end is not None:
            result.update(
                session_end=current_end,
                session_end_reason=current_end["reason"],
                session_end_evidence="claude_hook",
                session_end_identity="matched" if identity_verified else "unverified",
            )
        return result
    except Exception:  # noqa: BLE001 - unreadable telemetry cannot change the terminal outcome.
        result["read_status"] = "unreadable"
        return result
