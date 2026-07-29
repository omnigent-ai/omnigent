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


class _ScriptedConnectWaits:
    """Scripted stand-in for the runner-connect wait.

    Returns the queued outcome for each successive call and records the
    timeout it was given, so a test can assert both the number of waits and
    the budget each one received without any wall-clock sleeping.

    :param outcomes: Outcome per call, in order. ``None`` models "the wait
        expired with no runner"; anything else is returned as the client.
    """

    def __init__(self, outcomes: list[object | None]) -> None:
        """Initialize with the scripted outcomes.

        :param outcomes: Outcome per call, in order.
        """
        self._outcomes = list(outcomes)
        self.timeouts: list[float] = []

    async def __call__(
        self,
        session_id: str,
        runner_router: object,
        tunnel_registry: object,
        *,
        runner_id: str,
        timeout_s: float,
        runner_exit_reports: object = None,
    ) -> object | None:
        """Record the budget and return the next scripted outcome.

        :param session_id: Session id (unused).
        :param runner_router: Router (unused).
        :param tunnel_registry: Registry (unused).
        :param runner_id: Runner id (unused).
        :param timeout_s: Budget this wait was given; recorded.
        :param runner_exit_reports: Report store (unused).
        :returns: The next scripted outcome.
        """
        del session_id, runner_router, tunnel_registry, runner_id, runner_exit_reports
        self.timeouts.append(timeout_s)
        return self._outcomes.pop(0)


async def _call_host_bound_wait(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: _ScriptedConnectWaits,
    status_suspends: bool,
) -> object | None:
    """Drive ``_wait_for_host_bound_runner_client`` over scripted collaborators.

    :param monkeypatch: Pytest patcher.
    :param connect: Scripted connect-wait stand-in.
    :param status_suspends: When ``True`` the status query yields once, so the
        connect settles first; when ``False`` both resolve in the same wait
        turn (the simultaneous-completion case).
    :returns: Whatever the wait returns.
    """
    from omnigent.server.routes._sessions import orchestration

    async def _alive_status(*args: object, **kwargs: object) -> str:
        """Answer the host liveness query with ``alive``.

        :param args: Ignored positional args.
        :param kwargs: Ignored keyword args.
        :returns: ``"alive"``.
        """
        del args, kwargs
        if status_suspends:
            # Yield once so the connect wait reaches ``done`` first.
            await asyncio.sleep(0)
        return "alive"

    monkeypatch.setattr(orchestration, "_wait_for_runner_client", connect)
    monkeypatch.setattr(orchestration, "_query_host_runner_status", _alive_status)

    return await orchestration._wait_for_host_bound_runner_client(
        "conv_x",
        None,
        None,
        runner_id="runner_booting",
        timeout_s=10.0,
        runner_exit_reports=None,
        host_conn=object(),  # type: ignore[arg-type] — patched query ignores it
        host_registry=object(),  # type: ignore[arg-type]
        alive_timeout_s=60.0,
    )


async def test_alive_verdict_waits_out_the_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``alive`` runner gets the grace, then the rest of the budget.

    The cold-boot case: the host still holds the runner's process, so the
    tunnel is coming and only the timing is in doubt. Expiring at the grace
    rotates the binding and races a replacement, which is what produced
    ``runner_unavailable`` with nothing persisted.

    Scripted budgets prove the split without waiting: the grace-length wait
    (10) comes back empty, then a second wait for the remainder (50) returns
    the original runner's client.

    Mutation check: drop the ``alive`` extension and only ``[10.0]`` is
    recorded and ``None`` is returned, failing both assertions.
    """
    sentinel = object()
    connect = _ScriptedConnectWaits([None, sentinel])

    result = await _call_host_bound_wait(monkeypatch, connect=connect, status_suspends=True)

    assert result is sentinel, "the original binding's client must be returned, not None"
    assert connect.timeouts == [10.0, 50.0], (
        f"expected the grace then the remaining budget; got {connect.timeouts!r}"
    )


async def test_alive_verdict_survives_simultaneous_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grace that expires in the same turn as ``alive`` keeps the verdict.

    ``asyncio.wait`` can report both the connect wait and the status query as
    done together. Reading the connect result first and returning it would
    throw away an ``alive`` answer that arrived simultaneously, relaunching a
    runner the host had just confirmed was still coming up.

    Mutation check: return the connect result whenever it is done (ignoring a
    same-turn verdict) and this records only ``[10.0]`` and returns ``None``.
    """
    sentinel = object()
    connect = _ScriptedConnectWaits([None, sentinel])

    result = await _call_host_bound_wait(monkeypatch, connect=connect, status_suspends=False)

    assert result is sentinel, (
        "a same-turn 'alive' verdict must still extend the wait; returning "
        "None here means the verdict was discarded"
    )
    assert connect.timeouts == [10.0, 50.0], (
        f"expected the grace then the remaining budget; got {connect.timeouts!r}"
    )
