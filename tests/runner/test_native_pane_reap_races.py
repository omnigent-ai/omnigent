"""Races and timing: the reaper never closes a pane that is still needed.

Each test drives the real runner app (``create_runner_app``) through the
conformance driver: one fake monotonic clock for the reaper, the status book
and tmux; fakes only at tmux, the harness subprocess, vendor state and the
server. The work, prompt, client or message arrives while a reap is under way
(during the deep check's awaits, or under the teardown's lock), or the clocks
misbehave.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.native import native_cost_popup, prompt_parks
from omnigent.runner import app as runner_app
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.session_status import StatusSource
from tests.runner.test_native_pane_reap_conformance import (
    _AGENT_ID,
    _IDLE_WINDOW_S,
    _INTERVAL_S,
    _PROVIDERS,
    _REAPABLE_KEYS,
    _Pane,
    _pane,
    _Response,
    _stub_native_controls,
)

# Harnesses whose runner turn types the prompt into the TUI (tmux send-keys),
# so the pane echoes before the runner turn ends. codex, antigravity and
# opencode inject through their own server API instead.
_API_INJECTED = frozenset({"codex", "antigravity", "opencode"})


@pytest.fixture
def pane_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Awaitable[_Pane]]]:
    made: list[_Pane] = []

    async def _make(key: str, *, printed: bool = True) -> _Pane:
        pane = await _pane(tmp_path, monkeypatch, key, printed=printed)
        made.append(pane)
        return pane

    yield _make
    for pane in made:
        pane.cleanup()


async def _idle_after_a_turn(pane: _Pane) -> None:
    """A finished turn whose idle reached the runner through its own channel."""
    await pane.start_turn()
    await pane.work()
    await pane.end_turn(pane.end_turn_order())
    assert pane.rig.book.claim(pane.conv, include_relay=True) is None


async def _scan_until(pane: _Pane, done: Callable[[], bool], *, windows: float = 2.0) -> None:
    start = pane.clock.now
    while not done() and pane.clock.now - start < windows * _IDLE_WINDOW_S:
        pane.clock.now += _INTERVAL_S
        await pane.scan()
    assert done(), "the reaper never reached the step under test"


def _message() -> dict[str, Any]:
    return {
        "type": "message",
        "agent_id": _AGENT_ID,
        "content": [{"type": "input_text", "text": "next task"}],
    }


# ── 1. a 70-minute silent tool call ──────────────────────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_70_minute_silent_relayed_tool_call_is_never_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)  # worst case: every channel says idle
    server = pane.rig.server
    base_post = server.post
    release = asyncio.Event()

    async def _post(url: str, **kwargs: Any) -> Any:
        if not url.endswith("/mcp"):
            return await base_post(url, **kwargs)
        await release.wait()  # the tool runs for 70 minutes
        return _Response(
            {
                "jsonrpc": "2.0",
                "id": kwargs["json"]["id"],
                "result": {"content": [{"type": "text", "text": "built"}]},
            }
        )

    monkeypatch.setattr(server, "post", _post)
    manager = ProxyMcpManager(
        pane.conv,
        server,  # type: ignore[arg-type]
        publish_event=lambda *_a: None,
        execution_registry=pane.rig.app.state.mcp_execution_registry,
    )
    call = asyncio.create_task(manager.call_tool(None, "build__run", {}))
    await asyncio.sleep(0)
    for _ in range(70):
        pane.clock.now += _INTERVAL_S
        await pane.scan()
        assert pane.rig.alive(), f"{key} pane reaped during a live tool call"
    release.set()
    assert await asyncio.wait_for(call, timeout=5) == "built"
    await pane.assert_reaped_after(pane.clock.now, full_window=False)


# ── 2. a sub-agent waiting on its own children ───────────────────────────


@pytest.mark.parametrize("child_status", ["launching", "running", "waiting"])
@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_subagent_pane_waiting_on_its_own_children_is_never_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]], key: str, child_status: str
) -> None:
    pane = await pane_factory(key)
    parent = f"{pane.conv}_parent"
    grandchild = f"{pane.conv}_grandchild"
    try:
        await _idle_after_a_turn(pane)
        # This pane is itself a sub-agent (its parent waits on it), and its
        # turn dispatched a child of its own that is still working.
        own = runner_app.register_subagent_work(
            parent_session_id=parent, child_session_id=pane.conv, agent="lead", title="t"
        )
        own.status = "waiting"
        work = runner_app.register_subagent_work(
            parent_session_id=pane.conv, child_session_id=grandchild, agent="w", title="t"
        )
        work.status = child_status
        await pane.assert_never_reaped()
    finally:
        runner_app._subagent_work_by_parent.pop(parent, None)
        runner_app._subagent_work_by_child.pop(pane.conv, None)
        runner_app._subagent_work_by_child.pop(grandchild, None)


# ── 3. a human attaches while the reap is under way ──────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_client_attaching_during_the_deep_check_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    server = pane.rig.server
    base_get = server.get
    fired: list[float] = []

    async def _get(url: str, **kwargs: Any) -> Any:
        if url == f"/v1/sessions/{pane.conv}" and not fired:
            fired.append(pane.clock.now)
            pane.tmux.clients = ["/dev/ttys042"]  # the user opens the terminal
        return await base_get(url, **kwargs)

    monkeypatch.setattr(server, "get", _get)
    await _scan_until(pane, lambda: bool(fired))
    assert pane.rig.alive(), f"{key} pane reaped with a client attached"


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_client_attaching_after_the_final_recheck_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    probes: list[str] = []

    # The reaper's assessment bound the tmux helpers at app build; only the
    # teardown's own re-probe imports the module attribute, so this client is
    # seen by the teardown alone: it attached after the final re-check.
    def _clients(*_args: Any) -> list[str]:
        probes.append("teardown")
        return ["/dev/ttys043"]

    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", _clients)
    await _scan_until(pane, lambda: bool(probes))
    assert pane.rig.alive(), f"{key} pane reaped with a client attached at teardown"


# ── 4. a prompt appears while the reap is under way ──────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_prompt_opening_during_the_deep_check_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    server = pane.rig.server
    base_get = server.get
    fired: list[float] = []

    async def _get(url: str, **kwargs: Any) -> Any:
        if url == f"/v1/sessions/{pane.conv}" and not fired:
            fired.append(pane.clock.now)
            prompt_parks.open_park(pane.conv, f"{key}:late_prompt")
        return await base_get(url, **kwargs)

    monkeypatch.setattr(server, "get", _get)
    await _scan_until(pane, lambda: bool(fired))
    assert pane.rig.alive(), f"{key} pane reaped while a prompt opened"


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_prompt_opening_during_the_teardown_probe_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    fired: list[str] = []

    def _clients(*_args: Any) -> list[str]:
        # A worker thread, like the real mirrors; parks are thread-safe.
        fired.append(threading.current_thread().name)
        prompt_parks.open_park(pane.conv, f"{key}:teardown_prompt")
        return []

    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", _clients)
    await _scan_until(pane, lambda: bool(fired))
    assert pane.rig.alive(), f"{key} pane reaped after a prompt opened under the lock"


# ── 5. a turn starts while the reap is under way ─────────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_message_arriving_during_the_teardown_probe_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    loop = asyncio.get_running_loop()
    fired: list[int] = []

    def _clients(*_args: Any) -> list[str]:
        # The user's next message lands while the teardown holds the lock.
        resp = asyncio.run_coroutine_threadsafe(pane.event(_message()), loop).result(10)
        fired.append(resp.status_code)
        return []

    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", _clients)
    await _scan_until(pane, lambda: bool(fired))
    assert fired == [202]
    assert pane.rig.alive(), f"{key} pane closed under a message that just arrived"
    await pane.runner_turn_done()


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_turn_dispatched_during_the_deep_check_is_not_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    server = pane.rig.server
    base_get = server.get
    fired: list[float] = []

    async def _get(url: str, **kwargs: Any) -> Any:
        if url == f"/v1/sessions/{pane.conv}" and not fired:
            fired.append(pane.clock.now)
            resp = await pane.event(_message())
            assert resp.status_code == 202, resp.text
            if key not in _API_INJECTED:
                pane.tmux.printed()  # the TUI echoes the typed prompt
            await pane.runner_turn_done()
            # The agent has not reported running yet, and its vendor state
            # (forwarder-maintained bridge state, hook log) still reads idle:
            # opencode's runner has even published idle already. The dispatch
            # itself is the evidence.
        return await base_get(url, **kwargs)

    monkeypatch.setattr(server, "get", _get)
    await _scan_until(pane, lambda: bool(fired))
    assert pane.rig.alive(), f"{key} pane reaped as its next turn was dispatched"


async def test_a_refutation_never_ends_a_turn_dispatched_during_the_probe(
    pane_factory: Callable[..., Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex: the deep check refutes the stale claim it read before its probe.

    The turn's idle was lost, so a stale RUNNER ``running`` holds the pane. The
    reaper reads the claim, then asks ``thread/read``; while the app-server
    answers ``idle``, the user's next message is dispatched. Recording the
    refutation afterwards must not end the new turn's ``running``.
    """
    from omnigent.harnesses.codex_native import app_server

    pane = await pane_factory("codex")
    await pane.start_turn()
    await pane.work()
    await pane.end_turn(None)  # relayed idle lost
    pane.vendor("idle")
    assert pane.rig.book.claim(pane.conv) is not None
    fired: list[float] = []

    class _Codex:
        async def connect(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del method, params
            if not fired:
                fired.append(pane.clock.now)
                resp = await pane.event(_message())
                assert resp.status_code == 202, resp.text
                await pane.runner_turn_done()
            return {"result": {"thread": {"status": {"type": "idle"}}}}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: _Codex())
    await _scan_until(pane, lambda: bool(fired))
    current = pane.rig.book.current(pane.conv)
    assert pane.rig.alive(), f"codex pane reaped as its next turn was dispatched (book: {current})"
    # The new turn's running survives the refutation of the old one.
    assert current is not None and current.status == "running", current


# ── 6. devin: a prompt typed in the TUI after an interrupt ───────────────


async def _devin_interrupted(pane: _Pane) -> Path:
    from omnigent.harnesses.devin_native import bridge

    _stub_native_controls(pane)
    await pane.start_turn()  # web message: runner dispatch, relayed running
    pane.vendor("active")  # hooks.jsonl: UserPromptSubmit
    await pane.work(2)
    resp = await pane.event({"type": "interrupt"})
    assert resp.status_code == 204, resp.text
    assert pane.rig.book.last_control_idle_at(pane.conv) is not None
    bridge_dir = bridge.bridge_dir_for_session_id(pane.conv)
    bridge.record_hook_event(bridge_dir, {"hook_event_name": "Stop"})
    await pane.relay("idle")
    pane.clock.now += 300
    return bridge_dir


async def test_devin_prompt_typed_in_its_tui_after_an_interrupt_is_never_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
) -> None:
    from omnigent.harnesses.devin_native import bridge

    pane = await pane_factory("devin")
    bridge_dir = await _devin_interrupted(pane)
    # The user attaches, types the next prompt into Devin's own TUI, detaches.
    pane.tmux.printed()
    bridge.record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    await pane.relay("running")  # devin's forwarder relays its hook edge
    claim = pane.rig.book.claim(pane.conv, include_relay=True)
    assert claim is not None and claim.origin is StatusSource.RELAY
    # Devin works on a long, silent task: the hook log shows an open prompt
    # that is newer than the interrupt, and the relay says running.
    await pane.assert_never_reaped()


async def test_control_devin_web_message_after_an_interrupt_is_never_reaped(
    pane_factory: Callable[..., Awaitable[_Pane]],
) -> None:
    from omnigent.harnesses.devin_native import bridge

    pane = await pane_factory("devin")
    bridge_dir = await _devin_interrupted(pane)
    await pane.start_turn()  # the follow-up comes through the runner
    bridge.record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    await pane.assert_never_reaped()


# ── 7. clocks ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_stale_wall_clock_reading_never_reaps_a_printing_pane(
    pane_factory: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    """tmux's ``window_activity`` is wall-clock; one reading off by hours
    (an NTP step between the stamp and the read) must not reap a pane that
    printed within the window."""
    pane = await pane_factory(key)
    await pane.start_turn()
    for i in range(int(2 * _IDLE_WINDOW_S / _INTERVAL_S)):
        pane.clock.now += _INTERVAL_S
        if i % 7 == 3:
            pane.tmux._printed_at = pane.clock.now - 4 * _IDLE_WINDOW_S
        else:
            pane.tmux.printed()
        await pane.scan()
        assert pane.rig.alive(), f"{key} printing pane reaped at scan {i}"


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_the_idle_clock_is_measured_from_the_last_output_not_the_first(
    pane_factory: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    """Output that stops just after a scan is never reaped before one full
    window after it, even when that scan was the last one that saw it."""
    pane = await pane_factory(key)
    await _idle_after_a_turn(pane)
    pane.clock.now += 5 * _INTERVAL_S + 59.0
    pane.tmux.printed()  # a stray line one second before the next scan
    last_output = pane.clock.now
    reaped_at = await pane.scan_until_reaped(windows=2)
    assert reaped_at is not None
    assert reaped_at - last_output >= _IDLE_WINDOW_S


def test_the_races_cover_every_reapable_builtin() -> None:
    assert "devin" in _REAPABLE_KEYS
    assert all(_PROVIDERS[k].pane_reap == "reap" for k in _REAPABLE_KEYS)
