"""Session-change handlers must heal a dead-but-registered Claude pane.

The turn-delivery path already probes ``is_alive()`` and recreates a stale
tmux registry entry via ``_ensure_native_terminal_for_turn``. The
``model_change`` / ``effort_change`` handlers used to inject ``/model`` and
``/effort`` into whatever socket the record advertised, so a dead registry
entry plus a missing tmux socket surfaced as ``503 claude_native_*_failed``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent import claude_native_bridge
from omnigent.claude_native_bridge import (
    bridge_dir_for_conversation_id,
    write_tmux_target,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import app as runner_app_module
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


def _native_spec() -> AgentSpec:
    """Return a claude-native agent spec for session create."""
    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )


def _plant_dead_claude_pane(
    registry: TerminalRegistry,
    conv_id: str,
    tmp_path: Path,
    bridge_dir: Path,
) -> Path:
    """Register a dead Claude pane and advertise a missing tmux socket.

    This is the reproduced gap: the registry still has the instance, but
    the socket path published in ``tmux.json`` does not exist.

    :returns: The missing socket path advertised to ``inject_slash_command``.
    """
    missing_sock = tmp_path / "omnigent-terminal-dead" / "tmux.sock"
    dead = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=missing_sock,
        private_dir=tmp_path / "dead_private",
    )
    dead.running = True
    (tmp_path / "dead_private").mkdir(exist_ok=True)

    async def _fake_is_alive() -> bool:
        dead.running = False
        return False

    async def _noop_close() -> None:
        return None

    dead.is_alive = _fake_is_alive  # type: ignore[method-assign]
    dead.close = _noop_close  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("claude", "main"): dead}
        registry._instance_locks[(conv_id, "claude", "main")] = threading.Lock()
    write_tmux_target(bridge_dir, socket_path=missing_sock, tmux_target="claude:0.0")
    return missing_sock


async def _open_claude_native_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conv_id: str,
    auto_create_calls: list[str],
) -> tuple[Any, TerminalRegistry]:
    """Build a runner with a real terminal registry and a claude-native session.

    ``_auto_create_claude_terminal`` is stubbed so session create does not
    spawn a real Claude TUI. Calls after session create are the session-change
    heal path.
    """

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude",
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        _stub_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native._auto_create_claude_terminal",
        _stub_auto_create,
    )

    async def _stub_launch_claude(ctx: Any) -> SessionResourceView:
        return await _stub_auto_create(ctx.session_id, ctx.resource_registry, ctx.publish_event)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_claude",
        _stub_launch_claude,
    )
    monkeypatch.setattr("omnigent.runner.native._launch_claude", _stub_launch_claude)
    # No ``raising=False``: a renamed constant must fail the test loudly rather
    # than silently leave the real 30s budget in place.
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_POLL_S", 0.01)
    monkeypatch.setattr(
        claude_native_bridge,
        "read_model_env",
        lambda _bridge_dir: {"ANTHROPIC_CUSTOM_MODEL_OPTION": "claude-opus-4-7"},
    )
    monkeypatch.setattr(claude_native_bridge, "post_tools_changed", lambda _bridge_dir: None)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec()

    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
    )
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
    return app, registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected_command"),
    [
        ({"type": "model_change", "model": "claude-opus-4-7"}, "/model claude-opus-4-7"),
        ({"type": "effort_change", "effort": "high"}, "/effort high"),
    ],
    ids=["model_change", "effort_change"],
)
async def test_session_change_heals_dead_registered_missing_tmux_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event: dict[str, Any],
    expected_command: str,
) -> None:
    """Dead registry + missing sock must heal, then inject, not 503.

    ``inject_slash_command`` is the real function while the pane is still
    stale: that is the gap (tmux cannot connect to the advertised socket).
    After ``_ensure_native_terminal_for_turn`` recreates the pane, the
    inject is recorded so the handler can finish without a live Claude TUI.
    """
    conv_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    if event["type"] == "effort_change":
        conv_id = "b2c3d4e5f60718293a4b5c6d7e8f901a"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_dead_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    real_inject = claude_native_bridge.inject_slash_command
    captured: list[str] = []

    def _inject_hits_missing_sock_until_heal(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint
        if not auto_create_calls:
            real_inject(inject_bridge_dir, command=command, timeout_s=1.0)
            raise AssertionError(
                "inject_slash_command should have raised on the missing tmux socket"
            )
        captured.append(command)

    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_hits_missing_sock_until_heal
    )
    monkeypatch.setattr(
        claude_native_bridge,
        "claude_pane_ready",
        lambda _bridge_dir: bool(auto_create_calls),
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=event)

    assert resp.status_code == 204, (
        f"dead-but-registered pane must heal before inject; got {resp.status_code}: {resp.text}"
    )
    assert auto_create_calls == [conv_id], (
        f"_ensure_native_terminal_for_turn must recreate the pane; "
        f"got auto_create_calls={auto_create_calls!r}"
    )
    assert captured == [expected_command], (
        f"healed pane must receive {expected_command!r}; got {captured!r}"
    )


@pytest.mark.asyncio
async def test_model_change_heals_before_read_model_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Picker vocabulary must come from the recreated pane, not the stale one."""
    conv_id = "c3d4e5f60718293a4b5c6d7e8f901a2b"
    auto_create_calls: list[str] = []
    order: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )

    async def _tracking_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        order.append("heal")
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude",
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        _tracking_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native._auto_create_claude_terminal",
        _tracking_auto_create,
    )

    async def _tracking_launch(ctx: Any) -> SessionResourceView:
        return await _tracking_auto_create(
            ctx.session_id, ctx.resource_registry, ctx.publish_event
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_claude",
        _tracking_launch,
    )
    monkeypatch.setattr("omnigent.runner.native._launch_claude", _tracking_launch)
    monkeypatch.setattr(
        claude_native_bridge,
        "read_model_env",
        lambda _bridge_dir: (
            order.append("read_model_env") or {"ANTHROPIC_CUSTOM_MODEL_OPTION": "claude-opus-4-7"}
        ),
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_dead_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    real_inject = claude_native_bridge.inject_slash_command

    def _inject_hits_missing_sock_until_heal(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float = 1.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del command, timeout_s, auto_confirm, confirm_hint
        if not auto_create_calls:
            real_inject(inject_bridge_dir, command="/model claude-opus-4-7", timeout_s=1.0)

    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_hits_missing_sock_until_heal
    )
    monkeypatch.setattr(
        claude_native_bridge,
        "claude_pane_ready",
        lambda _bridge_dir: bool(auto_create_calls),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, resp.text
    assert order[:2] == ["heal", "read_model_env"], (
        f"model handler must heal before read_model_env; got {order!r}"
    )


@pytest.mark.asyncio
async def test_session_change_live_pane_does_not_recreate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live registered pane must inject immediately without recreate."""
    conv_id = "d4e5f60718293a4b5c6d7e8f901a2b3c"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()

    live = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "live.sock",
        private_dir=tmp_path / "live_private",
    )
    live.running = True
    (tmp_path / "live_private").mkdir(exist_ok=True)

    async def _alive() -> bool:
        return True

    async def _noop_close() -> None:
        return None

    live.is_alive = _alive  # type: ignore[method-assign]
    live.close = _noop_close  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("claude", "main"): live}
        registry._instance_locks[(conv_id, "claude", "main")] = threading.Lock()

    captured: list[str] = []
    pane_ready_calls: list[Path] = []

    def _fake_inject(
        bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir, timeout_s, auto_confirm, confirm_hint
        captured.append(command)

    def _ready(bridge_dir: Path) -> bool:
        pane_ready_calls.append(bridge_dir)
        return True

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    monkeypatch.setattr(claude_native_bridge, "claude_pane_ready", _ready)

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "effort_change", "effort": "high"},
        )

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == [], (
        f"live pane must not recreate; got auto_create_calls={auto_create_calls!r}"
    )
    assert captured == ["/effort high"]
    # A live pane is never readiness-polled: only a recreate reboots the TUI.
    # See ``test_live_pane_with_occupied_composer_does_not_stall`` for why
    # polling a live pane would be actively harmful.
    assert pane_ready_calls == [], (
        f"live pane must not be readiness-probed; got {pane_ready_calls!r}"
    )


def _plant_live_claude_pane(
    registry: TerminalRegistry,
    conv_id: str,
    tmp_path: Path,
    bridge_dir: Path,
) -> None:
    """Register a live Claude pane advertising a socket that exists."""
    sock = tmp_path / "omnigent-terminal-live" / "tmux.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.touch()
    live = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=sock,
        private_dir=tmp_path / "live_private",
    )
    live.running = True
    (tmp_path / "live_private").mkdir(exist_ok=True)

    async def _alive() -> bool:
        return True

    live.is_alive = _alive  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("claude", "main"): live}
        registry._instance_locks[(conv_id, "claude", "main")] = threading.Lock()
    write_tmux_target(bridge_dir, socket_path=sock, tmux_target="claude:0.0")


@pytest.mark.asyncio
async def test_live_pane_with_occupied_composer_does_not_stall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live pane reporting "not ready" must inject at once, not wait it out.

    ``claude_pane_ready`` is false while a surface occupies the composer
    (``!`` shell mode, the ctrl+r search, a hand-opened picker). Nothing in
    this handler can clear one: ``inject_slash_command`` reclaims the composer
    itself via ``_restore_occupied_input``. Polling here would burn the whole
    budget on a case inject already handles, so the live pane must not be
    polled at all.
    """
    conv_id = "e5f60718293a4b5c6d7e8f901a2b3c4d"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    # Budget the shipped value: a regression here shows up as a 30s test.
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_POLL_S", 0.25)
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    captured: list[str] = []
    pane_ready_calls: list[Path] = []

    def _fake_inject(bridge_dir: Path, *, command: str, **_kwargs: Any) -> None:
        del bridge_dir
        captured.append(command)

    def _never_ready(bridge_dir: Path) -> bool:
        pane_ready_calls.append(bridge_dir)
        return False

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    monkeypatch.setattr(claude_native_bridge, "claude_pane_ready", _never_ready)

    started = time.monotonic()
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "effort_change", "effort": "high"},
        )
    elapsed = time.monotonic() - started

    assert resp.status_code == 204, resp.text
    assert captured == ["/effort high"]
    assert auto_create_calls == [], "a live pane must not be recreated"
    assert pane_ready_calls == [], (
        f"live pane must not be readiness-probed; got {pane_ready_calls!r}"
    )
    assert elapsed < 5.0, (
        f"occupied composer on a live pane stalled {elapsed:.1f}s; inject "
        "clears the surface itself, so this must not wait out the budget"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "conv_id"),
    [
        (
            {"type": "permission_mode_change", "permission_mode": "acceptEdits"},
            "f60718293a4b5c6d7e8f901a2b3c4d5e",
        ),
        ({"type": "compact"}, "0718293a4b5c6d7e8f901a2b3c4d5e6f"),
    ],
    ids=["permission_mode_change", "compact"],
)
async def test_other_pane_writers_heal_dead_registered_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event: dict[str, Any],
    conv_id: str,
) -> None:
    """Every claude-native pane writer heals, not just model/effort.

    ``permission_mode_change`` and ``compact`` drive the same advertised tmux
    socket, so a dead-but-registered pane failed them the same way
    (``503 claude_native_permission_mode_failed`` / ``_compact_failed``).
    """
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_dead_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", lambda *_a, **_k: None)
    monkeypatch.setattr(
        claude_native_bridge, "set_permission_mode", lambda *_a, **_k: "acceptEdits"
    )
    monkeypatch.setattr(claude_native_bridge, "claude_pane_ready", lambda _bridge_dir: True)

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=event)

    assert resp.status_code in (200, 204), resp.text
    assert auto_create_calls == [conv_id], (
        f"dead registered pane must heal for {event['type']}; "
        f"got auto_create_calls={auto_create_calls!r}"
    )


@pytest.mark.asyncio
async def test_recreated_pane_is_waited_for_before_injection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recreate that yields a booting pane must be waited out, then typed into.

    This is the case the readiness poll exists for: the recreate relaunches
    Claude with ``--resume``, so the input box is not mounted yet and an
    immediate inject would be lost. A recreate that produces no pane at all is
    covered by ``test_failed_recreate_does_not_wait``.
    """
    conv_id = "18293a4b5c6d7e8f901a2b3c4d5e6f70"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    # A budget with room for several probes: the fixture's 0.2s is smaller than
    # the first ``asyncio.to_thread`` hop, so the poll would exit after one.
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_TIMEOUT_S", 5.0)
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_POLL_S", 0.01)
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_dead_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    # A successful recreate registers a live pane, the way the real ensure path
    # does before it advertises the tmux target.
    async def _recreating_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        _plant_live_claude_pane(registry, session_id, tmp_path, bridge_dir)
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude",
        )

    async def _recreating_launch(ctx: Any) -> SessionResourceView:
        return await _recreating_auto_create(ctx.session_id, None, None)

    for target in (
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        "omnigent.runner.native._auto_create_claude_terminal",
    ):
        monkeypatch.setattr(target, _recreating_auto_create)
    for target in (
        "omnigent.runner.native.orchestration._launch_claude",
        "omnigent.runner.native._launch_claude",
    ):
        monkeypatch.setattr(target, _recreating_launch)

    captured: list[str] = []
    # Not ready for the first two polls, as a booting TUI would be.
    readiness = iter([False, False, True])

    monkeypatch.setattr(
        claude_native_bridge,
        "inject_slash_command",
        lambda _bridge_dir, *, command, **_kwargs: captured.append(command),
    )
    monkeypatch.setattr(
        claude_native_bridge,
        "claude_pane_ready",
        lambda _bridge_dir: next(readiness, True),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "effort_change", "effort": "high"},
        )

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == [conv_id], "dead pane must be recreated"
    assert captured == ["/effort high"]
    assert next(readiness, "exhausted") == "exhausted", (
        "the poll must keep probing until the booting pane reports ready"
    )


@pytest.mark.asyncio
async def test_failed_recreate_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recreate that produced no pane must not burn the readiness budget.

    ``_ensure_native_terminal_for_turn`` swallows its own failures, so an
    unregistered pane afterwards means nothing is booting. Polling cannot help;
    the injection should fail fast as it did before the heal existed.
    """
    conv_id = "8293a4b5c6d7e8f901a2b3c4d5e6f701"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    # Budget the shipped value: a regression shows up as a 30s test.
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(runner_app_module, "_CLAUDE_PANE_READY_POLL_S", 0.25)
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_dead_claude_pane(registry, conv_id, tmp_path, bridge_dir)

    captured: list[str] = []
    pane_ready_calls: list[Path] = []

    def _never_ready(bridge_dir: Path) -> bool:
        pane_ready_calls.append(bridge_dir)
        return False

    monkeypatch.setattr(
        claude_native_bridge,
        "inject_slash_command",
        lambda _bridge_dir, *, command, **_kwargs: captured.append(command),
    )
    monkeypatch.setattr(claude_native_bridge, "claude_pane_ready", _never_ready)

    started = time.monotonic()
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "effort_change", "effort": "high"},
        )
    elapsed = time.monotonic() - started

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == [conv_id], "the heal must still be attempted"
    assert captured == ["/effort high"]
    assert pane_ready_calls == [], (
        f"a recreate that made no pane must not be polled; got {pane_ready_calls!r}"
    )
    assert elapsed < 5.0, f"failed recreate stalled {elapsed:.1f}s instead of failing fast"
