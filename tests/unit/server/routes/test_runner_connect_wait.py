"""Tests for ``_wait_for_runner_client`` crash-report short-circuit.

A runner that the daemon reports dead (``host.runner_exited`` →
``RunnerExitReports``) can never connect, so the runner-connect wait must
end the instant that report appears rather than burning the full timeout.
This is what turns a crashed-runner message from "appears ~33s later" into
"appears as soon as we're convinced the runner is busted".
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes.sessions import _wait_for_runner_client

pytestmark = pytest.mark.asyncio


class _NeverConnectsRegistry:
    """Tunnel registry stand-in whose runner never connects.

    ``wait_for_runner`` blocks for the full timeout then reports ``None``
    (the real "timed out" outcome), so any early return must come from the
    crash-report short-circuit, not from the connect signal.

    :param waited: Records ``(runner_id, timeout_s)`` of each wait so the
        test can assert the wait was actually attempted.
    """

    def __init__(self) -> None:
        """Initialize with an empty wait log."""
        self.waited: list[tuple[str, float]] = []

    async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> None:
        """Block for the timeout, then report no connection.

        :param runner_id: Runner id being awaited.
        :param timeout_s: Max seconds the caller allotted.
        :returns: ``None`` — the runner never connects.
        """
        self.waited.append((runner_id, timeout_s))
        await asyncio.sleep(timeout_s)
        return


async def test_wait_short_circuits_when_runner_reported_dead() -> None:
    """A crash report ends the wait well before the timeout.

    With a 5s timeout but a report already present, the wait must return
    ``None`` in a fraction of a second. A regression (ignoring the report)
    would block the whole 5s — the asserted ceiling catches that.
    """
    registry = _NeverConnectsRegistry()
    reports = RunnerExitReports()
    reports.record("runner_dead", "runner process exited with code 1", owner=None)

    loop = asyncio.get_event_loop()
    start = loop.time()
    result = await _wait_for_runner_client(
        "conv_x",
        None,  # runner_router unused on the report path (returns before resolve)
        registry,  # type: ignore[arg-type] — duck-typed wait_for_runner
        runner_id="runner_dead",
        timeout_s=5.0,
        runner_exit_reports=reports,
    )
    elapsed = loop.time() - start

    # Convicted busted → None, not a runner client.
    assert result is None
    # Returned on conviction, not after the 5s timeout. Generous ceiling
    # (one poll interval is 0.25s) that still fails loudly on a regression.
    assert elapsed < 1.0, f"wait did not short-circuit on the crash report (took {elapsed:.2f}s)"


async def test_wait_without_report_runs_to_timeout() -> None:
    """No report → the wait behaves as before (resolves at the timeout).

    Guards against the short-circuit firing spuriously: a runner that is
    merely slow to connect (no crash report) must still be waited for.
    """
    registry = _NeverConnectsRegistry()
    reports = RunnerExitReports()  # empty — nothing reported dead

    result = await _wait_for_runner_client(
        "conv_x",
        None,
        registry,  # type: ignore[arg-type]
        runner_id="runner_slow",
        timeout_s=0.1,
        runner_exit_reports=reports,
    )

    # Timed out with no connection and no report → None, after waiting.
    assert result is None
    assert registry.waited == [("runner_slow", 0.1)]
