"""E2E regression test: a runner sys_os_read whose OSEnvironment helper
cannot be forked must surface a *structured* error, not a bare OS errno.

Reproduces the reported failure mode. On a loaded host the runner's per-turn
``sys_os_*`` dispatch builds a runner-local ``CallerProcessOSEnvironment`` and
spawns its out-of-process helper with ``subprocess.Popen``. When the OS has hit
its per-user process/thread ceiling (``RLIMIT_NPROC`` / thread-table
exhaustion), ``fork(2)`` returns ``EAGAIN`` and CPython's ``_fork_exec`` raises
``BlockingIOError: [Errno 35] Resource temporarily unavailable`` (errno 35 on
macOS, 11 on Linux -- same ``EAGAIN``). The observed production stack tail was::

    tool_dispatch.py  _execute_os_env_tool  -> os_env.read
    os_env.py         read                  -> run_sync_on_thread
    async_utils.py    run_sync_on_thread    -> raise payload
    os_env.py         request               -> _request_locked
    os_env.py         _request_locked       -> _ensure_started_locked
    os_env.py         _ensure_started_locked-> _start_locked
    os_env.py         _start_locked         -> subprocess.Popen
    BlockingIOError: [Errno 35] Resource temporarily unavailable

logged as ``runner OSEnvironment dispatch failed for sys_os_read`` (logger
``omnigent.runner.tool_dispatch`` / function ``_execute_os_env_tool``).

This test drives the REAL runner dispatch entrypoint --
``omnigent.runner.tool_dispatch._execute_os_env_tool("sys_os_read", ...)``,
the exact function and logger the report fingerprints -- against a real
``CallerProcessOSEnvironment``. The reported trigger (the kernel refusing to
fork the helper under resource pressure) is injected as a fault: the helper's
``subprocess.Popen`` raises the identical CPython fork-``EAGAIN``
``BlockingIOError``. This runs the whole real dispatch -> os_env.read ->
run_sync_on_thread -> helper.request -> _start_locked chain; only the kernel's
fork refusal is simulated (it cannot be forced deterministically on a shared CI
host without also destabilising the process running the test).

Buggy behaviour (as reported): the helper-spawn failure is neither
retried (the ``allow_retry`` path guards only post-start pipe I/O, so
``_ensure_started_locked`` propagates on the first attempt) nor wrapped in a
useful reason. The agent-visible tool result is the bare
``{"error": "[Errno 35] Resource temporarily unavailable"}`` -- an opaque OS
errno with no hint that the OS environment helper failed to start.

Desired behaviour (asserted, so the test FAILS until a fix lands and PASSES
after): a helper-spawn failure surfaces a structured, actionable reason that
identifies the OS-environment/helper startup problem. The test still confirms the real
spawn point is reached (``subprocess.Popen`` invoked) and that dispatch returns
a JSON error result rather than crashing the turn.

Fully in-process -- needs neither a live server, a runner, nor an LLM. Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_os_env_helper_spawn_failure_e2e.py -v
"""

from __future__ import annotations

import errno
import json
import logging
import os

import pytest

import omnigent.inner.os_env as os_env_mod
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.tools.builtins.os_env import SysOsReadTool

# The exact CPython _fork_exec text for a fork that returns EAGAIN. Platform
# errno differs (35 on macOS as reported, 11 on Linux CI); the message and the
# BlockingIOError type are identical.
_RAW_FORK_EAGAIN = str(BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN)))

# Substrings that mark a structured, human-actionable reason: any of these in
# the surfaced error identifies the OS-environment helper startup failure rather
# than leaking a bare OS errno. A reasonable fix (mirroring the existing
# "os_env helper failed: {exc}" wrapper for post-start I/O errors) surfaces one.
_STRUCTURED_REASON_MARKERS = ("helper", "os_env", "os environment")


async def test_sys_os_read_helper_spawn_failure_surfaces_structured_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fork-``EAGAIN`` helper spawn must not leak a bare OS errno to the agent.

    Injects the reported fault at the real spawn point and drives the real
    runner dispatch. Asserts the agent-visible tool result carries a structured
    reason naming the OS-environment/helper startup failure -- which fails on
    the unfixed tree (the raw ``[Errno ..] Resource temporarily unavailable``
    is surfaced) and passes once the spawn failure is wrapped in a useful reason.

    :param monkeypatch: Patches ``subprocess.Popen`` to raise fork-``EAGAIN``.
    :param caplog: Captures the ``runner OSEnvironment dispatch failed`` record.
    """
    spawn_attempts = 0

    def _fork_eagain_popen(*args: object, **kwargs: object) -> object:
        nonlocal spawn_attempts
        spawn_attempts += 1
        # Byte-identical to what CPython's _fork_exec raises when fork(2)
        # returns EAGAIN -- the reported trigger.
        raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    monkeypatch.setattr(os_env_mod.subprocess, "Popen", _fork_eagain_popen)

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.tool_dispatch"):
        raw_result = await _execute_os_env_tool(
            SysOsReadTool.name(),
            {"path": "README.md", "offset": 1},
            conversation_id="conv_spawn_eagain",
        )

    # The real spawn point was reached (proving the whole dispatch -> read ->
    # helper.request -> _start_locked chain ran through to subprocess.Popen).
    assert spawn_attempts >= 1, (
        "expected the OSEnvironment helper spawn (subprocess.Popen) to be "
        "attempted; the injected fault was never hit -- dispatch did not reach "
        "the real _start_locked spawn point"
    )
    # Sanity: our patch is fully restored via monkeypatch teardown.
    assert os_env_mod.subprocess.Popen is _fork_eagain_popen

    # Dispatch surfaces an error result instead of crashing the turn.
    result = json.loads(raw_result)
    assert isinstance(result, dict) and "error" in result, (
        f"expected a JSON error result from dispatch, got: {raw_result!r}"
    )
    surfaced_error = str(result["error"])

    # Regression: the surfaced error must be a structured, actionable reason
    # identifying the OS-environment/helper startup failure, NOT the bare OS
    # errno. Unfixed, dispatch surfaces exactly `_RAW_FORK_EAGAIN`, so this
    # fails until the spawn failure is wrapped in a useful reason.
    assert surfaced_error.strip().lower() != _RAW_FORK_EAGAIN.lower(), (
        "sys_os_read helper-spawn failure surfaced the bare OS errno "
        f"{surfaced_error!r} with no context -- the fork-EAGAIN helper-spawn "
        "failure is not wrapped in a structured, actionable reason"
    )
    assert any(marker in surfaced_error.lower() for marker in _STRUCTURED_REASON_MARKERS), (
        "sys_os_read helper-spawn failure did not identify the OS-environment "
        f"helper in its error; got {surfaced_error!r}, expected one of "
        f"{_STRUCTURED_REASON_MARKERS}"
    )
