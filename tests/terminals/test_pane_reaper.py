"""Tests for the native-pane idle reaper (issue #1349)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.native import native_cost_popup
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.pane_reaper import (
    _DEFAULT_IDLE_TIMEOUT_S,
    _IDLE_TIMEOUT_ENV,
    ConfirmVerdict,
    NativePaneReaper,
    PaneAssessment,
    PaneRef,
    SpareReason,
    resolve_native_pane_idle_timeout_s,
)
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient
from tests.terminals.native_pane_rig import build_pane_rig


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


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "Infinity", "NaN"])
def test_a_non_finite_seconds_knob_falls_back_to_its_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    """``float()`` parses nan and inf, but neither is a usable number of seconds.

    Every comparison with nan is false and an infinite window never ends, so
    both are rejected with a warning, like a negative value.
    """
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, raw)
    with caplog.at_level("WARNING", logger="omnigent.terminals.pane_reaper"):
        assert resolve_native_pane_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1, messages
    assert f"{_IDLE_TIMEOUT_ENV}={raw!r} is not a finite number" in messages[0]


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


def test_kimi_is_exempt_from_pane_reaping() -> None:
    # kimi records no resumable chat id, so a reaped pane cannot be re-created
    # with its context; the name filter must never offer kimi panes to the reaper.
    from omnigent.terminals.pane_reaper import NATIVE_PANE_TERMINAL_NAMES

    assert "kimi" not in NATIVE_PANE_TERMINAL_NAMES
    assert "claude" in NATIVE_PANE_TERMINAL_NAMES


async def test_runner_busy_check_spares_a_pane_parked_on_an_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The runner's busy check treats a fresh approval-wait marker as busy.

    A pane parked on a permission prompt has no active turn, no ``running``
    status, no attached client and no output, so every other signal reads idle
    and the reaper would kill the prompt under a still-answerable card.
    """
    # Bound by name when the app is built, so stub before building: no tmux here.
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda *_args: [])
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    pane = PaneRef(
        "conv_parked", terminal_resource_id("claude", "main"), "claude", tmp_path / "tmux.sock"
    )

    assert not await reaper._is_busy(pane)

    marker = claude_native_bridge.approval_wait_marker_path("conv_parked")
    marker.parent.mkdir(parents=True)
    claude_native_bridge.touch_approval_wait_marker(marker)
    assert await reaper._is_busy(pane)
    # Another session's parked prompt does not spare this pane.
    other = PaneRef("conv_other", pane.terminal_id, "claude", pane.socket_path)
    assert not await reaper._is_busy(other)


# ── Evidence-anchored idle clock ─────────────────────────────────────────────


def test_classify_moves_the_clock_to_the_last_evidence_of_work() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=100.0)
    assert r._classify(1000.0, [p], set(), {"conv_a": 5000.0}) == []  # first sight: grace
    # Output 30s ago moves the clock forward to 1020: not reapable until 1120.
    assert r._classify(1050.0, [p], set(), {"conv_a": 30.0}) == []
    assert r._classify(1119.0, [p], set(), {"conv_a": 99.0}) == []
    assert r._classify(1120.0, [p], set(), {"conv_a": 100.0}) == [p]


def test_classify_evidence_never_moves_the_clock_back() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=100.0)
    r._classify(1000.0, [p], set())
    assert r._classify(1100.0, [p], set(), {"conv_a": 3 * 3600.0}) == [p]


def test_classify_without_ages_matches_the_legacy_clock() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    legacy = _make(f, timeout=10.0)
    aged = _make(f, timeout=10.0)
    for now, busy in ((0.0, set()), (5.0, {"conv_a"}), (14.0, set()), (15.0, set())):
        assert legacy._classify(now, [p], busy) == aged._classify(now, [p], busy, {})


async def test_scan_uses_only_the_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from omnigent.terminals import pane_reaper as pane_reaper_module

    clock = {"now": 1e6}
    monkeypatch.setattr(
        pane_reaper_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _make(f, timeout=100.0)
    await r._scan_once()
    clock["now"] += 100.0
    await r._scan_once()
    assert f.reaped == ["conv_a"]


# ── Assessment wiring: reasons, confirm, ceilings, logs ─────────────────────


def _assessing(
    f: _Fakes,
    assessments: dict[str, PaneAssessment],
    *,
    confirm: object = None,
    timeout: float = 100.0,
    max_turn_s: float | None = None,
) -> NativePaneReaper:
    async def _assess(pane: PaneRef) -> PaneAssessment:
        return assessments.get(pane.conversation_id, PaneAssessment())

    return NativePaneReaper(
        list_native_panes=lambda: list(f.panes),
        assess=_assess,
        confirm_reap=confirm,  # type: ignore[arg-type]
        reap=f.reap,
        idle_timeout_s=timeout,
        max_turn_s=max_turn_s,
    )


async def test_confirm_spare_rearms_and_proceed_reaps() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    verdicts = [ConfirmVerdict(False, SpareReason.TURN_PROBE), ConfirmVerdict(True)]

    async def _confirm(pane: PaneRef) -> ConfirmVerdict:
        return verdicts.pop(0)

    r = _assessing(f, {}, confirm=_confirm)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []
    assert time.monotonic() - r._last_busy_at["conv_a"] < 5
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == ["conv_a"]


async def test_confirm_exception_spares_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]

    async def _confirm(pane: PaneRef) -> ConfirmVerdict:
        raise RuntimeError("probe blew up")

    r = _assessing(f, {}, confirm=_confirm)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    with caplog.at_level(logging.ERROR, logger="omnigent.terminals.pane_reaper"):
        await r._scan_once()
    assert f.reaped == []
    assert any("pre-reap check failed" in rec.getMessage() for rec in caplog.records)


async def test_confirm_spare_past_its_ceiling_reaps() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]

    async def _confirm(pane: PaneRef) -> ConfirmVerdict:
        return ConfirmVerdict(False, SpareReason.TURN_PROBE)

    r = _assessing(f, {}, confirm=_confirm, timeout=0.01, max_turn_s=0.02)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []
    await asyncio.sleep(0.05)
    await r._scan_once()
    assert f.reaped == ["conv_a"]


async def test_children_reason_expires_after_max_turn() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held = {"conv_a": PaneAssessment(frozenset({SpareReason.CHILDREN}), 3 * 3600.0)}
    r = _assessing(f, held, timeout=0.01, max_turn_s=0.05)
    await r._scan_once()
    await asyncio.sleep(0.02)
    await r._scan_once()
    assert f.reaped == []
    await asyncio.sleep(0.06)
    await r._scan_once()
    await asyncio.sleep(0.02)
    await r._scan_once()
    assert f.reaped == ["conv_a"]


async def test_reason_transitions_log_once(caplog: pytest.LogCaptureFixture) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held = {"conv_a": PaneAssessment(frozenset({SpareReason.RUNNER_TURN}))}
    r = _assessing(f, held)
    with caplog.at_level(logging.INFO, logger="omnigent.terminals.pane_reaper"):
        for _ in range(3):
            await r._scan_once()
        held["conv_a"] = PaneAssessment()
        await r._scan_once()
    spared = [
        rec for rec in caplog.records if rec.__dict__.get("event_name") == "native_pane_spared"
    ]
    assert [rec.attributes["reasons"] for rec in spared] == ["runner_turn", ""]


async def test_forgotten_attach_and_dead_watcher_warn_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held = {
        "conv_a": PaneAssessment(
            frozenset({SpareReason.CLIENT_ATTACHED}), 9999.0, {"watcher_alive": False}
        )
    }
    r = _assessing(f, held, timeout=0.01)
    with caplog.at_level(logging.WARNING, logger="omnigent.terminals.pane_reaper"):
        await r._scan_once()
        await asyncio.sleep(0.02)
        for _ in range(3):
            await r._scan_once()
    kinds = [
        rec.attributes["kind"]
        for rec in caplog.records
        if rec.__dict__.get("event_name") == "native_pane_warning"
    ]
    assert kinds.count("client_attached") == 1
    assert kinds.count("watcher_dead") == 1
    assert f.reaped == []


@pytest.mark.parametrize(
    "reason",
    [
        SpareReason.RUNNER_TURN,
        SpareReason.TOOL_CALL,
        SpareReason.CHILDREN,
        SpareReason.AWAITING_HUMAN,
    ],
)
async def test_long_live_work_warns_hourly_not_every_scan(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reason: SpareReason
) -> None:
    from types import SimpleNamespace

    from omnigent.terminals import pane_reaper as pane_reaper_module

    clock = {"now": 1e6}
    monkeypatch.setattr(
        pane_reaper_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _assessing(f, {"conv_a": PaneAssessment(frozenset({reason}))}, timeout=600.0)
    with caplog.at_level(logging.WARNING, logger="omnigent.terminals.pane_reaper"):
        for _ in range(int(3 * 3600 / 60) + 1):  # three hours of scans
            await r._scan_once()
            clock["now"] += 60.0
    long_holds = [
        rec
        for rec in caplog.records
        if rec.__dict__.get("attributes", {}).get("kind") == f"long:{reason}"
    ]
    # First past the idle window (10 min), then once an hour.
    assert len(long_holds) == 3
    assert f.reaped == []


async def test_the_reap_line_names_the_reasons_that_held_the_pane(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from types import SimpleNamespace

    from omnigent.terminals import pane_reaper as pane_reaper_module

    clock = {"now": 1e6}
    monkeypatch.setattr(
        pane_reaper_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held = {"conv_a": PaneAssessment(frozenset({SpareReason.TOOL_CALL}))}
    r = _assessing(f, held, timeout=100.0)
    with caplog.at_level(logging.INFO, logger="omnigent.terminals.pane_reaper"):
        await r._scan_once()
        held["conv_a"] = PaneAssessment(frozenset({SpareReason.AWAITING_HUMAN}))
        clock["now"] += 10.0
        await r._scan_once()
        held["conv_a"] = PaneAssessment()
        for _ in range(3):
            clock["now"] += 60.0
            await r._scan_once()
    assert f.reaped == ["conv_a"]
    (reaped,) = [
        rec for rec in caplog.records if rec.__dict__.get("event_name") == "native_pane_reaped"
    ]
    assert reaped.attributes["recent_reasons"] == "awaiting_human,tool_call"
    # A later pane under the same conversation starts a fresh history.
    assert "conv_a" not in r.snapshot()


@pytest.mark.parametrize(
    ("resolver", "env"),
    [
        ("resolve_approval_max_s", "OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S"),
        ("resolve_max_turn_s", "OMNIGENT_NATIVE_PANE_MAX_TURN_S"),
        ("resolve_native_pane_idle_timeout_s", "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S"),
    ],
)
def test_seconds_knobs_fall_back_to_their_defaults_on_bad_input(
    monkeypatch: pytest.MonkeyPatch, resolver: str, env: str
) -> None:
    from omnigent.terminals import pane_reaper as pane_reaper_module

    resolve = getattr(pane_reaper_module, resolver)
    monkeypatch.delenv(env, raising=False)
    default = resolve()
    assert default > 0
    for bad in ("soon", "-5", "nan", "inf", "-inf"):
        monkeypatch.setenv(env, bad)
        assert resolve() == default
    monkeypatch.setenv(env, "42.5")
    assert resolve() == 42.5


def test_approval_and_turn_ceilings_default_to_one_day() -> None:
    from omnigent.runner import pending_approvals
    from omnigent.terminals.pane_reaper import resolve_approval_max_s, resolve_max_turn_s

    assert resolve_approval_max_s() == pending_approvals._DEFAULT_WAIT_SECONDS == 86400.0
    assert resolve_max_turn_s() == 86400.0


def test_server_grace_defaults_to_the_idle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.terminals.pane_reaper import (
        resolve_server_check_enabled,
        resolve_server_unreachable_grace_s,
    )

    monkeypatch.delenv("OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S", raising=False)
    assert resolve_server_unreachable_grace_s(1234.0) == 1234.0
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S", "30")
    assert resolve_server_unreachable_grace_s(1234.0) == 30.0
    for raw, enabled in (("", True), ("1", True), ("0", False), ("off", False), ("FALSE", False)):
        monkeypatch.setenv("OMNIGENT_NATIVE_PANE_REAP_SERVER_CHECK", raw)
        assert resolve_server_check_enabled() is enabled


async def test_output_alone_while_status_idle_warns_idle_repaint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held = {"conv_a": PaneAssessment(frozenset(), 1.0, {"status": "idle"})}
    r = _assessing(f, held, timeout=0.005)
    with caplog.at_level(logging.WARNING, logger="omnigent.terminals.pane_reaper"):
        await r._scan_once()
        await asyncio.sleep(0.03)
        await r._scan_once()
    assert any(
        rec.__dict__.get("attributes", {}).get("kind") == "idle_repaint" for rec in caplog.records
    )
    assert f.reaped == []


async def test_assessment_error_spares_the_pane(caplog: pytest.LogCaptureFixture) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]

    async def _assess(pane: PaneRef) -> PaneAssessment:
        raise RuntimeError("tmux wedged")

    r = NativePaneReaper(
        list_native_panes=lambda: list(f.panes), assess=_assess, reap=f.reap, idle_timeout_s=1.0
    )
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    with caplog.at_level(logging.ERROR, logger="omnigent.terminals.pane_reaper"):
        await r._scan_once()
    assert f.reaped == []


async def test_reap_returning_false_rearms_the_clock() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]

    async def _spared(pane: PaneRef) -> bool:
        return False

    r = NativePaneReaper(
        list_native_panes=lambda: list(f.panes),
        is_busy=f.is_busy,
        reap=_spared,
        idle_timeout_s=10.0,
    )
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert time.monotonic() - r._last_busy_at["conv_a"] < 5


def test_reaper_requires_an_assessment() -> None:
    with pytest.raises(TypeError):
        NativePaneReaper(list_native_panes=list, reap=_Fakes().reap)  # type: ignore[arg-type]


# ── Human-wait and live-work signals in the runner's assessment ─────────────


async def test_silent_idle_pane_reads_idle_and_probes_tmux_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    assessment = await rig.assess()
    assert assessment.reasons == frozenset()
    assert not assessment.busy
    assert rig.tmux.calls == ["list_clients", "window_activity"]


_LIVE_SIGNALS = {
    "runner_turn": SpareReason.RUNNER_TURN,
    "tool_call": SpareReason.TOOL_CALL,
    "pending_approval": SpareReason.AWAITING_HUMAN,
    "prompt_park": SpareReason.AWAITING_HUMAN,
    "blocked_on": SpareReason.AWAITING_HUMAN,
    "approval_marker": SpareReason.AWAITING_HUMAN,
    "child_launching": SpareReason.CHILDREN,
    "child_running": SpareReason.CHILDREN,
    "child_waiting": SpareReason.CHILDREN,
}


@pytest.mark.parametrize("signal", sorted(_LIVE_SIGNALS))
async def test_each_live_signal_alone_spares_a_silent_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    from omnigent.native import prompt_parks
    from omnigent.runner import app as runner_app
    from omnigent.runner import pending_approvals
    from omnigent.runner.session_status import StatusSource

    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    child = f"{rig.conv_id}_child"
    try:
        if signal == "runner_turn":
            rig.app.state.active_turns[rig.conv_id] = None  # a turn binding its slot
        elif signal == "tool_call":
            rig.app.state.mcp_execution_registry.retain_operation(rig.conv_id, "mcpop_1")
        elif signal == "pending_approval":
            monkeypatch.setitem(pending_approvals._session_pending, rig.conv_id, 1)
        elif signal == "prompt_park":
            prompt_parks.open_park(rig.conv_id, "goose:1")
        elif signal == "blocked_on":
            rig.book.record(
                rig.conv_id, "running", source=StatusSource.RELAY, blocked_on="permission"
            )
        elif signal == "approval_marker":
            marker = claude_native_bridge.approval_wait_marker_path(rig.conv_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            claude_native_bridge.touch_approval_wait_marker(marker)
        else:
            runner_app.register_subagent_work(
                parent_session_id=rig.conv_id, child_session_id=child, agent="w", title="t"
            ).status = signal.removeprefix("child_")
        assessment = await rig.assess()
        assert assessment.reasons == {_LIVE_SIGNALS[signal]}
        assert await rig.is_busy() is True
        rig.reaper._last_busy_at[rig.conv_id] = time.monotonic() - 10 * 3600
        await rig.reaper._scan_once()
        assert rig.alive()
    finally:
        rig.app.state.active_turns.pop(rig.conv_id, None)
        prompt_parks.clear_session(rig.conv_id)
        runner_app._subagent_work_by_child.pop(child, None)
        runner_app._subagent_work_by_parent.pop(rig.conv_id, None)


@pytest.mark.parametrize("child_status", ["completed", "failed", "cancelled"])
async def test_a_finished_child_alone_does_not_spare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_status: str
) -> None:
    from omnigent.runner import app as runner_app

    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    child = f"{rig.conv_id}_child"
    try:
        runner_app.register_subagent_work(
            parent_session_id=rig.conv_id, child_session_id=child, agent="w", title="t"
        ).status = child_status
        assert (await rig.assess()).reasons == frozenset()
    finally:
        runner_app._subagent_work_by_child.pop(child, None)
        runner_app._subagent_work_by_parent.pop(rig.conv_id, None)


async def test_a_child_wake_still_owed_to_the_parent_spares_its_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished child's result waits in the parent's inbox until its wake lands.

    In flight, the wake keeps the parent's pane (the wake starts its next
    turn); once every attempt failed the parent is stranded and still owed it.
    """
    import httpx

    from omnigent.runner import app as runner_app

    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions", json={"session_id": rig.conv_id, "agent_id": "a" * 32}
        )
    assert resp.status_code == 201, resp.text
    wake_url = f"/v1/sessions/{rig.conv_id}/events"
    wake_posted = asyncio.Event()
    wake_answer = asyncio.Event()
    base_post = rig.server.post

    async def _post(url: str, **kwargs: object) -> object:
        if url != wake_url:
            return await base_post(url, **kwargs)
        wake_posted.set()
        await wake_answer.wait()
        # A permanent rejection: no retry, the parent is stranded.
        return httpx.Response(409, request=httpx.Request("POST", f"http://server{wake_url}"))

    monkeypatch.setattr(rig.server, "post", _post)
    child = f"{rig.conv_id}_child"
    try:
        runner_app.register_subagent_work(
            parent_session_id=rig.conv_id, child_session_id=child, agent="w", title="t"
        ).status = "running"
        assert (await rig.assess()).reasons == {SpareReason.CHILDREN}
        rig.app.state.mark_subagent_terminal_and_wake(child, status="completed", output="done")
        await asyncio.wait_for(wake_posted.wait(), timeout=5)
        # The child is done; only the wake in flight is owed to the parent.
        assert (await rig.assess()).reasons == {SpareReason.CHILDREN}
        wake_answer.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if (await rig.assess()).reasons != {SpareReason.CHILDREN}:
                break
        # Every attempt was refused: the parent is stranded and still owed it.
        assert (await rig.assess()).reasons == {SpareReason.CHILDREN}
    finally:
        wake_answer.set()
        runner_app._subagent_work_by_child.pop(child, None)
        runner_app._subagent_work_by_parent.pop(rig.conv_id, None)
        rig.drain()


async def test_a_park_older_than_the_approval_ceiling_does_not_spare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.native import prompt_parks

    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", "5")
    clock = {"now": 100.0}
    monkeypatch.setattr(prompt_parks, "_clock", lambda: clock["now"])
    rig = await build_pane_rig(tmp_path, monkeypatch, key="hermes")
    try:
        prompt_parks.open_park(rig.conv_id, "hermes:1")
        assert (await rig.assess()).reasons == {SpareReason.AWAITING_HUMAN}
        clock["now"] += 6
        assessment = await rig.assess()
        assert assessment.reasons == frozenset()
        assert assessment.facts["park_age_s"] == 6.0
    finally:
        prompt_parks.clear_session(rig.conv_id)


async def test_a_finished_turn_task_in_the_slot_is_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")

    async def _done() -> None:
        return None

    task = asyncio.create_task(_done())
    await task
    rig.app.state.active_turns[rig.conv_id] = task
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        assessment = await rig.assess()
        await rig.assess()
    assert SpareReason.RUNNER_TURN not in assessment.reasons
    assert assessment.facts["turn_slot"] == "done"
    assert sum("finished task" in rec.getMessage() for rec in caplog.records) == 1
    rig.app.state.active_turns[rig.conv_id] = None
    assert (await rig.assess()).reasons == {SpareReason.RUNNER_TURN}
    rig.app.state.active_turns.pop(rig.conv_id)


async def test_a_pane_that_turns_busy_during_the_deep_check_is_spared() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held: dict[str, PaneAssessment] = {}

    async def _confirm(pane: PaneRef) -> ConfirmVerdict:
        held["conv_a"] = PaneAssessment(frozenset({SpareReason.RUNNER_TURN}))
        return ConfirmVerdict(True)

    r = _assessing(f, held, confirm=_confirm)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []


async def test_a_deep_check_hold_restarts_after_the_pane_is_busy_again() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    held: dict[str, PaneAssessment] = {}

    async def _confirm(pane: PaneRef) -> ConfirmVerdict:
        return ConfirmVerdict(False, SpareReason.TURN_PROBE)

    r = _assessing(f, held, confirm=_confirm, timeout=0.01, max_turn_s=0.05)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert "conv_a" in r._confirm_spare_since
    held["conv_a"] = PaneAssessment(frozenset({SpareReason.RUNNER_TURN}))
    await r._scan_once()
    assert "conv_a" not in r._confirm_spare_since
    await asyncio.sleep(0.06)
    held.pop("conv_a")
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []


async def test_a_teardown_that_spares_is_not_logged_or_counted_as_a_reap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    outcomes = [False, True]

    async def _reap(pane: PaneRef) -> bool:
        return outcomes.pop(0)

    held = {"conv_a": PaneAssessment(frozenset({SpareReason.TOOL_CALL}))}

    async def _assess(pane: PaneRef) -> PaneAssessment:
        return held.get(pane.conversation_id, PaneAssessment())

    r = NativePaneReaper(
        list_native_panes=lambda: list(f.panes),
        assess=_assess,
        reap=_reap,
        idle_timeout_s=10.0,
    )
    await r._scan_once()  # the tool call holds it once
    held.clear()
    with caplog.at_level(logging.INFO, logger="omnigent.terminals.pane_reaper"):
        r._last_busy_at["conv_a"] = time.monotonic() - 1000
        await r._scan_once()  # the teardown's own re-check spares it
        assert not [
            rec for rec in caplog.records if rec.__dict__.get("event_name") == "native_pane_reaped"
        ]
        assert r._summary["spared:teardown"] == 1
        assert r._summary["reaped"] == 0
        assert time.monotonic() - r._last_busy_at["conv_a"] < 5
        r._last_busy_at["conv_a"] = time.monotonic() - 1000
        await r._scan_once()
    (reaped,) = [
        rec for rec in caplog.records if rec.__dict__.get("event_name") == "native_pane_reaped"
    ]
    # The reason history survived the spared attempt.
    assert reaped.attributes["recent_reasons"] == "tool_call"
    assert r._summary["reaped"] == 1
