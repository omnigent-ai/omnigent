"""
Deterministic regression test for the D6 fan-out bug in
:func:`omnigent_client.tools.build_tool_handler`.

The bug: the handler's ``async def execute`` wrapper called a
user-supplied sync ``@tool`` function inline, blocking the
event loop on every invocation. Concurrent invocations (e.g.
a parallel fan-out of async client tools) serialized instead
of running in parallel — and any render loop sharing the
event loop (``omnigent chat`` TUI) froze for the duration.

Fix: ``execute`` now checks ``inspect.iscoroutinefunction``
and dispatches sync bodies to ``asyncio.to_thread``.

This test doesn't rely on a real LLM — it invokes ``execute``
concurrently via ``asyncio.gather`` and asserts that the
total wall-clock is close to the single-body duration, not
N-times it. Fast (~sleep duration) and deterministic.

The re-homed D6 parallel fan-out coverage
(``tests/integration/test_d6_parallel_fan_out_round_trip.py``) exercises
the sessions-layer fan-out round trip.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from omnigent_client._tool_handler import ToolCallInfo
from omnigent_client.tools import build_tool_handler, tool

_SLEEP_S = 0.5
_FAN_OUT = 4


@tool
def _sync_sleep(label: str) -> str:
    """Block for a fixed duration, then echo the label.

    Args:
        label: A tag echoed back so each concurrent call can
            be matched to its return value.
    """
    time.sleep(_SLEEP_S)
    return f"done-{label}"


@pytest.mark.asyncio
async def test_build_tool_handler_runs_sync_bodies_concurrently() -> None:
    """
    N concurrent invocations of a sync ``@tool`` must finish
    in ≈ 1 * _SLEEP_S wall-clock, not N * _SLEEP_S.

    Failure modes this test catches:

    - ``execute`` calls the sync body directly on the event
      loop (the pre-fix pattern ``result = fn(**args)``): the
      loop blocks during each body, siblings queued behind
      each other, elapsed ≈ N * _SLEEP_S. Assertion below
      trips with elapsed close to _FAN_OUT * _SLEEP_S.
    - ``execute`` uses ``asyncio.get_event_loop().run_in_executor``
      with the default thread pool and the pool is size-1:
      same serialization. ``asyncio.to_thread`` uses the
      shared default pool (min 40 threads on CPython) so
      _FAN_OUT concurrent sleeps all find their own worker.

    The comparison uses a 2x ceiling on the single-body
    duration — loose enough to tolerate normal scheduling
    jitter, tight enough to unambiguously fail if bodies
    serialize (N=4 → 4 * 0.5 = 2s serial vs 1s ceiling).
    """
    handler = build_tool_handler([_sync_sleep])

    calls = [
        ToolCallInfo(
            name="_sync_sleep",
            arguments={"label": f"{i}"},
            call_id=f"call_{i}",
            agent_name="test",
            response_id="resp_test",
            iteration=0,
        )
        for i in range(_FAN_OUT)
    ]

    start = time.monotonic()
    results = await asyncio.gather(*(handler.execute(c) for c in calls))
    elapsed = time.monotonic() - start

    assert results == [f"done-{i}" for i in range(_FAN_OUT)], (
        f"Tool results out of order or mismatched: {results!r}"
    )

    # Serial execution would take _FAN_OUT * _SLEEP_S seconds.
    # Parallel should take ≈ _SLEEP_S plus small overhead.
    # A 2x single-body ceiling is the cleanest dividing line.
    serial_floor = _FAN_OUT * _SLEEP_S
    parallel_ceiling = _SLEEP_S * 2.0
    assert elapsed < parallel_ceiling, (
        f"Sync @tool bodies did not run concurrently: "
        f"elapsed={elapsed:.2f}s, parallel ceiling={parallel_ceiling:.2f}s "
        f"(single-body is {_SLEEP_S}s, serial floor would be "
        f"{serial_floor}s). Almost certainly build_tool_handler's "
        f"execute wrapper is calling the sync fn inline on the "
        f"event loop instead of via asyncio.to_thread."
    )
