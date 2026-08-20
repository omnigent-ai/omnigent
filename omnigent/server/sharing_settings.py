"""File-backed session-sharing settings for the OSS server.

Two server-wide sharing policies default from env vars at boot but can be
overridden at runtime from the Settings → Sharing admin panel, each persisted to
a plaintext file in :func:`resolve_data_dir` (next to the ``admins`` roster) so
it survives restarts without a database migration and takes effect without a
redeploy:

- the sharing *mode* — ``OMNIGENT_SHARING_MODE`` → ``<data_dir>/sharing_mode``
  (``on`` / ``read_only`` / ``restricted_read_only`` / ``off``);
- whether *public* (anyone-with-the-link) read access may be granted —
  ``OMNIGENT_PUBLIC_SHARING`` → ``<data_dir>/public_sharing`` (``on`` / ``off``).

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
from pathlib import Path

from omnigent.server.admin_list import resolve_data_dir
from omnigent.server.auth import SharingMode

logger = logging.getLogger(__name__)

_SHARING_MODE_FILE = "sharing_mode"
_PUBLIC_SHARING_FILE = "public_sharing"
# Public sharing is enabled unless a value explicitly says otherwise, so a typo
# or a stray value fails OPEN (never silently disables a working feature).
_PUBLIC_FALSY = ("0", "false", "no", "off")

# mtime cache keyed by absolute path → (mtime, stripped text). Keyed by path so a
# data-dir change (e.g. across tests) never reads through a stale entry.
_cache: dict[str, tuple[float, str]] = {}


def resolve_sharing_mode_path() -> Path:
    """Path of the file holding the admin sharing-mode override."""
    return resolve_data_dir() / _SHARING_MODE_FILE


def resolve_public_sharing_path() -> Path:
    """Path of the file holding the admin public-sharing override."""
    return resolve_data_dir() / _PUBLIC_SHARING_FILE


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
