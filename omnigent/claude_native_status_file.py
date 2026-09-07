"""Resolve and read Claude Code's per-session status metadata file.

Claude Code writes a per-process JSON file at
``<config_dir>/sessions/<pid>.json`` (its internal "concurrentSessions"
registry, present since v2.1.139 — the file that also backs
``claude agents``). For an interactive session it carries a ``status``
field that flips ``idle`` ⇄ ``busy`` ⇄ ``waiting`` as the agent works. It
reports what Claude is doing rather than inferring it from pane redraws, so
it — not the tmux pane diff — is the session's running/idle status whenever
it is readable.

This module owns two pure pieces the claude-native status watcher builds
on:

- :func:`resolve_status_file` — find the file for a launched Claude,
  keyed primarily by its pid (which equals the tmux ``#{pane_pid}`` on
  the omnigent launch path), with a ``sessionId`` cross-check and a
  bounded directory scan as fallback.
- :func:`read_session_status` — read the file and map its interactive
  status to the runner's ``running`` / ``idle`` vocabulary.

The file is an **undocumented internal detail** of Claude Code, so the
watcher that consumes this treats an unresolved / unreadable file as
"fall back to the PTY watcher", never as an error.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# Runner-side status vocabulary the file maps onto. ``busy`` and
# ``waiting`` both mean "the turn is not finished" from the session's point
# of view, so both map to ``running``; ``waiting`` is distinguished for the
# UI by the ``waitingFor`` reason rather than by a separate status. (The
# session vocabulary's own ``waiting`` means something else entirely —
# "turn ended, background work remains" — and must not be reused here.)
RUNNING = "running"
IDLE = "idle"

# Claude interactive-session status literals. The interactive writer emits
# exactly ``busy`` / ``shell`` / ``idle`` / ``waiting``: ``busy`` while the
# turn is loading or a delegate is active, and ``waiting`` while a dialog
# owns the input.
_STATUS_TO_RUNNER: dict[str, str] = {
    "busy": RUNNING,
    "waiting": RUNNING,
    "idle": IDLE,
    # The turn ended but a background shell is still alive (Claude Code
    # >= v2.1.197). The agent loop is idle, so this maps to ``idle``; the
    # working indicator stays lit off the ``Stop`` hook's shell tally, which
    # carries the count this boolean literal cannot. Mapping ``shell`` to
    # ``running`` would strand the composer on the "(queued)" placeholder,
    # since the session never reads idle while a background shell runs.
    "shell": IDLE,
    # Background-job literals, mapped defensively for robustness.
    "running": RUNNING,
    "completed": IDLE,
    "failed": IDLE,
    "error": IDLE,
    "done": IDLE,
}

# Only consider files touched within this window when scanning by
# sessionId, so a heavy user's accumulated stale files (Claude does not
# always unlink on crash) never turn resolution into an unbounded read.
# A just-launched Claude's file is fresh by definition.
_SCAN_FRESHNESS_WINDOW_S = 120.0


@dataclass(frozen=True)
class SessionStatus:
    """A single read of the Claude session status file.

    :param runner_status: Mapped runner vocabulary — :data:`RUNNING` or
        :data:`IDLE`.
    :param raw_status: The file's own ``status`` string, e.g. ``"busy"``,
        ``"idle"``, ``"waiting"`` — kept for logging / future
        "needs input" surfacing.
    :param status_updated_at: The file's ``statusUpdatedAt`` epoch-ms
        value, or ``None`` when absent.
    :param blocked_on: The file's ``waitingFor`` reason when the raw status
        is ``waiting`` — a short human phrase, e.g. ``"permission prompt"``,
        ``"input needed"``, ``"dialog open"``. ``None`` otherwise.
    """

    runner_status: str
    raw_status: str
    status_updated_at: int | None
    blocked_on: str | None = None


def sessions_dir(config_dir: Path | None = None) -> Path:
    """Return Claude Code's ``sessions`` directory.

    Mirrors Claude's own resolution: ``CLAUDE_CONFIG_DIR`` when set,
    otherwise ``~/.claude`` — matching :mod:`omnigent.session_import.local`.

    :param config_dir: Explicit Claude config dir override (tests). When
        ``None`` the environment / home default is used.
    :returns: The ``<config_dir>/sessions`` path (not guaranteed to exist).
    """
    if config_dir is not None:
        return config_dir / "sessions"
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return home / "sessions"


def _read_json_file(path: Path) -> dict[str, object] | None:
    """Read and parse a JSON object file, or ``None`` on any failure.

    :param path: File to read.
    :returns: The parsed object, or ``None`` when missing, unreadable, or
        not a JSON object (a half-written file reads as ``None`` and the
        caller simply retries next tick).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _matches_session(
    record: dict[str, object],
    *,
    expected_session_id: str | None,
) -> bool:
    """Whether a status record belongs to the launched Claude session.

    When we know Claude's own session uuid (captured by the bridge from
    hook events), require an exact match. When we don't yet (the first
    hook has not fired), accept any interactive record — the pid-keyed
    lookup already scoped it to this process.

    :param record: A parsed ``<pid>.json`` object.
    :param expected_session_id: Claude session uuid to match, or ``None``
        to skip the cross-check.
    :returns: ``True`` when the record is a usable match.
    """
    if record.get("kind") != "interactive":
        return False
    if expected_session_id is None:
        return True
    return record.get("sessionId") == expected_session_id


def resolve_status_file(
    *,
    pane_pid: int | None,
    expected_session_id: str | None,
    config_dir: Path | None = None,
    now: float | None = None,
) -> Path | None:
    """Resolve the status file for a launched Claude session.

    Resolution order:

    1. **Pid lookup** (``<pane_pid>.json``): on the omnigent launch path
       tmux ``exec``s ``claude`` as the pane process, so the file's pid
       equals ``#{pane_pid}``. This is the common O(1) case.
    2. **SessionId scan**: if the pid file is absent or fails the
       cross-check (e.g. a shell/sandbox wrapper sits between the pane and
       ``claude``), scan recent files for a matching ``sessionId``. Only
       runs when ``expected_session_id`` is known, bounded to files
       modified within :data:`_SCAN_FRESHNESS_WINDOW_S`.

    :param pane_pid: The tmux pane pid, or ``None`` when unknown.
    :param expected_session_id: Claude session uuid from the bridge, or
        ``None`` before the first hook reports it.
    :param config_dir: Explicit Claude config dir override (tests).
    :param now: Monotonic-independent wall clock override (tests); uses
        :func:`time.time` when ``None``.
    :returns: The resolved file path, or ``None`` when no confident match
        exists yet (caller falls back to the PTY watcher).
    """
    directory = sessions_dir(config_dir)

    if pane_pid is not None:
        candidate = directory / f"{pane_pid}.json"
        record = _read_json_file(candidate)
        if record is not None and _matches_session(
            record, expected_session_id=expected_session_id
        ):
            return candidate

    if expected_session_id is None:
        return None

    clock = time.time() if now is None else now
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if clock - mtime > _SCAN_FRESHNESS_WINDOW_S:
            continue
        record = _read_json_file(entry)
        if record is not None and _matches_session(
            record, expected_session_id=expected_session_id
        ):
            return entry
    return None


def read_session_status(path: Path) -> SessionStatus | None:
    """Read a resolved status file and map it to the runner vocabulary.

    :param path: A status file previously returned by
        :func:`resolve_status_file`.
    :returns: The mapped :class:`SessionStatus`, or ``None`` when the file
        is missing, unreadable, or carries no recognized ``status`` (the
        caller keeps its previous status and retries next tick).
    """
    record = _read_json_file(path)
    if record is None:
        return None
    raw_status = record.get("status")
    if not isinstance(raw_status, str):
        return None
    runner_status = _STATUS_TO_RUNNER.get(raw_status)
    if runner_status is None:
        return None
    updated = record.get("statusUpdatedAt")
    status_updated_at = updated if isinstance(updated, int) else None
    # Only meaningful alongside ``waiting`` — the writer merges updates into
    # the existing record, so ignore any reason left over from an earlier one.
    reason = record.get("waitingFor") if raw_status == "waiting" else None
    return SessionStatus(
        runner_status=runner_status,
        raw_status=raw_status,
        status_updated_at=status_updated_at,
        blocked_on=reason if isinstance(reason, str) and reason else None,
    )


# Attempts to resolve the file before giving up and leaving the PTY
# watcher authoritative for the session's lifetime. At the claude-native
# poll cadence (~0.2s) this is a few seconds — long enough for a booting
# Claude to write its file and for the first hook to report the session
# id, short enough that an old Claude (pre-v2.1.139, no file) or a broken
# config dir falls back promptly without scanning forever.
_MAX_RESOLVE_ATTEMPTS = 40


class SessionStatusPoller:
    """Per-tick reader that turns the status file into status edges.

    Owns the small amount of state the claude-native watcher needs across
    ticks: the lazily-resolved file path, an mtime short-circuit so an
    unchanged file costs one ``stat``, and edge-deduping so a status
    callback fires only on ``running`` ⇄ ``idle`` transitions.

    Lifecycle, all on the single watcher thread (no lock needed):

    - **Resolving:** each :meth:`tick` retries :func:`resolve_status_file`
      until it locks on or :data:`_MAX_RESOLVE_ATTEMPTS` is exhausted.
    - **Active:** once resolved, :attr:`active` is ``True`` and each tick
      reads the file and fires the callback on a changed runner status.
    - **Exhausted:** if resolution never succeeds, :attr:`active` stays
      ``False`` permanently and the file contributes nothing.

    While :attr:`active` the poller *is* the session's status — it reports what
    Claude is doing, where the pane diff only infers it from redraws — and the
    PTY watcher publishes none. The watcher takes over when no file was ever
    resolved (Claude older than v2.1.139) and always owns pane death, which the
    file structurally cannot report: a killed Claude leaves its record behind
    (see :meth:`retire`).

    :param on_status: Callback invoked as ``(runner_status, blocked_on)``
        on each transition (and once on first read). Fires when either part
        changes, so ``busy`` → ``waiting`` still delivers its reason even
        though both map to :data:`RUNNING`. Must not block the watcher
        thread for long.
    :param pane_pid_getter: Returns the terminal's current pane pid, or
        ``None``. Called during resolution; on the omnigent launch path
        this pid names the status file.
    :param session_id_getter: Returns Claude's own session uuid once the
        bridge has captured it, or ``None`` before the first hook. Used to
        cross-check the pid file and to scan as a fallback.
    :param config_dir: Explicit Claude config dir override (tests).
    """

    def __init__(
        self,
        *,
        on_status: Callable[[str, str | None], None],
        pane_pid_getter: Callable[[], int | None],
        session_id_getter: Callable[[], str | None],
        config_dir: Path | None = None,
        omnigent_session_id: str | None = None,
    ) -> None:
        self._on_status = on_status
        self._pane_pid_getter = pane_pid_getter
        # Claude's own session uuid, resolved lazily from the bridge.
        self._session_id_getter = session_id_getter
        # The Omnigent conversation id this poller watches — for debug-log
        # attribution only (distinct from Claude's session uuid above).
        self._omnigent_session_id = omnigent_session_id
        self._config_dir = config_dir
        self._path: Path | None = None
        self._attempts = 0
        self._exhausted = False
        self._last_mtime: float | None = None
        self._last_edge: tuple[str, str | None] | None = None
        self._last_status: SessionStatus | None = None

    @property
    def active(self) -> bool:
        """Whether a resolved file is still being read.

        ``False`` while resolving, once resolution gave up, or after the
        file disappears (clean exit unlinks it). Status edges from the PTY
        watcher are published regardless — this only reports whether the
        file is contributing.
        """
        return self._path is not None and not self._exhausted

    def tick(self) -> None:
        """Advance one poll: resolve if needed, then publish on change.

        Safe to call every poll; a no-op once resolution is exhausted, and
        an unchanged active file costs a single ``stat``.
        """
        if self._exhausted:
            return
        if self._path is None:
            self._try_resolve()
            if self._path is None:
                return
        self._read_and_publish()

    def _try_resolve(self) -> None:
        """Attempt one resolution, retiring to the PTY watcher on timeout."""
        self._attempts += 1
        pane_pid = self._pane_pid_getter()
        path = resolve_status_file(
            pane_pid=pane_pid,
            expected_session_id=self._session_id_getter(),
            config_dir=self._config_dir,
        )
        if path is not None:
            # Log the hit: whether the file was found at all decides which
            # source owns the session's status, and without this the answer is
            # only reachable by re-deriving the resolution by hand.
            _logger.info(
                "claude status file resolved: path=%s attempts=%d pane_pid=%s",
                path,
                self._attempts,
                pane_pid,
                extra={"session_id": self._omnigent_session_id},
            )
            self._path = path
            return
        if self._attempts >= _MAX_RESOLVE_ATTEMPTS:
            _logger.warning(
                "claude status file never resolved after %d attempts "
                "(pane_pid=%s); session status falls back to the pane watcher",
                self._attempts,
                pane_pid,
                extra={"session_id": self._omnigent_session_id},
            )
            self._exhausted = True

    def retire(self) -> None:
        """Stop reading the file, permanently.

        Called when the pane's process is gone: a killed Claude does not unlink
        its file, so the record survives holding whatever it last said. Since
        the file owns the session's status while it is readable, a dead pane
        must retire it or that final value would pin the session forever.
        """
        _logger.info(
            "claude status file retired: path=%s",
            self._path,
            extra={"session_id": self._omnigent_session_id},
        )
        self._exhausted = True

    def resync(self) -> None:
        """Forget what was published so the next tick re-asserts the file.

        The file is written only when its value *changes*, so a poller that
        already published ``running`` has nothing more to say until Claude's
        status moves. That is a problem when the *listener* restarts: a server
        recycle wipes its status cache, and the session would sit on a stale
        ``idle`` for the rest of the turn because every source believes it
        already reported. Dropping the edge/mtime baselines makes the next tick
        publish the file's current value verbatim.

        Keeps the resolved path and the attempt count — this re-asserts a
        working poller, it does not restart resolution.
        """
        _logger.info(
            "claude status file resync: path=%s",
            self._path,
            extra={"session_id": self._omnigent_session_id},
        )
        self._last_mtime = None
        self._last_edge = None

    @property
    def blocked_on(self) -> str | None:
        """Why Claude is parked, when it is parked on a dialog.

        ``None`` unless the last read was ``waiting`` and carried a reason.
        """
        status = self._last_status
        if status is None or status.raw_status != "waiting":
            return None
        return status.blocked_on

    def _read_and_publish(self) -> None:
        """Read the resolved file and fire the callback on a status change."""
        assert self._path is not None
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            # File vanished (clean exit unlinks it, or a crash). Exit
            # detection rides the PTY watcher, so just stop polling.
            self._exhausted = True
            return
        if self._last_mtime is not None and mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        status = read_session_status(self._path)
        if status is None:
            # An unrecognized literal — the file is an undocumented internal
            # detail whose vocabulary can grow. We are now blind to this
            # transition, so drop the dedup baseline: the next readable
            # status must publish rather than be swallowed as a duplicate.
            self._last_status = None
            self._last_edge = None
            return
        self._last_status = status
        edge = (status.runner_status, status.blocked_on)
        if edge == self._last_edge:
            return
        self._last_edge = edge
        self._on_status(status.runner_status, status.blocked_on)
