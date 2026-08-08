"""Gateway-servlet discovery state.

The host daemon writes ``~/.omnigent/gateway-servlet.json`` when the servlet
is listening; session-launch code on the same machine reads it to register
sessions. The file carries the loopback URL and the admin bearer, so it is
written 0600. A stale file (daemon crash) is harmless: registration attempts
against a dead URL fail fast and launches fall back to the direct gateway.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# Fixed default port: omnigent-server-adjacent (6767 + 1), outside the macOS and
# Linux ephemeral ranges. The state file remains the source of truth for
# consumers; the constant only makes the common case predictable.
DEFAULT_GATEWAY_PORT = 6768

# Registered sessions kept across daemon restarts, newest last. Stale tokens
# are unguessable and harmless, so pruning is a size cap, not a TTL.
_REGISTRY_MAX_SESSIONS = 512


def _pid_alive(pid: int) -> bool:
    """
    Report whether *pid* is a live process.

    :param pid: Process id from a state file.
    :returns: ``True`` when signal-0 delivery succeeds (or is denied, which
        still proves liveness).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _state_path() -> Path:
    """
    Path of the servlet discovery file.

    :returns: ``~/.omnigent/gateway-servlet.json`` (same convention as
        ``host.pid``).
    """
    return Path.home() / ".omnigent" / "gateway-servlet.json"


@dataclass(frozen=True)
class ServletState:
    """Published servlet coordinates.

    :param url: Loopback base URL, e.g. ``"http://127.0.0.1:53211"``.
    :param admin_token: Bearer for the ``/admin/*`` control plane.
    :param pid: PID of the host process that owns the listener.
    """

    url: str
    admin_token: str
    pid: int


def write_servlet_state(state: ServletState) -> None:
    """
    Atomically publish the servlet state file at mode 0600.

    :param state: Coordinates to publish.
    :returns: None.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"url": state.url, "admin_token": state.admin_token, "pid": state.pid}
    fd, tmp_name = tempfile.mkstemp(prefix=".gateway-servlet.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_servlet_state(*, allow_stale: bool = False) -> ServletState | None:
    """
    Read the published servlet state, if any.

    The daemon has no reliable SIGTERM finalizer, so a crash or a hard stop
    can strand the file. Liveness is therefore checked here: a file whose
    owner pid is dead reads as absent, which makes launchers fall back to the
    direct gateway without a connect timeout.

    :param allow_stale: Return the state even when the owner pid is dead —
        used by the next daemon start to reclaim the previous port.
    :returns: Parsed :class:`ServletState`, or ``None`` when the file is
        absent, unreadable/malformed, or (unless *allow_stale*) stale.
    """
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = raw.get("url")
    admin_token = raw.get("admin_token")
    pid = raw.get("pid")
    if not isinstance(url, str) or not url or not isinstance(admin_token, str) or not admin_token:
        return None
    state = ServletState(url=url, admin_token=admin_token, pid=pid if isinstance(pid, int) else -1)
    if not allow_stale and not _pid_alive(state.pid):
        return None
    return state


def clear_servlet_state(owner_pid: int) -> None:
    """
    Remove the state file, but only when this process still owns it.

    :param owner_pid: PID that wrote the file; a file re-written by a newer
        daemon (different pid) is left in place.
    :returns: None.
    """
    state = read_servlet_state(allow_stale=True)
    if state is None or state.pid != owner_pid:
        return
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        _logger.warning("could not remove %s", _state_path(), exc_info=True)


def _registry_path() -> Path:
    """
    Path of the persisted session registry.

    :returns: ``~/.omnigent/gateway-servlet-sessions.json``.
    """
    return Path.home() / ".omnigent" / "gateway-servlet-sessions.json"


def write_session_registry(entries: dict[str, dict[str, str]]) -> None:
    """
    Atomically persist the session registry at mode 0600.

    :param entries: ``token -> {"profile": ..., "workspace_host": ...}``,
        insertion-ordered oldest first; capped to the newest
        :data:`_REGISTRY_MAX_SESSIONS`.
    :returns: None.
    """
    if len(entries) > _REGISTRY_MAX_SESSIONS:
        entries = dict(list(entries.items())[-_REGISTRY_MAX_SESSIONS:])
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".gateway-sessions.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_session_registry() -> dict[str, dict[str, str]]:
    """
    Load the persisted session registry, tolerating absence and corruption.

    :returns: Valid entries only (``profile`` and ``workspace_host`` both
        non-empty strings); ``{}`` when the file is absent or unreadable.
    """
    try:
        raw = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, dict[str, str]] = {}
    for token, row in raw.items():
        if not isinstance(token, str) or not token or not isinstance(row, dict):
            continue
        profile = row.get("profile")
        workspace_host = row.get("workspace_host")
        if (
            isinstance(profile, str)
            and profile
            and isinstance(workspace_host, str)
            and workspace_host
        ):
            entries[token] = {"profile": profile, "workspace_host": workspace_host}
    return entries
