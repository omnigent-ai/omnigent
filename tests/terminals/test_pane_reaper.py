"""Tests for the native-pane idle reaper (issue #1349)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.inner.terminal import TerminalInstance
from omnigent.native import native_cost_popup
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.pane_reaper import (
    _DEFAULT_IDLE_TIMEOUT_S,
    _IDLE_TIMEOUT_ENV,
    PANE_OUTPUT_BUSY_WINDOW_S,
    NativePaneReaper,
    PaneRef,
    resolve_native_pane_idle_timeout_s,
)
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "failed",
        "disconnected",
        "completed_backfill",
        "completed_backfill_stale_spawn_snapshot",
        "terminal_before_registration",
        "terminal_before_registration_unavailable",
        "terminal_before_registration_empty",
        "unknown_unavailable",
        "unknown_empty",
        "queued_idle_after_resume",
        "unknown_idle_only_empty",
    ],
)
async def test_quiet_codex_discovered_child_holds_its_parent_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Native children protect the pane without runner-dispatched work or recent output."""
    from omnigent.harnesses.codex_native import forwarder

    class EventClient:
        def __init__(self) -> None:
            self.events: asyncio.Queue = asyncio.Queue()
            self.queued_idle_handled: asyncio.Future | None = None

        async def request(self, _method: str, params: dict) -> dict:
            if params["threadId"] == "thread_child" and outcome.endswith("unavailable"):
                raise RuntimeError("synthetic child history unavailable")
            if params["threadId"] == "thread_child" and outcome == "queued_idle_after_resume":
                self.queued_idle_handled = asyncio.get_running_loop().create_future()
                await self.events.put(
                    (
                        {
                            "method": "thread/status/changed",
                            "params": {"threadId": "thread_child", "status": {"type": "idle"}},
                        },
                        self.queued_idle_handled,
                    )
                )
            status = (
                "completed"
                if outcome.startswith(("completed_backfill", "terminal_before_registration"))
                else "inProgress"
            )
            return {
                "result": {
                    "thread": {
                        "id": params["threadId"],
                        "turns": []
                        if outcome.endswith("empty")
                        else [
                            {
                                "id": "turn_new"
                                if outcome == "queued_idle_after_resume"
                                else "turn_child",
                                "status": status,
                                "items": [],
                            }
                        ],
                    }
                }
            }

        async def iter_events(self):
            while True:
                event, handled = await self.events.get()
                if event is None:
                    return
                yield event
                handled.set_result(None)

        async def emit(self, method: str, params: dict) -> None:
            handled = asyncio.get_running_loop().create_future()
            await self.events.put(({"method": method, "params": params}, handled))
            await asyncio.wait_for(handled, 5)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input_at", lambda *_args: None)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    pane = PaneRef(
        "conv_native_parent",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
    )
    other = PaneRef("conv_other_parent", pane.terminal_id, "codex", tmp_path / "other.sock")
    client = EventClient()
    task = asyncio.create_task(
        forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id=pane.conversation_id,
            bridge_dir=tmp_path / "bridge",
            app_server_url="ws://127.0.0.1:9876",
            thread_id="thread_parent",
            client=client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"child_session_id": "conv_native_child"})
            ),
        )
    )
    try:
        await client.emit(
            "thread/started",
            {
                "thread": {
                    "id": "thread_child",
                    "source": {
                        "subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}
                    },
                }
            },
        )
        reaper = app.state.native_pane_reaper
        assert await reaper._is_busy(pane)
        if outcome.startswith("terminal_before_registration"):
            await client.emit(
                "turn/completed",
                {
                    "threadId": "thread_child",
                    "turn": {"id": "turn_child", "status": "completed", "items": []},
                },
            )
            assert not await reaper._is_busy(pane)
        await client.emit(
            "item/started",
            {
                "threadId": "thread_parent",
                "item": {
                    "type": "collabAgentToolCall",
                    "tool": "spawnAgent",
                    "receiverThreadIds": ["thread_child"],
                    "agentsStates": {"thread_child": {"status": "running"}},
                }
                if outcome.endswith("stale_spawn_snapshot")
                else {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "thread_child",
                },
            },
        )
        if outcome.startswith(("completed_backfill", "terminal_before_registration")):
            assert not await reaper._is_busy(pane)
            return
        if outcome == "queued_idle_after_resume":
            assert client.queued_idle_handled is not None
            await asyncio.wait_for(client.queued_idle_handled, 5)
        elif outcome == "unknown_idle_only_empty":
            await client.emit(
                "thread/status/changed", {"threadId": "thread_child", "status": {"type": "idle"}}
            )
            assert not await reaper._is_busy(pane)
            return
        assert await reaper._is_busy(pane)
        assert not await reaper._is_busy(other)
        # No further child output is needed to hold past the full idle window.
        assert reaper._classify(0, [pane], busy_convs={pane.conversation_id}) == []
        assert reaper._classify(1_000_000, [pane], busy_convs={pane.conversation_id}) == []
        await client.emit("turn/started", {"threadId": "thread_child", "turn": {"id": "turn_new"}})
        await client.emit(
            "turn/completed",
            {
                "threadId": "thread_child",
                "turn": {"id": "turn_child", "status": "completed", "items": []},
            },
        )
        assert await reaper._is_busy(pane)
        if outcome != "disconnected" and not outcome.startswith("unknown"):
            # A failed child turn ends through its own notification method.
            await client.emit(
                "turn/failed" if outcome == "failed" else "turn/completed",
                {
                    "threadId": "thread_child",
                    "turn": {
                        "id": "turn_new",
                        "status": "completed"
                        if outcome == "queued_idle_after_resume"
                        else outcome,
                        "items": [],
                    },
                },
            )
        else:
            await client.events.put((None, None))
            await asyncio.wait_for(task, 5)
        assert not await reaper._is_busy(pane)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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
    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input_at", lambda *_args: None)
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


async def test_runner_busy_check_counts_a_viewer_only_on_recent_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An attached viewer spares the pane only while a human recently drove it.

    A CLI tab's keypress (tmux ``client_activity``) or a web-bridge event (the
    terminal's interaction stamp) inside the busy window reads busy; an idle
    attached viewer, or stale input, does not — a tab left open overnight must
    not keep the native stack resident.
    """
    tmux_input_at: dict[str, float | None] = {"value": None}
    monkeypatch.setattr(
        native_cost_popup, "_tmux_last_client_input_at", lambda *_args: tmux_input_at["value"]
    )
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    registry._by_conversation["conv_viewed"] = {("claude", "main"): instance}
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    pane = PaneRef(
        "conv_viewed", terminal_resource_id("claude", "main"), "claude", tmp_path / "tmux.sock"
    )

    # Attached but idle on both signals: not busy.
    assert not await reaper._is_busy(pane)
    # A CLI keypress inside the window spares the pane; a stale one does not.
    tmux_input_at["value"] = time.time() - 1.0
    assert await reaper._is_busy(pane)
    tmux_input_at["value"] = time.time() - PANE_OUTPUT_BUSY_WINDOW_S - 1.0
    assert not await reaper._is_busy(pane)
    # A web-bridge event on the pane's terminal spares it; a stale one does not.
    instance.note_client_interaction()
    assert await reaper._is_busy(pane)
    instance._last_client_interaction_at = time.monotonic() - PANE_OUTPUT_BUSY_WINDOW_S - 1.0
    assert not await reaper._is_busy(pane)
