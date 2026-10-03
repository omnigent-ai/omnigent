"""Regression: overflow ``wait_for_runner`` callers must observe a reconnect
promptly instead of sleeping their whole timeout."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry


class _NoopWS:
    async def send_text(self, data: str) -> None:
        pass

    async def receive_text(self) -> str:
        return await asyncio.Future()


def _hello() -> HelloFrame:
    return HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])


async def _wait_until(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_overflow_waiter_resolves_promptly_on_reconnect() -> None:
    """A caller past the per-runner waiter cap has no future for ``register`` to
    resolve, so it must re-check the registry rather than sleep the full timeout."""
    runner_id = "runner_saturation_reconnect"
    timeout_s = 4.0
    reconnect_after_s = 0.3
    # Well under the timeout: passes only if the reconnect is observed early.
    prompt_bound_s = 1.5

    reg = TunnelRegistry(max_connect_waiters_per_runner=1)

    saturating = asyncio.create_task(reg.wait_for_runner(runner_id, timeout_s=30.0))
    await _wait_until(lambda: reg.connect_waiter_count(runner_id) == 1)

    started = time.monotonic()
    overflow = asyncio.create_task(reg.wait_for_runner(runner_id, timeout_s=timeout_s))
    # The overflow caller is not a registered waiter; the count stays at the cap.
    await asyncio.sleep(reconnect_after_s)
    assert reg.connect_waiter_count(runner_id) == 1

    session = reg.register(runner_id, _NoopWS(), _hello())
    assert await saturating is session

    result = await overflow
    elapsed = time.monotonic() - started

    assert result is session, "overflow caller did not observe the reconnected runner"
    assert elapsed < prompt_bound_s, (
        f"overflow caller took {elapsed:.2f}s to see a runner that reconnected at "
        f"{reconnect_after_s:.2f}s; it slept the full {timeout_s:.1f}s timeout"
    )


@pytest.mark.asyncio
async def test_global_cap_overflow_waiter_resolves_promptly_on_reconnect() -> None:
    """The global-cap overflow branch re-checks the registry the same way."""
    runner_id = "runner_global_cap_reconnect"
    timeout_s = 4.0
    reconnect_after_s = 0.3
    prompt_bound_s = 1.5

    reg = TunnelRegistry(max_connect_waiters_total=1)

    saturating = asyncio.create_task(reg.wait_for_runner("runner_other", timeout_s=30.0))
    await _wait_until(lambda: reg.connect_waiter_count() == 1)

    started = time.monotonic()
    overflow = asyncio.create_task(reg.wait_for_runner(runner_id, timeout_s=timeout_s))
    await asyncio.sleep(reconnect_after_s)
    assert reg.connect_waiter_count(runner_id) == 0
    assert reg.connect_waiter_count() == 1

    session = reg.register(runner_id, _NoopWS(), _hello())
    result = await overflow
    elapsed = time.monotonic() - started
    await _cancel(saturating)

    assert result is session, "global-cap overflow caller did not observe the reconnected runner"
    assert elapsed < prompt_bound_s, (
        f"global-cap overflow caller took {elapsed:.2f}s to see a runner that "
        f"reconnected at {reconnect_after_s:.2f}s"
    )


@pytest.mark.asyncio
async def test_overflow_waiter_still_waits_out_timeout_without_runner() -> None:
    """Re-checking must not end the wait early: an absent runner yields ``None``
    only once the timeout elapses, and the caller never registers a waiter."""
    runner_id = "runner_saturation_timeout"
    timeout_s = 0.3

    reg = TunnelRegistry(max_connect_waiters_per_runner=1)
    saturating = asyncio.create_task(reg.wait_for_runner(runner_id, timeout_s=30.0))
    await _wait_until(lambda: reg.connect_waiter_count(runner_id) == 1)

    started = time.monotonic()
    result = await reg.wait_for_runner(runner_id, timeout_s=timeout_s)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed >= timeout_s - 0.05, f"overflow caller gave up after {elapsed:.2f}s"
    assert reg.connect_waiter_count(runner_id) == 1

    await _cancel(saturating)
    assert reg.connect_waiter_count(runner_id) == 0
