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
    status_updated_at: int = 1785480000000,
    blocked_on: str | None = None,
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
                "statusUpdatedAt": status_updated_at,
                "updatedAt": status_updated_at,
                **({"waitingFor": blocked_on} if blocked_on is not None else {}),
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
        on_status=lambda status, _reason: published.append(status),
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
        on_status=lambda status, _reason: published.append(status),
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
        on_status=lambda status, _reason: published.append(status),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert poller.active and published == [RUNNING]

    path.unlink()
    poller.tick()
    assert not poller.active


def test_shell_status_maps_to_idle(tmp_path: Path) -> None:
    """``shell`` is Claude's idle-family value, not a working one.

    The interactive writer substitutes ``shell`` for ``idle`` while a shell is
    attached. Leaving it unmapped made the read unparseable, so the transition
    was dropped and the session kept whatever status it already had.
    """
    path = _write_session_file(tmp_path / "sessions", pid=7, session_id="s", status="shell")
    status = read_session_status(path)
    assert status is not None
    assert status.runner_status == IDLE
    assert status.raw_status == "shell"


def test_unknown_status_clears_dedup_so_next_read_publishes(tmp_path: Path) -> None:
    """An unrecognized literal must not swallow the next real edge.

    The file is an undocumented internal detail whose vocabulary can grow. A
    value we cannot map leaves us blind to that transition, so the dedup
    baseline has to drop — otherwise the next readable status looks like a
    duplicate of a stale one and never publishes.
    """
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=lambda status, _reason: published.append(status),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [RUNNING]

    # A literal this version does not know about.
    _write_session_file(sessions, pid=1, session_id="s", status="teleporting")
    poller.tick()
    assert published == [RUNNING]

    # Back to a value we understand — same runner status as before, but our
    # knowledge lapsed in between, so it must publish rather than dedup.
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    poller.tick()
    assert published == [RUNNING, RUNNING]


def test_stale_busy_is_reported_as_written(tmp_path: Path) -> None:
    """A long-standing ``busy`` still reads as running — no freshness window.

    Claude reports ``busy`` while a delegate or background shell works, which
    can outlast the turn that started it, and that is correct: it *is* still
    doing work. The file says what Claude is doing, so we report it as written
    rather than timing it out and second-guessing with a pane diff.
    """
    sessions = tmp_path / "sessions"
    now = 1785480100.0
    _write_session_file(
        sessions,
        pid=1,
        session_id="s",
        status="busy",
        status_updated_at=int((now - 3600) * 1000),
    )
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=lambda status, _reason: published.append(status),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [RUNNING]
    assert poller.active is True


def test_retire_stops_the_file_owning_status(tmp_path: Path) -> None:
    """A dead pane retires the poller, even with a readable file left behind.

    Claude does not unlink its status file when killed, so the record survives
    holding whatever it last said — here a ``waiting`` that would otherwise
    keep asserting the session is parked mid-turn forever. Pane death is the
    one thing the file cannot report, so the watcher retires the poller and
    the PTY side owns the outcome.
    """
    sessions = tmp_path / "sessions"
    _write_session_file(
        sessions, pid=1, session_id="s", status="waiting", blocked_on="input needed"
    )
    published: list[tuple[str, str | None]] = []
    poller = SessionStatusPoller(
        on_status=lambda status, reason: published.append((status, reason)),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [(RUNNING, "input needed")]
    assert poller.active is True

    poller.retire()
    assert poller.active is False

    # The file is still there and still readable; a retired poller reads it no
    # more, so its stale value cannot keep owning the session's status.
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    poller.tick()
    assert published == [(RUNNING, "input needed")]


def test_resync_republishes_an_unchanged_file(tmp_path: Path) -> None:
    """A resync makes the next tick re-assert the file's current value.

    The file is rewritten only when its value *changes*, so a poller mid-turn
    has nothing more to say — which strands the server when the SERVER is what
    restarted and lost its cache. A resync drops the edge/mtime baselines so the
    same value publishes again.
    """
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[tuple[str, str | None]] = []
    poller = SessionStatusPoller(
        on_status=lambda status, reason: published.append((status, reason)),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [(RUNNING, None)]

    # Unchanged file: normally silent, which is the whole problem.
    poller.tick()
    assert published == [(RUNNING, None)]

    poller.resync()
    poller.tick()
    assert published == [(RUNNING, None), (RUNNING, None)]
    # Still reading the same resolved file — a resync re-asserts a working
    # poller rather than restarting resolution.
    assert poller.active is True


def test_resync_does_not_revive_a_retired_poller(tmp_path: Path) -> None:
    """A retired poller stays retired across a reconnect.

    Retirement means the pane's process is gone (or no file ever resolved), so
    the PTY side owns the outcome. A reconnect must not hand ownership back to a
    dead Claude's leftover record.
    """
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[str] = []
    poller = SessionStatusPoller(
        on_status=lambda status, _reason: published.append(status),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    poller.retire()

    poller.resync()
    poller.tick()

    assert poller.active is False
    assert published == [RUNNING]


def test_waiting_carries_its_reason(tmp_path: Path) -> None:
    """``waiting`` exposes ``waitingFor`` so the UI can say what it is parked on."""
    sessions = tmp_path / "sessions"
    parked = _write_session_file(
        sessions, pid=1, session_id="s", status="waiting", blocked_on="permission prompt"
    )
    status = read_session_status(parked)
    assert status is not None
    assert status.runner_status == RUNNING
    assert status.blocked_on == "permission prompt"

    # The writer merges updates into the existing record, so a reason left
    # behind by an earlier ``waiting`` must not leak onto a later status.
    busy = _write_session_file(
        sessions, pid=2, session_id="s", status="busy", blocked_on="permission prompt"
    )
    assert read_session_status(busy).blocked_on is None


def test_poller_publishes_when_only_the_reason_changes(tmp_path: Path) -> None:
    """``busy`` → ``waiting`` must publish even though both map to running.

    Deduping on the runner status alone would swallow the transition and the
    reason would never reach the UI.
    """
    sessions = tmp_path / "sessions"
    _write_session_file(sessions, pid=1, session_id="s", status="busy")
    published: list[tuple[str, str | None]] = []
    poller = SessionStatusPoller(
        on_status=lambda status, reason: published.append((status, reason)),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [(RUNNING, None)]

    _write_session_file(
        sessions, pid=1, session_id="s", status="waiting", blocked_on="dialog open"
    )
    poller.tick()
    assert published == [(RUNNING, None), (RUNNING, "dialog open")]
    assert poller.blocked_on == "dialog open"


def test_parked_session_stays_running_indefinitely(tmp_path: Path) -> None:
    """A dialog holds the session open however long it stays up.

    ``waiting`` clears only when Claude writes a new status, so an hour-old
    parked record still reports running — the pane is quiet the whole time and
    only the file can tell that from a finished turn.
    """
    sessions = tmp_path / "sessions"
    now = 1785480100.0
    _write_session_file(
        sessions,
        pid=1,
        session_id="s",
        status="waiting",
        status_updated_at=int((now - 3600) * 1000),
        blocked_on="input needed",
    )
    published: list[tuple[str, str | None]] = []
    poller = SessionStatusPoller(
        on_status=lambda status, reason: published.append((status, reason)),
        pane_pid_getter=_StubPidGetter(1),
        session_id_getter=lambda: "s",
        config_dir=tmp_path,
    )
    poller.tick()
    assert published == [(RUNNING, "input needed")]
    assert poller.blocked_on == "input needed"
