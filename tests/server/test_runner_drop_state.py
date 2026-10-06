"""Tests for the runner drop record, drop classification and the silent-drop grace."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator

import pytest

from omnigent.server import runner_drop_state
from omnigent.server.routes.runner_tunnel import (
    PING_TIMEOUT_CLOSE_CODE,
    SILENT_DROP_FRAME_AGE_S,
    classify_tunnel_end,
)
from omnigent.server.runner_drop_state import RunnerDrop, SilentGraceEnd


@pytest.fixture(autouse=True)
def _clean_drop_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    runner_drop_state.reset_for_tests()
    monkeypatch.setattr(runner_drop_state, "_host_online_probe", None)
    yield
    runner_drop_state.reset_for_tests()


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "last_frame_age_s", "expected"),
    [
        # The keepalive declared the runner dead.
        (PING_TIMEOUT_CLOSE_CODE, 0.0, "silent"),
        (PING_TIMEOUT_CLOSE_CODE, 120.0, "silent"),
        # Quiet for nearly the keepalive window, however the socket finally ended:
        # the server's own close, the library's keepalive timeout, or a late reset.
        (None, SILENT_DROP_FRAME_AGE_S, "silent"),
        (None, 126.0, "silent"),
        (1006, 90.0, "silent"),
        (1011, 95.0, "silent"),
        (1001, 100.0, "silent"),
        # A close or reset while the runner was still talking.
        (1006, SILENT_DROP_FRAME_AGE_S - 0.1, "sudden"),
        (1006, 0.5, "sudden"),
        (1000, 3.0, "sudden"),
        (1001, 29.0, "sudden"),
        (None, None, "sudden"),
        (None, 5.0, "sudden"),
        # 1012 is the server shutting down: never silent, however long the runner was quiet.
        (1012, 0.0, "sudden"),
        (1012, 200.0, "sudden"),
    ],
)
def test_classify_tunnel_end(
    code: int | None, last_frame_age_s: float | None, expected: str
) -> None:
    assert classify_tunnel_end(code=code, last_frame_age_s=last_frame_age_s) == expected


def test_silent_threshold_sits_just_under_the_keepalive_window() -> None:
    from omnigent.util.tunnel_limits import TUNNEL_KEEPALIVE_PING_TIMEOUT_S

    assert 0 < TUNNEL_KEEPALIVE_PING_TIMEOUT_S - SILENT_DROP_FRAME_AGE_S <= 10


# ── drop record ─────────────────────────────────────────────────────────────


def test_note_get_clear_round_trip() -> None:
    assert runner_drop_state.get("runner-a") is None
    before = time.monotonic()
    drop = runner_drop_state.note("runner-a", "silent")
    assert drop.kind == "silent"
    assert before <= drop.dropped_at <= time.monotonic()
    assert runner_drop_state.get("runner-a") == drop
    # Records are per runner.
    assert runner_drop_state.get("runner-b") is None
    runner_drop_state.clear("runner-a")
    assert runner_drop_state.get("runner-a") is None
    # Clearing a runner with no record is a no-op.
    runner_drop_state.clear("runner-a")


def test_a_later_drop_replaces_the_earlier_one() -> None:
    runner_drop_state.note("runner-a", "silent")
    second = runner_drop_state.note("runner-a", "sudden")
    assert runner_drop_state.get("runner-a") == second
    assert second.kind == "sudden"


def test_a_runner_without_a_binding_has_no_record() -> None:
    assert runner_drop_state.get(None) is None


def test_reset_for_tests_drops_every_record() -> None:
    runner_drop_state.note("runner-a", "silent")
    runner_drop_state.note("runner-b", "sudden")
    runner_drop_state.reset_for_tests()
    assert runner_drop_state.get("runner-a") is None
    assert runner_drop_state.get("runner-b") is None


async def test_records_of_runners_that_never_returned_are_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_drop_state, "_RETAIN_S", 0.05)
    runner_drop_state.note("runner-gone", "silent")
    await asyncio.sleep(0.1)
    runner_drop_state.note("runner-new", "silent")
    assert runner_drop_state.get("runner-gone") is None
    assert runner_drop_state.get("runner-new") is not None


# ── host liveness ───────────────────────────────────────────────────────────


async def test_host_liveness_defaults_to_offline() -> None:
    assert await runner_drop_state.host_is_online("host-a") is False
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    # An unresolvable host is offline even when the probe would say otherwise.
    assert await runner_drop_state.host_is_online(None) is False
    assert await runner_drop_state.host_is_online("host-a") is True


async def test_a_failing_host_probe_reads_as_offline() -> None:
    def probe(_host_id: str) -> bool:
        raise RuntimeError("hosts table unavailable")

    runner_drop_state.configure_host_probe(probe)
    assert await runner_drop_state.host_is_online("host-a") is False


# ── silent-drop grace ───────────────────────────────────────────────────────


class _FakeRunner:
    """Event-driven stand-in for ``TunnelRegistry.wait_for_runner``."""

    def __init__(self) -> None:
        self._back = asyncio.Event()
        self.timeouts: list[float] = []

    def come_back(self) -> None:
        self._back.set()

    async def wait(self, timeout_s: float) -> bool:
        self.timeouts.append(timeout_s)
        if timeout_s <= 0:
            return self._back.is_set()
        try:
            await asyncio.wait_for(self._back.wait(), timeout_s)
        except asyncio.TimeoutError:
            return False
        return True


def _hosts(*host_ids: str) -> Callable[[], Awaitable[list[str]]]:
    async def bound_host_ids() -> list[str]:
        return list(host_ids)

    return bound_host_ids


def _silent(age_s: float = 0.0) -> RunnerDrop:
    return RunnerDrop(kind="silent", dropped_at=time.monotonic() - age_s)


async def _hold(
    drop: RunnerDrop | None,
    runner: _FakeRunner,
    *,
    hosts: Callable[[], Awaitable[list[str]]] | None = None,
    grace_s: float = 5.0,
    recheck_s: float = 0.03,
) -> SilentGraceEnd | None:
    return await runner_drop_state.hold_for_silent_drop(
        drop,
        grace_s=grace_s,
        recheck_s=recheck_s,
        wait_for_runner=runner.wait,
        bound_host_ids=hosts or _hosts("host-a"),
    )


@pytest.mark.parametrize("kind", [None, "sudden"])
async def test_only_a_silent_drop_earns_extra_grace(kind: str | None) -> None:
    runner = _FakeRunner()
    drop = None if kind is None else RunnerDrop(kind="sudden", dropped_at=time.monotonic())
    assert await _hold(drop, runner) is None
    assert runner.timeouts == [], "a sudden drop must not wait or consult the runner"


async def test_a_silent_drop_past_the_silent_grace_earns_nothing() -> None:
    runner = _FakeRunner()
    assert await _hold(_silent(age_s=10.0), runner, grace_s=5.0) is None


async def test_a_registered_runner_earns_nothing() -> None:
    # Its stream failed for another reason (an HTTP error), not a dropped tunnel.
    runner = _FakeRunner()
    runner.come_back()
    assert await _hold(_silent(), runner) is None


async def test_a_host_that_is_already_online_ends_the_grace_at_once() -> None:
    runner_drop_state.configure_host_probe(lambda host_id: host_id == "host-a")
    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(), runner)
    assert end == SilentGraceEnd("expired", host_online=True, extended=False)
    assert time.monotonic() - started < 1.0


async def test_runner_returning_with_its_host_offline_ends_the_wait() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.1, runner.come_back)
    started = time.monotonic()
    end = await _hold(_silent(), runner)
    assert end == SilentGraceEnd("reconnected", host_online=False, extended=True)
    assert time.monotonic() - started < 2.0, "the wait must resolve when the runner returns"
    assert max(runner.timeouts) <= 0.03 + 1e-6, "every wait is bounded by the recheck interval"


async def test_an_unresolved_host_counts_as_offline() -> None:
    # Even a probe that reports everything online cannot vouch for a host nobody could name.
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.05, runner.come_back)
    end = await _hold(_silent(), runner, hosts=_hosts())
    assert end == SilentGraceEnd("reconnected", host_online=False, extended=True)


async def test_a_failed_host_lookup_counts_as_offline() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.05, runner.come_back)

    async def failing_lookup() -> list[str]:
        raise RuntimeError("conversations table unavailable")

    end = await _hold(_silent(), runner, hosts=failing_lookup)
    assert end == SilentGraceEnd("reconnected", host_online=False, extended=True)


async def test_nothing_returning_expires_at_the_silent_grace() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(age_s=0.1), runner, grace_s=0.3)
    elapsed = time.monotonic() - started
    assert end == SilentGraceEnd("expired", host_online=False, extended=True)
    # Measured from the drop, not from when the extension began.
    assert 0.1 <= elapsed < 1.0


async def test_a_host_back_without_its_runner_ends_the_wait_after_one_recheck() -> None:
    came_back_at: list[float] = []

    def probe(_host_id: str) -> bool:
        if came_back_at:
            return True
        if time.monotonic() - started >= 0.1:
            came_back_at.append(time.monotonic())
            return True
        return False

    runner_drop_state.configure_host_probe(probe)
    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(), runner, grace_s=30.0, recheck_s=0.05)
    assert end == SilentGraceEnd("host_back_runner_missing", host_online=True, extended=True)
    assert came_back_at, "the host never read as back"
    # Shortly after the host returns (one recheck to notice, one more for the runner),
    # nowhere near the 30 s grace.
    assert time.monotonic() - came_back_at[0] < 1.0


async def test_a_runner_returning_after_its_host_still_wins() -> None:
    loop = asyncio.get_running_loop()
    runner = _FakeRunner()
    probes: list[bool] = []

    def probe(_host_id: str) -> bool:
        # Offline at the first check, back on every later one; the runner follows at once.
        back = bool(probes)
        probes.append(back)
        if back:
            loop.call_soon_threadsafe(runner.come_back)
        return back

    runner_drop_state.configure_host_probe(probe)
    end = await _hold(_silent(), runner, grace_s=30.0)
    assert end == SilentGraceEnd("reconnected", host_online=True, extended=True)


async def test_a_host_that_leaves_again_keeps_the_wait_open() -> None:
    samples: list[bool] = []

    def flapping(_host_id: str) -> bool:
        # Offline at the start, back for one probe, gone again, then never seen.
        value = len(samples) == 1
        samples.append(value)
        return value

    runner_drop_state.configure_host_probe(flapping)
    runner = _FakeRunner()
    end = await _hold(_silent(), runner, grace_s=0.4, recheck_s=0.05)
    assert end == SilentGraceEnd("expired", host_online=False, extended=True)
    assert len(samples) > 3, "the host kept being rechecked while the wait stayed open"
