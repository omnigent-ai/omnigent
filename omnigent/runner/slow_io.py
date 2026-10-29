"""Dedicated executor for slow blocking work the runner offloads from its loop.

The runner's default executor is small (see ``_entry.py``) and carries
latency-critical work such as native pane checks and terminal delivery. Work
that can block for tens of seconds — ``git`` over a large tree, a provider
CLI — runs here instead, so a backlog cannot stall message delivery.
Cancelling the awaiting coroutine does not stop a worker thread, which is
why such work must not share that pool.
"""

from __future__ import annotations

from omnigent.util.dedicated_executor import DedicatedExecutor

_SLOW_IO = DedicatedExecutor(max_workers=4, thread_name_prefix="runner-slow-io")

run_slow_io = _SLOW_IO.run
