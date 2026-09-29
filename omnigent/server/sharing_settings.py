"""File-backed session-sharing settings for the OSS server.

Two server-wide sharing policies default from env vars at boot but can be
overridden at runtime from the Settings → Sharing admin panel, each persisted to
a plaintext file in :func:`resolve_data_dir` (next to the ``admins`` roster) so
it survives restarts without a database migration and takes effect without a
redeploy:

- the sharing *mode* — ``OMNIGENT_SHARING_MODE`` → ``<data_dir>/sharing_mode``
  (``on`` / ``read_only`` / ``restricted_read_only`` / ``off``);
- whether *public* (anyone-with-the-link) read access may be granted —
  ``OMNIGENT_PUBLIC_SHARING`` → ``<data_dir>/public_sharing`` (``on`` / ``off``);
- which *new* sessions start public: ``OMNIGENT_DEFAULT_PUBLIC_SESSIONS`` →
  ``<data_dir>/default_public_sessions`` (``off`` / ``sandbox`` / ``all``).

A missing, empty, or unreadable file means "no override recorded", so the caller
falls back to the env-var default; an unrecognized value is likewise ignored
(falling back rather than silently changing behavior). Reads are mtime-cached
per file so the per-request hot path is cheap, mirroring the ``admins`` roster
loader.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from omnigent.server.admin_list import resolve_data_dir
from omnigent.server.auth import SharingMode, workspace_sharing_blocked

logger = logging.getLogger(__name__)

_SHARING_MODE_FILE = "sharing_mode"
_PUBLIC_SHARING_FILE = "public_sharing"
_DEFAULT_PUBLIC_SESSIONS_FILE = "default_public_sessions"
# Public sharing is enabled unless a value explicitly says otherwise, so a typo
# or a stray value fails OPEN (never silently disables a working feature).
_PUBLIC_FALSY = ("0", "false", "no", "off")

# mtime cache keyed by absolute path → (mtime, stripped text). Keyed by path so a
# data-dir change (e.g. across tests) never reads through a stale entry.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by filesystem path
_cache: dict[str, tuple[float, str]] = {}


def resolve_sharing_mode_path() -> Path:
    """Path of the file holding the admin sharing-mode override."""
    return resolve_data_dir() / _SHARING_MODE_FILE


def resolve_public_sharing_path() -> Path:
    """Path of the file holding the admin public-sharing override."""
    return resolve_data_dir() / _PUBLIC_SHARING_FILE


def resolve_default_public_sessions_path() -> Path:
    """Path of the file holding the admin default-public-sessions override."""
    return resolve_data_dir() / _DEFAULT_PUBLIC_SESSIONS_FILE


class DefaultPublicSessions(str, Enum):
    """Which newly created sessions get a public (anyone-with-the-link) read grant.

    - ``OFF``: every session starts private (the default).
    - ``SANDBOX``: sessions running in a server-managed cloud sandbox start
      public; sessions on a user's own machine stay private.
    - ``ALL``: every new session starts public.

    Only the creation default: owners can still revoke the public grant.
    """

    OFF = "off"
    SANDBOX = "sandbox"
    ALL = "all"

    @classmethod
    def coerce(cls, value: object) -> DefaultPublicSessions:
        """Map a value to a policy, failing closed to ``OFF`` (private) for
        anything unset or unrecognized so a typo never exposes sessions."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        return cls.OFF


def _read_override_text(path: Path) -> str | None:
    """mtime-cached read of an override file's stripped contents.

    Returns ``None`` for a missing or unreadable file (never raises), so callers
    fall back to their env-var default.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    _cache[key] = (mtime, raw)
    return raw


def _write_override_text(path: Path, value: str) -> None:
    """Persist an override atomically.

    Writes to a temp file in the data dir and ``os.replace``s it into place so a
    concurrent read never sees a half-written file. Invalidates the cache entry
    so the next read reflects the change.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _cache.pop(str(path), None)


def read_sharing_mode_override() -> SharingMode | None:
    """Return the admin-set sharing-mode override, or ``None`` when unset.

    A missing/empty/unreadable file or an unrecognized value yields ``None`` —
    the caller then falls back to the env-var default rather than silently
    changing behavior.
    """
    raw = _read_override_text(resolve_sharing_mode_path())
    if not raw:
        return None
    try:
        return SharingMode(raw.lower())
    except ValueError:
        logger.warning("Ignoring unrecognized sharing_mode override %r", raw)
        return None


def write_sharing_mode_override(mode: SharingMode) -> None:
    """Persist the admin sharing-mode override atomically."""
    _write_override_text(resolve_sharing_mode_path(), mode.value)


def public_sharing_env_default() -> bool:
    """Boot default for public sharing from ``OMNIGENT_PUBLIC_SHARING``.

    Enabled unless the value is explicitly falsy (``0``/``false``/``no``/``off``,
    case-insensitive); unset or unrecognized fails open to enabled.
    """
    raw = os.environ.get("OMNIGENT_PUBLIC_SHARING")
    if not raw or not raw.strip():
        return True
    return raw.strip().lower() not in _PUBLIC_FALSY


def read_public_sharing_override() -> bool | None:
    """Return the admin-set public-sharing override, or ``None`` when unset.

    ``True``/``False`` reflect a recorded ``on``/``off``; a missing/empty file
    yields ``None`` so the caller falls back to the env-var default.
    """
    raw = _read_override_text(resolve_public_sharing_path())
    if raw is None or raw == "":
        return None
    return raw.lower() not in _PUBLIC_FALSY


def write_public_sharing_override(enabled: bool) -> None:
    """Persist the admin public-sharing override atomically."""
    _write_override_text(resolve_public_sharing_path(), "on" if enabled else "off")


def default_public_sessions_env_default() -> DefaultPublicSessions:
    """Boot default from ``OMNIGENT_DEFAULT_PUBLIC_SESSIONS`` (``off`` if unset)."""
    return DefaultPublicSessions.coerce(os.environ.get("OMNIGENT_DEFAULT_PUBLIC_SESSIONS"))


def read_default_public_sessions_override() -> DefaultPublicSessions | None:
    """Return the admin-set default-public-sessions override, or ``None`` when unset.

    Unlike the other overrides, an unrecognized value resolves to ``OFF`` rather
    than falling back to the env-var default: a corrupted or hand-edited file
    must never restore a more permissive boot default.
    """
    raw = _read_override_text(resolve_default_public_sessions_path())
    if not raw:
        return None
    try:
        return DefaultPublicSessions(raw.lower())
    except ValueError:
        logger.warning("Unrecognized default_public_sessions override %r; using off", raw)
        return DefaultPublicSessions.OFF


def write_default_public_sessions_override(policy: DefaultPublicSessions) -> None:
    """Persist the admin default-public-sessions override atomically."""
    _write_override_text(resolve_default_public_sessions_path(), policy.value)


def default_public_policy(state: Any) -> DefaultPublicSessions:
    """The live default-public policy off ``app.state`` (``OFF`` when unwired)."""
    return DefaultPublicSessions.coerce(
        getattr(state, "default_public_sessions", lambda: DefaultPublicSessions.OFF)()
    )


def host_is_managed_sandbox(host_registry: Any, host_id: str | None) -> bool:
    """Whether ``host_id`` is currently connected as a server-provisioned sandbox.

    Keys on the live connection's launch-token provenance, not a persisted
    ``sandbox_provider`` marker: an owner can reconnect their own machine on a
    managed host's id under ordinary login, which keeps the marker but is not a
    sandbox. A genuine sandbox proves itself with its launch token on every
    connect. Lets a session started on an already-running sandbox count as a
    sandbox session, not just one that requested a fresh sandbox.
    """
    if host_registry is None or host_id is None:
        return False
    conn = host_registry.get(host_id)
    return conn is not None and bool(getattr(conn, "registered_with_managed_token", False))


def new_session_starts_public(state: Any, *, managed: bool, workspace: str | None) -> bool:
    """Whether a just-created session should get the default ``__public__`` grant.

    Combines the default-public policy with the grant gates a manual share would
    hit (sharing mode, public-access switch, restricted workspaces), so the
    default never creates a grant an owner could not create by hand. ``state``
    is ``app.state``; ``getattr`` defaults cover hand-built test apps.
    """
    policy = default_public_policy(state)
    if policy is DefaultPublicSessions.OFF:
        return False
    if policy is DefaultPublicSessions.SANDBOX and not managed:
        return False
    mode = getattr(state, "sharing_mode", lambda: SharingMode.ON)()
    if mode == SharingMode.OFF or not getattr(state, "public_sharing", lambda: True)():
        return False
    if mode != SharingMode.RESTRICTED_READ_ONLY:
        return True
    if workspace:
        return not workspace_sharing_blocked(workspace)
    # Unknown cwd (a fork, or a create that binds later): nothing re-checks the
    # grant at bind time, so fail closed. Server-provisioned sandboxes are exempt;
    # the server picks their cwd inside a disposable container.
    return managed
