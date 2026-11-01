"""Tests for the host resource snapshot carried on keepalive pongs."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import namedtuple
from pathlib import Path

import psutil
import pytest

from omnigent.host import stats as stats_module
from omnigent.host.stats import HostStatsSampler, parse_host_stats

_Memory = namedtuple("_Memory", ["total", "available"])
_Net = namedtuple("_Net", ["bytes_recv", "bytes_sent"])
_Nic = namedtuple("_Nic", ["isup", "flags"])
# psutil before 5.9.3 has no ``flags``.
_OldNic = namedtuple("_OldNic", ["isup"])
_Addr = namedtuple("_Addr", ["family", "address"])
_Disk = namedtuple("_Disk", ["total", "free"])

_GIB = 1024**3
_UP = _Nic(True, "up,broadcast,running,multicast")


class _FakeProbes:
    """Scripted psutil readings, advanced one step per ``sample()`` call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.cpu = [0.0, 48.0, 12.5]
        self.net = [{"en0": _Net(1_000, 500)}, {"en0": _Net(61_000, 3_500)}] * 2
        self.nics = {"en0": _UP}
        self.addrs: dict[str, list[_Addr]] = {}
        self.disk_paths: list[str] = []
        monkeypatch.setattr(stats_module.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(psutil, "cpu_percent", self._cpu_percent)
        monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(16 * _GIB, 4 * _GIB))
        monkeypatch.setattr(psutil, "net_io_counters", self._net_io_counters)
        monkeypatch.setattr(psutil, "net_if_stats", lambda: self.nics)
        monkeypatch.setattr(psutil, "net_if_addrs", lambda: self.addrs)
        monkeypatch.setattr(psutil, "disk_usage", self._disk_usage)

    def _cpu_percent(self, interval: float | None = None) -> float:
        # A blocking interval would stall the pong; the sampler must never pass one.
        assert interval is None
        return self.cpu.pop(0)

    def _net_io_counters(self, pernic: bool = False) -> dict[str, _Net]:
        # Per-interface counters, so loopback and down links can be left out.
        assert pernic
        return self.net.pop(0)

    def _disk_usage(self, path: str) -> _Disk:
        self.disk_paths.append(path)
        return _Disk(500 * 10**9, 180 * 10**9)


async def _settle_disk_read(sampler: HostStatsSampler) -> None:
    """Let the background disk read started by ``sample()`` finish."""
    task = sampler._disk_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)


async def test_first_sample_primes_cpu_and_network_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil's first non-blocking CPU reading is meaningless, so it is withheld."""
    _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()

    assert sampler.sample() == {
        "memory_total_bytes": 16 * _GIB,
        "memory_used_bytes": 12 * _GIB,
    }
    await _settle_disk_read(sampler)


async def test_second_sample_reports_cpu_and_network_throughput(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Network is bytes per second from counter deltas, not link capacity."""
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))
    probes = _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)
    probes.now += 30.0

    assert sampler.sample() == {
        "cpu_percent": 48.0,
        "memory_total_bytes": 16 * _GIB,
        "memory_used_bytes": 12 * _GIB,
        "net_rx_bytes_per_s": 2_000,
        "net_tx_bytes_per_s": 100,
        "disk_total_bytes": 500 * 10**9,
        "disk_free_bytes": 180 * 10**9,
    }
    assert probes.disk_paths[0] == str(tmp_path)


async def test_network_skips_loopback_and_down_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback (the runner's own localhost traffic) and down links aren't host traffic."""
    probes = _FakeProbes(monkeypatch)
    probes.nics = {
        "en0": _UP,
        "en1": _UP,
        "lo0": _Nic(True, "up,loopback,running"),
        "utun4": _Nic(False, "pointopoint,multicast"),
    }
    idle = _Net(0, 0)
    busy = _Net(9_000_000, 9_000_000)
    probes.net = [
        {"en0": idle, "en1": idle, "lo0": idle, "utun4": idle, "gone0": idle},
        {
            "en0": _Net(30_000, 3_000),
            "en1": _Net(30_000, 0),
            "lo0": busy,
            "utun4": busy,
            "gone0": busy,
        },
    ]
    sampler = HostStatsSampler()
    sampler.sample()
    probes.now += 30.0

    second = sampler.sample()

    assert second is not None
    assert (second["net_rx_bytes_per_s"], second["net_tx_bytes_per_s"]) == (2_000, 100)
    await _settle_disk_read(sampler)


async def test_interface_flapping_never_spikes_or_zeroes_the_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only interfaces up in both samples count: no drop to 0, no since-boot spike."""
    probes = _FakeProbes(monkeypatch)
    down = _Nic(False, "broadcast,multicast")
    since_boot = _Net(5_000_000_000, 5_000_000_000)
    sampler = HostStatsSampler()
    samples = []
    for nics, counters in [
        ({"en0": _UP, "en1": _UP}, {"en0": _Net(0, 0), "en1": since_boot}),
        ({"en0": _UP, "en1": down}, {"en0": _Net(30_000, 3_000), "en1": since_boot}),
        ({"en0": _UP, "en1": _UP}, {"en0": _Net(60_000, 6_000), "en1": since_boot}),
    ]:
        probes.nics, probes.net = nics, [counters]
        samples.append(sampler.sample())
        probes.now += 30.0

    rates = [(s["net_rx_bytes_per_s"], s["net_tx_bytes_per_s"]) for s in samples[1:] if s]
    assert rates == [(1_000, 100), (1_000, 100)]
    await _settle_disk_read(sampler)


async def test_loopback_is_found_by_address_without_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows and psutil < 5.9.3 report no flags; all-loopback addresses still count."""
    probes = _FakeProbes(monkeypatch)
    probes.nics = {"Loopback Pseudo-Interface 1": _OldNic(True), "Ethernet": _OldNic(True)}
    probes.addrs = {
        "Loopback Pseudo-Interface 1": [
            _Addr(socket.AF_INET, "127.0.0.1"),
            _Addr(socket.AF_INET6, "::1"),
        ],
        "Ethernet": [
            _Addr(psutil.AF_LINK, "aa:bb:cc:dd:ee:ff"),
            _Addr(socket.AF_INET, "10.0.0.5"),
        ],
    }
    idle = _Net(0, 0)
    probes.net = [
        {"Loopback Pseudo-Interface 1": idle, "Ethernet": idle},
        {
            "Loopback Pseudo-Interface 1": _Net(9_000_000, 9_000_000),
            "Ethernet": _Net(30_000, 3_000),
        },
    ]
    sampler = HostStatsSampler()
    sampler.sample()
    probes.now += 30.0

    second = sampler.sample()

    assert second is not None
    assert (second["net_rx_bytes_per_s"], second["net_tx_bytes_per_s"]) == (1_000, 100)
    await _settle_disk_read(sampler)


async def test_counter_reset_reads_as_no_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counters that go backwards (an interface restarted) never yield negative rates."""
    probes = _FakeProbes(monkeypatch)
    probes.net = [{"en0": _Net(10_000, 5_000)}, {"en0": _Net(1_000, 500)}]
    sampler = HostStatsSampler()
    sampler.sample()
    probes.now += 30.0

    second = sampler.sample()

    assert second is not None
    assert (second["net_rx_bytes_per_s"], second["net_tx_bytes_per_s"]) == (0, 0)
    await _settle_disk_read(sampler)


async def test_zero_elapsed_time_reports_no_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two samples on the same clock tick can't divide by zero; rates are just omitted."""
    _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()

    second = sampler.sample()

    assert second is not None
    assert "cpu_percent" in second
    assert "net_rx_bytes_per_s" not in second
    assert "net_tx_bytes_per_s" not in second
    await _settle_disk_read(sampler)


async def test_disk_falls_back_to_home_without_a_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unset or missing runner workspace root measures the home filesystem."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path / "missing"))
    probes = _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)

    assert probes.disk_paths == [str(tmp_path)]


async def test_hung_disk_read_never_delays_a_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disk stat runs off-loop, one at a time, so a dead mount can't stall pongs."""
    probes = _FakeProbes(monkeypatch)
    release = asyncio.Event()
    reads = 0

    async def hung_read() -> None:
        nonlocal reads
        reads += 1
        await release.wait()

    sampler = HostStatsSampler()
    monkeypatch.setattr(sampler, "_read_disk", hung_read)
    samples = []
    for _ in range(3):
        samples.append(sampler.sample())
        # Yield so any disk read a sample started gets to run and be counted.
        await asyncio.sleep(0)
        probes.now += 30.0

    assert samples[1] is not None
    assert "disk_total_bytes" not in samples[1]
    assert reads == 1
    release.set()
    await _settle_disk_read(sampler)


async def test_no_disk_value_while_the_previous_read_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read still pending at the next sample is hung, so its old value isn't resent."""
    probes = _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)
    release = asyncio.Event()

    async def hung_read() -> None:
        await release.wait()

    monkeypatch.setattr(sampler, "_read_disk", hung_read)
    probes.now += 30.0
    recent = sampler.sample()
    probes.now += 30.0
    stale = sampler.sample()

    assert recent is not None
    assert recent["disk_free_bytes"] == 180 * 10**9
    assert stale is not None
    assert "disk_free_bytes" not in stale
    release.set()
    await _settle_disk_read(sampler)


async def test_unexpected_disk_error_yields_no_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-OSError disk failure is absorbed, not left in an un-awaited task."""
    probes = _FakeProbes(monkeypatch)

    def broken_disk(path: str) -> None:
        raise ValueError(f"bad mount table entry for {path}")

    monkeypatch.setattr(psutil, "disk_usage", broken_disk)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)
    probes.now += 30.0

    second = sampler.sample()

    assert second is not None
    assert "disk_total_bytes" not in second
    await _settle_disk_read(sampler)


async def test_probe_failure_yields_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing probe drops the snapshot instead of failing the pong it rides on."""
    _FakeProbes(monkeypatch)

    def broken() -> None:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "virtual_memory", broken)
    sampler = HostStatsSampler()

    assert sampler.sample() is None
    await _settle_disk_read(sampler)


async def test_unexpected_sampling_error_is_absorbed_and_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Any exception type is absorbed; only the first failure is logged loudly."""
    probes = _FakeProbes(monkeypatch)

    def broken() -> None:
        raise KeyError("swap")

    monkeypatch.setattr(psutil, "virtual_memory", broken)
    caplog.set_level(logging.DEBUG, logger=stats_module.__name__)
    sampler = HostStatsSampler()

    first = sampler.sample()
    probes.now += 30.0
    second = sampler.sample()

    assert (first, second) == (None, None)
    levels = [r.levelno for r in caplog.records if r.name == stats_module.__name__]
    assert levels == [logging.ERROR, logging.DEBUG]
    await _settle_disk_read(sampler)


def test_sample_needs_the_event_loop_but_does_not_raise() -> None:
    """Called off the loop, the sampler reports no stats rather than raising."""
    assert HostStatsSampler().sample() is None


def test_parse_host_stats_keeps_only_valid_known_readings() -> None:
    """A malformed value drops just that key; unknown keys never pass through."""
    assert parse_host_stats(
        {
            "cpu_percent": 48,
            "memory_total_bytes": 17_179_869_184,
            "memory_used_bytes": -1,
            "disk_free_bytes": "lots",
            "disk_total_bytes": True,
            "net_rx_bytes_per_s": float("inf"),
            "net_tx_bytes_per_s": 310.5,
            "hostname": "bryan-mbp",
        }
    ) == {
        "cpu_percent": 48,
        "memory_total_bytes": 17_179_869_184,
        "net_tx_bytes_per_s": 310.5,
    }


def test_parse_host_stats_never_raises_on_oversized_integers() -> None:
    """JSON integers too big for a float are handled, not raised as OverflowError."""
    assert parse_host_stats(
        {"cpu_percent": 10**400, "memory_total_bytes": 10**400, "net_rx_bytes_per_s": -(10**400)}
    ) == {"cpu_percent": 100}


def test_parse_host_stats_rejects_implausible_readings() -> None:
    """CPU is clamped to 0-100; absurd byte values and part > total pairs are dropped."""
    assert parse_host_stats({"cpu_percent": 1e308}) == {"cpu_percent": 100}
    assert parse_host_stats({"cpu_percent": -3}) == {"cpu_percent": 0}
    assert parse_host_stats({"cpu_percent": float("nan")}) is None
    assert parse_host_stats({"disk_total_bytes": 2**61, "disk_free_bytes": 10}) == {
        "disk_free_bytes": 10
    }
    assert parse_host_stats({"memory_total_bytes": 8, "memory_used_bytes": 9}) is None
    assert parse_host_stats({"disk_total_bytes": 8, "disk_free_bytes": 9}) is None


@pytest.mark.parametrize("raw", [None, [], "stats", {}, {"hostname": "bryan-mbp"}])
def test_parse_host_stats_reads_unusable_payloads_as_no_stats(raw: object) -> None:
    """Absence, non-objects and objects with nothing usable all mean no stats."""
    assert parse_host_stats(raw) is None
