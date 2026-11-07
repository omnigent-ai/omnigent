"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
opencode interrupt (native ``/interrupt``) dispatch, the antigravity
connect-RPC-cancel/TUI-escape interrupt and tmux-kill stop, the no-handler
fall-through contract for harnesses that genuinely have none, and the 503
mapping.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.responses import Response

from omnigent.harnesses.opencode_native import app_server as opencode_app_server
from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from omnigent.harnesses.opencode_native.client import OpenCodeClientError
from omnigent.runner.native.interrupt import NativeInterruptRunner


@dataclass
class _FakeAck:
    """Stand-in for ``_SubagentDeliveryAck``."""

    delivered: bool = True
    entry: object | None = None
    reason: str = "delivered"


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
        confirmed_cancellation: bool = False,
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
                "confirmed_cancellation": confirmed_cancellation,
            }
        )
        return _FakeAck()

    async def _codex_bridge_state(
        conv_id: str, *, action: str, **_kw: Any
    ) -> tuple[Any | None, Path]:
        return None, Path("unused-bridge-dir")

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
        if agent.key in ("claude", "antigravity") or agent.key in _UNIFORM_STOP:
            assert capability == "stop", agent.key
        else:
            assert capability == "best_effort", agent.key
        if agent.subagent_wrapper_label:
            assert native_cancel_capability(agent.subagent_wrapper_label) == capability

    assert native_cancel_capability(None) == "inprocess"
    assert native_cancel_capability("not-a-native-wrapper") == "inprocess"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    assert await runner.stop(harness, "conv_x") is None


_OPENCODE_STATE = SimpleNamespace(
    server_base_url="http://127.0.0.1:9999/",
    opencode_session_id="ses_oc",
    auth_secret="secret",
    workspace="/ws",
)


class _FakeOpenCodeClient:
    """Records the native interrupt, raising *error* when one is given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.interrupt_calls: list[str] = []
        self.closed = False

    async def interrupt(self, session_id: str) -> bool:
        self.interrupt_calls.append(session_id)
        if self.error is not None:
            raise self.error
        return True

    async def aclose(self) -> None:
        self.closed = True


def _patch_opencode_bridge(
    monkeypatch: pytest.MonkeyPatch, *, state: Any, client: _FakeOpenCodeClient
) -> dict[str, Any]:
    """Serve *state* as the bridge state and *client* as its client; return the routing kwargs."""
    routed: dict[str, Any] = {}

    def _client_for_state(**kwargs: Any) -> _FakeOpenCodeClient:
        routed.update(kwargs)
        return client

    monkeypatch.setattr(opencode_bridge, "read_bridge_state", lambda bridge_dir: state)
    monkeypatch.setattr(opencode_app_server, "client_for_state", _client_for_state)
    return routed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["interrupt", "stop"])
async def test_opencode_interrupt_stops_native_turn(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Interrupt and stop end the turn on OpenCode's own server and record it for the dispatch.

    The pending record lets a bare idle or the grace timer settle the dispatch as
    cancelled; the handler itself wakes nothing.
    """
    client = _FakeOpenCodeClient()
    routed = _patch_opencode_bridge(monkeypatch, state=_OPENCODE_STATE, client=client)
    runner, captured = _make_runner()
    captured["current_work_id"] = "work_oc"

    resp = await getattr(runner, method)("opencode-native", "conv_oc")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert client.interrupt_calls == ["ses_oc"] and client.closed
    assert routed == {
        "base_url": "http://127.0.0.1:9999/",
        "auth_secret": "secret",
        "directory": "/ws",
    }
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_oc") == (True, "work_oc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "error", "expected_status"),
    [
        pytest.param(None, None, None, id="no-bridge-state"),
        pytest.param(_OPENCODE_STATE, OpenCodeClientError("boom"), 503, id="client-error"),
        pytest.param(_OPENCODE_STATE, httpx.ReadTimeout("slow"), 503, id="transport-error"),
    ],
)
async def test_opencode_interrupt_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    state: Any,
    error: Exception | None,
    expected_status: int | None,
) -> None:
    """Missing bridge state falls through; a failed interrupt is a 503."""
    client = _FakeOpenCodeClient(error)
    _patch_opencode_bridge(monkeypatch, state=state, client=client)
    runner, captured = _make_runner()

    resp = await runner.interrupt("opencode-native", "conv_oc")

    if expected_status is None:
        assert resp is None
    else:
        assert resp is not None and resp.status_code == expected_status
        assert json.loads(bytes(resp.body))["error"] == "opencode_native_interrupt_failed"
    assert client.closed is (state is not None)
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_oc")[0] is False


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
    assert captured["wake_calls"][0]["confirmed_cancellation"] is True


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
    assert captured["wake_calls"][0]["confirmed_cancellation"] is True


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


def _patch_antigravity_bridge_id(monkeypatch: pytest.MonkeyPatch, interrupt_mod: Any) -> None:
    """Resolve antigravity's bridge id to a fixed, easily asserted value."""

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bid-agy"

    monkeypatch.setattr(
        interrupt_mod, "_antigravity_native_bridge_id_for_session", _fake_bridge_id
    )


def _agy_waiting_permission_step(
    *, trajectory_id: str = "traj-1", step_index: int = 3
) -> dict[str, Any]:
    """A trajectory step snapshot entry WAITING on an agy permission prompt."""
    return {
        "status": "CORTEX_STEP_STATUS_WAITING",
        "requestedInteraction": {
            "permission": {
                "resource": {"action": "run_command", "target": "rm -rf /tmp/x"},
                "actionDescription": "Run `rm -rf /tmp/x`",
            }
        },
        "metadata": {
            "sourceTrajectoryStepInfo": {"trajectoryId": trajectory_id, "stepIndex": step_index}
        },
    }


@pytest.mark.asyncio
async def test_antigravity_interrupt_running_cascade_uses_rpc_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RUNNING cascade (no WAITING step) is cancelled over connect-RPC; no Escape."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="cascade-1", active_turn_id=None
        ),
    )

    tui_calls: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "send_interaction_keys_via_tui",
        lambda bridge_dir, *keys: tui_calls.append((bridge_dir, keys)),
    )

    rpc_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: 52548)
    monkeypatch.setattr(agy_rpc, "get_trajectory_steps", lambda port, cascade_id: [])
    monkeypatch.setattr(
        agy_rpc,
        "cancel_cascade_steps",
        lambda port, cascade_id: (rpc_calls.append((port, cascade_id)), True)[1],
    )

    def _fail_deliver(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not deliver an interaction answer; no step is WAITING")

    monkeypatch.setattr(agy_rpc, "handle_user_interaction", _fail_deliver)

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert rpc_calls == [(52548, "cascade-1")]
    assert tui_calls == []  # RPC cancel succeeded; no TUI fallback needed.
    # No optimistic 'cancelled': the outcome is unknown until an edge lands.
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is True


@pytest.mark.asyncio
async def test_antigravity_interrupt_waiting_step_denies_via_interaction_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WAITING permission prompt is denied through the interaction bridge RPC.

    ``CancelCascadeSteps`` is a no-op on a WAITING step despite returning HTTP
    < 400, so it must never be trusted alone here: delivering the DENY via
    ``HandleCascadeUserInteraction`` is the only action that can report success.
    """
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="cascade-1", active_turn_id=None
        ),
    )

    call_order: list[str] = []

    tui_calls: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "send_interaction_keys_via_tui",
        lambda bridge_dir, *keys: (call_order.append("tui"), tui_calls.append((bridge_dir, keys)))[
            1
        ],
    )
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: 52548)
    monkeypatch.setattr(
        agy_rpc,
        "get_trajectory_steps",
        lambda port, cascade_id: [_agy_waiting_permission_step()],
    )

    deliver_calls: list[dict[str, Any]] = []

    def _deliver(
        port: int,
        cascade_id: str,
        *,
        trajectory_id: str,
        step_index: int,
        payload: dict[str, Any],
    ) -> None:
        call_order.append("deny")
        deliver_calls.append(
            {
                "port": port,
                "cascade_id": cascade_id,
                "trajectory_id": trajectory_id,
                "step_index": step_index,
                "payload": payload,
            }
        )

    monkeypatch.setattr(agy_rpc, "handle_user_interaction", _deliver)

    cancel_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        agy_rpc,
        "cancel_cascade_steps",
        lambda port, cascade_id: (
            call_order.append("cancel"),
            cancel_calls.append((port, cascade_id)),
            False,
        )[2],
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert deliver_calls == [
        {
            "port": 52548,
            "cascade_id": "cascade-1",
            "trajectory_id": "traj-1",
            "step_index": 3,
            "payload": {"permission": {"allow": False}},
        }
    ]
    # Some agy versions leave the attended TUI prompt open after RPC delivery;
    # the decline keys dismiss it, mirroring bridge_interaction.
    assert tui_calls == [("dir/bid-agy", ("4", "Enter"))]
    # The denial may resume the cascade into a running turn; best-effort
    # cancel that too, but only AFTER the denial and TUI dismissal.
    assert cancel_calls == [(52548, "cascade-1")]
    assert call_order == ["deny", "tui", "cancel"]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is True


@pytest.mark.asyncio
async def test_antigravity_interrupt_waiting_step_tui_dismissal_failure_still_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort TUI dismissal never undoes the already-confirmed RPC deny."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="cascade-1", active_turn_id=None
        ),
    )

    def _boom(bridge_dir: Any, *keys: str) -> None:
        raise RuntimeError("the agy terminal is no longer running (the TUI exited)")

    monkeypatch.setattr(agy_bridge, "send_interaction_keys_via_tui", _boom)
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: 52548)
    monkeypatch.setattr(
        agy_rpc,
        "get_trajectory_steps",
        lambda port, cascade_id: [_agy_waiting_permission_step()],
    )
    monkeypatch.setattr(agy_rpc, "handle_user_interaction", lambda *a, **kw: None)
    cancel_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        agy_rpc,
        "cancel_cascade_steps",
        lambda port, cascade_id: (cancel_calls.append((port, cascade_id)), False)[1],
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    # The denial already succeeded over RPC; a flaky TUI must not turn that into a 503.
    assert cancel_calls == [(52548, "cascade-1")]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is True


@pytest.mark.asyncio
async def test_antigravity_interrupt_waiting_step_deny_failure_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed interaction-bridge denial is a 503, not a false success."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="cascade-1", active_turn_id=None
        ),
    )
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: 52548)
    monkeypatch.setattr(
        agy_rpc,
        "get_trajectory_steps",
        lambda port, cascade_id: [_agy_waiting_permission_step()],
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise agy_rpc.AntigravityRpcError("input not registered for step 3")

    monkeypatch.setattr(agy_rpc, "handle_user_interaction", _boom)

    def _fail_cancel(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("must not fall through to cascade-cancel on a failed denial")

    monkeypatch.setattr(agy_rpc, "cancel_cascade_steps", _fail_cancel)

    escape_calls: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "send_interaction_keys_via_tui",
        lambda bridge_dir, *keys: escape_calls.append((bridge_dir, keys)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "antigravity_native_interrupt_failed"
    assert escape_calls == []  # no silent fallback that would mask the failure
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is False


@pytest.mark.asyncio
async def test_antigravity_interrupt_steps_read_failure_falls_back_to_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the step snapshot can't be read, fall back to Escape rather than guess."""
    import httpx

    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="cascade-1", active_turn_id=None
        ),
    )
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: 52548)

    def _boom(port: int, cascade_id: str) -> list[dict[str, Any]]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(agy_rpc, "get_trajectory_steps", _boom)

    def _fail_cancel(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("must not guess at cancel when the step snapshot is unreadable")

    monkeypatch.setattr(agy_rpc, "cancel_cascade_steps", _fail_cancel)

    escape_calls: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "send_interaction_keys_via_tui",
        lambda bridge_dir, *keys: escape_calls.append((bridge_dir, keys)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert escape_calls == [("dir/bid-agy", ("Escape",))]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is True


@pytest.mark.asyncio
async def test_antigravity_interrupt_falls_back_to_tui_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cascade/port yet (pre-mint placeholder) falls back to a TUI Escape."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        agy_bridge,
        "read_bridge_state",
        lambda bridge_dir: SimpleNamespace(
            session_id="conv_agy", conversation_id="agy_conv_placeholder", active_turn_id=None
        ),
    )

    def _fail_port(cascade_id: str) -> int | None:
        raise AssertionError("must not resolve a port for a placeholder conversation id")

    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", _fail_port)

    escape_calls: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "send_interaction_keys_via_tui",
        lambda bridge_dir, *keys: escape_calls.append((bridge_dir, keys)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert escape_calls == [("dir/bid-agy", ("Escape",))]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is True


@pytest.mark.asyncio
async def test_antigravity_interrupt_returns_503_when_both_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPC cancel unavailable and a dead TUI pane together are a 503, not a lie."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    import omnigent.harnesses.antigravity_native.rpc as agy_rpc
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(agy_bridge, "read_bridge_state", lambda bridge_dir: None)
    monkeypatch.setattr(agy_rpc, "resolve_language_server_port", lambda cascade_id: None)

    def _boom(bridge_dir: Any, *keys: str) -> None:
        raise RuntimeError("the agy terminal is no longer running (the TUI exited)")

    monkeypatch.setattr(agy_bridge, "send_interaction_keys_via_tui", _boom)

    runner, captured = _make_runner()
    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "antigravity_native_interrupt_failed"
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_agy")[0] is False


@pytest.mark.asyncio
async def test_antigravity_stop_kills_tears_down_and_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop kills the agy tmux session, publishes idle, and wakes the parent."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    killed: list[Any] = []
    monkeypatch.setattr(
        agy_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: killed.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    # A stop that follows an unresolved interrupt settles it: the kill is
    # confirmed, so no grace-timer cancel may fire later.
    runner._pending_interrupts["conv_agy"] = 0.0
    resp = await runner.stop("antigravity-native", "conv_agy")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert runner.take_pending_interrupt("conv_agy")[0] is False
    assert killed == [("dir/bid-agy", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_agy", "cancelled", None)]
    assert captured["wake_calls"][0]["confirmed_cancellation"] is True


@pytest.mark.asyncio
async def test_antigravity_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kill returns 503 and does NOT publish idle (no lie to the UI)."""
    import omnigent.harnesses.antigravity_native.bridge as agy_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    _patch_antigravity_bridge_id(monkeypatch, interrupt_mod)
    monkeypatch.setattr(agy_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(agy_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "antigravity_native_stop_failed"
    assert captured["wakes"] == []
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []
