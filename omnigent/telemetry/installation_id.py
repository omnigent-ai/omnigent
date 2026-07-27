"""Persistent installation ID for usage telemetry.

Mirrors the MLflow pattern: generate a UUID4 on first run, persist it
atomically to ``_local_data_dir() / "telemetry.json"``, and cache in
memory behind a lock.  All errors are silently swallowed — this module
MUST NOT raise.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from omnigent.version import VERSION

_KEY_INSTALLATION_ID = "installation_id"
_CACHE_LOCK = threading.RLock()
_cache: str | None = None  # in-memory installation ID after first load
_cache_initialized = False


def get_installation_id() -> str | None:
    """Return a persistent installation ID, creating it on first call.

    Stores at ``_local_data_dir() / "telemetry.json"``.  Returns
    ``None`` on any error — never raises.
    """
    global _cache, _cache_initialized

    if _cache_initialized:
        return _cache

    try:
        with _CACHE_LOCK:
            if _cache_initialized:
                return _cache

            if loaded := _load_from_disk():
                _cache = loaded
                _cache_initialized = True
                return loaded

            new_id = str(uuid.uuid4())
            _write_to_disk(new_id)
            # Set after disk write so a disk failure leaves the cache empty.
            _cache = new_id
            _cache_initialized = True
            return new_id
    except Exception:
        _cache_initialized = True
        return None


def _telemetry_file_path() -> Path:
    """Return the path to the telemetry JSON file."""
    # Late import to avoid a circular import at module load time.
    from omnigent.host.local_server import _local_data_dir

    return _local_data_dir() / "telemetry.json"


def _load_from_disk() -> str | None:
    """Load an existing installation ID from disk.

    :returns: The stored UUID string, or ``None`` when absent or invalid.
    """
    try:
        path = _telemetry_file_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get(_KEY_INSTALLATION_ID)
        if isinstance(raw, str) and raw:
            uuid.UUID(raw)  # validate format
            return raw
        return None
    except Exception:
        return None


def _write_to_disk(installation_id: str) -> None:
    """Persist a new installation ID atomically via temp-file rename."""
    try:
        path = _telemetry_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            _KEY_INSTALLATION_ID: installation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_version": VERSION,
            "schema_version": 1,
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(config), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        pass  # best-effort persistence; a missing file just triggers regeneration next run
