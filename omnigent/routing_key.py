"""Stable random slice key for CLI requests that need no particular replica.

A sharded workspace routes an unkeyed request by workspace id, so every unkeyed
request from one workspace lands on one replica. Each machine keeps one random
key in its data dir so its requests spread across replicas but stay on one.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
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
    except (OSError, UnicodeDecodeError):
        return None
    return stored if _KEY_RE.match(stored) else None


def _create() -> str:
    """Store a new key unless another process stored one first, and return the stored key."""
    key = secrets.token_hex(16)
    try:
        path = _key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".routing_key.")
    except OSError:
        return key  # the in-memory key still holds for this process
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(key)
        try:
            # Links the complete file only if no key exists, so racing processes agree.
            os.link(tmp_path, path)
        except FileExistsError:
            stored = _load()
            if stored is not None:
                return stored
            tmp_path.replace(path)  # the existing file holds no valid key
    except OSError:
        pass
    finally:
        tmp_path.unlink(missing_ok=True)
    return key
