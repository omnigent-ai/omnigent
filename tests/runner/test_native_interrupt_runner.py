"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
no-handler fall-through contract (antigravity/opencode), and the 503 mapping.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import Response

from omnigent.harnesses.codex_native import app_server as codex_app_server
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runner.resource_registry import SessionResourceRegistry


@dataclass
class _FakeAck:
    """Stand-in for ``_SubagentDeliveryAck``."""

    delivered: bool = True
    entry: object | None = None
    reason: str = "delivered"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.scheduled: list[asyncio.TimerHandle] = []

    def call_later(
        self, delay: float, callback: Callable[..., None], *args: Any
    ) -> asyncio.TimerHandle:
        handle = asyncio.TimerHandle(self.now + delay, callback, args, asyncio.get_running_loop())
        self.scheduled.append(handle)
        return handle

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [handle for handle in self.scheduled if handle.when() <= self.now]
        self.scheduled = [handle for handle in self.scheduled if handle.when() > self.now]
        for handle in due:
            if not handle.cancelled():
                handle._run()


class _FakeTerminalRegistry:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def list_for_conversation(self, conv_id: str) -> list[Any]:
        return []


class _FakeResourceRegistry:
    def __init__(self) -> None:
        self.terminal_registry = _FakeTerminalRegistry()

    async def close_terminal(self, conv_id: str, terminal_id: str) -> bool:
        return True


def _make_runner(**overrides: Any) -> tuple[NativeInterruptRunner, dict[str, Any]]:
    """Build a runner with recording fakes; return it plus a capture dict."""
    captured: dict[str, Any] = {
        "published": [],
        "wakes": [],
        "wake_calls": [],
        "superseded": [],
        # The work_id the runner's ``subagent_work_id_for_session`` callback
        # reports; flip it to simulate a newer send replacing the dispatch.
        "current_work_id": None,
    }

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        captured["published"].append((conv_id, event))

    def _mark_and_wake(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
    ) -> _FakeAck:
        if only_if_work_id is not None and only_if_work_id != captured["current_work_id"]:
            # A newer dispatch replaced the entry: drop the delayed op untouched.
            captured["superseded"].append((child_session_id, status, only_if_work_id))
            return _FakeAck(delivered=False, reason="superseded_dispatch")
        captured["wakes"].append((child_session_id, status, output))
        captured["wake_calls"].append(
            {
                "child_session_id": child_session_id,
                "status": status,
                "output": output,
                "only_if_work_id": only_if_work_id,
            }
        )
        return _FakeAck()

    async def _codex_bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any | None:
        return None

    def _client_safe(exc: BaseException, *, context: str) -> str:
        return f"safe:{context}"

    def _work_id_for_session(conv_id: str) -> str | None:
        return captured["current_work_id"]

    kwargs: dict[str, Any] = {
        "server_client": SimpleNamespace(),
        "resource_registry": _FakeResourceRegistry(),
        "publish_event": _publish,
        "mark_subagent_terminal_and_wake": _mark_and_wake,
        "session_sub_agent_names": {},
        "codex_bridge_state_for_session": _codex_bridge_state,
        "client_safe_error_detail": _client_safe,
        "logger": logging.getLogger("test.interrupt"),
        "subagent_work_id_for_session": _work_id_for_session,
    }
    kwargs.update(overrides)
    return NativeInterruptRunner(**kwargs), captured


def test_native_cancel_capability_follows_stop_registry() -> None:
    """Parent cancel capability must track ``_UNIFORM_STOP`` plus Claude."""
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
    from omnigent.runner.native.interrupt import (
        _UNIFORM_STOP,
        native_cancel_capability,
    )

    for agent in NATIVE_CODING_AGENTS:
        capability = native_cancel_capability(agent.wrapper_label)
        if agent.key == "claude" or agent.key in _UNIFORM_STOP:
            assert capability == "stop", agent.key
        else:
            assert capability == "best_effort", agent.key
        if agent.subagent_wrapper_label:
            assert native_cancel_capability(agent.subagent_wrapper_label) == capability

    assert native_cancel_capability(None) == "inprocess"
    assert native_cancel_capability("not-a-native-wrapper") == "inprocess"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native", "claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    assert await runner.stop(harness, "conv_x") is None


@pytest.mark.asyncio
async def test_uniform_interrupt_defers_parent_wake_until_outcome_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt injects the Escape but must not report a terminal status yet.

    Injecting an Escape does not confirm the agent stopped, so no ``cancelled``
    may be delivered until the harness's terminal edge or the grace timer
    resolves the outcome.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    calls: list[Any] = []

    def _inject(bridge_dir: Any, *, timeout_s: float) -> None:
        calls.append((bridge_dir, timeout_s))

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", _inject)

    runner, captured = _make_runner()
    resp = await runner.interrupt("goose-native", "conv_g")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == [("dir/conv_g", 1.0)]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_g")[0] is True
    # Consumed: a second take finds nothing and the grace timer is disarmed.
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_interrupt_grace_timer_delivers_unconfirmed_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no terminal edge after the interrupt, the grace timer reports cancelled."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_g"  # a live dispatch to bind the cancel to
    resp = await runner.interrupt("goose-native", "conv_g")
    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == []

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_grace_timer_does_not_cancel_superseded_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old interrupt's grace timer must not cancel a newer dispatch.

    Interrupt turn A, then a new send registers turn B on the same child before
    A's timer fires. The timer is bound to A's ``work_id``, so when it fires the
    newer dispatch B is left untouched and can still complete — instead of B
    being cancelled and its result lost.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # defers, bound to work_A
    # A new send reuses the child session before the timer fires.
    captured["current_work_id"] = "work_B"

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "the newer dispatch must not be cancelled"
    assert captured["superseded"] == [("conv_g", "cancelled", "work_A")]


@pytest.mark.asyncio
async def test_resolve_pending_interrupt_only_consumes_matching_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending interrupt resolves only for its own dispatch, else is dropped.

    An idle for a NEWER dispatch (the interrupted one exited or was superseded)
    must not consume the stale pending — for a legacy harness that idle is the
    new dispatch's completion. ``resolve_pending_interrupt`` reports pending
    only on a work_id match; otherwise it drops the stale record and reports
    not-pending so the caller handles the idle normally.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # pending bound to work_A

    # An idle arrives for a NEWER dispatch: the pending is stale and must be
    # dropped, letting the idle be handled as work_B's own outcome.
    resolved, work_id = runner.resolve_pending_interrupt("conv_g", "work_B")
    assert (resolved, work_id) == (False, None)
    assert runner.take_pending_interrupt("conv_g")[0] is False  # stale record dropped

    # A matching dispatch's idle DOES resolve its interrupt.
    captured["current_work_id"] = "work_C"
    await runner.interrupt("goose-native", "conv_h")
    resolved, work_id = runner.resolve_pending_interrupt("conv_h", "work_C")
    assert (resolved, work_id) == (True, "work_C")


@pytest.mark.asyncio
async def test_repeated_interrupt_on_reused_child_gives_new_dispatch_its_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pending interrupt must not deny a new dispatch its cancellation.

    Interrupt A and leave its pending record (its launch was reaped before the
    timer fired). Reuse the child for B and interrupt it: B must get its OWN
    pending record and grace timer — the earlier record for A is replaced, not
    dedup-suppressed — so B still has a cancellation fallback and its parent is
    not left waiting indefinitely.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.05)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # A's pending (work_A) lingers
    # The child is reused for dispatch B and interrupted before A's timer fires.
    captured["current_work_id"] = "work_B"
    await runner.interrupt("goose-native", "conv_g")  # must replace A with work_B

    await asyncio.sleep(0.2)
    # B receives its OWN grace-period cancellation (bound to work_B, delivered),
    # rather than being suppressed by A's stale record.
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_grace_timer_skips_cancel_when_dispatch_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt with no bound dispatch must not cancel anything on timeout.

    If no work entry existed when the interrupt fired (e.g. a runner restart had
    not recovered it), the timer captures ``None`` and cannot bind. Delivering a
    cancel then could settle a newer send that reused the child, so the timer
    must skip entirely — the restart recovery scan owns unbound children.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = None  # no dispatch to bind to
    await runner.interrupt("goose-native", "conv_g")

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "an unbound interrupt must deliver no cancel"
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_resolved_interrupt_disarms_grace_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal edge that consumed the pending interrupt silences the timer."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    await runner.interrupt("goose-native", "conv_g")
    assert runner.take_pending_interrupt("conv_g")[0] is True

    await asyncio.sleep(0.1)
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_pi_interrupt_uses_enqueue_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's uniform interrupt uses enqueue_interrupt with no timeout kwarg."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[Any] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.interrupt("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == ["dir/conv_p"]


@pytest.mark.asyncio
async def test_uniform_interrupt_bridge_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError from the bridge inject maps to a 503 with the error code."""
    import json

    import omnigent.harnesses.qwen_native.bridge as qwen_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(qwen_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(qwen_bridge, "inject_interrupt", _boom)

    runner, captured = _make_runner()
    resp = await runner.interrupt("qwen-native", "conv_q")

    assert resp is not None and resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"] == "qwen_native_interrupt_failed"
    assert body["detail"] == "safe:qwen-native interrupt"
    # No parent wake on failure.
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_uniform_stop_kills_tears_down_and_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform stop kills the bridge, publishes idle, and wakes the parent."""
    import omnigent.harnesses.cursor_native.bridge as cursor_bridge

    killed: list[Any] = []
    monkeypatch.setattr(cursor_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        cursor_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: killed.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    # A stop that follows an unresolved interrupt settles it: the kill is
    # confirmed, so no grace-timer cancel may fire later.
    runner._pending_interrupts["conv_c"] = 0.0
    resp = await runner.stop("cursor-native", "conv_c")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert runner.take_pending_interrupt("conv_c")[0] is False
    assert killed == [("dir/conv_c", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_c", "cancelled", None)]


@pytest.mark.asyncio
async def test_uniform_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kill returns 503 and does NOT publish idle (no lie to the UI)."""
    import json

    import omnigent.harnesses.hermes_native.bridge as hermes_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(hermes_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(hermes_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("hermes-native", "conv_h")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "hermes_native_stop_failed"
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_codex_and_pi_stop_route_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/pi have no distinct stop — stop() routes to their interrupt handler."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[str] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: conv)
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.stop("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    # The interrupt path ran (enqueue_interrupt), not a kill_session.
    assert calls == ["conv_p"]


@pytest.mark.asyncio
async def test_codex_interrupt_noop_when_no_bridge_state() -> None:
    """codex interrupt returns 204 when there is no live bridge state."""
    runner, _ = _make_runner()  # default codex_bridge_state returns None
    resp = await runner.interrupt("codex-native", "conv_cx")
    assert isinstance(resp, Response) and resp.status_code == 204


def _write_codex_turn(bridge_dir: Path, turn_id: str | None) -> None:
    codex_bridge.write_bridge_state(
        bridge_dir,
        codex_bridge.CodexNativeBridgeState(
            session_id="conv_codex",
            socket_path="ws://127.0.0.1:9999",
            thread_id="thread-codex",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=turn_id,
        ),
    )


def _active_codex_turn(bridge_dir: Path) -> str | None:
    state = codex_bridge.read_bridge_state(bridge_dir)
    assert state is not None
    return state.active_turn_id


_CodexInterruptRig = tuple[
    NativeInterruptRunner, dict[str, Any], AsyncMock, Path, SessionResourceRegistry
]


@pytest.fixture
async def codex_interrupt_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_CodexInterruptRig]:
    bridge_dir = tmp_path / "bridge"
    _write_codex_turn(bridge_dir, "turn-ended")
    registry = SessionResourceRegistry()
    registry.note_external_session_status("conv_codex", "running")

    async def resolve_state(
        conv_id: str, *, action: str, missing_state_log_level: int = logging.WARNING
    ) -> codex_bridge.CodexNativeBridgeState | None:
        assert conv_id == "conv_codex"
        assert action == "interrupt"
        return codex_bridge.read_bridge_state(bridge_dir)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_codex/labels"
        return httpx.Response(200, json={"labels": {}}, request=request)

    # Replace external HTTP/RPC endpoints; bridge files and status handling stay real.
    codex_client = AsyncMock(spec=codex_app_server.CodexAppServerClient)
    monkeypatch.setattr(codex_bridge, "bridge_dir_for_bridge_id", lambda _: bridge_dir)
    monkeypatch.setattr(codex_app_server, "client_for_transport", lambda *a, **kw: codex_client)
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as server_client:
        runner, captured = _make_runner(
            server_client=server_client,
            resource_registry=registry,
            codex_bridge_state_for_session=resolve_state,
        )
        try:
            yield runner, captured, codex_client, bridge_dir, registry
        finally:
            runner.clear_pending_interrupt("conv_codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
@pytest.mark.parametrize("forwarder_cleared_turn", [False, True])
async def test_codex_ended_turn_control_succeeds_and_reconciles_idle(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
    forwarder_cleared_turn: bool,
) -> None:
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner
    assert registry.session_turn_is_active("conv_codex")

    async def reject(*args: Any, **kwargs: Any) -> None:
        if forwarder_cleared_turn:
            _write_codex_turn(bridge_dir, None)
        raise codex_app_server.CodexAppServerResponseError(
            {"code": -32600, "message": "no active turn to interrupt"}
        )

    codex_client.request.side_effect = reject

    response = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(response, Response) and response.status_code == 204
    assert _active_codex_turn(bridge_dir) is None
    assert not registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_codex")[0] is False
    repeated = await getattr(runner, action)("codex-native", "conv_codex")
    assert isinstance(repeated, Response) and repeated.status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
@pytest.mark.parametrize("outcome", ["completed", "failed"])
@pytest.mark.parametrize("newer_turn", [False, True])
async def test_codex_reconciliation_preserves_delayed_terminal_correlation(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
    outcome: str,
    newer_turn: bool,
) -> None:
    from omnigent.harnesses.codex_native import forwarder

    runner, _, codex_client, bridge_dir, registry = codex_interrupt_runner
    codex_client.request.side_effect = codex_app_server.CodexAppServerResponseError(
        {"code": -32600, "message": "no active turn to interrupt"}
    )
    response = await getattr(runner, action)("codex-native", "conv_codex")
    assert isinstance(response, Response) and response.status_code == 204
    if newer_turn:
        codex_bridge.update_active_turn_id(bridge_dir, "turn-newer")
        registry.note_external_session_status("conv_codex", "running")
    assert forwarder._terminal_turn_status_edge(bridge_dir, "turn/completed", {}) is None
    edge = forwarder._terminal_turn_status_edge(
        bridge_dir,
        "turn/completed",
        {"turn": {"id": "turn-ended", "status": outcome}},
    )
    if newer_turn:
        assert edge is None
        assert _active_codex_turn(bridge_dir) == "turn-newer"
        assert registry.session_turn_is_active("conv_codex")
    else:
        assert edge is not None and edge.turn_id == "turn-ended"
        assert edge.status == ("failed" if outcome == "failed" else "idle")
        state = codex_bridge.read_bridge_state(bridge_dir)
        assert state is not None and state.pending_terminal_turn_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
async def test_codex_ended_turn_response_preserves_newer_local_turn(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
) -> None:
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner

    async def reject(*args: Any, **kwargs: Any) -> None:
        _write_codex_turn(bridge_dir, "turn-newer")
        raise codex_app_server.CodexAppServerResponseError(
            {"code": -32600, "message": "no active turn to interrupt"}
        )

    codex_client.request.side_effect = reject
    response = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(response, Response) and response.status_code == 204
    assert _active_codex_turn(bridge_dir) == "turn-newer"
    assert registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_codex")[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
async def test_codex_superseded_remote_turn_does_not_publish_idle(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
) -> None:
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner
    codex_client.request.side_effect = codex_app_server.CodexAppServerResponseError(
        {"code": -32600, "message": "expected active turn id 'turn-ended' but found 'turn-newer'"}
    )
    response = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(response, Response) and response.status_code == 204
    assert _active_codex_turn(bridge_dir) is None
    assert registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_codex")[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
@pytest.mark.parametrize(
    "error",
    [
        codex_app_server.CodexAppServerResponseError(
            {"code": -32600, "message": "thread not found"}
        ),
        codex_app_server.CodexAppServerResponseError(
            {"code": -32603, "message": "no active turn to interrupt"}
        ),
        OSError("transport disconnected"),
    ],
    ids=["unrelated-rpc-error", "wrong-error-code", "transport-failure"],
)
async def test_codex_control_preserves_real_failures(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
    error: Exception,
) -> None:
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner
    codex_client.request.side_effect = error
    response = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(response, Response) and response.status_code == 503
    assert json.loads(bytes(response.body))["error"] == "codex_native_interrupt_failed"
    assert _active_codex_turn(bridge_dir) == "turn-ended"
    assert registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
async def test_codex_active_interrupt_waits_for_turn_outcome(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
) -> None:
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner
    codex_client.request.return_value = {"result": {}}
    response = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(response, Response) and response.status_code == 204
    assert _active_codex_turn(bridge_dir) == "turn-ended"
    assert registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []
    assert runner.take_pending_interrupt("conv_codex")[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
@pytest.mark.parametrize("newer_dispatch", [False, True])
async def test_codex_repeated_control_preserves_pending_interrupt_recovery(
    codex_interrupt_runner: _CodexInterruptRig,
    action: str,
    newer_dispatch: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.native import interrupt as interrupt_mod

    clock = _FakeClock()
    monkeypatch.setattr(
        interrupt_mod,
        "asyncio",
        SimpleNamespace(get_running_loop=lambda: clock, to_thread=asyncio.to_thread),
    )
    runner, captured, codex_client, bridge_dir, registry = codex_interrupt_runner
    captured["current_work_id"] = "work-ended"
    first = await runner.interrupt("codex-native", "conv_codex")
    assert isinstance(first, Response) and first.status_code == 204
    codex_client.request.side_effect = codex_app_server.CodexAppServerResponseError(
        {"code": -32600, "message": "no active turn to interrupt"}
    )

    repeated = await getattr(runner, action)("codex-native", "conv_codex")

    assert isinstance(repeated, Response) and repeated.status_code == 204
    assert _active_codex_turn(bridge_dir) is None
    assert not registry.session_turn_is_active("conv_codex")
    assert captured["published"] == []
    assert captured["wakes"] == []
    if newer_dispatch:
        captured["current_work_id"] = "work-newer"
    clock.advance(interrupt_mod._NATIVE_INTERRUPT_CANCEL_GRACE_S - 1)
    assert captured["wakes"] == []
    assert captured["superseded"] == []
    clock.advance(1)
    if newer_dispatch:
        assert captured["wakes"] == []
        assert captured["superseded"] == [("conv_codex", "cancelled", "work-ended")]
    else:
        assert captured["wakes"] == [("conv_codex", "cancelled", None)]
        assert captured["wake_calls"][0]["only_if_work_id"] == "work-ended"
    assert runner.take_pending_interrupt("conv_codex") == (False, None)


@pytest.mark.asyncio
async def test_claude_stop_is_idempotent_without_advertised_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-absent Claude pane still completes stop teardown."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _absent(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise claude_bridge.TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _absent)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == [("conv_cn", "cancelled", None)]


@pytest.mark.asyncio
async def test_claude_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Claude kill ``RuntimeError`` is 503; only ``TmuxSessionNotAdvertised`` is 204."""
    import json

    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise RuntimeError("tmux kill-session failed: connection refused")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "claude_native_stop_failed"
    assert captured["wakes"] == []
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_claude_interrupt_resolves_bridge_id_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude interrupt resolves the bridge id and injects, deferring the wake."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        return f"bid-{session_id}"

    injected: list[Any] = []
    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("claude-native", "conv_cl")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert injected == [("dir/bid-conv_cl", 1.0)]
    # No optimistic 'cancelled': the outcome is unknown until an edge lands.
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_cl")[0] is True
