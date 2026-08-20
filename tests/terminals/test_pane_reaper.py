"""Tests for the native-pane idle reaper (issue #1349)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.terminals.pane_reaper import (
    _DEFAULT_IDLE_TIMEOUT_S,
    _IDLE_TIMEOUT_ENV,
    NativePaneReaper,
    PaneRef,
    resolve_native_pane_idle_timeout_s,
)


def _pane(conv: str, name: str = "claude") -> PaneRef:
    return PaneRef(conv, f"terminal:{name}:main", name, Path(f"/tmp/omni-test/{conv}.sock"))


class _Fakes:
    """Mutable test doubles so a test can flip busy/panes between scans."""

    def __init__(self) -> None:
        self.panes: list[PaneRef] = []
        self.busy: set[str] = set()
        self.reaped: list[str] = []
        # Optional per-call override: (pane, call_index) -> bool. Lets a test make
        # is_busy answer differently on the classify pass vs the re-check pass.
        self.busy_override: Callable[[PaneRef, int], bool] | None = None
        self.busy_calls = 0

    async def is_busy(self, pane: PaneRef) -> bool:
        self.busy_calls += 1
        if self.busy_override is not None:
            return self.busy_override(pane, self.busy_calls)
        return pane.conversation_id in self.busy

    async def reap(self, pane: PaneRef) -> None:
        self.reaped.append(pane.conversation_id)
        self.panes = [p for p in self.panes if p.conversation_id != pane.conversation_id]


def _make(fakes: _Fakes, *, timeout: float = 100.0, interval: float = 0.01) -> NativePaneReaper:
    return NativePaneReaper(
        list_native_panes=lambda: list(fakes.panes),
        is_busy=fakes.is_busy,
        reap=fakes.reap,
        idle_timeout_s=timeout,
        reaper_interval_s=interval,
    )


# ── Pure idle-clock decision (_classify) ────────────────────────────────────


def test_classify_reaps_only_after_full_window() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    r = _make(f, timeout=100.0)
    assert r._classify(1000.0, [p], busy_convs=set()) == []  # first obs: grace
    assert r._classify(1099.0, [p], busy_convs=set()) == []  # 99s < 100s
    assert r._classify(1100.0, [p], busy_convs=set()) == [p]  # window elapsed


def test_classify_busy_rearms_clock() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=10.0)
    r._classify(0.0, [p], busy_convs={"conv_a"})
    assert r._classify(1000.0, [p], busy_convs={"conv_a"}) == []  # busy re-arms
    r._classify(1000.0, [p], busy_convs=set())  # now idle, grace
    assert r._classify(1010.0, [p], busy_convs=set()) == [p]


def test_classify_first_observation_grace() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=0.001)
    assert r._classify(5.0, [p], busy_convs=set()) == []  # clock seeded this pass


def test_classify_forgets_gone_panes() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=10.0)
    r._classify(0.0, [p], busy_convs=set())
    assert "conv_a" in r._last_busy_at
    r._classify(1.0, [], busy_convs=set())  # pane gone
    assert "conv_a" not in r._last_busy_at


# ── Scan behaviour (_scan_once): reap, skip-busy, TOCTOU re-check ────────────


async def test_scan_reaps_idle_unbusy_pane() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000  # already idle past window
    await r._scan_once()
    assert f.reaped == ["conv_a"]


async def test_scan_skips_busy_pane() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    f.busy = {"conv_a"}
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []  # busy → not reaped, clock re-armed


async def test_scan_recheck_spares_pane_that_became_busy() -> None:
    """TOCTOU guard: a pane idle at selection but busy at the pre-reap re-check
    must NOT be reaped (a turn/client/autonomous run started in between)."""
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    # is_busy: False on the classify-phase call (call 1), True on the re-check
    # call (call 2) — simulating a turn starting between selection and teardown.
    f.busy_override = lambda pane, n: n >= 2
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []  # spared by the re-check
    assert f.busy_calls == 2  # classify + re-check


# ── Env resolver ────────────────────────────────────────────────────────────


def test_resolve_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_IDLE_TIMEOUT_ENV, raising=False)
    assert resolve_native_pane_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


def test_resolve_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "120")
    assert resolve_native_pane_idle_timeout_s() == 120.0


def test_resolve_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "0")
    assert resolve_native_pane_idle_timeout_s() == 0.0


@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_resolve_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, bad)
    assert resolve_native_pane_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


# ── Loop smoke (start/shutdown + disable) ───────────────────────────────────


async def test_loop_reaps_idle_pane() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _make(f, timeout=0.0001, interval=0.01)
    await r.start()
    try:
        for _ in range(100):
            if f.reaped:
                break
            await asyncio.sleep(0.01)
    finally:
        await r.shutdown()
    assert f.reaped == ["conv_a"]


async def test_loop_disabled_when_timeout_non_positive() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _make(f, timeout=0.0, interval=0.01)  # 0 disables
    await r.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await r.shutdown()
    assert f.reaped == []
