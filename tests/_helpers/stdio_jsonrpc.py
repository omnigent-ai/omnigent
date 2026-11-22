"""Bounded newline-delimited JSON-RPC exchanges with a stdio subprocess (POSIX)."""

from __future__ import annotations

import json
import os
import select
import time
import weakref
from typing import TextIO

# Bytes read past the last returned line, per stream.
_unread: weakref.WeakKeyDictionary[TextIO, bytearray] = weakref.WeakKeyDictionary()


def write_json_line(handle: TextIO, payload: dict[str, object]) -> None:
    """Write one JSON-RPC line to a subprocess stdin stream and flush it."""
    handle.write(json.dumps(payload) + "\n")
    handle.flush()


def read_json_line(handle: TextIO, *, timeout_s: float) -> dict[str, object]:
    """Read one JSON object line from a subprocess stdout stream within ``timeout_s``.

    Reads the descriptor directly, so a partial line cannot block past the deadline.
    Bytes after the newline are kept for the next call: read ``handle`` only here.

    :raises TimeoutError: If no complete line arrives before the timeout.
    :raises EOFError: If the subprocess closes its output first.
    """
    buffer = _unread.setdefault(handle, bytearray())
    deadline = time.monotonic() + timeout_s
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("subprocess did not emit a JSON line")
        ready, _write, _error = select.select([handle.fileno()], [], [], min(remaining, 0.05))
        if not ready:
            continue
        chunk = os.read(handle.fileno(), 65536)
        if not chunk:
            raise EOFError("subprocess closed its output before a complete JSON line")
        buffer += chunk
    line, _newline, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    payload = json.loads(line)
    assert isinstance(payload, dict)
    return payload
