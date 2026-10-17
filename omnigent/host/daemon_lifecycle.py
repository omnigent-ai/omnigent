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
relies on the record being rewritten in place (see :func:`write_daemon_record`):
an atomic-rename write would swap the inode and strand the daemon's flock on
the old one, so takeover must keep modifying the existing file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from omnigent.process_logging import data_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

_LOCAL_DAEMON_MARKER = "local"
DAEMON_CONFIG_SIG_ENV_VAR = "OMNIGENT_HOST_DAEMON_CONFIG_SIG"


@dataclass(frozen=True)
class HostDaemonRecord:
    """Registry metadata for one host daemon process.

    :param pid: Process id of the daemon.
    :param target: Normalized server URL or ``"local"``.
    :param mode: Launch mode, either ``"server"`` or ``"local"``.
    :param server_url: Requested URL in server mode; ``None`` in local mode.
    :param log_path: Captured daemon log, or ``None`` for foreground hosts.
    :param started_at: Unix epoch seconds when the daemon started.
    :param host_id: Stable host id advertised to the server.
    :param resolved_server_url: Concrete URL owned by a local-mode daemon.
    :param config_sig: Signature of server-affecting launch configuration.
    """

    pid: int
    target: str
    mode: str
    server_url: str | None
    log_path: str | None
    started_at: int
    host_id: str | None = None
    resolved_server_url: str | None = None
    config_sig: str | None = None


def normalize_daemon_target(server_url: str | None) -> str:
    """Return the registry key for a daemon target.

    :param server_url: Requested server URL, or ``None`` / empty for local mode.
    :returns: ``"local"`` for local mode, else a canonical server URL.
    """
    if not server_url:
        return _LOCAL_DAEMON_MARKER

    fallback_target = server_url.rstrip("/")
    try:
        parsed = urlsplit(server_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return fallback_target
    if not parsed.scheme or hostname is None:
        return fallback_target

    scheme = parsed.scheme.lower()
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        port_suffix = ""
    else:
        port_suffix = f":{port}"

    raw_userinfo, separator, _ = parsed.netloc.rpartition("@")
    userinfo = f"{raw_userinfo}@" if separator else ""
    netloc = f"{userinfo}{hostname}{port_suffix}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def daemon_workspace_id(server_url: str) -> str | None:
    """Return the Databricks workspace id a server-mode target connects to.

    Two different URLs can front one workspace (a vanity host and the
    ``dbc-…`` workspace host, say). The server keys host connections by
    workspace and host id, so the workspace id, not the URL, decides whether
    two daemons collide.

    :param server_url: Server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent?o=123"``.
    :returns: The ``?o=`` selector when the URL carries one, else the org id
        stored by ``omnigent login`` for the URL, e.g. ``"123"``. ``None``
        when neither is known (non-Databricks servers, no login yet).
    """
    from omnigent.util.server_url import ServerUrl

    return ServerUrl.from_api_base(server_url).org_id


def find_same_workspace_daemon(
    records: Iterable[HostDaemonRecord],
    *,
    server_url: str,
    host_id: str | None,
    is_live: Callable[[HostDaemonRecord], bool],
) -> HostDaemonRecord | None:
    """Find a live daemon serving *server_url*'s workspace under another URL.

    A second daemon with the same host id on another URL for the same
    workspace doesn't fail: the server keeps the newest tunnel per
    ``(workspace, host id)``, so the two daemons keep evicting each other.
    Callers reuse or refuse instead of starting one. URLs whose workspace id
    is unknown never match, so non-Databricks servers keep per-URL daemons.

    :param records: Daemon registry records to search.
    :param server_url: Server URL the caller wants a daemon for, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param host_id: Host id the caller would advertise, e.g.
        ``"9eb97e60d38b43919d996b4918f63493"``. ``None`` never matches.
    :param is_live: Liveness check for a record's owner.
    :returns: The live record, or ``None``.
    """
    if host_id is None:
        return None
    target = normalize_daemon_target(server_url)
    workspace_id: str | None = None
    for record in records:
        if record.mode != "server" or record.server_url is None or record.host_id != host_id:
            continue
        if normalize_daemon_target(record.target) == target:
            continue
        if workspace_id is None:
            workspace_id = daemon_workspace_id(server_url)
            if workspace_id is None:
                return None
        if daemon_workspace_id(record.server_url) != workspace_id:
            continue
        if is_live(record):
            return record
    return None


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


def write_daemon_record(
    record: HostDaemonRecord,
    *,
    base_dir: Path | None = None,
    update_legacy_pidfile: bool = False,
) -> None:
    """Persist *record* in place while preserving its lifecycle-lock inode."""
    root = base_dir if base_dir is not None else data_dir()
    path = daemon_record_path(record.target, base_dir=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")
    if update_legacy_pidfile:
        (root / "host.pid").write_text(f"{record.pid}\n{record.target}\n")


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
        reported, never raised. The lock handle only opens the file; the
        elected daemon persists its metadata separately after claiming it.

        :returns: ``True`` if the flock is now held by this process.
        """
        return self.try_acquire() is True

    def try_acquire(self) -> bool | None:
        """Try to claim this target, distinguishing contention from no support.

        :returns: ``True`` when acquired, ``False`` when another daemon owns
            the target, or ``None`` when locking is unavailable so callers can
            retain the historical best-effort fallback.
        """
        if self._fd is not None:
            return True
        if fcntl is None:
            return None
        fd: int | None = None
        try:
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self._record_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            return False
        except OSError:
            _logger.warning(
                "daemon lifecycle lock acquire failed for %s", self._target, exc_info=True
            )
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            return None
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
