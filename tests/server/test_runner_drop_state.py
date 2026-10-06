"""Tests for the runner drop record, drop classification and the silent-drop grace."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterator

import pytest

from omnigent.db.db_models import workspace_scope
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
    monkeypatch.setattr(runner_drop_state, "_host_managed_probe", None)
    # Each test flips its own host answers; the reuse window has tests of its own.
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 0.0)
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


async def test_managed_hosts_default_to_none_and_follow_the_probe() -> None:
    assert await runner_drop_state.host_is_managed("host-a") is False
    runner_drop_state.configure_host_probe(None, is_managed=lambda host_id: host_id == "host-a")
    assert await runner_drop_state.host_is_managed("host-a") is True
    assert await runner_drop_state.host_is_managed("host-b") is False
    assert await runner_drop_state.host_is_managed(None) is False


async def test_a_managed_check_that_fails_reads_as_managed() -> None:
    def boom(_host_id: str) -> bool:
        raise RuntimeError("hosts table unavailable")

    runner_drop_state.configure_host_probe(None, is_managed=boom)
    # A host that cannot be confirmed to be a laptop keeps the normal grace.
    assert await runner_drop_state.host_is_managed("host-a") is True


class _CountingProbe:
    """A host probe that counts calls and can be held open from the test."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail = False

    def __call__(self, _host_id: str) -> bool:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10), "the test never released the probe"
        if self.fail:
            raise RuntimeError("hosts table unavailable")
        return self.answer


async def test_a_host_answer_is_reused_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 30.0)
    probe = _CountingProbe()
    runner_drop_state.configure_host_probe(probe)

    assert await runner_drop_state.host_is_online("host-a") is True
    assert await runner_drop_state.host_is_online("host-a") is True
    assert probe.calls == 1
    # Another host is its own question.
    assert await runner_drop_state.host_is_online("host-b") is True
    assert probe.calls == 2


async def test_a_host_answer_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 0.05)
    probe = _CountingProbe()
    runner_drop_state.configure_host_probe(probe)

    assert await runner_drop_state.host_is_online("host-a") is True
    await asyncio.sleep(0.1)
    probe.answer = False
    assert await runner_drop_state.host_is_online("host-a") is False
    assert probe.calls == 2


async def test_host_answers_are_kept_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 30.0)
    probe = _CountingProbe()
    runner_drop_state.configure_host_probe(probe)

    with workspace_scope(1):
        await runner_drop_state.host_is_online("host-a")
    with workspace_scope(2):
        await runner_drop_state.host_is_online("host-a")
    assert probe.calls == 2


async def test_concurrent_callers_share_one_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 30.0)
    probe = _CountingProbe()
    probe.release.clear()
    runner_drop_state.configure_host_probe(probe)
    try:
        callers = [
            asyncio.create_task(runner_drop_state.host_is_online("host-a")) for _ in range(5)
        ]
        assert await asyncio.to_thread(probe.entered.wait, 5)
        # Let every caller reach the shared lookup before it finishes.
        await asyncio.sleep(0.05)
        probe.release.set()
        assert await asyncio.gather(*callers) == [True] * 5
    finally:
        probe.release.set()
    assert probe.calls == 1


async def test_failed_or_slow_lookups_are_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 30.0)
    probe = _CountingProbe()
    runner_drop_state.configure_host_probe(probe)

    probe.fail = True
    assert await runner_drop_state.host_is_online("host-a") is False
    probe.fail = False
    assert await runner_drop_state.host_is_online("host-a") is True
    assert probe.calls == 2

    # A lookup that outlives its window reads as offline, and the next one asks again.
    runner_drop_state.reset_for_tests()
    probe.release.clear()
    try:
        assert await runner_drop_state.host_is_online("host-a", timeout_s=0.05) is False
    finally:
        probe.release.set()
    assert await runner_drop_state.host_is_online("host-a") is True
    assert probe.calls == 4


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


def _answer(value: bool) -> Callable[[], Awaitable[bool]]:
    async def answer() -> bool:
        return value

    return answer


def _silent(age_s: float = 0.0) -> RunnerDrop:
    return RunnerDrop(kind="silent", dropped_at=time.monotonic() - age_s)


async def _hold(
    drop: RunnerDrop | None,
    runner: _FakeRunner,
    *,
    hosts: Callable[[], Awaitable[list[str]]] | None = None,
    stake: Callable[[], Awaitable[bool]] | None = None,
    live_elsewhere: Callable[[], Awaitable[bool]] | None = None,
    grace_s: float = 5.0,
    recheck_s: float = 0.03,
) -> SilentGraceEnd | None:
    return await runner_drop_state.hold_for_silent_drop(
        drop,
        grace_s=grace_s,
        recheck_s=recheck_s,
        wait_for_runner=runner.wait,
        turn_at_stake=stake or _answer(True),
        bound_host_ids=hosts or _hosts("host-a"),
        runner_live_elsewhere=live_elsewhere or _answer(False),
    )


@pytest.mark.parametrize("kind", [None, "sudden"])
async def test_only_a_silent_drop_earns_extra_grace(kind: str | None) -> None:
    runner = _FakeRunner()
    drop = None if kind is None else RunnerDrop(kind="sudden", dropped_at=time.monotonic())
    assert await _hold(drop, runner) is None
    assert runner.timeouts == [], "a sudden drop must not wait or consult the runner"


async def test_a_zero_grace_turns_the_hold_off_without_asking_anything() -> None:
    asked: list[str] = []

    async def stake() -> bool:
        asked.append("stake")
        return True

    runner = _FakeRunner()
    assert await _hold(_silent(), runner, stake=stake, grace_s=0.0) is None
    assert await _hold(_silent(), runner, stake=stake, grace_s=-5.0) is None
    assert asked == [] and runner.timeouts == []


async def test_a_silent_drop_past_the_silent_grace_earns_nothing() -> None:
    runner = _FakeRunner()
    assert await _hold(_silent(age_s=10.0), runner, grace_s=5.0) is None


async def test_a_registered_runner_earns_nothing() -> None:
    # Its stream failed for another reason (an HTTP error), not a dropped tunnel.
    runner = _FakeRunner()
    runner.come_back()
    assert await _hold(_silent(), runner) is None


async def test_a_session_with_nothing_mid_turn_earns_nothing() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    asked: list[str] = []

    async def hosts() -> list[str]:
        asked.append("hosts")
        return ["host-a"]

    runner = _FakeRunner()
    assert await _hold(_silent(), runner, stake=_answer(False), hosts=hosts) is None
    assert asked == [], "the host is only consulted for a turn that is at stake"


async def test_a_failed_turn_check_earns_nothing() -> None:
    async def stake() -> bool:
        raise RuntimeError("conversations table unavailable")

    assert await _hold(_silent(), _FakeRunner(), stake=stake) is None


async def test_a_runner_already_live_on_another_replica_earns_nothing() -> None:
    assert await _hold(_silent(), _FakeRunner(), live_elsewhere=_answer(True)) is None


async def test_a_managed_sandbox_host_earns_nothing() -> None:
    online_asked: list[str] = []

    def online(host_id: str) -> bool:
        online_asked.append(host_id)
        return False

    runner_drop_state.configure_host_probe(online, is_managed=lambda host_id: host_id == "host-a")
    assert await _hold(_silent(), _FakeRunner()) is None
    assert online_asked == [], "a sandbox that cannot wake is not polled"


async def test_a_managed_check_that_fails_earns_nothing() -> None:
    def boom(_host_id: str) -> bool:
        raise RuntimeError("hosts table unavailable")

    runner_drop_state.configure_host_probe(lambda _host_id: False, is_managed=boom)
    assert await _hold(_silent(), _FakeRunner()) is None


async def test_a_machine_that_is_not_a_sandbox_is_held() -> None:
    runner_drop_state.configure_host_probe(
        lambda _host_id: False, is_managed=lambda host_id: host_id == "host-sandbox"
    )
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.05, runner.come_back)
    end = await _hold(_silent(), runner, hosts=_hosts("host-laptop"))
    assert end == SilentGraceEnd("reconnected", host_online=False)


async def test_runner_returning_with_its_host_offline_ends_the_wait() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.1, runner.come_back)
    started = time.monotonic()
    end = await _hold(_silent(), runner)
    assert end == SilentGraceEnd("reconnected", host_online=False)
    assert time.monotonic() - started < 2.0, "the wait must resolve when the runner returns"
    assert max(runner.timeouts) <= 0.03 + 1e-6, "every wait is bounded by the recheck interval"


async def test_a_host_already_online_still_gives_the_runner_one_recheck() -> None:
    runner_drop_state.configure_host_probe(lambda host_id: host_id == "host-a")
    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(), runner, grace_s=30.0, recheck_s=0.1)
    elapsed = time.monotonic() - started
    assert end == SilentGraceEnd("host_back_runner_missing", host_online=True)
    assert elapsed >= 0.1, "the runner was not given a full recheck interval"
    assert elapsed < 5.0


async def test_a_runner_following_an_already_online_host_wins_the_recheck() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.05, runner.come_back)
    started = time.monotonic()
    end = await _hold(_silent(), runner, grace_s=30.0, recheck_s=5.0)
    assert end == SilentGraceEnd("reconnected", host_online=True)
    assert time.monotonic() - started < 2.0


async def test_an_unresolved_host_counts_as_offline() -> None:
    # Even a probe that reports everything online cannot vouch for a host nobody could name.
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    runner = _FakeRunner()
    asyncio.get_running_loop().call_later(0.05, runner.come_back)
    end = await _hold(_silent(), runner, hosts=_hosts())
    assert end == SilentGraceEnd("reconnected", host_online=False)


async def test_a_failed_host_lookup_is_retried_at_each_recheck() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: True)
    lookups: list[int] = []

    async def flaky_lookup() -> list[str]:
        lookups.append(1)
        # The first two lookups fail; the host is named from the third on.
        if len(lookups) <= 2:
            raise RuntimeError("conversations table unavailable")
        return ["host-a"]

    runner = _FakeRunner()
    end = await _hold(_silent(), runner, hosts=flaky_lookup, grace_s=30.0, recheck_s=0.03)
    # Held as an offline host while the lookup failed, then ended by the host it found.
    assert end == SilentGraceEnd("host_back_runner_missing", host_online=True)
    assert len(lookups) >= 3


async def test_nothing_returning_expires_at_the_silent_grace() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(age_s=0.1), runner, grace_s=0.3)
    elapsed = time.monotonic() - started
    assert end == SilentGraceEnd("expired", host_online=False)
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
    assert end == SilentGraceEnd("host_back_runner_missing", host_online=True)
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
    assert end == SilentGraceEnd("reconnected", host_online=True)


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
    assert end == SilentGraceEnd("expired", host_online=False)
    assert len(samples) > 3, "the host kept being rechecked while the wait stayed open"


async def test_a_runner_turning_up_on_another_replica_ends_the_hold() -> None:
    runner_drop_state.configure_host_probe(lambda _host_id: False)
    checks: list[int] = []

    async def live_elsewhere() -> bool:
        # Not there when the hold opens; there from the first recheck on.
        checks.append(1)
        return len(checks) >= 2

    runner = _FakeRunner()
    started = time.monotonic()
    end = await _hold(_silent(), runner, live_elsewhere=live_elsewhere, grace_s=30.0)
    assert end == SilentGraceEnd("live_elsewhere", host_online=False)
    assert time.monotonic() - started < 5.0


@pytest.mark.parametrize("hung", ["online", "managed"])
async def test_a_hung_host_probe_cannot_outlive_the_window(hung: str) -> None:
    probe = _CountingProbe()
    probe.release.clear()
    expected: SilentGraceEnd | None
    if hung == "online":
        runner_drop_state.configure_host_probe(probe)
        expected = SilentGraceEnd("expired", host_online=False)
    else:
        runner_drop_state.configure_host_probe(lambda _host_id: False, is_managed=probe)
        # A check that cannot answer in time reads as managed, which keeps the normal grace.
        expected = None
    grace_s = 0.5
    drop = _silent()
    try:
        end = await _hold(
            drop,
            _FakeRunner(),
            hosts=_hosts("host-a", "host-b"),
            grace_s=grace_s,
            recheck_s=0.05,
        )
    finally:
        probe.release.set()
    assert end == expected
    # Two hung hosts share the one window; a budget each would take twice the grace.
    assert time.monotonic() - drop.dropped_at < grace_s * 1.5


@pytest.mark.parametrize("question", ["online", "managed"])
async def test_the_first_host_to_say_yes_spares_the_rest(question: str) -> None:
    asked: list[str] = []

    def host_a_says_yes(host_id: str) -> bool:
        asked.append(host_id)
        return host_id == "host-a"

    hosts = _hosts("host-a", "host-b")
    if question == "online":
        runner_drop_state.configure_host_probe(host_a_says_yes)
        end = await _hold(_silent(), _FakeRunner(), hosts=hosts, grace_s=30.0, recheck_s=0.05)
        assert end == SilentGraceEnd("host_back_runner_missing", host_online=True)
    else:
        runner_drop_state.configure_host_probe(lambda _host_id: False, is_managed=host_a_says_yes)
        assert await _hold(_silent(), _FakeRunner(), hosts=hosts) is None
    assert set(asked) == {"host-a"}, "host-b was asked after host-a had already said yes"


async def test_the_sandbox_check_runs_once_per_lookup() -> None:
    managed = _CountingProbe(answer=False)
    runner_drop_state.configure_host_probe(lambda _host_id: False, is_managed=managed)
    end = await _hold(_silent(), _FakeRunner(), grace_s=0.4, recheck_s=0.05)
    assert end == SilentGraceEnd("expired", host_online=False)
    assert managed.calls == 1, "the rechecks must not ask again about a host already weighed"


async def test_a_host_found_on_a_retry_is_still_checked_for_being_a_sandbox() -> None:
    online_asked: list[str] = []

    def online(host_id: str) -> bool:
        online_asked.append(host_id)
        return False

    runner_drop_state.configure_host_probe(
        online, is_managed=lambda host_id: host_id == "host-sandbox"
    )
    lookups: list[int] = []

    async def flaky_lookup() -> list[str]:
        lookups.append(1)
        if len(lookups) == 1:
            raise RuntimeError("conversations table unavailable")
        return ["host-sandbox"]

    grace_s = 2.0
    drop = _silent()
    end = await _hold(drop, _FakeRunner(), hosts=flaky_lookup, grace_s=grace_s, recheck_s=0.05)
    assert end is None, "a sandbox cannot wake, however late the lookup finds it"
    assert time.monotonic() - drop.dropped_at < grace_s / 2, "the hold must end at that recheck"
    assert len(lookups) == 2
    assert online_asked == [], "a sandbox that cannot wake is not polled"


async def test_a_hung_lookup_cannot_outlive_the_window() -> None:
    async def hung_lookup() -> list[str]:
        await asyncio.sleep(30)
        return ["host-a"]

    started = time.monotonic()
    end = await _hold(_silent(), _FakeRunner(), hosts=hung_lookup, grace_s=0.3, recheck_s=0.05)
    assert end == SilentGraceEnd("expired", host_online=False)
    assert time.monotonic() - started < 2.0
