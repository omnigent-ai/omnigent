"""Native-pane reaping across a pane's life: relaunch, lost signals, bounds.

Each test drives a real runner (the conformance driver: real routes, real
status book, real claude status poller, real relays) through a longer story
than the conformance matrix: a reap followed by a relaunch, a watcher or
poller that dies mid-turn, a dialog nobody answers, an interrupt followed by a
turn typed into the pane, a user's terminal DELETE.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native import prompt_parks
from omnigent.runner import app as runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.session_status import StatusSource
from omnigent.terminals.pane_reaper import PANE_OUTPUT_BUSY_WINDOW_S, native_pane_reap_rows
from tests.runner.helpers import make_test_terminal_instance
from tests.runner.test_native_pane_reap_conformance import (
    _IDLE_WINDOW_S,
    _INTERVAL_S,
    _PROVIDERS,
    _REAPABLE_KEYS,
    _Pane,
    _pane,
)
from tests.terminals.native_pane_rig import CLAUDE_PANE_PID

_FORWARDER_OWNED = [k for k in _REAPABLE_KEYS if _PROVIDERS[k].status_owner == "forwarder"]


@pytest.fixture
async def make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[..., Awaitable[_Pane]]]:
    made: list[_Pane] = []

    async def _make(key: str, *, printed: bool = True) -> _Pane:
        pane = await _pane(tmp_path, monkeypatch, key, printed=printed)
        made.append(pane)
        return pane

    yield _make
    for pane in made:
        pane.cleanup()


def _reaped_count(pane: _Pane) -> int:
    return pane.rig.closed.count(pane.conv)


async def _scan_windows(pane: _Pane, windows: float) -> float | None:
    """Scan every interval for *windows* idle windows; the clock at the first reap."""
    start = pane.clock.now
    closed_before = _reaped_count(pane)
    while pane.clock.now - start < windows * _IDLE_WINDOW_S:
        pane.clock.now += _INTERVAL_S
        await pane.scan()
        if _reaped_count(pane) > closed_before:
            return pane.clock.now
    return None


async def _relaunch(pane: _Pane) -> None:
    """Re-create the reaped pane the way the next turn's ensure does."""
    rig = pane.rig
    agent = rig.agent
    relaunch_dir = pane.tmp_path / f"relaunch-{_reaped_count(pane)}"
    relaunch_dir.mkdir(exist_ok=True)
    instance = make_test_terminal_instance(agent.terminal_name, "main", relaunch_dir)
    rig.callbacks.clear()

    def _capture(on_idle: Callable[[], None] | None = None, **kwargs: Any) -> None:
        for name, callback in (("on_idle", on_idle), *kwargs.items()):
            if callable(callback):
                rig.callbacks[name] = callback

    async def _close() -> None:
        rig.closed.append(rig.conv_id)
        instance.running = False

    instance.start_idle_watcher_thread = _capture  # type: ignore[method-assign]
    instance.close = _close  # type: ignore[method-assign]
    instance.pane_pid_sync = lambda: CLAUDE_PANE_PID  # type: ignore[method-assign]
    rig.terminal_registry._by_conversation.setdefault(rig.conv_id, {})[
        (agent.terminal_name, "main")
    ] = instance
    await rig.resources.observe_auxiliary_terminal(
        rig.conv_id, agent.terminal_name, "main", instance, resource_role=agent.harness
    )
    pane.tmux.printed()
    pane.sidecars = None
    pane.relay_file = None


def _assert_released(pane: _Pane) -> None:
    assert pane.sidecars is not None
    assert pane.sidecars.leftovers(pane.rig.app) == []
    assert pane.rig.book.current(pane.conv) is None
    assert pane.rig.resources.session_turn_is_active(pane.conv) is False
    assert prompt_parks.oldest_open_age_s(pane.conv) is None


def _drain_wire(pane: _Pane) -> list[str]:
    """The session.status values queued since the last drain."""
    queue = runner_app._session_event_queues_ref.get(pane.conv)
    wire: list[str] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            wire.append(event["status"])
    return wire


def _bound(windows: int = 1) -> float:
    return windows * (_IDLE_WINDOW_S + _INTERVAL_S) + PANE_OUTPUT_BUSY_WINDOW_S


# ── FM6: devin is listed from the registry with its real role ────────────


async def test_devin_is_listed_by_its_real_role(make: Callable[..., Awaitable[_Pane]]) -> None:
    rows = native_pane_reap_rows()
    assert rows.get("devin") == "devin-native"
    assert "kimi" not in rows
    pane = await make("devin")
    assert pane.rig.listed()


# ── FM10: reap, a late relayed running, relaunch, reap again ─────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_relaunched_pane_starts_clean_and_is_reaped_again(
    make: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    pane = await make(key)
    await pane.start_turn()
    await pane.work(2)
    await pane.end_turn(pane.end_turn_order())
    await pane.assert_reaped_after(pane.turn_ended_at)
    _assert_released(pane)
    # A forwarder POST that was in flight at the reap lands afterwards: the
    # server heard it, so it becomes what the wire dedups against.
    await pane.relay("running")
    stale = pane.rig.book.current(pane.conv)
    assert stale is not None and stale.sources == {StatusSource.RELAY}
    assert pane.rig.resources._server_delivery_baseline[pane.conv] == ("running", None)
    await _relaunch(pane)
    # The reaper's first sight of the relaunched pane: a full grace window.
    await pane.scan()
    await pane.start_turn()
    await pane.work(2)
    _drain_wire(pane)
    await pane.end_turn(pane.end_turn_order())
    if pane.local_channel is not None:
        # The server last heard running, so the relaunched pane's idle is sent.
        assert _drain_wire(pane) == ["idle"]
    assert pane.rig.book.claim(pane.conv, include_relay=True) is None
    reaped_at = await _scan_windows(pane, 2)
    assert reaped_at is not None, f"{key} relaunched pane never reaped"
    elapsed = reaped_at - pane.turn_ended_at
    assert _IDLE_WINDOW_S <= elapsed <= _bound(), elapsed
    _assert_released(pane)


# ── FM4: hermes re-posting running at its forwarder cadence ──────────────


async def test_hermes_perpetual_running_at_forwarder_cadence_cannot_pin(
    make: Callable[..., Awaitable[_Pane]],
) -> None:
    pane = await make("hermes")
    await pane.start_turn()
    await pane.work(2)
    # An aborted turn: the pane goes quiet while the forwarder keeps
    # re-posting ``running`` (every 0.4 s in production; 5 per scan here).
    await pane.end_turn("local")
    reaped_at = None
    while pane.clock.now - pane.turn_ended_at <= 3 * _IDLE_WINDOW_S:
        for _ in range(5):
            await pane.relay("running")
        pane.clock.now += _INTERVAL_S
        await pane.scan()
        if not pane.rig.alive():
            reaped_at = pane.clock.now
            break
    assert reaped_at is not None
    assert reaped_at - pane.turn_ended_at <= _bound()
    _assert_released(pane)


# ── FM5: an old server's waiting->running coercion with running children ─


@pytest.mark.parametrize("key", _FORWARDER_OWNED)
async def test_old_server_waiting_with_children_does_not_latch(
    make: Callable[..., Awaitable[_Pane]], monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(runner_app, "_server_version", None)  # predates ``waiting``
    pane = await make(key)
    await pane.start_turn(hold_runner_turn=True, agent_running=False)
    child = runner_app.register_subagent_work(
        parent_session_id=pane.conv,
        child_session_id=f"{pane.conv}_child",
        agent="worker",
        title="long job",
    )
    child.status = "running"
    await pane.runner_turn_done()
    queue = runner_app._session_event_queues_ref.get(pane.conv)
    wire: list[str] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            wire.append(event["status"])
    assert wire[-1] == "running", wire  # the wire keeps the old-server downgrade
    current = pane.rig.book.current(pane.conv)
    assert current is not None and current.status == "waiting"
    assert pane.rig.book.claim(pane.conv, include_relay=True) is None
    await pane.agent_running()
    await pane.relay("idle")
    pane.vendor("idle")
    await pane.assert_never_reaped(windows=2)  # the child holds it
    child.status = "completed"
    released = pane.clock.now
    await pane.assert_reaped_after(released)


# ── FM13: a dead watcher mid-turn ────────────────────────────────────────


async def test_dead_pty_watcher_mid_turn_warns_and_reaps_after_quiet(
    make: Callable[..., Awaitable[_Pane]], caplog: pytest.LogCaptureFixture
) -> None:
    pane = await make("goose")
    await pane.start_turn()
    await pane.work(2)
    instance = pane.rig.terminal_registry.get(pane.conv, "goose", "main")
    assert instance is not None
    instance.watcher_alive = lambda: False  # type: ignore[method-assign]
    pane.tmux.printed()
    last_output = pane.clock.now
    caplog.set_level(logging.WARNING, logger="omnigent.terminals.pane_reaper")
    await pane.assert_reaped_after(last_output)
    warned = [r for r in caplog.records if "status watcher has stopped" in r.getMessage()]
    assert len(warned) == 1


# ── stale blocked_on: a hard human-wait no probe can refute ─────────────


async def test_claude_dialog_answered_after_its_poller_stopped_is_reaped(
    make: Callable[..., Awaitable[_Pane]],
) -> None:
    pane = await make("claude")
    await pane.start_turn()
    await pane.work(2)
    pane.vendor("parked")
    await pane.rig.fire("on_tick")
    assert pane.rig.book.blocked(pane.conv) is not None
    # The watcher (which ticks the poller) stops; the user answers the dialog
    # in the pane and Claude finishes. The file says idle; the book never hears,
    # but the probe re-reads the file the dialog was recorded from.
    pane.vendor("idle")
    pane.tmux.printed()
    pane.turn_ended_at = pane.clock.now
    await pane.assert_reaped_after(pane.turn_ended_at, windows=2)


@pytest.mark.parametrize("key", _FORWARDER_OWNED)
async def test_a_relayed_dialog_whose_idle_is_lost_holds_only_until_its_bound(
    make: Callable[..., Awaitable[_Pane]], monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    # A relayed dialog has no local re-read (a forwarder may surface a prompt
    # the vendor state does not show), so it holds even when the vendor reads
    # idle: the gate was answered and the idle relay lost. Never past the bound.
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", str(2 * _IDLE_WINDOW_S))
    pane = await make(key)
    await pane.start_turn()
    await pane.relay("running", blocked_on="permission prompt")
    assert pane.rig.book.blocked(pane.conv) is not None
    pane.vendor("idle")
    pane.tmux.printed()
    blocked_at = pane.clock.now
    reaped_at = await _scan_windows(pane, 6)
    assert reaped_at is not None, f"{key} pane held past the dialog bound"
    assert 2 * _IDLE_WINDOW_S <= reaped_at - blocked_at <= 2 * _IDLE_WINDOW_S + _bound()


async def test_pty_local_idle_clears_a_relayed_blocked_on(
    make: Callable[..., Awaitable[_Pane]],
) -> None:
    # Control: a pane-status harness's own idle ends the blocked episode.
    pane = await make("goose")
    await pane.start_turn()
    await pane.relay("running", blocked_on="permission prompt")
    await pane.end_turn("local")
    assert pane.rig.book.blocked(pane.conv) is None
    await pane.assert_reaped_after(pane.turn_ended_at)


async def test_claude_dialog_hold_is_bounded_by_approval_max(
    make: Callable[..., Awaitable[_Pane]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", str(2 * _IDLE_WINDOW_S))
    pane = await make("claude")
    await pane.start_turn()
    pane.vendor("parked")
    await pane.rig.fire("on_tick")
    pane.tmux.printed()
    parked_at = pane.clock.now
    reaped_at = await _scan_windows(pane, 6)
    assert reaped_at is not None, "an unanswered claude dialog held the pane past the bound"
    assert reaped_at - parked_at <= 2 * _IDLE_WINDOW_S + _bound()


async def test_codex_parked_hold_is_bounded_by_approval_max(
    make: Callable[..., Awaitable[_Pane]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deep check's PARKED is bounded from when the dialog appeared.
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", str(2 * _IDLE_WINDOW_S))
    pane = await make("codex")
    await pane.start_turn()
    await pane.end_turn("relay")
    pane.vendor("parked")
    parked_at = pane.clock.now
    reaped_at = await _scan_windows(pane, 6)
    assert reaped_at is not None
    assert reaped_at - parked_at <= 2 * _IDLE_WINDOW_S + _bound()


# ── FM8: the real opencode supervisor releases ``opencode serve`` ────────


class _Serve:
    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


class _Forwarder:
    async def run(self) -> None:
        await asyncio.Event().wait()


async def test_real_opencode_supervisor_closes_serve_once_on_teardown() -> None:
    sid = "conv_review_opencode"
    serve = _Serve()
    orchestration._AUTO_OPENCODE_SERVERS[sid] = serve  # type: ignore[assignment]
    task = asyncio.create_task(
        orchestration._supervise_opencode_forwarder(sid, serve, _Forwarder())  # type: ignore[arg-type]
    )
    orchestration._register_auto_forwarder_task(sid, task)
    await asyncio.sleep(0)
    try:
        released = await orchestration.teardown_native_pane_sidecars(sid, harness_key="opencode")
        assert set(released) == {"opencode_server", "forwarder"}
        assert serve.closes == 1
        assert task.done()
        assert not orchestration.has_native_pane_sidecars(sid)
    finally:
        task.cancel()
        orchestration._AUTO_OPENCODE_SERVERS.pop(sid, None)
        orchestration._AUTO_FORWARDER_TASKS.pop(sid, None)


# ── FM9: a user's terminal DELETE, for every reapable harness ────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_terminal_delete_releases_or_sweeps_every_sidecar(
    make: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    pane = await make(key)
    await pane.start_turn()
    await pane.work(2)
    await pane.end_turn(pane.end_turn_order())
    terminal_id = terminal_resource_id(pane.rig.agent.terminal_name, "main")
    transport = httpx.ASGITransport(app=pane.rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.delete(f"/v1/sessions/{pane.conv}/resources/terminals/{terminal_id}")
    assert resp.status_code == 200, resp.text
    tasks = set(pane.rig.app.state.native_sidecar_release_tasks)
    if tasks:
        await asyncio.wait(tasks, timeout=10)
    assert not pane.rig.alive()
    assert pane.sidecars is not None
    start = pane.clock.now
    while pane.sidecars.leftovers(pane.rig.app) and pane.clock.now - start <= 2 * _IDLE_WINDOW_S:
        pane.clock.now += _INTERVAL_S
        await pane.scan()
    assert pane.sidecars.leftovers(pane.rig.app) == [], f"{key} sidecars leaked after DELETE"
    assert pane.clock.now - start <= _IDLE_WINDOW_S + 2 * _INTERVAL_S


# ── C2: a stale relayed idle during a new turn ───────────────────────────


@pytest.mark.parametrize("key", [k for k in _REAPABLE_KEYS if _PROVIDERS[k].pane_turn_probe])
async def test_a_late_idle_from_the_previous_turn_does_not_reap_a_working_agent(
    make: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    pane = await make(key)
    await pane.start_turn()
    await pane.work(2)
    await pane.end_turn(pane.end_turn_order())
    await pane.start_turn()  # the next turn, dispatched through the runner
    pane.vendor("active")  # a long silent tool call
    await pane.relay("idle")  # the previous turn's idle, delivered late
    await pane.assert_never_reaped()
    await pane.end_turn(pane.end_turn_order())
    await pane.assert_reaped_after(pane.turn_ended_at)


# ── C2: devin's inferred ACTIVE after an interrupt ───────────────────────


@pytest.mark.parametrize("relayed", [True, False], ids=["relayed", "relay_lost"])
async def test_devin_turn_typed_in_the_pane_after_an_interrupt_is_not_reaped(
    make: Callable[..., Awaitable[_Pane]], relayed: bool
) -> None:
    from omnigent.harnesses.devin_native import bridge
    from omnigent.runner.native.interrupt import _UNIFORM_INTERRUPT

    pane = await make("devin")
    uniform = _UNIFORM_INTERRUPT["devin"]
    module = importlib.import_module(uniform.module)
    pane.monkeypatch.setattr(module, uniform.inject_fn, lambda *_a, **_k: None)
    await pane.start_turn()
    pane.vendor("active")
    await pane.work(1)  # the interrupt comes a scan after the dispatch
    resp = await pane.event({"type": "interrupt"})
    assert resp.status_code == 204, resp.text
    assert pane.rig.book.last_control_idle_at(pane.conv) is not None
    bridge_dir = bridge.prepare_bridge_dir(pane.conv)
    bridge.record_hook_event(bridge_dir, {"hook_event_name": "Stop"})
    await pane.relay("idle")
    # The user types a new prompt into the pane; Devin starts working, then
    # goes silent. The hook log dates the prompt after the interrupt, even
    # when the forwarder's relayed running is lost.
    bridge.record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    if relayed:
        await pane.relay("running")
    pane.tmux.printed()
    await pane.assert_never_reaped()


async def test_devin_relaunched_after_an_interrupt_with_a_missed_stop_is_reaped(
    make: Callable[..., Awaitable[_Pane]],
) -> None:
    from omnigent.runner.native.interrupt import _UNIFORM_INTERRUPT

    pane = await make("devin")
    uniform = _UNIFORM_INTERRUPT["devin"]
    module = importlib.import_module(uniform.module)
    pane.monkeypatch.setattr(module, uniform.inject_fn, lambda *_a, **_k: None)
    await pane.start_turn()
    pane.vendor("active")  # UserPromptSubmit; Devin never writes the Stop
    await pane.work(1)
    resp = await pane.event({"type": "interrupt"})
    assert resp.status_code == 204, resp.text
    await pane.relay("idle")
    pane.tmux.printed()
    # The CONTROL idle discounts the stale inferred ACTIVE: reaped on time.
    await pane.assert_reaped_after(pane.clock.now)
    # The reset keeps the interrupt stamp: the hook log still shows the
    # interrupted prompt as open.
    await _relaunch(pane)  # the user opens the terminal tab; no turn runs
    relaunched_at = pane.clock.now
    reaped_at = await _scan_windows(pane, 3)
    assert reaped_at is not None, "relaunched devin pane held by a pre-reap hook log"
    assert reaped_at - relaunched_at <= _bound()
