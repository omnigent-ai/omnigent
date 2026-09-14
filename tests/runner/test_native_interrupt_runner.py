"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
no-handler fall-through contract (opencode), and the 503 mapping.
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
    captured: dict[str, Any] = {"published": [], "wakes": []}

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        captured["published"].append((conv_id, event))

    def _mark_and_wake(child_session_id: str, *, status: str, output: str | None) -> _FakeAck:
        captured["wakes"].append((child_session_id, status, output))
        return _FakeAck()

    async def _codex_bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any | None:
        return None

    def _client_safe(exc: BaseException, *, context: str) -> str:
        return f"safe:{context}"

    kwargs: dict[str, Any] = {
        "server_client": SimpleNamespace(),
        "resource_registry": _FakeResourceRegistry(),
        "publish_event": _publish,
        "mark_subagent_terminal_and_wake": _mark_and_wake,
        "session_sub_agent_names": {},
        "codex_bridge_state_for_session": _codex_bridge_state,
        "client_safe_error_detail": _client_safe,
        "logger": logging.getLogger("test.interrupt"),
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
@pytest.mark.parametrize("harness", ["opencode-native", "claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    assert await runner.stop(harness, "conv_x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop"])
@pytest.mark.parametrize("bridge_id", ["active_bridge", None])
async def test_antigravity_interrupt_uses_active_bridge_label_and_wakes_parent(
    monkeypatch: pytest.MonkeyPatch, event_type: str, bridge_id: str | None
) -> None:
    """Both native cancel events reach the bridge shared with the executor."""
    import omnigent.harnesses.antigravity_native.bridge as bridge
    import omnigent.inner.antigravity_native_executor as executor
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _labels(
        *, server_client: Any, session_id: str, raise_on_error: bool
    ) -> dict[str, str]:
        assert session_id == "conv_agy"
        assert raise_on_error
        return (
            {bridge.ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: bridge_id}
            if bridge_id is not None
            else {}
        )

    calls: list[tuple[Any, str | None]] = []

    async def _cancel(bridge_dir: Any, *, expected_session_id: str | None) -> bool:
        calls.append((bridge_dir, expected_session_id))
        return True

    monkeypatch.setattr(interrupt_mod, "_session_labels_for_runner_spawn", _labels)
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda bridge_id: f"dir/{bridge_id}")
    monkeypatch.setattr(executor, "interrupt_bridge_turn", _cancel)
    runner, captured = _make_runner()

    resp = await getattr(runner, event_type)("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 204
    assert calls == [(f"dir/{bridge_id or 'conv_agy'}", "conv_agy")]
    assert captured["wakes"] == [("conv_agy", "cancelled", "[System: sub-agent interrupted]")]


@pytest.mark.asyncio
async def test_antigravity_interrupt_failure_does_not_acknowledge_active_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable native cancel does not report success for a live bridge."""
    import json

    import omnigent.harnesses.antigravity_native.bridge as bridge
    import omnigent.inner.antigravity_native_executor as executor
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _cancel(bridge_dir: Any, *, expected_session_id: str | None) -> bool:
        return False

    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)

    async def _labels(
        *, server_client: Any, session_id: str, raise_on_error: bool
    ) -> dict[str, str]:
        return {}

    monkeypatch.setattr(interrupt_mod, "_session_labels_for_runner_spawn", _labels)
    monkeypatch.setattr(
        bridge, "read_bridge_state", lambda bridge_dir: SimpleNamespace(session_id="conv_agy")
    )
    monkeypatch.setattr(executor, "interrupt_bridge_turn", _cancel)
    runner, captured = _make_runner()

    resp = await runner.interrupt("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "antigravity_native_interrupt_failed"
    assert captured["wakes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop"])
@pytest.mark.parametrize("transport", ["rpc", "tui", "idle"])
@pytest.mark.parametrize("confirmation_error", [False, True])
async def test_antigravity_unconfirmed_cancel_returns_503_without_parent_wake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
    transport: str,
    confirmation_error: bool,
) -> None:
    import omnigent.harnesses.antigravity_native.bridge as bridge
    import omnigent.inner.antigravity_native_executor as executor
    from omnigent.harnesses.antigravity_native.stop_hook import STOP_EVENTS_FILE
    from omnigent.runner.native import interrupt as interrupt_mod

    bridge.write_bridge_state(
        tmp_path,
        bridge.AntigravityNativeBridgeState(
            session_id="conv_agy", conversation_id="active-cascade"
        ),
    )

    async def _labels(
        *, server_client: Any, session_id: str, raise_on_error: bool
    ) -> dict[str, str]:
        return {}

    calls: list[str] = []
    idle = False

    def _confirm_idle(_bridge: Path) -> bool:
        if not idle and confirmation_error:
            raise RuntimeError("private pane transport detail")
        return idle

    monkeypatch.setattr(interrupt_mod, "_session_labels_for_runner_spawn", _labels)
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda _bridge_id: tmp_path)
    monkeypatch.setattr(executor, "turn_is_idle_via_tui", lambda _bridge: transport == "idle")
    monkeypatch.setattr(executor, "wait_for_turn_idle_via_tui", _confirm_idle)
    monkeypatch.setattr(
        executor,
        "resolve_language_server_port",
        lambda _cid: 43210 if transport == "rpc" else None,
    )
    monkeypatch.setattr(
        executor, "cancel_cascade_steps", lambda _port, _cid: calls.append("rpc") or True
    )
    monkeypatch.setattr(
        executor,
        "interrupt_turn_via_tui",
        lambda _bridge, **_kwargs: calls.append("tui") or True,
    )
    runner, captured = _make_runner()
    response = await getattr(runner, event_type)("antigravity-native", "conv_agy")

    assert response is not None and response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "error": "antigravity_native_interrupt_failed",
        "detail": "Antigravity cancellation could not be confirmed.",
    }
    assert calls == ([] if transport == "idle" else [transport])
    assert captured["wakes"] == []
    assert not (tmp_path / STOP_EVENTS_FILE).exists()

    idle = True
    response = await getattr(runner, event_type)("antigravity-native", "conv_agy")
    assert response is not None and response.status_code == 204
    assert calls == ([] if transport == "idle" else [transport, transport])
    assert captured["wakes"] == [("conv_agy", "cancelled", "[System: sub-agent interrupted]")]


@pytest.mark.asyncio
async def test_antigravity_interrupt_absent_bridge_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished bridge has no native turn to cancel or sub-agent to wake."""
    import omnigent.harnesses.antigravity_native.bridge as bridge
    import omnigent.inner.antigravity_native_executor as executor
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _labels(
        *, server_client: Any, session_id: str, raise_on_error: bool
    ) -> dict[str, str]:
        return {}

    async def _cancel(bridge_dir: Any, *, expected_session_id: str | None) -> bool:
        return False

    monkeypatch.setattr(interrupt_mod, "_session_labels_for_runner_spawn", _labels)
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(bridge, "read_bridge_state", lambda bridge_dir: None)
    monkeypatch.setattr(executor, "interrupt_bridge_turn", _cancel)
    runner, captured = _make_runner()

    resp = await runner.stop("antigravity-native", "conv_agy")

    assert resp is not None and resp.status_code == 204
    assert captured["wakes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "http", "json", "shape"])
async def test_antigravity_label_lookup_failure_returns_sanitized_503(
    monkeypatch: pytest.MonkeyPatch, event_type: str, failure: str
) -> None:
    import omnigent.harnesses.antigravity_native.bridge as bridge

    requests: list[httpx.Request] = []

    def _lookup(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private transport detail", request=request)
        if failure == "http":
            return httpx.Response(503, text="private transport detail")
        if failure == "json":
            return httpx.Response(200, text="private transport detail")
        return httpx.Response(200, json={"labels": None})

    monkeypatch.setattr(
        bridge,
        "bridge_dir_for_bridge_id",
        lambda bridge_id: pytest.fail("failed lookup must not select a bridge"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_lookup), base_url="http://server"
    ) as server_client:
        runner, captured = _make_runner(server_client=server_client)
        response = await getattr(runner, event_type)("antigravity-native", "conv_agy")

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/sessions/conv_agy/labels"
    assert response is not None and response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "error": "antigravity_native_interrupt_failed",
        "detail": "safe:antigravity-native interrupt",
    }
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_uniform_interrupt_injects_and_wakes_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform interrupt calls the bridge inject fn and wakes the parent."""
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
    assert captured["wakes"] == [("conv_g", "cancelled", "[System: sub-agent interrupted]")]


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
    resp = await runner.stop("cursor-native", "conv_c")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert killed == [("dir/conv_c", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_c", "cancelled", "[System: sub-agent stopped]")]


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
    assert captured["wakes"] == [("conv_cn", "cancelled", "[System: sub-agent stopped]")]


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
    """claude interrupt resolves the bridge id, injects, and wakes the parent."""
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
    assert captured["wakes"] == [("conv_cl", "cancelled", "[System: sub-agent interrupted]")]
