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
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from omnigent.process_logging import data_dir
from omnigent.util.uuid import normalize_uuid

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
    :param workspace_id: Databricks workspace id the daemon connects to, as
        the server reported it at startup, e.g. ``"123"``; ``None`` when
        unknown or for records written by older daemons.
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
    workspace_id: str | None = None


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
    two daemons collide. Only the workspace API mount is that shared server;
    each app on a workspace (a Databricks App, say) runs its own.

    :param server_url: Server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent?o=123"``.
    :returns: The ``?o=`` selector when the URL carries one, else the org id
        stored by ``omnigent login`` for the URL, e.g. ``"123"``. ``None``
        when neither is known (non-Databricks servers, no login yet) or the
        URL is not the workspace API mount.
    """
    from omnigent.util.server_url import ServerUrl, is_workspace_hosted_url

    if not is_workspace_hosted_url(server_url):
        return None
    return ServerUrl.from_api_base(server_url).org_id


# Databricks answers every request with the workspace it routed to, even
# unauthenticated ones, so this header names a URL's workspace without a token.
_ORG_ID_RESPONSE_HEADER = "x-databricks-org-id"
WORKSPACE_PROBE_TIMEOUT_S = 3.0
# Total time a starting daemon spends asking servers which workspace a URL
# reaches. Must stay below the launcher's claim wait, cli._DAEMON_CLAIM_TIMEOUT_S.
WORKSPACE_DISCOVERY_BUDGET_S = 5.0


def _workspace_probe_url(server_url: str) -> str | None:
    """Return the ``/v1/me`` URL to probe, without any userinfo credentials.

    :param server_url: Server API base, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :returns: The probe URL, or ``None`` for a malformed or non-HTTP URL.
    """
    try:
        parts = urlsplit(server_url.rstrip("/"))
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not hostname:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, f"{parts.path}/v1/me", "", ""))


def probe_workspace_id(
    server_url: str,
    *,
    org_id: str | None = None,
    timeout_s: float = WORKSPACE_PROBE_TIMEOUT_S,
) -> str | None:
    """Ask the server which Databricks workspace *server_url* reaches.

    Only the response headers are read; the request carries no credentials.
    The answer is trusted like the server itself: a server can name any
    workspace, which makes another daemon for that workspace yield.

    :param server_url: Server API base, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param org_id: Workspace selector to send, e.g. ``"123"``. A host shared
        by several workspaces names one only when the request selects it.
    :param timeout_s: Total time in seconds to wait for the answer, including
        name resolution and the TLS handshake, e.g. ``3.0``.
    :returns: The workspace id the server reported, e.g. ``"123"``, or
        ``None`` when the server is unreachable, slower than *timeout_s*, or
        names none (non-Databricks servers, a shared host without a selector,
        a malformed URL).
    """
    url = _workspace_probe_url(server_url)
    if url is None:
        return None
    answer: list[str | None] = [None]

    def _request() -> None:
        answer[0] = _read_workspace_header(url, org_id=org_id, timeout_s=timeout_s)

    # httpx times each phase separately and never times name resolution, so a
    # worker thread enforces the total; one still stuck is left to finish.
    worker = threading.Thread(target=_request, name="omnigent-workspace-probe", daemon=True)
    worker.start()
    worker.join(timeout_s)
    return None if worker.is_alive() else answer[0]


def _read_workspace_header(url: str, *, org_id: str | None, timeout_s: float) -> str | None:
    """Send the probe request and read the workspace header.

    :param url: Probe URL from :func:`_workspace_probe_url`.
    :param org_id: Workspace selector to send, e.g. ``"123"``.
    :param timeout_s: httpx's per-phase timeout in seconds, e.g. ``3.0``.
    :returns: The reported workspace id, e.g. ``"123"``, or ``None``.
    """
    import httpx

    from omnigent.cli_auth import DATABRICKS_ORG_ID_HEADER
    from omnigent.util.tls import client_ssl_context

    headers = {DATABRICKS_ORG_ID_HEADER: org_id} if org_id else {}
    try:
        # The tunnel's trust store, so a TLS-intercepting proxy it trusts works here too.
        with httpx.stream(
            "GET",
            url,
            headers=headers,
            timeout=timeout_s,
            follow_redirects=False,
            verify=client_ssl_context(),
        ) as response:
            value = response.headers.get(_ORG_ID_RESPONSE_HEADER, "").strip()
    # The probe is best-effort: an unencodable selector, a URL httpx rejects, or
    # an unreadable CA bundle means "unknown", like an unreachable server.
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError):
        return None
    # Workspace ids are short ASCII digit strings; anything else is "unknown".
    return value if value.isascii() and value.isdigit() and len(value) <= 32 else None


def resolve_daemon_workspace_id(
    server_url: str,
    *,
    probe: Callable[..., str | None] | None = None,
) -> str | None:
    """Return the workspace id a daemon for *server_url* will connect to.

    The server's answer wins over the login record, which can be missing (a
    sign-in through a Databricks CLI profile writes none) or stale.

    :param server_url: Server API base, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param probe: Server lookup, :func:`probe_workspace_id` by default.
    :returns: The workspace id, e.g. ``"123"``, or ``None`` when unknown or
        the URL is not the workspace API mount.
    """
    from omnigent.util.server_url import is_workspace_hosted_url

    if not is_workspace_hosted_url(server_url):
        return None
    known = daemon_workspace_id(server_url)
    return (probe or probe_workspace_id)(server_url, org_id=known) or known


def find_same_workspace_daemon(
    records: Iterable[HostDaemonRecord],
    *,
    server_url: str,
    host_id: str | None,
    is_live: Callable[[HostDaemonRecord], bool],
    workspace_id: str | None = None,
    probe: Callable[..., str | None] | None = None,
    legacy_only: bool = False,
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
    :param workspace_id: *server_url*'s workspace id when the caller already
        knows it, e.g. ``"123"``.
    :param probe: Server lookup (see :func:`probe_workspace_id`) for a URL
        whose daemon record names no workspace. Its answer wins over the login
        record, as in :func:`resolve_daemon_workspace_id`; ``None`` keeps the
        lookup offline.
    :param legacy_only: Consider only records without a ``workspace_id``,
        i.e. daemons from versions that don't take the workspace lock.
    :returns: The live record, or ``None``.
    """
    from omnigent.util.server_url import is_workspace_hosted_url

    wanted_host = normalize_uuid(host_id)
    if wanted_host is None:
        return None
    target = normalize_daemon_target(server_url)
    resolved: dict[str, str | None] = {}

    def _lookup(url: str, recorded: str | None) -> str | None:
        if not is_workspace_hosted_url(url):
            return None
        if recorded:
            return recorded
        if probe is None:
            return daemon_workspace_id(url)
        if url not in resolved:
            resolved[url] = resolve_daemon_workspace_id(url, probe=probe)
        return resolved[url]

    for record in records:
        if record.mode != "server" or record.server_url is None:
            continue
        if normalize_uuid(record.host_id) != wanted_host:
            continue
        if normalize_daemon_target(record.target) == target:
            continue
        if legacy_only and record.workspace_id:
            continue
        # Liveness first: a dead record must not cost a server probe.
        if not is_live(record):
            continue
        if workspace_id is None:
            workspace_id = _lookup(server_url, None)
            if workspace_id is None:
                return None
        if _lookup(record.server_url, record.workspace_id) == workspace_id:
            return record
    return None


def workspace_lock_path(host_id: str, workspace_id: str, *, base_dir: Path | None = None) -> Path:
    """Return the lock file one daemon per (host id, workspace) holds.

    :param host_id: Host id in any accepted spelling.
    :param workspace_id: Databricks workspace id, e.g. ``"123"``.
    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :returns: ``<base>/daemons/workspace-<digest>.lock``.
    """
    key = f"{normalize_uuid(host_id)}:{workspace_id}"
    return daemon_registry_dir(base_dir) / f"workspace-{_target_digest(key)}.lock"


def record_from_json(raw: Mapping[str, object]) -> HostDaemonRecord | None:
    """Parse a daemon record from decoded JSON.

    :param raw: Decoded JSON object, e.g.
        ``{"pid": 4242, "target": "local", "mode": "local"}``.
    :returns: The parsed record, or ``None`` if the record is malformed.
    """
    try:
        pid_raw = raw["pid"]
        if not isinstance(pid_raw, str | int) or isinstance(pid_raw, bool):
            return None
        pid = int(pid_raw)
        target = str(raw["target"])
        mode = str(raw["mode"])
        started_at_raw = raw["started_at"]
        if not isinstance(started_at_raw, str | int) or isinstance(started_at_raw, bool):
            return None
        started_at = int(started_at_raw)
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in {"local", "server"} or not target:
        return None

    def _text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    return HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=_text("server_url"),
        log_path=_text("log_path"),
        started_at=started_at,
        host_id=_text("host_id"),
        resolved_server_url=_text("resolved_server_url"),
        config_sig=_text("config_sig"),
        workspace_id=_text("workspace_id"),
    )


def read_daemon_record(path: Path) -> HostDaemonRecord | None:
    """Read one daemon registry record from disk.

    :param path: JSON record path, e.g. ``Path("~/.omnigent/daemons/abc.json")``.
    :returns: The parsed record, or ``None`` if unreadable or malformed.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record_from_json(raw) if isinstance(raw, dict) else None


def read_daemon_records(base_dir: Path | None = None) -> list[HostDaemonRecord]:
    """Read every well-formed per-target record, for daemon-side checks.

    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :returns: Records in no particular order; malformed files are skipped.
    """
    registry = daemon_registry_dir(base_dir)
    paths = registry.glob("*.json") if registry.exists() else ()
    return [record for path in paths if (record := read_daemon_record(path)) is not None]


DAEMON_PID_START_TOLERANCE_S = 5.0


def pid_is_recorded_daemon(
    record: HostDaemonRecord, *, pid_alive: Callable[[int], bool] | None = None
) -> bool:
    """Whether *record*'s pid still names the recorded daemon, not a recycled pid.

    A process created after the record's ``started_at`` (beyond a small clock
    tolerance) cannot be the daemon that wrote it; an earlier one, even by
    minutes, still is. Records without a start time, and pids whose creation
    time can't be read, fall back to alive-only.

    :param record: Record to check.
    :param pid_alive: Existence check for a pid; defaults to a cross-platform
        psutil check that never signals the process.
    :returns: ``True`` if the pid is alive and can be the recorded daemon.
    """
    import psutil

    if pid_alive is None:
        from omnigent.host.local_server import _pid_alive as pid_alive
    if not pid_alive(record.pid):
        return False
    if record.started_at <= 0:
        return True
    try:
        created = psutil.Process(record.pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except Exception:  # noqa: BLE001 — unreadable creation time: alive-only
        return True
    return created <= record.started_at + DAEMON_PID_START_TOLERANCE_S


def record_owner_is_live(record: HostDaemonRecord, *, base_dir: Path | None = None) -> bool:
    """Whether the daemon that wrote *record* is still alive.

    A held record flock is a definitive live owner. When the lock is free or
    can't be probed (no ``fcntl``), fall back to whether the pid still names
    the recorded daemon, so a daemon still mid-startup (it hasn't taken the
    lock yet) counts as live while a recycled pid does not.

    :param record: Record to check.
    :param base_dir: Data-directory override; defaults to :func:`data_dir`.
    :returns: ``True`` if the daemon should be treated as alive.
    """
    if record_flock_is_held(daemon_record_path(record.target, base_dir=base_dir)) is True:
        return True
    return pid_is_recorded_daemon(record)


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

    @classmethod
    def for_workspace(
        cls,
        host_id: str,
        workspace_id: str,
        *,
        base_dir: Path | None = None,
    ) -> DaemonLifecycleLock:
        """Build the lock that admits one daemon per (host id, workspace).

        Two URLs can reach one workspace; the per-target lock can't tell them
        apart, so a daemon also holds this one for its whole life.

        :param host_id: Host id the daemon advertises.
        :param workspace_id: Databricks workspace id, e.g. ``"123"``.
        :param base_dir: Data-directory override; defaults to :func:`data_dir`.
        :returns: An unacquired :class:`DaemonLifecycleLock`.
        """
        return cls(
            target=f"workspace:{workspace_id}",
            record_path=workspace_lock_path(host_id, workspace_id, base_dir=base_dir),
            pid=os.getpid(),
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
