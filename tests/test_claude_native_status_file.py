"""Tests for the Claude Code ``sessions/<pid>.json`` status reader.

Covers :mod:`omnigent.claude_native_status_file` — resolving the status
file for a launched Claude, mapping its interactive status to the runner
vocabulary, and the per-tick poller that turns it into status edges and
falls back to the PTY watcher when the file never appears or vanishes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from omnigent.claude_native_status_file import (
    IDLE,
    RUNNING,
    SessionStatusPoller,
    read_session_status,
    resolve_status_file,
    sessions_dir,
)


def _write_session_file(
    directory: Path,
    *,
    pid: int,
    session_id: str,
    status: str,
    kind: str = "interactive",
) -> Path:
    """Write a minimal ``<pid>.json`` matching Claude's schema.

    :param directory: The ``sessions`` directory to write into.
    :param pid: The pid the file is named by.
    :param session_id: The Claude session uuid recorded in the file.
    :param status: The raw status literal, e.g. ``"busy"``.
    :param kind: The session kind, ``"interactive"`` by default.
    :returns: The path written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": "/repo",
                "kind": kind,
                "status": status,
                "statusUpdatedAt": 1785480000000,
                "updatedAt": 1785480000000,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_sessions_dir_honors_config_dir_env(monkeypatch) -> None:
    """``CLAUDE_CONFIG_DIR`` overrides the ``~/.claude`` default."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/cfg")
    assert sessions_dir() == Path("/custom/cfg/sessions")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert sessions_dir() == Path.home() / ".claude" / "sessions"


def test_resolve_by_pid_with_session_cross_check(tmp_path: Path) -> None:
    """The pid file is used when its ``sessionId`` matches."""
    _write_session_file(tmp_path / "sessions", pid=42, session_id="sid-1", status="busy")
    path = resolve_status_file(pane_pid=42, expected_session_id="sid-1", config_dir=tmp_path)
    assert path is not None and path.name == "42.json"


def test_resolve_pid_mismatch_falls_back_to_scan(tmp_path: Path) -> None:
    """A wrong-pid file is rejected; a fresh sessionId match wins."""
    sessions = tmp_path / "sessions"
    # pid 99 exists but belongs to a different session.
    _write_session_file(sessions, pid=99, session_id="other", status="idle")
    # The real session's file is named by a pid we don't know.
    _write_session_file(sessions, pid=123, session_id="sid-x", status="busy")
    path = resolve_status_file(pane_pid=99, expected_session_id="sid-x", config_dir=tmp_path)
    assert path is not None and path.name == "123.json"


def test_resolve_cross_check_rejects_wrong_session(tmp_path: Path) -> None:
    """A pid file for a different session is not returned."""
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=7, session_id="not-me", status="busy")
    path = resolve_status_file(pane_pid=7, expected_session_id="mine", config_dir=tmp_path)
    assert path is None


def test_resolve_pre_hook_accepts_pid_only(tmp_path: Path) -> None:
    """Before the session id is known, the pid file is accepted as-is."""
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=7, session_id="sid", status="busy")
    path = resolve_status_file(pane_pid=7, expected_session_id=None, config_dir=tmp_path)
    assert path is not None and path.name == "7.json"


def test_resolve_scan_skips_stale_files(tmp_path: Path) -> None:
    """A matching but stale file is skipped by the freshness window."""
    sessions = tmp_path / "sessions"
    stale = _write_session_file(sessions, pid=5, session_id="sid", status="idle")
    old = time.time() - 10_000
    import os

    os.utime(stale, (old, old))
    # pid unknown, so only the scan runs — the stale file is filtered out.
    path = resolve_status_file(pane_pid=None, expected_session_id="sid", config_dir=tmp_path)
    assert path is None


def test_resolve_missing_pid_no_session_returns_none(tmp_path: Path) -> None:
    """No pid file and no session id → nothing to resolve."""
    (tmp_path / "sessions").mkdir()
    path = resolve_status_file(pane_pid=999, expected_session_id=None, config_dir=tmp_path)
    assert path is None


def test_read_status_maps_interactive_vocabulary(tmp_path: Path) -> None:
    """``busy``/``waiting`` → running, ``idle``/``shell`` → idle."""
    sessions = tmp_path / "sessions"
    busy = _write_session_file(sessions, pid=1, session_id="s", status="busy")
    waiting = _write_session_file(sessions, pid=2, session_id="s", status="waiting")
    idle = _write_session_file(sessions, pid=3, session_id="s", status="idle")
    shell = _write_session_file(sessions, pid=4, session_id="s", status="shell")
    assert read_session_status(busy).runner_status == RUNNING
    assert read_session_status(waiting).runner_status == RUNNING
    assert read_session_status(idle).runner_status == IDLE
    # Turn ended, background shell still alive → the agent loop is idle. Mapping
    # this to running would strand the composer queueing messages.
    assert read_session_status(shell).runner_status == IDLE
    # Raw status is preserved for future "needs input" surfacing.
    assert read_session_status(waiting).raw_status == "waiting"


def test_read_status_unknown_or_missing_is_none(tmp_path: Path) -> None:
    """An unrecognized status or missing file reads as ``None``."""
    sessions = tmp_path / "sessions"
    weird = _write_session_file(sessions, pid=1, session_id="s", status="???")
    assert read_session_status(weird) is None
    assert read_session_status(sessions / "nope.json") is None


class _StubPidGetter:
    """A pane-pid getter whose value can change across ticks."""

    def __init__(self, value: int | None) -> None:
        self.value = value

    def __call__(self) -> int | None:
        return self.value


def test_poller_publishes_edges_only(tmp_path: Path) -> None:
    """The poller fires the callback on transitions, not on every tick."""
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=published.append,
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )

    poller.tick()  # resolves + first read: busy → running
    assert poller.active
    assert published == [RUNNING]

    poller.tick()  # unchanged mtime → no-op
    assert published == [RUNNING]

    # Turn ends: file flips to idle.
    _write_session_file(sessions, pid=1, session_id="s", status="idle")
    poller.tick()
    assert published == [RUNNING, IDLE]


def test_poller_gives_up_when_file_never_appears(tmp_path: Path) -> None:
    """With no file (old Claude), the poller retires and stays inactive."""
    (tmp_path / "sessions").mkdir()
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=published.append,
        pane_pid_getter=_StubPidGetter(404),
        session_id_getter=lambda: None,
        config_dir=tmp_path,
    )
    # Drive well past the resolve-attempt cap.
    for _ in range(60):
        poller.tick()
    assert not poller.active
    assert published == []


def test_poller_deactivates_when_file_vanishes(tmp_path: Path) -> None:
    """A clean exit unlinks the file; the poller goes inactive so the PTY
    watcher reclaims status and exit detection."""
    sessions = tmp_path / "sessions"
    path = _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=published.append,
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert poller.active and published == [RUNNING]

    path.unlink()
    poller.tick()
    assert not poller.active
