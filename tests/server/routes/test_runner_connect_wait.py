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


class _GatedConnectWaits:
    """Connect-wait stand-in whose first wait is released by an event.

    Makes the race ordering explicit instead of scheduler-dependent: the
    first wait blocks until *release* is set, so a test can guarantee the
    liveness verdict has already resolved before the grace-length wait
    reports back. Each call records the budget it was handed.

    :param outcomes: Outcome per call, in order. ``None`` models "this wait
        expired with no runner"; anything else is returned as the client.
    :param release: Event gating the first call, or ``None`` to let every
        call return without suspending.
    """

    def __init__(
        self,
        outcomes: list[object | None],
        release: asyncio.Event | None = None,
    ) -> None:
        """Initialize with the scripted outcomes and optional gate.

        :param outcomes: Outcome per call, in order.
        :param release: Event gating the first call, if any.
        """
        self._outcomes = list(outcomes)
        self._release = release
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
        """Record the budget, wait for the gate, return the next outcome.

        :param session_id: Session id (unused).
        :param runner_router: Router (unused).
        :param tunnel_registry: Registry (unused).
        :param runner_id: Runner id (unused).
        :param timeout_s: Budget this wait was given; recorded.
        :param runner_exit_reports: Report store (unused).
        :returns: The next scripted outcome.
        """
        del session_id, runner_router, tunnel_registry, runner_id, runner_exit_reports
        first = not self.timeouts
        self.timeouts.append(timeout_s)
        if first and self._release is not None:
            await self._release.wait()
        return self._outcomes.pop(0)


async def _call_host_bound_wait(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: _GatedConnectWaits,
    on_status: asyncio.Event | None,
) -> object | None:
    """Drive ``_wait_for_host_bound_runner_client`` over scripted collaborators.

    :param monkeypatch: Pytest patcher.
    :param connect: Gated connect-wait stand-in.
    :param on_status: Event the liveness query sets once it has answered
        ``alive``, used to release *connect*'s first wait. ``None`` leaves the
        query with no side effect.
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
        if on_status is not None:
            on_status.set()
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

    The cold-boot case: the host still holds the runner's process, so it may
    yet register and the grace alone cannot tell. Expiring there rotates the
    binding and launches a replacement, leaving two runners racing for one
    session — and the message failing if the replacement also misses its
    rendezvous.

    Ordering is explicit rather than scheduler-dependent: the grace-length
    wait is gated on the verdict having been answered, so ``alive`` is
    resolved before that wait reports empty. Scripted budgets then prove the
    split — 10 for the grace, 50 for the remainder — and the original
    runner's client is returned.

    Mutation check: drop the ``alive`` extension and only ``[10.0]`` is
    recorded and ``None`` is returned, failing both assertions.
    """
    answered = asyncio.Event()
    sentinel = object()
    connect = _GatedConnectWaits([None, sentinel], release=answered)

    result = await _call_host_bound_wait(monkeypatch, connect=connect, on_status=answered)

    assert result is sentinel, "the original binding's client must be returned, not None"
    assert connect.timeouts == [10.0, 50.0], (
        f"expected the grace then the remaining budget; got {connect.timeouts!r}"
    )


async def test_alive_verdict_survives_simultaneous_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grace that expires in the same turn as ``alive`` keeps the verdict.

    Neither stand-in suspends, so both the connect wait and the liveness
    query finish before ``asyncio.wait``'s waiter is resumed — completing a
    future schedules its done-callbacks through ``call_soon`` rather than
    running them inline, so the waiter observes both in the done set. That is
    the case where reading the connect result first would throw away an
    ``alive`` answer that arrived alongside it and relaunch a runner the host
    had just reported as still held.

    Mutation check: return the connect result whenever it is done (ignoring a
    same-turn verdict) and this records only ``[10.0]`` and returns ``None``.
    """
    sentinel = object()
    connect = _GatedConnectWaits([None, sentinel])  # ungated: never suspends

    result = await _call_host_bound_wait(monkeypatch, connect=connect, on_status=None)

    assert result is sentinel, (
        "a same-turn 'alive' verdict must still extend the wait; returning "
        "None here means the verdict was discarded"
    )
    assert connect.timeouts == [10.0, 50.0], (
        f"the extension must run even though the connect wait had already "
        f"settled; got {connect.timeouts!r}"
    )
