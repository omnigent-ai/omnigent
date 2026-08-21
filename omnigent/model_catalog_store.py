"""The shared on-disk model-catalog store (model-flows-design.md §1.2).

One probe result, many consumers: whoever ran a harness's ``list_models``
(the host at boot, the runner at launch when the file is absent, a live
codex session writing back) persists the catalog here, keyed by harness and
a launch-config fingerprint, and every surface — the pre-launch picker, the
in-session gear, launch resolution and validation — reads the same bytes.
Because writer and readers share one file, host/runner drift and
probe-vs-session mismatch are impossible by construction.

The store holds only verbatim harness answers; nothing else ever writes it.
A fingerprint mismatch is a miss (never a "close enough" hit), so an answer
probed under one config can never serve another.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def fingerprint_of(*parts: object) -> str:
    """
    Stable fingerprint of a resolved harness configuration.

    :param parts: Hashable configuration facets — resolved overrides, env
        pairs, binary identity. Stringified in order.
    :returns: A short hex digest.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


#: Catalog entries older than this get a background refresh on read (the
#: readers decide; the store only reports staleness).
CATALOG_STALE_AFTER_S = 3600.0


def _data_dir() -> Path:
    """Return the omnigent data dir (must stay in lock-step with
    ``omnigent.host.local_server._local_data_dir`` /
    ``omnigent.chat._omnigent_persistent_dir``).

    :returns: ``$OMNIGENT_DATA_DIR`` when set, else ``~/.omnigent``.
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".omnigent"


def catalog_path(harness: str, fingerprint: str) -> Path:
    """Return the catalog file path for one (harness, fingerprint).

    :param harness: Canonical harness name, e.g. ``"claude-native"``.
    :param fingerprint: The launch-config fingerprint (:func:`fingerprint_of`).
    :returns: ``<data-dir>/cache/model-catalogs/<harness>-<fingerprint>.json``.
    """
    return _data_dir() / "cache" / "model-catalogs" / f"{harness}-{fingerprint}.json"


def read_catalog(harness: str, fingerprint: str) -> list[dict[str, Any]] | None:
    """Read the stored catalog rows for one (harness, fingerprint).

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :returns: The verbatim rows, or ``None`` on a miss / damaged file.
    """
    path = catalog_path(harness, fingerprint)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def catalog_age_s(harness: str, fingerprint: str) -> float | None:
    """Age of the stored catalog in seconds, or ``None`` on a miss."""
    path = catalog_path(harness, fingerprint)
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def write_catalog(harness: str, fingerprint: str, rows: list[dict[str, Any]]) -> None:
    """Persist catalog rows atomically (best-effort; failures only log).

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :param rows: Verbatim harness rows to persist.
    """
    path = catalog_path(harness, fingerprint)
    payload = {
        "harness": harness,
        "fingerprint": fingerprint,
        "written_at": time.time(),
        "models": rows,
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        _logger.warning("could not persist the %s model catalog", harness, exc_info=True)


#: In-flight probes, keyed (harness, fingerprint) — the thin single-flight
#: wrapper the design keeps process-side: concurrent misses join one probe
#: instead of each spawning CLI processes.
_inflight: dict[tuple[str, str], asyncio.Task[list[dict[str, Any]] | None]] = {}


async def ensure_catalog(
    harness: str,
    fingerprint: str,
    resolve: Callable[[], Awaitable[list[dict[str, Any]] | None]],
) -> list[dict[str, Any]] | None:
    """Store-first catalog access with a single probe in flight per key.

    A hit serves immediately; a miss runs *resolve* once (concurrent
    callers join it), persists a non-empty answer, and returns it.

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :param resolve: Probe coroutine factory producing verbatim rows.
    :returns: Catalog rows, or ``None`` when no catalog could be obtained.
    """
    cached = read_catalog(harness, fingerprint)
    if cached is not None:
        return cached
    key = (harness, fingerprint)
    task = _inflight.get(key)
    if task is None or task.done():

        async def _run() -> list[dict[str, Any]] | None:
            try:
                rows = await resolve()
            finally:
                _inflight.pop(key, None)
            if rows:
                write_catalog(harness, fingerprint, rows)
            return rows

        task = asyncio.create_task(_run(), name=f"model-catalog-{harness}")
        _inflight[key] = task
    return await asyncio.shield(task)


def default_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the catalog's single ``isDefault`` row, if any.

    :param rows: Catalog rows.
    :returns: The default row, or ``None``.
    """
    return next((row for row in rows if row.get("isDefault") is True), None)


def catalog_contains(rows: list[dict[str, Any]], token: str) -> bool:
    """Whether *token* names a catalog row (by ``id`` or wire ``model``).

    :param rows: Catalog rows.
    :param token: A picker row id or wire model id.
    :returns: ``True`` when some row's ``id`` or ``model`` equals *token*.
    """
    return any(row.get("id") == token or row.get("model") == token for row in rows)


__all__ = [
    "CATALOG_STALE_AFTER_S",
    "catalog_age_s",
    "catalog_contains",
    "catalog_path",
    "default_row",
    "ensure_catalog",
    "fingerprint_of",
    "read_catalog",
    "write_catalog",
]
