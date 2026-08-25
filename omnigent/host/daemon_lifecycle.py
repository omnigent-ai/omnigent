"""Kernel-backed lifecycle guard binding a host daemon to its registry record.

A host daemon's record lives at ``<data-dir>/daemons/<target-hash>.json`` and
carries the owning ``pid``. This module lets the daemon *hold* that ownership
from inside its own process: it takes an exclusive ``flock`` on that record
file for its whole life (a held lock is a live daemon, immune to PID reuse) and
watches the same file. If the record is deleted (``omnigent host stop``) or its
``pid`` no longer matches (a newer daemon claimed the target), the daemon knows
it is stale and terminates itself.

The flock also serves reuse: :func:`record_flock_is_held` probes it so a
spin-up can tell a live daemon (reuse it) from a dead one (reap + respawn),
with a PID check as the fallback when the lock is free or unprobeable. This
relies on the record being rewritten in place (``Path.write_text``, see
``cli._write_daemon_record``): an atomic-rename write would swap the inode and
strand the daemon's flock on the old one, so takeover must keep modifying the
existing file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path

from omnigent.process_logging import data_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

_LOCAL_DAEMON_MARKER = "local"


def normalize_daemon_target(server_url: str | None) -> str:
    """Return the registry key for a daemon target.

    :param server_url: Requested server URL, or ``None`` / empty for local mode.
    :returns: ``"local"`` for local mode, else the URL without a trailing slash.
    """
    return _LOCAL_DAEMON_MARKER if not server_url else server_url.rstrip("/")


def _target_digest(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]


def daemon_registry_dir(base_dir: Path | None = None) -> Path:
    """Return the directory holding per-target daemon records.

    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :returns: ``<base>/daemons``.
    """
    return (base_dir if base_dir is not None else data_dir()) / "daemons"


def daemon_record_path(target: str, *, base_dir: Path | None = None) -> Path:
    """Return the JSON record path for *target*.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :returns: JSON record path.
    """
    return daemon_registry_dir(base_dir) / f"{_target_digest(target)}.json"


def record_flock_is_held(record_path: Path) -> bool | None:
    """Return whether a live process holds the record's flock.

    The owning daemon holds this lock for its whole life and the kernel
    releases it on death (crash / ``SIGKILL`` included), so it is a liveness
    signal immune to PID reuse: a held lock means the owner is alive; a free
    lock means it is gone even if its PID was recycled.

    :param record_path: The daemon's ``<hash>.json`` record.
    :returns: ``True`` if the lock is held (owner alive), ``False`` if it is
        free (owner dead), or ``None`` when it can't be determined (no
        ``fcntl``, or the record is missing / unreadable) so the caller falls
        back to a PID check.
    """
    if fcntl is None:
        return None
    try:
        fd = os.open(record_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        # We took it — the owner is dead. Release at once so we never linger
        # holding another daemon's lock.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


class DaemonLifecycleLock:
    """Kernel-backed ownership handle for one daemon target's registry record."""

    def __init__(
        self,
        *,
        target: str,
        record_path: Path,
        pid: int,
    ) -> None:
        """Initialize the lifecycle lock.

        :param target: Normalized daemon target, e.g. ``"local"``.
        :param record_path: JSON record this daemon flocks and must keep owning.
        :param pid: This daemon's process id.
        """
        self._target = target
        self._record_path = record_path
        self._pid = pid
        self._fd: int | None = None

    @classmethod
    def for_target(
        cls,
        target: str,
        *,
        base_dir: Path | None = None,
        pid: int | None = None,
    ) -> DaemonLifecycleLock:
        """Build a lock for *target* from the standard registry layout.

        :param target: Normalized daemon target, e.g. ``"local"``.
        :param base_dir: Data-directory override; defaults to :func:`data_dir`.
        :param pid: Process id to claim; defaults to the current process.
        :returns: An unacquired :class:`DaemonLifecycleLock`.
        """
        return cls(
            target=target,
            record_path=daemon_record_path(target, base_dir=base_dir),
            pid=pid if pid is not None else os.getpid(),
        )

    @property
    def target(self) -> str:
        """Return the daemon target this lock guards."""
        return self._target

    def acquire(self) -> bool:
        """Take the exclusive lifetime lock on the record file.

        Best-effort: a failure (no ``fcntl``, contended lock, IO error) is
        logged and reported, never raised — the record-based ownership check
        still guards the daemon even without a held lock. The record's content
        is owned by the CLI, so we only open + lock it, never truncate or write
        (that would clobber the record the launching CLI wrote).

        :returns: ``True`` if the flock is now held by this process.
        """
        if fcntl is None:
            return False
        fd: int | None = None
        try:
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self._record_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _logger.warning(
                "daemon lifecycle lock acquire failed for %s", self._target, exc_info=True
            )
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            return False
        self._fd = fd
        return True

    def still_owner(self) -> bool:
        """Return whether the on-disk record still names this process.

        A missing record (``host stop`` deleted it) or a different ``pid`` (a
        newer daemon claimed the target) both mean this daemon is stale. A
        transient read error or a partially-written record is treated as
        "still owner" so a momentary glitch never triggers self-termination;
        the next poll re-checks.

        :returns: ``True`` while the record exists and its ``pid`` matches;
            ``False`` when the record is gone or reassigned.
        """
        try:
            raw = self._record_path.read_text()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        try:
            payload = json.loads(raw)
            record_pid = int(payload["pid"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return True
        return record_pid == self._pid

    def release(self) -> None:
        """Release the flock and close the fd, leaving the record in place.

        :returns: None.
        """
        if self._fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
