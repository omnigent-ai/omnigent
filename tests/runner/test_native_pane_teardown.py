"""Native pane teardown: what a reap or a terminal DELETE releases, and when.

Reaping a native pane used to close only the pane (plus codex's app-server),
leaking every other harness's forwarder / bridge task, tool relay and
``opencode serve``; a user's terminal DELETE released nothing at all. These
pin the complete, lock-serialized teardown for every reapable built-in harness.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.harness_plugins import _BUILTIN_NATIVE_PROVIDERS
from omnigent.runner.app import _session_event_queues_ref
from omnigent.runner.native import orchestration
from omnigent.runner.session_status import StatusSource
from tests.runner.helpers import make_test_terminal_instance
from tests.terminals.native_pane_rig import (
    PaneRig,
    build_pane_rig,
    plant_sidecars,
    report_harness_state,
)

_REAPABLE_KEYS = [p.key for p in _BUILTIN_NATIVE_PROVIDERS if p.key != "kimi"]


def _deleted_events(conv_id: str) -> list[dict[str, Any]]:
    queue = _session_event_queues_ref.get(conv_id)
    events: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.resource.deleted":
            events.append(event)
    return events


async def _delete_terminal(
    rig: PaneRig, conv_id: str | None = None, name: str | None = None
) -> int:
    conv = conv_id or rig.conv_id
    terminal_id = terminal_resource_id(name or rig.agent.terminal_name, "main")
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.delete(f"/v1/sessions/{conv}/resources/terminals/{terminal_id}")
    tasks = set(rig.app.state.native_sidecar_release_tasks)
    if tasks:
        await asyncio.wait(tasks, timeout=10)
    return resp.status_code


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_reap_releases_every_sidecar_and_resets_the_sessions_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, key: str
) -> None:
    from omnigent.native import prompt_parks

    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", "5")
    clock = {"now": 100.0}
    monkeypatch.setattr(prompt_parks, "_clock", lambda: clock["now"])
    rig = await build_pane_rig(tmp_path, monkeypatch, key=key)
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    # A park past its ceiling no longer spares, but it must not survive the pane.
    prompt_parks.open_park(rig.conv_id, f"{key}:stale")
    clock["now"] += 60
    rig.book.record(rig.conv_id, "idle", source=StatusSource.RELAY)
    rig.resources._claim_status_edge(rig.conv_id, "idle", None)
    rig.drain()
    caplog.set_level("INFO", logger="omnigent.runner.app")
    try:
        assert await rig.reaper._reap(rig.pane) is True
        assert rig.closed == [rig.conv_id]
        assert sidecars.leftovers(rig.app) == []
        teardown = [
            r for r in caplog.records if r.__dict__.get("event_name") == "native_pane_teardown"
        ]
        assert len(teardown) == 1
        assert teardown[0].attributes["reason"] == "idle_reap"  # type: ignore[attr-defined]
        released = teardown[0].attributes["sidecars"].split(",")  # type: ignore[attr-defined]
        assert sorted(released) == sorted(sidecars.planted)
        assert prompt_parks.open_keys(rig.conv_id) == ()
        assert rig.book.current(rig.conv_id) is None
        # What the server heard is not changed by closing the pane.
        assert rig.resources._server_delivery_baseline[rig.conv_id] == ("idle", None)
        assert [e["resource_id"] for e in _deleted_events(rig.conv_id)] == [
            terminal_resource_id(rig.agent.terminal_name, "main")
        ]
    finally:
        sidecars.discard()
        prompt_parks.clear_session(rig.conv_id)
        rig.drain()


@pytest.mark.parametrize(
    "signal", ["runner_turn", "tool_call", "pending_approval", "queued_input", "client_attached"]
)
async def test_live_work_found_at_teardown_spares_the_pane_and_its_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    from omnigent.runner import pending_approvals

    rig = await build_pane_rig(tmp_path, monkeypatch, key="opencode")
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    try:
        if signal == "runner_turn":
            # A message bound its turn after the reaper selected the pane.
            rig.app.state.active_turns[rig.conv_id] = None
        elif signal == "tool_call":
            rig.app.state.mcp_execution_registry.retain_operation(rig.conv_id, "mcpop_1")
        elif signal == "pending_approval":
            monkeypatch.setitem(pending_approvals._session_pending, rig.conv_id, 1)
        elif signal == "queued_input":
            rig.app.state.session_message_buffers[rig.conv_id] = [{"content": "next"}]
        else:
            rig.tmux.clients = ["/dev/ttys002"]
        assert await rig.reaper._reap(rig.pane) is False
        assert rig.alive()
        assert rig.closed == []
        assert sidecars.intact(rig.app)
    finally:
        rig.app.state.active_turns.pop(rig.conv_id, None)
        rig.app.state.session_message_buffers.pop(rig.conv_id, None)
        sidecars.discard()


async def test_a_turn_bound_mid_scan_makes_the_reaper_rearm_instead_of_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    rig.reaper._last_busy_at[rig.conv_id] = 0.0

    async def _bind_turn_during_confirm(pane: Any) -> Any:
        from omnigent.terminals.pane_reaper import ConfirmVerdict

        rig.app.state.active_turns[rig.conv_id] = None
        return ConfirmVerdict(True)

    monkeypatch.setattr(rig.reaper, "_confirm_reap", _bind_turn_during_confirm)
    try:
        await rig.reaper._scan_once()
        assert rig.alive()
        assert rig.reaper._last_busy_at[rig.conv_id] > 0.0
    finally:
        rig.app.state.active_turns.pop(rig.conv_id, None)


async def test_an_ensure_during_a_reap_waits_on_the_lock_then_recreates_the_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    instance = rig.terminal_registry.get(rig.conv_id, "goose", "main")
    assert instance is not None
    release_close = asyncio.Event()
    close = instance.close

    async def _slow_close() -> None:
        await release_close.wait()
        await close()

    instance.close = _slow_close  # type: ignore[method-assign]
    launches: list[str] = []

    async def _launch_goose(ctx: Any) -> SessionResourceView:
        launches.append(ctx.session_id)
        fresh = make_test_terminal_instance("goose", "main", tmp_path / "fresh")
        rig.terminal_registry._by_conversation.setdefault(ctx.session_id, {})[
            ("goose", "main")
        ] = fresh
        return SessionResourceView(
            id=terminal_resource_id("goose", "main"),
            type="terminal",
            session_id=ctx.session_id,
            name="goose",
        )

    monkeypatch.setattr("omnigent.runner.native._launch_goose", _launch_goose)
    reap = asyncio.create_task(rig.reaper._reap(rig.pane))
    for _ in range(50):
        await asyncio.sleep(0)
        if rig.app.state.native_terminal_ensure_locks["goose"].get(rig.conv_id, None):
            break
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        ensure = asyncio.create_task(
            client.post(
                f"/v1/sessions/{rig.conv_id}/resources/terminals",
                json={"terminal": "goose", "session_key": "main", "ensure_native_terminal": True},
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
        # The ensure queues behind the teardown instead of racing it.
        assert launches == []
        assert not ensure.done()
        release_close.set()
        assert await reap is True
        resp = await ensure
    assert resp.status_code == 200, resp.text
    assert launches == [rig.conv_id]
    assert rig.alive()
    rig.drain()


@pytest.mark.parametrize("key", ["codex", "opencode", "goose", "claude", "devin"])
async def test_terminal_delete_releases_the_sidecars_of_an_idle_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key=key)
    report_harness_state(rig, monkeypatch, tmp_path, "idle")
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    rig.drain()
    try:
        assert await _delete_terminal(rig) == 200
        assert rig.closed == [rig.conv_id]
        assert sidecars.leftovers(rig.app) == []
        # The server publishes the REST delete's event; the runner adds none.
        assert _deleted_events(rig.conv_id) == []
    finally:
        sidecars.discard()


@pytest.mark.parametrize(
    "signal", ["runner_turn", "tool_call", "pending_approval", "running_claim", "probe_active"]
)
async def test_terminal_delete_keeps_sidecars_that_live_work_still_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    from omnigent.runner import pending_approvals

    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    report_harness_state(
        rig, monkeypatch, tmp_path, "active" if signal == "probe_active" else "idle"
    )
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    try:
        if signal == "runner_turn":
            rig.app.state.active_turns[rig.conv_id] = None
        elif signal == "tool_call":
            rig.app.state.mcp_execution_registry.retain_operation(rig.conv_id, "mcpop_1")
        elif signal == "pending_approval":
            monkeypatch.setitem(pending_approvals._session_pending, rig.conv_id, 1)
        elif signal == "running_claim":
            # codex keeps working after its TUI is gone; the relay says so.
            rig.book.record(rig.conv_id, "running", source=StatusSource.RELAY)
        assert await _delete_terminal(rig) == 200
        # The pane always closes on a user's DELETE...
        assert not rig.alive()
        # ...but the app-server and forwarder outlive it while work is live.
        assert sidecars.intact(rig.app)
    finally:
        rig.app.state.active_turns.pop(rig.conv_id, None)
        sidecars.discard()


async def test_deleting_a_generic_terminal_releases_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    bash = make_test_terminal_instance("bash", "main", tmp_path)

    async def _close() -> None:
        bash.running = False

    bash.close = _close  # type: ignore[method-assign]
    rig.terminal_registry._by_conversation[rig.conv_id][("bash", "main")] = bash
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    try:
        assert await _delete_terminal(rig, name="bash") == 200
        assert rig.alive()
        assert sidecars.intact(rig.app)
    finally:
        sidecars.discard()


async def test_a_reap_after_a_clear_rotation_releases_the_launching_sessions_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude")
    old_conv = rig.conv_id
    new_conv = f"{old_conv}_new"
    sidecars = plant_sidecars(rig.app, old_conv, tmp_path, harness_key=rig.agent.key)
    moved = await rig.resources.transfer_terminal(
        source_session_id=old_conv,
        target_session_id=new_conv,
        terminal_id=terminal_resource_id("claude", "main"),
    )
    assert moved is not None
    assert rig.resources.sidecar_home(new_conv) == old_conv
    rig.conv_id = new_conv
    try:
        assert await rig.reaper._reap(rig.pane) is True
        assert sidecars.leftovers(rig.app) == []
        assert rig.resources.sidecar_home(new_conv) == new_conv
    finally:
        sidecars.discard()
        rig.drain()
        _session_event_queues_ref.pop(new_conv, None)


async def test_a_reap_keeps_the_launching_sessions_sidecars_once_it_has_its_own_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude")
    old_conv = rig.conv_id
    new_conv = f"{old_conv}_new"
    await rig.resources.transfer_terminal(
        source_session_id=old_conv,
        target_session_id=new_conv,
        terminal_id=terminal_resource_id("claude", "main"),
    )
    # The old session was resumed with a fresh pane and fresh sidecars.
    resumed = make_test_terminal_instance("claude", "main", tmp_path / "resumed")
    rig.terminal_registry._by_conversation.setdefault(old_conv, {})[("claude", "main")] = resumed
    sidecars = plant_sidecars(rig.app, old_conv, tmp_path, harness_key=rig.agent.key)
    rig.conv_id = new_conv
    try:
        assert await rig.reaper._reap(rig.pane) is True
        assert sidecars.intact(rig.app)
    finally:
        sidecars.discard()
        rig.drain()
        _session_event_queues_ref.pop(new_conv, None)


async def _record_teardown_hook(session_id: str) -> None:
    _HOOK_CALLS.append(session_id)


_HOOK_CALLS: list[str] = []


async def test_the_providers_pane_teardown_hook_runs_after_the_shared_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    from omnigent.native import native_dispatch

    real = native_dispatch.native_provider_for_key("goose")
    assert real is not None
    hooked = dataclasses.replace(real, pane_teardown=f"{__name__}:_record_teardown_hook")
    monkeypatch.setattr(
        native_dispatch,
        "native_provider_for_key",
        lambda key: hooked if key == "goose" else None,
    )
    _HOOK_CALLS.clear()
    task: asyncio.Task[object] = asyncio.create_task(asyncio.sleep(3600))
    orchestration._register_auto_forwarder_task("conv_hook", task)
    try:
        released = await orchestration.teardown_native_pane_sidecars(
            "conv_hook", harness_key="goose"
        )
    finally:
        orchestration._AUTO_FORWARDER_TASKS.pop("conv_hook", None)
        task.cancel()
    assert released == ("forwarder", "provider_hook")
    assert _HOOK_CALLS == ["conv_hook"]
    assert task.cancelled() or task.done()


async def test_one_failing_sidecar_release_does_not_leak_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(_sid: str) -> None:
        raise RuntimeError("app-server close failed")

    monkeypatch.setattr(orchestration, "teardown_codex_native_app_server", _boom)

    class _Server:
        closed = False

        async def close(self) -> None:
            self.closed = True

    server = _Server()
    orchestration._AUTO_CODEX_APP_SERVERS["conv_boom"] = server  # type: ignore[assignment]
    orchestration._AUTO_OPENCODE_SERVERS["conv_boom"] = server  # type: ignore[assignment]
    task: asyncio.Task[object] = asyncio.create_task(asyncio.sleep(3600))
    orchestration._register_auto_forwarder_task("conv_boom", task)
    try:
        released = await orchestration.teardown_native_pane_sidecars("conv_boom")
    finally:
        orchestration._AUTO_CODEX_APP_SERVERS.pop("conv_boom", None)
        orchestration._AUTO_OPENCODE_SERVERS.pop("conv_boom", None)
        orchestration._AUTO_FORWARDER_TASKS.pop("conv_boom", None)
        task.cancel()
    assert released == ("opencode_server", "forwarder")
    assert server.closed
    assert "conv_boom" not in orchestration._AUTO_FORWARDER_TASKS


@pytest.mark.parametrize("claude_status", ["idle", "busy"])
async def test_terminal_delete_judges_claude_by_its_status_file_read_before_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_status: str
) -> None:
    import json

    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude")
    status_file = tmp_path / "claude-status.json"
    status_file.write_text(json.dumps({"status": claude_status}))
    # The poller, and with it the file path, goes away with the pane.
    monkeypatch.setattr(
        rig.resources, "status_poller_path", lambda _sid: status_file if rig.alive() else None
    )
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    try:
        assert await _delete_terminal(rig) == 200
        if claude_status == "idle":
            assert sidecars.leftovers(rig.app) == []
        else:
            assert sidecars.intact(rig.app)
    finally:
        sidecars.discard()


async def test_a_reap_never_waits_on_a_launch_that_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="kiro")
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    lock = rig.app.state.native_terminal_ensure_locks["kiro"].setdefault(
        rig.conv_id, asyncio.Lock()
    )
    try:
        async with lock:
            # Waiting would let the reap close the pane the launch creates.
            assert await asyncio.wait_for(rig.reaper._reap(rig.pane), timeout=1) is False
        assert rig.alive()
        assert sidecars.intact(rig.app)
        assert await rig.reaper._reap(rig.pane) is True
    finally:
        sidecars.discard()
        rig.drain()


async def test_a_turn_that_starts_while_the_pane_closes_keeps_its_own_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.app import _CommentRelayBinding
    from tests.terminals.native_pane_rig import _FakeRelay

    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude")
    instance = rig.terminal_registry.get(rig.conv_id, "claude", "main")
    assert instance is not None
    release_close = asyncio.Event()
    close = instance.close

    async def _slow_close() -> None:
        await release_close.wait()
        await close()

    instance.close = _slow_close  # type: ignore[method-assign]
    reap = asyncio.create_task(rig.reaper._reap(rig.pane))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not reap.done()
    # A message arrives mid-close. It needs no lock to bind its turn, publish
    # its running, and start its prompt waiter and tool relay; its ensure then
    # waits on the lock the teardown holds.
    rig.resources.note_session_turn_started(rig.conv_id)
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    waiter: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
    rig.app.state.claude_prompt_waiters[rig.conv_id] = waiter
    relay = _FakeRelay()
    rig.app.state.session_comment_relays[rig.conv_id] = _CommentRelayBinding(
        relay=relay,  # type: ignore[arg-type]
        spec_entry=None,
        bridge_dir=tmp_path,
    )
    try:
        release_close.set()
        assert await reap is True
        assert rig.book.claim(rig.conv_id) is not None
        assert rig.resources.session_turn_is_active(rig.conv_id)
        assert rig.app.state.claude_prompt_waiters.get(rig.conv_id) is waiter
        assert not waiter.done()
        assert rig.app.state.session_comment_relays[rig.conv_id].relay is relay
        assert not relay.closed
    finally:
        waiter.cancel()
        rig.app.state.claude_prompt_waiters.pop(rig.conv_id, None)
        rig.app.state.session_comment_relays.pop(rig.conv_id, None)
        rig.drain()


async def test_queued_input_holds_a_pane_for_one_idle_window_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.terminals.pane_reaper import SpareReason

    # The bound is one idle window; a tiny one keeps the test fast.
    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude", idle_timeout_s=0.05)
    queued = [{"content": "next"}]
    rig.app.state.session_message_buffers[rig.conv_id] = queued
    try:
        assessment = await rig.assess()
        assert SpareReason.QUEUED_INPUT in assessment.reasons
        assert assessment.facts["queued_input"] == 1
        assert await rig.reaper._reap(rig.pane) is False
        await asyncio.sleep(0.1)
        with caplog.at_level("WARNING", logger="omnigent.runner.app"):
            for _ in range(2):
                assessment = await rig.assess()
                assert SpareReason.QUEUED_INPUT not in assessment.reasons
        stranded = [r for r in caplog.records if "queued message" in r.getMessage()]
        assert len(stranded) == 1
        assert await rig.reaper._reap(rig.pane) is True
        # Stranded input is not dropped: the next turn drains it.
        assert rig.app.state.session_message_buffers[rig.conv_id] == queued
    finally:
        rig.app.state.session_message_buffers.pop(rig.conv_id, None)
        rig.drain()


@pytest.mark.parametrize("channel", ["pty", "runner"])
async def test_a_fresh_running_claim_from_a_local_channel_spares_a_silent_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    """A ``running`` the pane watcher or the runner just recorded keeps the pane.

    The pane has been silent for two hours and nothing else holds it; the
    claim alone decides. (A relay-only ``running`` does not; see
    tests/runner/test_runner_idle_active_work.py.)
    """
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi")
    try:
        if channel == "pty":
            await rig.fire("on_activity")  # the pane watcher's own running edge
        else:
            rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
        assessment = await rig.assess()
        assert assessment.busy
        assert assessment.facts["claim_source"] == channel
        rig.reaper._last_busy_at[rig.conv_id] = time.monotonic() - 10 * 3600
        await rig.reaper._scan_once()
        assert rig.alive()
    finally:
        rig.drain()


async def test_a_new_dispatch_restarts_a_claims_clock_and_it_is_warned_about_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim is timed from its last runner dispatch; a silent one warns once per episode."""
    clock = _BookClock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", status_clock=clock)

    def _contradictions() -> int:
        return sum(
            getattr(r, "event_name", None) == "native_pane_status_contradiction"
            for r in caplog.records
        )

    try:
        with caplog.at_level("WARNING", logger="omnigent.runner.app"):
            rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
            clock.now += 2 * 3600.0
            for _ in range(2):
                facts = (await rig.assess()).facts
                assert facts["claim_age_s"] == pytest.approx(2 * 3600.0)
            assert _contradictions() == 1
            # The next turn's dispatch re-asserts running: same episode, new clock.
            rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
            facts = (await rig.assess()).facts
            assert facts["claim_age_s"] == pytest.approx(0.0)
            assert facts["claim_silent_s"] == pytest.approx(0.0)
            # A new episode that goes silent is warned about again.
            rig.book.record(rig.conv_id, "idle", source=StatusSource.RUNNER)
            rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
            clock.now += 2 * 3600.0
            await rig.assess()
            assert _contradictions() == 2
    finally:
        rig.drain()


async def test_a_turn_dispatched_after_the_deep_check_spares_the_pane_at_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The teardown re-tests that no turn was sent since the pre-reap check.

    opencode and devin publish the runner's idle right after injecting a
    prompt, so by teardown the turn has left no live-work signal, only a
    moved dispatch stamp.
    """
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi")
    try:
        confirm = rig.reaper._confirm_reap
        assert confirm is not None
        assert (await confirm(rig.pane)).proceed
        rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
        rig.book.record(rig.conv_id, "idle", source=StatusSource.RUNNER)
        caplog.set_level("INFO", logger="omnigent.runner.app")
        assert await rig.reaper._reap(rig.pane) is False
        assert rig.alive()
        spared = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "native_pane_spared"
            and r.attributes.get("stage") == "teardown"  # type: ignore[attr-defined]
        ]
        assert [r.attributes["reasons"] for r in spared] == ["turn_dispatched"]  # type: ignore[attr-defined]
        # The fence belongs to that one decision; a reap decided afresh proceeds.
        assert (await confirm(rig.pane)).proceed
        assert await rig.reaper._reap(rig.pane) is True
        assert not rig.alive()
    finally:
        rig.drain()


class _BookClock:
    """A hand-driven monotonic clock for the status book."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now
