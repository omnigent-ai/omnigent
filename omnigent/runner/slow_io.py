"""Dedicated executors for slow blocking work the runner offloads from its loop.

The runner's default executor is small (see ``_entry.py``) and carries
latency-critical work such as native pane checks and terminal delivery. Work
that can block for tens of seconds runs on these pools instead, so a backlog
cannot stall message delivery: ``git`` over a large tree on one, and provider
CLI calls behind the PR panel on another, so neither panel holds up the other.
Cancelling the awaiting coroutine does not stop a worker thread, which is
why such work must not share a pool with latency-critical work.
"""

from __future__ import annotations

from omnigent.util.dedicated_executor import DedicatedExecutor

_SLOW_IO = DedicatedExecutor(max_workers=4, thread_name_prefix="runner-slow-io")
# A PR panel load fires several reads at once; 4 workers keep them parallel.
_PROVIDER_CLI = DedicatedExecutor(max_workers=4, thread_name_prefix="runner-provider-cli")

run_slow_io = _SLOW_IO.run
run_provider_cli = _PROVIDER_CLI.run
