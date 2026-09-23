"""Tests for the scheduled daily host-daemon restart feature."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host import connect
from omnigent.host.connect import (
    _DAILY_RESTART_IDLE_POLL_S,
    _DAILY_RESTART_MIN_UPTIME_S,
    HostProcess,
)
from omnigent.host.identity import HostIdentity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _host() -> HostProcess:
    """Return a minimal HostProcess suitable for restart-loop testing."""
    return HostProcess(
        identity=HostIdentity(host_id="test-host", name="test"),
        server_url="http://localhost:8000",
    )


# ---------------------------------------------------------------------------
# Next-fire-time computation
# ---------------------------------------------------------------------------


def test_next_fire_time_basic() -> None:
    """04:00 fires at 04:00 the next calendar day when now is after 04:00."""
    tz = ZoneInfo("America/New_York")
    # 10:00 on a Tuesday — next 04:00 is Wednesday.
    after = datetime(2024, 3, 5, 10, 0, 0, tzinfo=tz)
    fire = connect._next_daily_fire(4, 0, after, tz)
    assert fire.hour == 4
    assert fire.minute == 0
    assert fire.date() == after.date() + timedelta(days=1)


def test_next_fire_time_same_day_if_before() -> None:
    """04:00 fires today when now is before 04:00."""
    tz = ZoneInfo("America/New_York")
    after = datetime(2024, 3, 5, 3, 0, 0, tzinfo=tz)
    fire = connect._next_daily_fire(4, 0, after, tz)
    assert fire.hour == 4
    assert fire.minute == 0
    assert fire.date() == after.date()


def test_next_fire_time_exactly_at_deadline_rolls_to_tomorrow() -> None:
    """A now equal to the deadline is not "strictly after" — rolls forward."""
    tz = ZoneInfo("America/New_York")
    after = datetime(2024, 3, 5, 4, 0, 0, tzinfo=tz)
    fire = connect._next_daily_fire(4, 0, after, tz)
    assert fire.date() == after.date() + timedelta(days=1)


def test_next_fire_time_dst_spring_forward_does_not_crash() -> None:
    """Computing the next fire across a spring-forward DST day never raises."""
    # US/Eastern springs forward on the second Sunday of March 2024
    # (2024-03-10): clocks jump from 02:00 to 03:00, so 04:00 still exists.
    tz = ZoneInfo("America/New_York")
    after = datetime(2024, 3, 9, 10, 0, 0, tzinfo=tz)  # day before spring-forward
    fire = connect._next_daily_fire(4, 0, after, tz)
    assert fire.hour == 4
    assert fire.date() == date(2024, 3, 10)


def test_next_fire_time_dst_fall_back_does_not_double_fire() -> None:
    """Computing the next fire across a fall-back DST day returns one occurrence."""
    # US/Eastern falls back on 2024-11-03: 01:00-02:00 happens twice.
    tz = ZoneInfo("America/New_York")
    after = datetime(2024, 11, 2, 10, 0, 0, tzinfo=tz)
    fire = connect._next_daily_fire(4, 0, after, tz)
    assert fire.hour == 4
    assert fire.date() == date(2024, 11, 3)
    # Computing "the next fire after this fire" advances a full day, not zero
    # (which would indicate the ambiguous hour caused a repeat).
    fire2 = connect._next_daily_fire(4, 0, fire, tz)
    assert fire2.date() == fire.date() + timedelta(days=1)


# ---------------------------------------------------------------------------
# Config parsing and feature disabled
# ---------------------------------------------------------------------------


async def test_restart_loop_disabled_when_config_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop exits immediately when host_daily_restart is not in config."""
    monkeypatch.setattr(connect, "load_global_config", lambda *_a, **_kw: {})
    host = _host()
    await host._daily_restart_loop()
    assert not host._restart_requested.is_set()


async def test_restart_loop_disabled_when_config_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop exits immediately when host_daily_restart is an empty string."""
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": ""}
    )
    host = _host()
    await host._daily_restart_loop()
    assert not host._restart_requested.is_set()


async def test_restart_loop_warns_on_malformed_config(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Loop warns and exits when the config value is not a valid HH:MM time."""
    monkeypatch.setattr(
        connect,
        "load_global_config",
        lambda *_a, **_kw: {"host_daily_restart": "not-a-time"},
    )
    host = _host()
    import logging

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host._daily_restart_loop()
    assert not host._restart_requested.is_set()
    assert any("not a valid HH:MM" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fires when idle and deadline passes
# ---------------------------------------------------------------------------


async def test_restart_fires_when_idle_and_deadline_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart fires immediately when deadline is past and no runners are live."""
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": "04:00"}
    )
    monkeypatch.setattr(connect, "_DAILY_RESTART_MIN_UPTIME_S", 0.0)
    # A poll cap larger than the remaining wait collapses the deadline-wait
    # loop to a single hop, keeping the test fast and deterministic.
    monkeypatch.setattr(connect, "_DAILY_RESTART_WAIT_POLL_S", 10**6)

    now_base = datetime(2024, 3, 5, 10, 0, 0, tzinfo=_UTC)
    calls: list[float] = []

    def fake_now() -> datetime:
        return now_base

    async def fake_sleep(s: float) -> None:
        calls.append(s)
        nonlocal now_base
        now_base = now_base + timedelta(seconds=s)

    host = _host()
    # No runners.
    await host._daily_restart_loop(
        now_fn=fake_now,
        sleep_fn=fake_sleep,
        mono_clock=lambda: _DAILY_RESTART_MIN_UPTIME_S + 1.0,
    )

    assert host._restart_requested.is_set()
    # One sleep to reach the deadline, no idle-poll needed (already idle).
    assert len(calls) == 1


async def test_restart_promptly_fires_after_wall_clock_jump_across_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall-clock jump across the deadline (e.g. laptop wake from suspend)
    fires on the next bounded poll instead of waiting out the original
    delay-at-schedule-time computed before the jump.
    """
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": "04:00"}
    )
    monkeypatch.setattr(connect, "_DAILY_RESTART_MIN_UPTIME_S", 0.0)
    monkeypatch.setattr(connect, "_DAILY_RESTART_WAIT_POLL_S", 60.0)

    # ~18h before the 04:00 deadline — the old single-sleep design would
    # compute one ~64800s delay and sleep for the whole span uninterrupted.
    now_base = datetime(2024, 3, 5, 10, 0, 0, tzinfo=_UTC)
    sleep_calls: list[float] = []

    def fake_now() -> datetime:
        return now_base

    async def fake_sleep(s: float) -> None:
        nonlocal now_base
        sleep_calls.append(s)
        # Simulate a suspend: the caller asked to sleep <= 60s (the bounded
        # poll cap), but the wall clock actually jumps forward 20h — well
        # past the deadline — while "monotonic" uptime below barely moves.
        now_base = now_base + timedelta(hours=20)

    host = _host()
    await host._daily_restart_loop(
        now_fn=fake_now,
        sleep_fn=fake_sleep,
        mono_clock=lambda: 10_000.0,  # already well past the uptime guard
    )

    assert host._restart_requested.is_set()
    # A single bounded poll (<=60s requested) was enough to observe the jump
    # and fire — proving the wait loop re-reads the wall clock every poll
    # rather than trusting one ~18h delay computed before the jump.
    assert sleep_calls == [60.0]


# ---------------------------------------------------------------------------
# Defers while runner is live, fires once it exits
# ---------------------------------------------------------------------------


async def test_restart_defers_while_runner_live_then_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart defers while a runner subprocess is live, fires after it exits."""
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": "04:00"}
    )
    monkeypatch.setattr(connect, "_DAILY_RESTART_MIN_UPTIME_S", 0.0)
    monkeypatch.setattr(connect, "_DAILY_RESTART_IDLE_POLL_S", 0.01)
    monkeypatch.setattr(connect, "_DAILY_RESTART_WAIT_POLL_S", 10**6)

    # A fake runner whose process "exits" after the first idle poll.
    fake_proc = MagicMock()
    poll_count = 0

    def fake_poll() -> int | None:
        nonlocal poll_count
        poll_count += 1
        return None if poll_count <= 1 else 0

    fake_proc.poll = fake_poll

    now_base = datetime(2024, 3, 5, 10, 0, 0, tzinfo=_UTC)
    sleep_calls: list[float] = []

    def fake_now() -> datetime:
        return now_base

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)
        nonlocal now_base
        now_base = now_base + timedelta(seconds=s)

    host = _host()
    # Register the live runner.
    host._runners["sess-1"] = connect._RunnerHandle(proc=fake_proc, log_path=Path("/dev/null"))

    await host._daily_restart_loop(
        now_fn=fake_now,
        sleep_fn=fake_sleep,
        mono_clock=lambda: _DAILY_RESTART_MIN_UPTIME_S + 1.0,
    )

    assert host._restart_requested.is_set()
    # There should have been at least one deferred idle poll before firing.
    assert any(s <= _DAILY_RESTART_IDLE_POLL_S for s in sleep_calls)


async def test_live_runner_ids_polls_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """_live_runner_ids offloads proc.poll() so a blocking poll can't stall the loop."""
    host = _host()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    host._runners["sess-1"] = connect._RunnerHandle(proc=fake_proc, log_path=Path("/dev/null"))

    calls: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append("to_thread")
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    live = await host._live_runner_ids()

    assert live == ["sess-1"]
    assert calls == ["to_thread"]


# ---------------------------------------------------------------------------
# Uptime guard
# ---------------------------------------------------------------------------


async def test_restart_does_not_fire_before_min_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart is deferred until the minimum uptime guard is satisfied."""
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": "04:00"}
    )
    monkeypatch.setattr(connect, "_DAILY_RESTART_IDLE_POLL_S", 0.01)
    monkeypatch.setattr(connect, "_DAILY_RESTART_WAIT_POLL_S", 10**6)

    now_base = datetime(2024, 3, 5, 10, 0, 0, tzinfo=_UTC)
    mono_val = [0.0]  # starts at 0 (below guard), jumps past it on first poll

    def fake_now() -> datetime:
        return now_base

    async def fake_sleep(s: float) -> None:
        nonlocal now_base
        now_base = now_base + timedelta(seconds=s)
        mono_val[0] += _DAILY_RESTART_MIN_UPTIME_S + 1.0

    host = _host()
    await host._daily_restart_loop(
        now_fn=fake_now,
        sleep_fn=fake_sleep,
        mono_clock=lambda: mono_val[0],
    )

    assert host._restart_requested.is_set()


async def test_restart_defers_while_below_min_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart does not fire while process uptime is below the guard."""
    monkeypatch.setattr(
        connect, "load_global_config", lambda *_a, **_kw: {"host_daily_restart": "04:00"}
    )
    monkeypatch.setattr(connect, "_DAILY_RESTART_IDLE_POLL_S", 0.01)
    monkeypatch.setattr(connect, "_DAILY_RESTART_WAIT_POLL_S", 10**6)

    # Uptime stays at 0.0 (never meets guard); limit polls via CancelledError.
    now_base = datetime(2024, 3, 5, 10, 0, 0, tzinfo=_UTC)
    poll_count = [0]
    max_polls = 3

    def fake_now() -> datetime:
        return now_base

    async def fake_sleep(s: float) -> None:
        nonlocal now_base
        now_base = now_base + timedelta(seconds=s)
        poll_count[0] += 1
        if poll_count[0] >= max_polls:
            raise asyncio.CancelledError

    host = _host()
    with pytest.raises(asyncio.CancelledError):
        await host._daily_restart_loop(
            now_fn=fake_now,
            sleep_fn=fake_sleep,
            mono_clock=lambda: 0.0,  # always below guard
        )

    assert not host._restart_requested.is_set()


# ---------------------------------------------------------------------------
# _daemon_entry respawn path
# ---------------------------------------------------------------------------


def test_daemon_entry_spawns_replacement_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a scheduled restart, _daemon_entry releases the lock, THEN spawns."""
    from omnigent.host import _daemon_entry
    from omnigent.host.daemon_lifecycle import daemon_record_path, record_flock_is_held
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    target = "https://server.example.com"
    log_path = tmp_path / "daemon.log"

    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--server", target])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging",
        lambda *_a, **_kw: log_path,
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: HostIdentity(host_id="host-test", name="test"),
    )

    spawned: list[dict] = []
    lock_free_at_spawn_time: list[bool | None] = []

    def fake_run_host_process(**kwargs: object) -> bool:
        return True  # signal restart

    def fake_spawn_replacement(*, local: bool, server: str | None) -> bool:
        # The invariant that makes the whole feature work: by the time we're
        # asked to spawn a replacement, the old daemon's flock must already
        # be free, or the replacement's try_acquire() would find it held.
        record = daemon_record_path(target, base_dir=tmp_path)
        lock_free_at_spawn_time.append(record_flock_is_held(record))
        spawned.append({"local": local, "server": server})
        return True

    monkeypatch.setattr("omnigent.host.connect.run_host_process", fake_run_host_process)
    monkeypatch.setattr(_daemon_entry, "_spawn_replacement", fake_spawn_replacement)

    exit_code = _daemon_entry.main()

    assert exit_code == 0
    # Replacement was spawned with the correct mode.
    assert len(spawned) == 1
    assert spawned[0]["local"] is False
    assert spawned[0]["server"] == target
    # The lock was free at the moment _spawn_replacement was called — not
    # just eventually, after main() returns.
    assert lock_free_at_spawn_time == [False]

    # Lock remains free after main() returns too.
    record = daemon_record_path(target, base_dir=tmp_path)
    assert record_flock_is_held(record) is False


def test_daemon_entry_no_spawn_on_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No replacement is spawned when run_host_process returns False."""
    from omnigent.host import _daemon_entry
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    log_path = tmp_path / "daemon.log"

    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--local"])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging",
        lambda *_a, **_kw: log_path,
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: HostIdentity(host_id="host-test", name="test"),
    )

    spawned: list[dict] = []

    def fake_run_host_process(**kwargs: object) -> bool:
        return False  # normal exit

    def fake_spawn_replacement(**kwargs: object) -> bool:
        spawned.append(kwargs)
        return True

    monkeypatch.setattr("omnigent.host.connect.run_host_process", fake_run_host_process)
    monkeypatch.setattr(_daemon_entry, "_spawn_replacement", fake_spawn_replacement)

    # local mode requires ensure_local_omnigent_server; stub it out.
    monkeypatch.setattr(
        "omnigent.host.local_server.ensure_local_omnigent_server",
        lambda: MagicMock(url="http://localhost:6767"),
    )

    exit_code = _daemon_entry.main()

    assert exit_code == 0
    assert spawned == []


def test_daemon_entry_exits_nonzero_when_replacement_fails_to_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed respawn is reported through the exit code, not swallowed.

    Otherwise a disk-full (or similar) error at the scheduled restart moment
    leaves the machine with no host daemon and no signal that anything failed.
    """
    from omnigent.host import _daemon_entry
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    log_path = tmp_path / "daemon.log"

    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--local"])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging",
        lambda *_a, **_kw: log_path,
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: HostIdentity(host_id="host-test", name="test"),
    )
    monkeypatch.setattr(
        "omnigent.host.local_server.ensure_local_omnigent_server",
        lambda: MagicMock(url="http://localhost:6767"),
    )
    monkeypatch.setattr("omnigent.host.connect.run_host_process", lambda **_kw: True)
    monkeypatch.setattr(_daemon_entry, "_spawn_replacement", lambda **_kw: False)

    exit_code = _daemon_entry.main()

    assert exit_code == 1


def test_spawn_replacement_success_uses_platform_spawn_kwargs_and_log_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_spawn_replacement rebuilds argv, attaches a real log file, and uses
    the platform-appropriate detach kwargs rather than a POSIX-only flag.
    """
    from omnigent.host._daemon_entry import _spawn_replacement
    from omnigent.inner import _proc
    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    captured: list[dict] = []

    original_popen = subprocess.Popen

    def fake_popen(args, *, env, **kwargs):  # type: ignore[no-untyped-def]
        captured.append({"args": args, "env": env, **kwargs})
        return original_popen(["true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv(PROCESS_LOG_FILE_ENV_VAR, "/tmp/old.log")

    result = _spawn_replacement(local=False, server="https://example.com")

    assert result is True
    assert len(captured) == 1
    assert captured[0]["args"] == [
        sys.executable,
        "-P",
        "-m",
        "omnigent.host._daemon_entry",
        "--server",
        "https://example.com",
    ]
    # The replacement gets a fresh log path (not the old daemon's), and it is
    # a real file — not /dev/null — so a crash is diagnosable.
    assert captured[0]["env"][PROCESS_LOG_FILE_ENV_VAR] != "/tmp/old.log"
    assert captured[0]["stdout"] not in (subprocess.DEVNULL, None)
    assert captured[0]["stderr"] not in (subprocess.DEVNULL, None)
    # Platform-appropriate detach kwargs, not a hardcoded POSIX flag.
    for key, value in _proc.spawn_kwargs().items():
        assert captured[0][key] == value


def test_spawn_replacement_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Popen failure is logged and reported as False, not raised."""
    from omnigent.host._daemon_entry import _spawn_replacement

    def fake_popen(*args: object, **kwargs: object) -> None:
        raise OSError("no more processes")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert _spawn_replacement(local=True, server=None) is False


# ---------------------------------------------------------------------------
# `omnigent host` CLI exit-code split on a scheduled restart
# ---------------------------------------------------------------------------


def test_host_exits_nonzero_on_scheduled_restart_when_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service-managed host (--non-interactive) exits non-zero on a
    scheduled restart, since launchd/systemd only restart the supervised
    process on a non-zero exit (see service.py's KeepAlive/Restart config).
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")

    def _fake_run(server_url: str, **kwargs: object) -> bool:
        return True  # scheduled restart

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "https://from-arg.example.com", "--non-interactive"])

    assert result.exit_code == 1, result.output


def test_host_exits_zero_and_explains_scheduled_restart_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interactive (non-service) host exits 0 and explains the scheduled
    restart, since no supervisor is watching and a non-zero exit would look
    like a crash.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")

    def _fake_run(server_url: str, **kwargs: object) -> bool:
        return True  # scheduled restart

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "https://from-arg.example.com"])

    assert result.exit_code == 0, result.output
    assert "scheduled daily restart" in result.output
