"""Bounded newline-delimited JSON-RPC exchanges with a stdio subprocess (POSIX)."""

from __future__ import annotations

import json
import select
import subprocess
import time
from typing import TextIO


def write_json_line(handle: TextIO, payload: dict[str, object]) -> None:
    """Write one JSON-RPC line to a subprocess stdin stream and flush it."""
    handle.write(json.dumps(payload) + "\n")
    handle.flush()


def read_json_line(handle: TextIO, *, timeout_s: float) -> dict[str, object]:
    """Read one JSON object line from a subprocess stdout stream.

    :raises TimeoutError: If no line is available before the timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _write, _error = select.select([handle], [], [], 0.05)
        if not ready:
            continue
        line = handle.readline()
        if line:
            payload = json.loads(line)
            assert isinstance(payload, dict)
            return payload
    raise TimeoutError("subprocess did not emit a JSON line")


def stop_process(process: subprocess.Popen[str], *, timeout_s: float = 5.0) -> None:
    """Terminate a subprocess, killing it if it ignores the request, and reap it."""
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_s)
