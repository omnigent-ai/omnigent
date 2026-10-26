"""Per-process read accounting for checks that a poll reads only new data."""

from __future__ import annotations

from pathlib import Path

import pytest

_PROC_SELF_IO = Path("/proc/self/io")


def process_bytes_read() -> int:
    """
    Return the bytes this process has read through read-family syscalls.

    The counter covers every thread, including ``asyncio.to_thread`` workers.
    Skips the calling test where Linux ``/proc/self/io`` is unavailable.

    :returns: The cumulative ``rchar`` counter.
    """
    try:
        text = _PROC_SELF_IO.read_text(encoding="ascii")
    except OSError:
        pytest.skip("per-process read accounting needs Linux /proc/self/io")
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == "rchar":
            return int(value)
    pytest.skip("/proc/self/io has no rchar counter")
