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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from omnigent.inner._proc import process_alive
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
    :param adopted: The daemon connects to a local server it does not own
        (requested through an explicit loopback URL); ``config_sig`` is then
        that server's signature, or ``None`` when the server has none.
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
    adopted: bool = False


def normalize_daemon_target(
    server_url: str | None,
    *,
    base_dir: Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> str:
    """Return the registry key for a daemon target.

    A local server instance is identified by its data dir, so a plain-http
    loopback URL naming the port tracked in that dir's ``local_server.pid``
    addresses the same instance as local mode: both collapse to ``"local"``.
    One instance therefore keeps one record (and one daemon) across
    ``--server`` spellings such as ``http://127.0.0.1:6767`` vs
    ``http://localhost:6767``. The pidfile is honored only while its recorded
    server process is alive, so a stale claim cannot capture an explicit
    target, and an ``https`` spelling is never rewritten onto the http server.

    :param server_url: Requested server URL, or ``None`` / empty for local mode.
    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :param pid_alive: Liveness probe for the recorded server pid; defaults to
        :func:`omnigent.inner._proc.process_alive`.
    :returns: ``"local"`` for local mode or a loopback spelling of the data
        dir's live tracked server, else a canonical server URL.
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
    if _is_tracked_local_server(scheme, hostname, port, base_dir=base_dir, pid_alive=pid_alive):
        return _LOCAL_DAEMON_MARKER
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


_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _loopback_port(scheme: str, hostname: str, port: int | None) -> int | None:
    """Return the effective port when the URL parts name a loopback host, else ``None``."""
    if hostname not in _LOOPBACK_HOSTNAMES:
        return None
    return port if port is not None else _DEFAULT_SCHEME_PORTS.get(scheme)


def loopback_server_port(server_url: str) -> int | None:
    """Return the effective port of a loopback *server_url*, or ``None`` otherwise.

    Every loopback spelling of one port (``http://127.0.0.1:6767``,
    ``http://localhost:6767``, ``http://[::1]:6767``) names the same listener,
    so callers that ask whether a requested URL is the local server compare
    these ports instead of the raw strings.

    :param server_url: Requested or recorded server URL.
    :returns: The port, or ``None`` for a non-loopback or unparsable URL.
    """
    try:
        parsed = urlsplit(server_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not parsed.scheme or hostname is None:
        return None
    return _loopback_port(parsed.scheme.lower(), hostname.lower(), port)


def _is_tracked_local_server(
    scheme: str,
    hostname: str,
    port: int | None,
    *,
    base_dir: Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> bool:
    """Whether the URL parts name a plain-http loopback spelling of the live tracked server.

    The pidfile is the data dir's declaration of which port its local server
    owns, trusted only while that server process is alive. HTTP health is not
    probed, so a live but momentarily unhealthy server keeps a stable key.

    :param scheme: Lowercased URL scheme, e.g. ``"http"``.
    :param hostname: Lowercased URL hostname, e.g. ``"localhost"``.
    :param port: Explicit URL port, or ``None`` for the scheme default.
    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :param pid_alive: Liveness probe for the recorded server pid.
    :returns: ``True`` when the scheme is ``http``, the host is loopback, and
        the effective port matches the live tracked local server port.
    """
    if scheme != "http":
        return False
    effective_port = _loopback_port(scheme, hostname, port)
    if effective_port is None:
        return False
    return effective_port == _tracked_local_server_port(base_dir=base_dir, pid_alive=pid_alive)


def _tracked_local_server_port(
    *, base_dir: Path | None = None, pid_alive: Callable[[int], bool] | None = None
) -> int | None:
    """Return the port of the live local server recorded in ``local_server.pid``.

    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :param pid_alive: Liveness probe for the recorded server pid.
    :returns: The tracked port, or ``None`` when the pidfile is absent or
        malformed, or its recorded server process is gone.
    """
    pid_path = (base_dir if base_dir is not None else data_dir()) / "local_server.pid"
    try:
        lines = pid_path.read_text().strip().splitlines()
        pid, port = int(lines[0]), int(lines[1])
    except (OSError, ValueError, IndexError):
        return None
    return port if (pid_alive or process_alive)(pid) else None


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
