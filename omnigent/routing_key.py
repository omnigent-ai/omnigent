"""Stable random slice key for CLI requests that need no particular replica.

A sharded workspace routes an unkeyed request by workspace id, so every unkeyed
request from one workspace lands on one replica. Each machine keeps one random
key in its data dir so its requests spread across replicas but stay on one.
"""

from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.Lock()
_cached: str | None = None


def client_routing_key() -> str:
    """Return this machine's random slice key, creating and storing it on first use.

    :returns: A 32-character lowercase hex key, e.g. ``"9f86d081884c7d659a2feaa0c55ad015"``.
        Kept in memory for the process when the data dir can't be read or written.
    """
    global _cached
    with _lock:
        if _cached is None:
            _cached = _load() or _create()
        return _cached


def _key_path() -> Path:
    # Late import: local_server pulls in server modules.
    from omnigent.host.local_server import _local_data_dir

    return _local_data_dir() / "routing_key"


def _load() -> str | None:
    try:
        stored = _key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored if _KEY_RE.match(stored) else None


def _create() -> str:
    key = secrets.token_hex(16)
    try:
        path = _key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(key, encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        pass  # the in-memory key still holds for this process
    return key
