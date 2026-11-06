"""Tests for ``_wait_for_runner_client``.

A runner that the daemon reports dead (``host.runner_exited`` →
``RunnerExitReports``) can never connect, so the runner-connect wait must
end the instant that report appears rather than burning the full timeout.
This is what turns a crashed-runner message from "appears ~33s later" into
"appears as soon as we're convinced the runner is busted".

A runner that DID connect but whose client lookup misses (the binding or tunnel
registration settles a moment later) is retried a bounded number of times
inside the same deadline instead of being reported as failed to start.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.errors import ErrorCode
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes import sessions as sessions_facade
from omnigent.server.routes._sessions import helpers
from omnigent.server.routes.sessions import _wait_for_runner_client
from tests._helpers.polling import poll_until_async
from tests.budgets import budget

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


class _VirtualTime:
    """Fake monotonic clock whose sleeps advance it instantly.

    :param now: Current virtual time in seconds.
    :param sleeps: Every delay the code under test slept, in order.
    """

    def __init__(self) -> None:
        """Start the clock at zero with no sleeps recorded."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the current virtual time."""
        return self.now

    async def sleep(self, delay: float, result: Any = None) -> Any:
        """Advance virtual time by ``delay`` without waiting.

        :param delay: Seconds the caller asked to sleep.
        :param result: Value returned to the caller, as ``asyncio.sleep`` does.
        :returns: ``result``.
        """
        self.sleeps.append(delay)
        self.now += delay
        return result


@pytest.fixture
def virtual_time(monkeypatch: pytest.MonkeyPatch) -> _VirtualTime:
    """Run the wait helper on a fake clock so deadline arithmetic is exact.

    Only the helpers module sees the fake ``time.monotonic`` and
    ``asyncio.sleep``; the event loop itself keeps real time.
    """
    clock = _VirtualTime()
    monkeypatch.setattr(helpers, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        helpers, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": clock.sleep})
    )
    return clock


class _ConnectsRegistry:
    """Tunnel registry stand-in whose runner connects.

    :param clock: Virtual clock advanced by ``connect_after_s`` on each wait,
        or ``None`` to connect instantly in real time.
    :param connect_after_s: Virtual seconds the connect takes.
    :param waited: ``(runner_id, timeout_s)`` of each wait.
    """

    def __init__(self, clock: _VirtualTime | None = None, connect_after_s: float = 0.0) -> None:
        """Record the clock and connect delay."""
        self.clock = clock
        self.connect_after_s = connect_after_s
        self.waited: list[tuple[str, float]] = []

    async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> object:
        """Report the runner connected after the configured virtual delay.

        :param runner_id: Runner id being awaited.
        :param timeout_s: Max seconds the caller allotted.
        :returns: A stand-in registry session.
        """
        self.waited.append((runner_id, timeout_s))
        if self.clock is not None:
            self.clock.now += self.connect_after_s
        return SimpleNamespace(runner_id=runner_id)


class _ScriptedLookup:
    """Runner-client lookup that misses a set number of times, then resolves.

    :param misses: How many leading lookups return ``None``.
    :param on_call: Hook run on every lookup, e.g. to record a crash report.
    :param client: The client returned once the misses are used up.
    :param calls: Lookups made so far.
    """

    def __init__(self, misses: int, on_call: Callable[[int], None] | None = None) -> None:
        """Script the misses and the resolved client."""
        self.misses = misses
        self.on_call = on_call
        self.client = object()
        self.calls = 0

    async def __call__(self, *_args: object, **_kwargs: object) -> object | None:
        """Return ``None`` for the scripted misses, then the client."""
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        return None if self.calls <= self.misses else self.client


def _wait_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the structured ``runner_client_wait`` records captured so far."""
    return [r for r in caplog.records if getattr(r, "event_name", None) == "runner_client_wait"]


async def test_wait_for_runner_client_retries_a_lookup_that_misses_after_connect(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner that connected is not reported failed over one missed lookup.

    The first lookup right after the connect misses; the retry resolves the
    client, so the turn proceeds. Dropping the retry returns ``None`` here and
    the caller reports a healthy runner as failed to start.
    """
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    lookup = _ScriptedLookup(misses=1)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x", None, _ConnectsRegistry(virtual_time), runner_id="runner_ok", timeout_s=30.0
    )

    assert result is lookup.client
    assert lookup.calls == 2
    assert virtual_time.sleeps == [sessions_facade._RUNNER_CLIENT_RESOLVE_RETRY_S]
    [event] = _wait_events(caplog)
    assert event.attributes["outcome"] == "resolved"
    assert event.attributes["attempts"] == 2
    assert event.attributes["waited_s"] == sessions_facade._RUNNER_CLIENT_RESOLVE_RETRY_S


async def test_wait_for_runner_client_first_lookup_hit_needs_no_retry(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The common case resolves on the first lookup without pausing."""
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    lookup = _ScriptedLookup(misses=0)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x",
        None,
        _ConnectsRegistry(virtual_time),
        runner_id="runner_ok",
        timeout_s=30.0,
        runner_exit_reports=RunnerExitReports(),
    )

    assert result is lookup.client
    assert lookup.calls == 1
    assert virtual_time.sleeps == []
    [event] = _wait_events(caplog)
    assert (event.attributes["outcome"], event.attributes["attempts"]) == ("resolved", 1)


async def test_wait_for_runner_client_gives_up_after_bounded_lookups(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connected runner that never resolves is retried a fixed number of times."""
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    lookup = _ScriptedLookup(misses=10**6)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x", None, _ConnectsRegistry(virtual_time), runner_id="runner_x", timeout_s=30.0
    )

    attempts = sessions_facade._RUNNER_CLIENT_RESOLVE_ATTEMPTS
    assert result is None
    assert lookup.calls == attempts
    assert virtual_time.sleeps == [sessions_facade._RUNNER_CLIENT_RESOLVE_RETRY_S] * (attempts - 1)
    [event] = _wait_events(caplog)
    assert event.attributes["outcome"] == "connected_but_unresolved"
    assert event.attributes["attempts"] == attempts
    assert event.levelno == logging.WARNING


@pytest.mark.parametrize(
    ("timeout_s", "connect_after_s", "expected_sleeps", "expected_lookups"),
    [
        # Whole budget left: every retry gets the full gap.
        (30.0, 0.0, [2.0, 2.0], 3),
        # Connect used the whole budget: no lookup or pause remains.
        (5.0, 5.0, [], 0),
        # 1s left: one pause clamped to the budget, then the deadline ends it.
        (5.0, 4.0, [1.0], 1),
        (5.0, 3.5, [1.5], 1),
        # Enough for two pauses, the second clamped to what remains.
        (5.0, 2.5, [2.0, 0.5], 2),
    ],
)
async def test_wait_for_runner_client_lookup_retries_stay_inside_the_deadline(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
    timeout_s: float,
    connect_after_s: float,
    expected_sleeps: list[float],
    expected_lookups: int,
) -> None:
    """Connect wait and lookup retries share one budget; nothing outlasts it."""
    lookup = _ScriptedLookup(misses=10**6)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x",
        None,
        _ConnectsRegistry(virtual_time, connect_after_s),
        runner_id="runner_slow",
        timeout_s=timeout_s,
    )

    assert result is None
    assert virtual_time.sleeps == expected_sleeps
    assert lookup.calls == expected_lookups
    assert virtual_time.now <= timeout_s


@pytest.mark.parametrize("lookup_finishes_at", [5.0, 5.5])
async def test_wait_for_runner_client_rejects_a_hit_after_the_deadline(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
    lookup_finishes_at: float,
) -> None:
    """A lookup cannot rescue a wait whose budget expired while routing."""
    lookup = _ScriptedLookup(
        misses=0,
        on_call=lambda _n: setattr(virtual_time, "now", lookup_finishes_at),
    )
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x", None, _ConnectsRegistry(virtual_time), runner_id="runner_slow", timeout_s=5.0
    )

    assert result is None
    assert lookup.calls == 1
    assert virtual_time.sleeps == []


async def test_wait_for_runner_client_rejects_a_hit_when_runner_reported_dead(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed client is not usable if a crash report arrived during its lookup."""
    reports = RunnerExitReports()
    lookup = _ScriptedLookup(
        misses=0,
        on_call=lambda _n: reports.record("runner_dead", "exited with code 1", owner=None),
    )
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x",
        None,
        _ConnectsRegistry(virtual_time),
        runner_id="runner_dead",
        timeout_s=30.0,
        runner_exit_reports=reports,
    )

    assert result is None
    assert lookup.calls == 1
    assert virtual_time.sleeps == []


async def test_wait_for_runner_client_stops_retrying_when_runner_reported_dead(
    virtual_time: _VirtualTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash report that lands after the connect ends the lookup retries."""
    reports = RunnerExitReports()
    lookup = _ScriptedLookup(
        misses=10**6,
        on_call=lambda _n: reports.record("runner_dead", "exited with code 1", owner=None),
    )
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    result = await _wait_for_runner_client(
        "conv_x",
        None,
        _ConnectsRegistry(virtual_time),
        runner_id="runner_dead",
        timeout_s=30.0,
        runner_exit_reports=reports,
    )

    assert result is None
    assert lookup.calls == 1
    assert virtual_time.sleeps == []


async def test_wait_for_runner_client_never_connected_makes_no_lookup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner that never connects fails at the existing deadline, unresolved."""
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    lookup = _ScriptedLookup(misses=0)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)

    loop = asyncio.get_event_loop()
    start = loop.time()
    result = await _wait_for_runner_client(
        "conv_x",
        None,
        _NeverConnectsRegistry(),  # type: ignore[arg-type] — duck-typed wait_for_runner
        runner_id="runner_absent",
        timeout_s=0.1,
    )
    elapsed = loop.time() - start

    assert result is None
    assert lookup.calls == 0
    assert elapsed < 1.0
    [event] = _wait_events(caplog)
    assert event.attributes["outcome"] == "never_connected"
    assert event.attributes["attempts"] == 0


@pytest.mark.parametrize(
    "outcome",
    [
        "connected",
        "deadline_tie",
        "late_pending",
        "stopped",
        "rebound",
        "cancelled",
        "dead",
        "expired",
    ],
)
async def test_explicit_sign_in_wait_is_bounded_and_owned(monkeypatch, outcome) -> None:
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions import orchestration

    # Only the orchestration deadline uses this clock; asyncio keeps its real clock.
    clock = _VirtualTime()
    monkeypatch.setattr(orchestration, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(orchestration, "_HOST_RUNNER_STARTUP_POLL_S", 0)
    monkeypatch.setattr("omnigent.runner.identity.RUNNER_SIGN_IN_PENDING_WAIT_S", 150.0)
    ordinary_expired = asyncio.Event()
    pending_wait_started = asyncio.Event()
    status_poll_started = asyncio.Event()
    pending_rechecked = asyncio.Event()
    clock_checked = asyncio.Event()
    clock_tick = object()
    connection_results: asyncio.Queue[object] = asyncio.Queue()
    next_status: asyncio.Queue[str] = asyncio.Queue()
    current = SimpleNamespace(runner_id="runner-owned")
    connection = SimpleNamespace(host_id="host-owned")
    registry = SimpleNamespace(get=lambda _host_id: connection)
    store = SimpleNamespace(get_conversation=lambda _session_id: current)
    client = object()
    waits: list[float] = []
    active: list[asyncio.Task] = []
    status_calls = 0

    async def wait(*_args, timeout_s, **_kwargs):
        waits.append(timeout_s)
        task = asyncio.current_task()
        active.append(task)
        try:
            if len(waits) == 1:
                if outcome == "deadline_tie":
                    clock.now = 30.0
                    return None
                await ordinary_expired.wait()
                return None
            pending_wait_started.set()
            deadline = clock.monotonic() + timeout_s
            while clock.monotonic() < deadline:
                result = await connection_results.get()
                if result is clock_tick:
                    clock_checked.set()
                    continue
                return result
            return None
        finally:
            active.remove(task)

    async def status(*_args):
        nonlocal status_calls
        status_calls += 1
        if outcome == "late_pending" and status_calls == 1:
            return "alive"
        # Late pending follows alive, a cancelled poll, then a fresh deadline query.
        if status_calls == 1 or (outcome == "late_pending" and status_calls == 3):
            return "sign_in_pending"
        status_poll_started.set()
        if status_calls == (5 if outcome == "late_pending" else 3):
            pending_rechecked.set()
        return await next_status.get()

    monkeypatch.setattr(orchestration, "_wait_for_runner_client", wait)
    monkeypatch.setattr(orchestration, "_query_host_runner_status", status)
    task = asyncio.create_task(
        orchestration._wait_for_host_bound_runner_client(
            "session-owned",
            None,
            None,
            runner_id="runner-owned",
            timeout_s=30.0,
            runner_exit_reports=None,
            host_conn=connection,
            host_registry=registry,
            conversation_store=store,
        )
    )
    try:
        async with asyncio.timeout(budget(5)):
            if outcome == "late_pending":
                await status_poll_started.wait()
                clock.now = 30.0
                ordinary_expired.set()
            await pending_wait_started.wait()
            assert waits == [30.0, 150.0]
            if outcome in {"connected", "deadline_tie", "late_pending", "expired"}:
                clock.now += 31.0
                next_status.put_nowait("sign_in_pending")
                await pending_rechecked.wait()
                assert waits == [30.0, 150.0], "a repeated status renewed the sign-in wait"
            if outcome in {"connected", "deadline_tie", "late_pending"}:
                assert not task.done(), "ordinary startup grace discarded pending sign-in"
                connection_results.put_nowait(client)
                assert await task is client
            elif outcome == "stopped":
                monkeypatch.setitem(
                    orchestration._intentional_stop_sessions, "session-owned", "runner-owned"
                )
                next_status.put_nowait("sign_in_pending")
                with pytest.raises(OmnigentError, match="was stopped"):
                    await task
            elif outcome == "rebound":
                current.runner_id = "runner-replacement"
                next_status.put_nowait("sign_in_pending")
                with pytest.raises(OmnigentError, match="binding changed"):
                    await task
            elif outcome == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "dead":
                next_status.put_nowait("dead")
                assert await task is None
            else:
                clock.now = 149.0
                connection_results.put_nowait(clock_tick)
                await clock_checked.wait()
                assert not task.done(), "sign-in wait expired before its deadline"
                clock.now = 150.0
                connection_results.put_nowait(clock_tick)
                assert await task is None
            assert not active
            assert len(waits) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome",
    [
        "connected",
        "stopped",
        "rebound",
        "host_rebound",
        "host_lost",
        "host_reconnected",
        "retired_host_verdict",
        "dead",
        "unknown",
        "no_reply",
        "expired",
        "cancelled",
        "sign_in",
        "exit_report",
        "saved_cancelled",
        "unsupported",
        "unsupported_connected",
        "expired_process",
        "short_budget",
        "late_connect",
        "sign_in_resumed",
        "resolution_retry",
        "fresh_expired",
        "fresh_latency",
        "fixed_ceiling",
        "before_deadline",
        "after_deadline",
        "late_owner_read",
        "old_then_young",
        "response_consumed",
        "ordinary_floor",
        "auth_deadline",
    ],
)
async def test_accepted_startup_uses_only_the_owned_process_budget(monkeypatch, outcome) -> None:
    from starlette.datastructures import Headers

    from omnigent.entities.conversation import MessageData, StartupInputOptions, StartupRunnerWait
    from omnigent.errors import OmnigentError
    from omnigent.server import startup_inputs
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    clock = _VirtualTime()
    monkeypatch.setattr(
        orchestration, "time", SimpleNamespace(monotonic=clock.monotonic, time=clock.monotonic)
    )
    monkeypatch.setattr(startup_inputs, "time", SimpleNamespace(time=clock.monotonic))
    monkeypatch.setattr(orchestration, "_HOST_RUNNER_STARTUP_POLL_S", 0)
    current = SimpleNamespace(runner_id="runner-owned", host_id="host-owned", labels={})
    connection = SimpleNamespace(host_id="host-owned")
    registered = connection
    registry = SimpleNamespace(get=lambda _host_id: registered)
    owner_reads = 0
    cross_owner_deadline = False

    def current_binding(_session_id):
        nonlocal owner_reads
        owner_reads += 1
        if outcome == "late_owner_read" and cross_owner_deadline:
            clock.now = 150.0
        return current

    retained = None

    def pending_item(_session_id, _input_id):
        return SimpleNamespace(
            response_id=PENDING_INPUT_RESPONSE_ID,
            status="in_progress",
            data=MessageData(
                role="user", content=[], startup_options=StartupInputOptions(runner_wait=retained)
            ),
        )

    def claim_runner(
        _session_id, _input_id, *, runner_id, deadline, connected=False, tighten_deadline=False
    ):
        nonlocal retained
        if retained is None:
            retained = StartupRunnerWait(runner_id=runner_id, deadline=deadline)
        elif tighten_deadline and deadline < retained.deadline and not retained.connected:
            retained = retained.model_copy(update={"deadline": deadline})
        if connected:
            retained = retained.model_copy(update={"connected": True})
        return retained

    store = SimpleNamespace(
        get_conversation=current_binding,
        get_item=pending_item,
        claim_pending_input_runner=claim_runner,
    )
    saved_input = startup_inputs.StartupInputContext(None, Headers(), None, "input-owned")
    extended = asyncio.Event()
    polled = asyncio.Event()
    initial_status = asyncio.Event()
    initial_results: asyncio.Queue[object] = asyncio.Queue()
    next_status: asyncio.Queue[tuple[str | None, float | None]] = asyncio.Queue()
    connection_results: asyncio.Queue[object] = asyncio.Queue()
    waits: list[float] = []
    status_calls = 0
    queried_connections = []
    active: list[asyncio.Task] = []
    client = object()
    reports = RunnerExitReports()

    async def wait(*_args, timeout_s, **_kwargs):
        waits.append(timeout_s)
        task = asyncio.current_task()
        active.append(task)
        try:
            if len(waits) == 1:
                return await initial_results.get()
            extended.set()
            return await connection_results.get()
        finally:
            active.remove(task)

    async def status(*_args, on_launch_age=None):
        nonlocal status_calls
        queried_connections.append(_args[0])
        status_calls += 1
        initial_status.set()
        if status_calls == 1:
            if outcome in {"fresh_expired", "fresh_latency"}:
                await asyncio.Future()
            if outcome in {"unsupported", "unsupported_connected"}:
                return None
            if outcome == "fixed_ceiling":
                clock.now = 100.0
                return "alive"
            if outcome == "response_consumed":
                clock.now = 2.0
            age = {
                "expired_process": 600.0,
                "old_then_young": 600.0,
                "short_budget": 590.0,
                "response_consumed": 599.0,
                "auth_deadline": 0.0,
            }.get(outcome, 450.0)
            if on_launch_age is not None:
                on_launch_age(age)
            return "alive"
        if outcome in {"fresh_expired", "fresh_latency"}:
            if outcome == "fresh_latency":
                clock.now = 90.0
            if on_launch_age is not None:
                on_launch_age(600.0 if outcome == "fresh_expired" else 550.0)
            return "alive"
        polled.set()
        verdict, age = await next_status.get()
        if age is not None and on_launch_age is not None:
            on_launch_age(age)
        return verdict

    monkeypatch.setattr(orchestration, "_wait_for_runner_client", wait)
    monkeypatch.setattr(orchestration, "_query_host_runner_status", status)
    timeout = 400.0 if outcome == "fixed_ceiling" else 30.0
    task = asyncio.create_task(
        orchestration._wait_for_host_bound_runner_client(
            "session-owned",
            None,
            None,
            runner_id="runner-owned",
            timeout_s=timeout,
            runner_exit_reports=reports,
            host_conn=connection,
            host_registry=registry,
            conversation_store=store,
            startup_input=saved_input,
        )
    )
    try:
        async with asyncio.timeout(budget(5)):
            await initial_status.wait()
            if outcome in {
                "unsupported",
                "unsupported_connected",
                "fresh_expired",
                "fresh_latency",
            }:
                clock.now = 20.0 if outcome == "unsupported_connected" else 30.0
                initial_results.put_nowait(client if outcome == "unsupported_connected" else None)
                if outcome == "unsupported_connected":
                    assert await task is client
                    assert saved_input.runner_wait.connected
                elif outcome == "unsupported":
                    assert await task is None
                else:
                    with pytest.raises(OmnigentError, match="startup deadline"):
                        await task
                assert waits == [30.0]
                return
            if outcome in {
                "expired_process",
                "short_budget",
                "response_consumed",
                "old_then_young",
            }:
                await polled.wait()
                if outcome == "old_then_young":
                    polled.clear()
                    next_status.put_nowait(("alive", 0.0))
                    await polled.wait()
                assert not extended.is_set()
                clock.now = 20.0
                initial_results.put_nowait(client)
                assert await task is client
                assert waits == [30.0]
                return
            if outcome == "fixed_ceiling":
                await polled.wait()
                next_status.put_nowait(("alive", 0.0))
            await extended.wait()
            await polled.wait()
            allowance = (
                500.0
                if outcome == "fixed_ceiling"
                else (600.0 if outcome == "auth_deadline" else 150.0)
            )
            assert waits == [timeout, allowance]
            if outcome in {"connected", "no_reply", "fixed_ceiling"}:
                clock.now = 120.0
                polled.clear()
                next_status.put_nowait((None, None) if outcome == "no_reply" else ("alive", 0.0))
                await polled.wait()
                assert waits == [timeout, allowance], "another reply renewed the wait"
                connection_results.put_nowait(client)
                assert await task is client
            elif outcome in {"stopped", "rebound", "host_rebound"}:
                if outcome == "stopped":
                    monkeypatch.setitem(
                        orchestration._intentional_stop_sessions, "session-owned", "runner-owned"
                    )
                elif outcome == "rebound":
                    current.runner_id = "runner-replacement"
                else:
                    current.host_id = "host-replacement"
                next_status.put_nowait(("alive", None))
                if outcome == "stopped":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(OmnigentError) as error:
                        await task
                    assert error.value.code == ErrorCode.WRONG_REPLICA
            elif outcome == "host_lost":
                registered = None
                next_status.put_nowait((None, None))
                clock.now = allowance
                connection_results.put_nowait(None)
                with pytest.raises(OmnigentError, match="startup deadline"):
                    await task
            elif outcome in {"host_reconnected", "retired_host_verdict"}:
                registered = SimpleNamespace(host_id="host-owned")
                clock.now = 120.0
                verdict = "dead" if outcome == "retired_host_verdict" else "alive"
                next_status.put_nowait((verdict, 0.0))
                await poll_until_async(
                    lambda: len(queried_connections) >= 3 or task.done(), timeout_s=budget(5)
                )
                assert not task.done()
                assert queried_connections[-1] is registered
                next_status.put_nowait(("alive", 0.0))
                connection_results.put_nowait(client)
                assert await task is client
                assert waits == [timeout, allowance], "reconnecting renewed the startup allowance"
            elif outcome in {"dead", "unknown"}:
                next_status.put_nowait((outcome, None))
                assert await task is None
            elif outcome in {"expired", "short_budget", "late_connect"}:
                clock.now = allowance
                connection_results.put_nowait(client if outcome == "late_connect" else None)
                with pytest.raises(OmnigentError, match="startup deadline"):
                    await task
                assert len(waits) == 2
            elif outcome in {"before_deadline", "after_deadline", "late_owner_read"}:
                clock.now = 151.0 if outcome == "after_deadline" else 149.0
                cross_owner_deadline = outcome == "late_owner_read"
                connection_results.put_nowait(client)
                if outcome != "after_deadline":
                    assert await task is client
                else:
                    with pytest.raises(OmnigentError, match="startup deadline"):
                        await task
            elif outcome == "exit_report":
                reports.record("runner-owned", "runner exited with code 1", owner=None)
                connection_results.put_nowait(None)
                assert await task is None
            elif outcome == "saved_cancelled":
                current.labels["omnigent.startup"] = "cancelled"
                next_status.put_nowait(("alive", None))
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome in {"sign_in", "sign_in_resumed", "auth_deadline"}:
                clock.now = {"sign_in": 140.0, "auth_deadline": 30.0}.get(outcome, 50.0)
                polled.clear()
                next_status.put_nowait(("sign_in_pending", None))
                await polled.wait()
                assert waits == [timeout, allowance], "sign-in must reuse the durable budget"
                assert saved_input.runner_wait.deadline == allowance
                if outcome == "auth_deadline":
                    clock.now = 600.0
                    connection_results.put_nowait(None)
                    with pytest.raises(OmnigentError, match="startup deadline"):
                        await task
                elif outcome == "sign_in":
                    clock.now = 150.0
                    connection_results.put_nowait(None)
                    with pytest.raises(OmnigentError, match="startup deadline"):
                        await task
                else:
                    clock.now = 60.0
                    polled.clear()
                    next_status.put_nowait(("alive", None))
                    await polled.wait()
                    assert waits == [timeout, allowance]
                    connection_results.put_nowait(client)
                    assert await task is client
            elif outcome == "resolution_retry":
                clock.now = 50.0
                connection_results.put_nowait(None)
                await poll_until_async(lambda: len(waits) == 3, timeout_s=budget(5))
                assert waits[-1] == 100.0
                connection_results.put_nowait(client)
                assert await task is client
            elif outcome == "ordinary_floor":
                clock.now = 20.0
                polled.clear()
                next_status.put_nowait(("alive", 599.0))
                await polled.wait()
                clock.now = 25.0
                connection_results.put_nowait(client)
                assert await task is client
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not active
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
