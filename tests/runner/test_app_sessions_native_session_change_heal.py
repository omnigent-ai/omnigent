"""Session-change handlers must heal a dead-but-registered Claude pane.

The turn-delivery path already probes ``is_alive()`` and recreates a stale
tmux registry entry via ``_ensure_native_terminal_for_turn``. The
``model_change`` / ``effort_change`` handlers used to inject ``/model`` and
``/effort`` into whatever socket the record advertised, so a dead registry
entry plus a missing tmux socket surfaced as ``503 claude_native_*_failed``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    _TMUX_FILE,
    bridge_dir_for_conversation_id,
    write_tmux_target,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, native_controls
from omnigent.runner.resource_registry import CLAUDE_NATIVE_TERMINAL_ROLE
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests._helpers.polling import poll_until_async
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


def _native_spec(harness: str = "claude-native") -> AgentSpec:
    """Return an agent spec running *harness* for session create."""
    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
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
    harness_client: _ScriptedHarnessClient | None = None,
    plant_pane_on_launch: Path | None = None,
    harness: str = "claude-native",
) -> tuple[Any, TerminalRegistry]:
    """Build a runner with a real terminal registry and a claude-native session.

    ``_auto_create_claude_terminal`` is stubbed so session create does not
    spawn a real Claude TUI. Calls after session create are the session-change
    heal path. *harness_client* receives forwarded turns; ``None`` uses a
    scripted client with no frames.
    """

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        if plant_pane_on_launch is not None:
            _plant_live_claude_native_pane(app, registry, session_id, plant_pane_on_launch)
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
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_POLL_S", 0.01)
    monkeypatch.setattr(
        claude_native_bridge,
        "read_model_env",
        lambda _bridge_dir: {"ANTHROPIC_CUSTOM_MODEL_OPTION": "claude-opus-4-7"},
    )
    monkeypatch.setattr(
        claude_native_bridge, "post_tools_changed", lambda _bridge_dir, **kwargs: None
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec(harness)

    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(  # type: ignore[arg-type]
            harness_client or _ScriptedHarnessClient([])
        ),
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
        ready_timeout_s: float | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint, ready_timeout_s
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
        ready_timeout_s: float | None = None,
    ) -> None:
        del command, timeout_s, auto_confirm, confirm_hint, ready_timeout_s
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
        ready_timeout_s: float | None = None,
    ) -> None:
        del bridge_dir, timeout_s, auto_confirm, confirm_hint, ready_timeout_s
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
    bridge_dir: Path | None,
    *,
    label: str = "live",
) -> TerminalInstance:
    """Register a live Claude pane, advertising its socket in *bridge_dir* when given."""
    sock = tmp_path / f"omnigent-terminal-{label}" / "tmux.sock"
    live = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=sock,
        private_dir=tmp_path / f"{label}_private",
    )
    live.running = True
    (tmp_path / f"{label}_private").mkdir(parents=True, exist_ok=True)

    async def _alive() -> bool:
        return True

    live.is_alive = _alive  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("claude", "main"): live}
        registry._instance_locks[(conv_id, "claude", "main")] = threading.Lock()
    if bridge_dir is not None:
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()
        write_tmux_target(bridge_dir, socket_path=sock, tmux_target="claude:0.0")
    return live


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
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_POLL_S", 0.25)
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
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_TIMEOUT_S", 5.0)
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_POLL_S", 0.01)
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
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(native_controls, "_CLAUDE_PANE_READY_POLL_S", 0.25)
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


def _read_advertisement(bridge_dir: Path) -> dict[str, object] | None:
    """Return the bridge's ``tmux.json`` payload, or ``None`` when absent/invalid."""
    try:
        payload = json.loads((bridge_dir / _TMUX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class _AdvertisementProbeHarnessClient(_ScriptedHarnessClient):
    """Snapshot the bridge's tmux advertisement at the moment a turn is forwarded."""

    def __init__(self, bridge_dir: Path) -> None:
        super().__init__([])
        self._bridge_dir = bridge_dir
        self.advertised_at_forward: list[dict[str, object] | None] = []
        self.forwarded = asyncio.Event()

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        self.advertised_at_forward.append(_read_advertisement(self._bridge_dir))
        self.forwarded.set()
        return super().stream(method, url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_turn_readvertises_live_pane_before_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Restore a live pane's missing advertisement before forwarding, without recreating it."""
    conv_id = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    harness_client = _AdvertisementProbeHarnessClient(bridge_dir)
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch,
        conv_id=conv_id,
        auto_create_calls=auto_create_calls,
        harness_client=harness_client,
    )
    auto_create_calls.clear()
    live = _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    # The fault: the pane is alive but nothing advertises its tmux target.
    (bridge_dir / _TMUX_FILE).unlink()

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hello"}],
                "harness": "claude-native",
            },
        )
        assert resp.status_code == 202, resp.text
        await asyncio.wait_for(harness_client.forwarded.wait(), timeout=10.0)

    assert auto_create_calls == [], "a live pane must be re-advertised in place, not recreated"
    advertised = [
        {k: v for k, v in (entry or {}).items() if k in ("socket_path", "tmux_target")}
        for entry in harness_client.advertised_at_forward
    ]
    expected = {"socket_path": str(live.socket_path), "tmux_target": live.tmux_target}
    assert advertised == [expected], (
        "the turn was forwarded without a usable advertisement: "
        f"{harness_client.advertised_at_forward!r}"
    )


def _claude_sign_in_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return the host's reported Claude sign-in state, by server name, for the test to edit."""
    statuses: dict[str, str] = {}
    monkeypatch.setattr(
        "omnigent.runner.native.claude_mcp_sign_in.harness_sign_in_by_live_name",
        lambda harness: dict(statuses) if harness == "claude" else {},
    )
    return statuses


def _plant_live_claude_native_pane(
    app: Any, registry: TerminalRegistry, conv_id: str, tmp_path: Path, label: str = "live"
) -> list[str]:
    """Register a running claude-native pane; returns the record of its closes."""
    live = _plant_live_claude_pane(registry, conv_id, tmp_path, None, label=label)
    closes: list[str] = []

    async def _close() -> None:
        closes.append(conv_id)

    live.close = _close  # type: ignore[method-assign]
    resources = app.state.session_resource_registry
    with resources._lock:
        resources._terminal_roles[(conv_id, "terminal_claude_main")] = CLAUDE_NATIVE_TERMINAL_ROLE
    return closes


async def _open_claude_native_session_before_sign_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conv_id: str,
    *,
    relaunches: bool = True,
    harness_client: _ScriptedHarnessClient | None = None,
    during_relaunch: Callable[[], None] | None = None,
    relaunch_gate: asyncio.Event | None = None,
    harness: str = "claude-native",
) -> tuple[Any, list[str], list[str], dict[str, str]]:
    """Start a session whose Claude Code launched while ``linear`` still needed sign-in.

    :param relaunches: Whether relaunching Claude Code brings up a new pane.
    :param during_relaunch: Runs while Claude Code relaunches, e.g. a sign-in finishing.
    :param relaunch_gate: When given, a relaunch waits for it before the pane comes up.
    :param harness: The session's harness, as its agent spec declares it.
    :returns: The app, the planted pane's closes, the relaunches, and the
        sign-in state to edit.
    """
    statuses = _claude_sign_in_state(monkeypatch)
    statuses["linear"] = "sign_in_required"
    app, registry = await _open_claude_native_session(
        monkeypatch,
        conv_id=conv_id,
        auto_create_calls=[],
        harness_client=harness_client,
        harness=harness,
    )
    closes = _plant_live_claude_native_pane(app, registry, conv_id, tmp_path)
    if harness == "claude-native":
        async with _runner_client(app) as client:
            # The runner records a pane it did not launch when it first checks it.
            seen = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")
            assert seen.json() == {"running": True, "partial": True, "servers": []}, seen.text
    relaunched: list[str] = []

    async def _relaunch(ctx: Any) -> SessionResourceView:
        relaunched.append(ctx.session_id)
        if during_relaunch is not None:
            during_relaunch()
        if relaunch_gate is not None:
            await relaunch_gate.wait()
        if relaunches:
            _plant_live_claude_native_pane(app, registry, ctx.session_id, tmp_path, "relaunched")
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=ctx.session_id, name="claude"
        )

    monkeypatch.setattr("omnigent.runner.native.orchestration._launch_claude", _relaunch)
    monkeypatch.setattr("omnigent.runner.native._launch_claude", _relaunch)
    return app, closes, relaunched, statuses


def _hold_restart_attach_check(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Make a restart wait in its host-attach check; returns ``(checking, release)``.

    Only a restart asks this, never a message. The runner binds the check when its
    app is built, so call this before opening the session.
    """
    checking, release = threading.Event(), threading.Event()

    def _held_attach_check(_socket: str, _target: str) -> float | None:
        checking.set()
        release.wait(5)
        return None

    monkeypatch.setattr(
        "omnigent.native.native_cost_popup._tmux_last_client_input_at", _held_attach_check
    )
    return checking, release


def _note_line_waits(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Return an event set once something starts waiting for its turn in a session's line."""
    waiting = asyncio.Event()
    condition_wait = asyncio.Condition.wait

    async def _noted_wait(self: asyncio.Condition) -> bool:
        waiting.set()
        return await condition_wait(self)

    monkeypatch.setattr(asyncio.Condition, "wait", _noted_wait)
    return waiting


def _note_launch_lock_waits(app: Any, conv_id: str) -> asyncio.Event:
    """Return an event set once a caller waits on the session's Claude launch lock."""
    waiting = asyncio.Event()

    class _NotedLock(asyncio.Lock):
        async def acquire(self) -> bool:
            if self.locked():
                waiting.set()
            return await super().acquire()

    app.state.native_terminal_ensure_locks["claude"][conv_id] = _NotedLock()
    return waiting


class _PaneAtDeliveryHarnessClient(_ScriptedHarnessClient):
    """Record the session's Claude pane at the moment each turn is forwarded."""

    def __init__(self, conv_id: str) -> None:
        super().__init__([])
        self._conv_id = conv_id
        self.terminals: TerminalRegistry | None = None
        self.panes_at_delivery: list[TerminalInstance | None] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        assert self.terminals is not None
        self.panes_at_delivery.append(self.terminals.get(self._conv_id, "claude", "main"))
        return super().stream(method, url, json=json, timeout=timeout)


_MESSAGE = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
# A turn someone typed in the terminal, as the forwarder reports it.
_TERMINAL_TURN = {"type": "external_session_status", "data": {"status": "running"}}


@pytest.mark.asyncio
async def test_mcp_status_names_a_claude_server_signed_in_since_the_pane_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/mcp offers Reconnect for a server Claude Code could not connect before sign-in."""
    conv_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    app, _closes, _relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )

    async with _runner_client(app) as client:
        before = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")
        statuses["linear"] = "connected"
        after = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert before.json() == {"running": True, "partial": True, "servers": []}
    assert after.json()["partial"] is True
    [server] = after.json()["servers"]
    assert (server["name"], server["status"]) == ("linear", "failed")
    assert server["error"].startswith("Signed in after Claude Code started.")


@pytest.mark.asyncio
@pytest.mark.parametrize("released_sign_in_hold", [False, True], ids=["no-hold", "released-hold"])
async def test_mcp_reconnect_restarts_an_idle_claude_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, released_sign_in_hold: bool
) -> None:
    """Reconnect relaunches Claude Code, which connects every server as it starts.

    A launcher sign-in hold that no longer waits does not stand in its way.
    """
    conv_id = "b1b2c3d4e5f60718293a4b5c6d7e8f90"
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"
    if released_sign_in_hold:
        app.state.sign_in_holds[conv_id] = runner_app._SignInHold(
            deadline=time.monotonic() + 60, waiting=False
        )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
        )

    assert resp.status_code == 200, resp.text
    # The relaunched pane started signed in, so it reports nothing it can't use.
    assert resp.json() == {"running": True, "partial": True, "servers": []}
    assert (closes, relaunched) == ([conv_id], [conv_id])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "linear_status", "status_code", "error"),
    [
        ("notion", "connected", 404, None),
        ("linear", "sign_in_required", 200, "Sign in to linear first."),
        ("github", "sign_in_required", 200, None),
    ],
    ids=["unknown-server", "still-signed-out", "connected-since-start"],
)
async def test_mcp_reconnect_restarts_only_for_a_server_signed_in_since_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    linear_status: str,
    status_code: int,
    error: str | None,
) -> None:
    """A stale panel or a wrong name never restarts a pane that gains nothing from it."""
    conv_id = "b2b2c3d4e5f60718293a4b5c6d7e8f90"
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses.update({"linear": linear_status, "github": "connected"})

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": name}
        )

    assert resp.status_code == status_code, resp.text
    if status_code == 404:
        assert resp.json() == {"error": "mcp_server_unknown"}
    else:
        assert resp.json().get("reconnect_error") == error
    assert (closes, relaunched) == ([], [])


@pytest.mark.asyncio
async def test_a_cli_launched_pane_keeps_the_sign_in_state_it_started_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sign-in finished before /mcp is first opened still offers Reconnect.

    ``omnigent claude`` creates the pane through the generic terminal route; a
    second launch that finds the pane running leaves its record alone.
    """
    conv_id = "b3b2c3d4e5f60718293a4b5c6d7e8f90"
    statuses = _claude_sign_in_state(monkeypatch)
    statuses["linear"] = "sign_in_required"
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=[]
    )
    resources = app.state.session_resource_registry

    async def _launch_cli_pane(**kwargs: Any) -> SessionResourceView:
        if registry.get(conv_id, "claude", "main") is None:
            _plant_live_claude_native_pane(app, registry, conv_id, tmp_path)
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=conv_id, name="claude"
        )

    monkeypatch.setattr(resources, "launch_required_terminal", _launch_cli_pane)
    launch = {"terminal": "claude", "session_key": "main", "bridge_inject_dir": True}

    async with _runner_client(app) as client:
        assert (
            await client.post(f"/v1/sessions/{conv_id}/resources/terminals", json=launch)
        ).is_success
        statuses["linear"] = "connected"
        assert (
            await client.post(f"/v1/sessions/{conv_id}/resources/terminals", json=launch)
        ).is_success
        status = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert [server["name"] for server in status.json()["servers"]] == ["linear"], status.text


@pytest.mark.asyncio
async def test_an_overlapping_cli_launch_keeps_the_starting_launch_sign_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second ``omnigent claude`` attach sent while the pane starts keeps the first's record."""
    conv_id = "b6b2c3d4e5f60718293a4b5c6d7e8f90"
    statuses = _claude_sign_in_state(monkeypatch)
    statuses["linear"] = "sign_in_required"
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=[]
    )
    resources = app.state.session_resource_registry
    second_on_lock = _note_launch_lock_waits(app, conv_id)
    starting, release, second_launching = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started: asyncio.Future[None] | None = None

    async def _launch_cli_pane(**kwargs: Any) -> SessionResourceView:
        # Like the registry: the first launch starts the pane, an overlapping one gets it.
        nonlocal started
        if started is None:
            started = asyncio.get_running_loop().create_future()
            starting.set()
            await release.wait()
            _plant_live_claude_native_pane(app, registry, conv_id, tmp_path)
            started.set_result(None)
        else:
            second_launching.set()
            await started
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=conv_id, name="claude"
        )

    monkeypatch.setattr(resources, "launch_required_terminal", _launch_cli_pane)
    launch = {"terminal": "claude", "session_key": "main", "bridge_inject_dir": True}
    url = f"/v1/sessions/{conv_id}/resources/terminals"

    async with _runner_client(app) as client:
        first = asyncio.create_task(client.post(url, json=launch))
        await asyncio.wait_for(starting.wait(), timeout=5)
        # Sign-in finishes while the first pane starts; the second launch overlaps it.
        statuses["linear"] = "connected"
        second = asyncio.create_task(client.post(url, json=launch))
        await poll_until_async(
            lambda: second_on_lock.is_set() or second_launching.is_set(), timeout_s=5
        )
        release.set()
        assert (await first).is_success and (await second).is_success
        status = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert [server["name"] for server in status.json()["servers"]] == ["linear"], status.text


@pytest.mark.asyncio
async def test_a_transferred_pane_keeps_the_sign_in_state_it_started_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/clear and fork move the pane to a successor session, and its record moves too."""
    conv_id = "b4b2c3d4e5f60718293a4b5c6d7e8f90"
    successor_id = "b5b2c3d4e5f60718293a4b5c6d7e8f90"
    app, _closes, _relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        moved = await client.post(
            f"/v1/sessions/{conv_id}/resources/terminals/terminal_claude_main/transfer",
            json={"target_session_id": successor_id},
        )
        assert moved.status_code == 200, moved.text
        status = await client.get(f"/v1/sessions/{successor_id}/live-mcp-servers")

    assert [server["name"] for server in status.json()["servers"]] == ["linear"], status.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_age_s", "restarted"),
    [(10.0, False), (20.0, True)],
    ids=["output-10s-ago", "output-20s-ago"],
)
async def test_mcp_reconnect_waits_15_seconds_after_terminal_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output_age_s: float, restarted: bool
) -> None:
    """Reconnect holds off for recent output, but not for the idle reaper's longer window."""
    conv_id = "c9b2c3d4e5f60718293a4b5c6d7e8f90"
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"
    pane = app.state.session_resource_registry.terminal_registry.get(conv_id, "claude", "main")
    pane.screen_changed_within = lambda window_s: window_s > output_age_s  # type: ignore[method-assign]

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
        )

    assert resp.status_code == 200, resp.text
    assert (closes == [conv_id]) is restarted
    assert ("reconnect_error" in resp.json()) is not restarted
    assert relaunched == ([conv_id] if restarted else [])


_REFUSALS = {
    "terminal_turn": "Claude Code is working",
    "background_shell": "Claude still has background work running",
    "background_agent": "Claude still has background work running",
    "pending_question": "Claude is waiting for an answer",
    "terminal_dialog": "Claude is waiting for an answer",
    "open_on_host": "Claude Code is open in a terminal on the host",
    "sign_in_hold": "Claude Code is waiting for a sign-in",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("work", sorted(_REFUSALS))
async def test_mcp_reconnect_leaves_a_working_claude_pane_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, work: str
) -> None:
    """A restart would cut off work in progress, so the panel says why it waits."""
    conv_id = "c1b2c3d4e5f60718293a4b5c6d7e8f90"
    if work == "open_on_host":
        # Bound when the runner app is built: someone attached long ago, idle since.
        monkeypatch.setattr(
            "omnigent.native.native_cost_popup._tmux_last_client_input_at",
            lambda _socket, _target: 1.0,
        )
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"
    resources = app.state.session_resource_registry
    if work == "background_shell":
        resources._session_background_publisher(conv_id, True)
    elif work == "background_agent":
        monkeypatch.setattr(resources, "session_has_background_agents", lambda _sid: True)
    elif work == "pending_question":
        monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", lambda _d: True)
    elif work == "terminal_dialog":
        monkeypatch.setattr(claude_native_bridge, "terminal_dialog_pending", lambda _d: True)
    elif work == "sign_in_hold":
        app.state.sign_in_holds[conv_id] = runner_app._SignInHold(
            deadline=time.monotonic() + 60, waiting=True
        )

    async with _runner_client(app) as client:
        if work == "terminal_turn":
            started = await client.post(f"/v1/sessions/{conv_id}/events", json=_TERMINAL_TURN)
            assert started.status_code < 300, started.text
        resp = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["reconnect_error"].startswith(_REFUSALS[work])
    assert [server["name"] for server in resp.json()["servers"]] == ["linear"]
    assert (closes, relaunched) == ([], [])


@pytest.mark.asyncio
async def test_mcp_reconnect_reports_a_relaunch_that_did_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conv_id = "e1b2c3d4e5f60718293a4b5c6d7e8f90"
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, relaunches=False
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
        )

    assert resp.json() == {
        "running": False,
        "servers": [],
        "reconnect_error": "Claude Code didn't start again. Send a message to start it.",
    }
    assert (closes, relaunched) == ([conv_id], [conv_id])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "terminal_turn", "restarted"),
    [
        ("connected", False, True),
        ("sign_in_required", False, False),
        ("connected", True, False),
    ],
    ids=["signed-in-since-launch", "still-signed-out", "terminal-turn-running"],
)
async def test_a_message_restarts_claude_code_for_a_server_signed_in_since_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    terminal_turn: bool,
    restarted: bool,
) -> None:
    """The next message relaunches an idle Claude Code once a server it couldn't use signs in.

    A turn someone typed in the terminal is never cut off for it.
    """
    conv_id = "d1b2c3d4e5f60718293a4b5c6d7e8f90"
    harness = _PaneAtDeliveryHarnessClient(conv_id)
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness
    )
    harness.terminals = app.state.session_resource_registry.terminal_registry
    original = harness.terminals.get(conv_id, "claude", "main")
    statuses["linear"] = status

    async with _runner_client(app) as client:
        if terminal_turn:
            started = await client.post(f"/v1/sessions/{conv_id}/events", json=_TERMINAL_TURN)
            assert started.status_code < 300, started.text
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE)
        assert resp.status_code < 300, resp.text
        deadline = time.monotonic() + 5
        while not harness.posted_bodies and time.monotonic() < deadline:
            await asyncio.sleep(0.01)  # custom-lint: disable=sleep-then-assert -- turn forwards

    assert harness.posted_bodies, "the message never reached the harness"
    expected = [conv_id] if restarted else []
    assert (closes, relaunched) == (expected, expected)
    # Delivered to the relaunched pane, so the restart came first.
    [delivered_to] = harness.panes_at_delivery
    assert delivered_to is not None
    assert (delivered_to is not original) is restarted


@pytest.mark.asyncio
async def test_a_pane_started_with_the_session_keeps_what_needed_sign_in_as_it_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sign-in that completes before /mcp is first opened still counts."""
    conv_id = "f1b2c3d4e5f60718293a4b5c6d7e8f90"
    statuses = _claude_sign_in_state(monkeypatch)
    statuses["linear"] = "sign_in_required"
    app, _registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=[], plant_pane_on_launch=tmp_path
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        resp = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert [server["name"] for server in resp.json()["servers"]] == ["linear"]


@pytest.mark.asyncio
async def test_a_relaunched_pane_keeps_what_needed_sign_in_as_it_relaunched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sign-in that finishes while Claude Code starts may have missed it, so it counts."""
    conv_id = "f2b2c3d4e5f60718293a4b5c6d7e8f90"
    statuses: dict[str, str] = {}
    app, _closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch,
        tmp_path,
        conv_id,
        during_relaunch=lambda: statuses.update({"notion": "connected"}),
    )
    statuses.update({"linear": "connected", "notion": "sign_in_required"})

    async with _runner_client(app) as client:
        reconnected = await client.post(
            f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
        )

    assert relaunched == [conv_id]
    assert [server["name"] for server in reconnected.json()["servers"]] == ["notion"]


@pytest.mark.asyncio
async def test_a_message_waits_for_a_reconnect_and_reaches_the_relaunched_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reconnect holds the session's place in line from its checks until the relaunch.

    A message sent while Reconnect is still deciding must not be typed into the
    pane it is about to close.
    """
    conv_id = "a3b2c3d4e5f60718293a4b5c6d7e8f90"
    checking, release = _hold_restart_attach_check(monkeypatch)
    harness = _PaneAtDeliveryHarnessClient(conv_id)
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness
    )
    harness.terminals = app.state.session_resource_registry.terminal_registry
    original = harness.terminals.get(conv_id, "claude", "main")
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        reconnect = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            )
        )
        await poll_until_async(checking.is_set, timeout_s=5)
        in_line = _note_line_waits(monkeypatch)
        message = asyncio.create_task(client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE))
        await asyncio.wait_for(in_line.wait(), timeout=5)
        assert harness.posted_bodies == [], "the message reached the pane being replaced"
        release.set()
        assert (await reconnect).status_code == 200
        assert (await message).status_code < 300
        await poll_until_async(lambda: bool(harness.posted_bodies), timeout_s=5)

    assert (closes, relaunched) == ([conv_id], [conv_id])
    [delivered_to] = harness.panes_at_delivery
    assert delivered_to is not None and delivered_to is not original


@pytest.mark.asyncio
async def test_a_message_queued_behind_a_reconnect_is_answered_when_the_session_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting the session frees a message waiting in its line instead of leaving it hanging."""
    conv_id = "a8b2c3d4e5f60718293a4b5c6d7e8f90"
    checking, release = _hold_restart_attach_check(monkeypatch)
    app, _closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    terminals = app.state.session_resource_registry.terminal_registry
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        reconnect = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            )
        )
        await poll_until_async(checking.is_set, timeout_s=5)
        in_line = _note_line_waits(monkeypatch)
        message = asyncio.create_task(client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE))
        await asyncio.wait_for(in_line.wait(), timeout=5)
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code < 300, deleted.text
        answered = await asyncio.wait_for(message, timeout=5)
        release.set()
        reconnected = await reconnect

    assert answered.status_code == 410, answered.text
    assert answered.json()["error"] == "session_gone"
    assert reconnected.json()["reconnect_error"] == "This session was deleted."
    # Nothing comes back for the deleted session.
    assert relaunched == []
    assert terminals.get(conv_id, "claude", "main") is None


@pytest.mark.asyncio
async def test_a_message_whose_restart_outlives_its_session_starts_no_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting the session while a message restarts Claude Code answers that message 410."""
    conv_id = "aab2c3d4e5f60718293a4b5c6d7e8f90"
    checking, release = _hold_restart_attach_check(monkeypatch)
    harness = _ScriptedHarnessClient([])
    app, _closes, _relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        message = asyncio.create_task(client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE))
        await poll_until_async(checking.is_set, timeout_s=5)
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code < 300, deleted.text
        release.set()
        answered = await asyncio.wait_for(message, timeout=5)

    assert answered.status_code == 410, answered.text
    assert harness.posted_bodies == []


@pytest.mark.asyncio
async def test_a_queued_drain_behind_a_reconnect_starts_no_turn_once_the_session_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The next queued message, waiting behind Reconnect, ends quietly when its session goes."""
    conv_id = "abb2c3d4e5f60718293a4b5c6d7e8f90"
    checking, release = _hold_restart_attach_check(monkeypatch)
    harness = _ScriptedHarnessClient([])
    app, _closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        # A message buffered behind a running turn.
        app.state.active_turns[conv_id] = None
        queued = await client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE)
        assert queued.status_code < 300, queued.text
        assert app.state.session_message_buffers[conv_id]
        # The turn ends just as Reconnect takes the line; its drain then queues behind it.
        app.state.active_turns.pop(conv_id)
        reconnect = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            )
        )
        await poll_until_async(checking.is_set, timeout_s=5)
        parked = _note_line_waits(monkeypatch)
        drain = asyncio.create_task(app.state.check_and_start_next_turn(conv_id))
        await asyncio.wait_for(parked.wait(), timeout=5)
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code < 300, deleted.text
        release.set()
        await asyncio.wait_for(drain, timeout=5)
        await asyncio.wait_for(reconnect, timeout=5)

    assert harness.posted_bodies == []
    assert conv_id not in app.state.active_turns
    assert relaunched == []


@pytest.mark.asyncio
async def test_a_reconnect_sent_while_the_session_is_being_deleted_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Reconnect arriving after delete's cancellation sweep is refused, not run late."""
    conv_id = "adb2c3d4e5f60718293a4b5c6d7e8f90"
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    resources = app.state.session_resource_registry
    terminals = resources.terminal_registry
    # Hold delete just before its resource cleanup, after it swept in-flight Reconnects.
    cleaning, release = asyncio.Event(), asyncio.Event()
    cleanup_session = resources.cleanup_session

    async def _held_cleanup(session_id: str) -> None:
        cleaning.set()
        await release.wait()
        await cleanup_session(session_id)

    monkeypatch.setattr(resources, "cleanup_session", _held_cleanup)

    async with _runner_client(app) as client:
        deleting = asyncio.create_task(client.delete(f"/v1/sessions/{conv_id}"))
        await asyncio.wait_for(cleaning.wait(), timeout=5)
        # The pane is still up: a status read records it again, then sign-in completes.
        statuses["linear"] = "sign_in_required"
        await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")
        statuses["linear"] = "connected"
        reconnected = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            ),
            timeout=5,
        )
        release.set()
        deleted = await asyncio.wait_for(deleting, timeout=10)

    assert deleted.status_code < 300, deleted.text
    assert reconnected.json()["reconnect_error"] == "This session was deleted."
    # Only delete's own cleanup closed the pane; nothing relaunched it.
    assert (closes, relaunched) == ([conv_id], [])
    assert terminals.get(conv_id, "claude", "main") is None


@pytest.mark.asyncio
async def test_deleting_the_session_during_a_reconnect_relaunch_leaves_no_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete cancels and awaits a relaunch in progress before tearing the session down."""
    conv_id = "a9b2c3d4e5f60718293a4b5c6d7e8f90"
    gate = asyncio.Event()
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, relaunch_gate=gate
    )
    terminals = app.state.session_resource_registry.terminal_registry
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        reconnect = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            )
        )
        await poll_until_async(lambda: relaunched == [conv_id], timeout_s=5)
        deleted = await client.delete(f"/v1/sessions/{conv_id}")
        assert deleted.status_code < 300, deleted.text
        gate.set()
        reconnected = await asyncio.wait_for(reconnect, timeout=5)

    assert closes == [conv_id]
    assert reconnected.json()["reconnect_error"] == "This session was deleted."
    assert terminals.get(conv_id, "claude", "main") is None


@pytest.mark.asyncio
async def test_a_reconnect_past_its_deadline_frees_the_line_and_starts_nothing_late(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stalled relaunch is cancelled at the deadline, so later messages go through."""
    conv_id = "acb2c3d4e5f60718293a4b5c6d7e8f90"
    # Long enough to reach the relaunch, which then stalls past it.
    monkeypatch.setattr(runner_app, "_CLAUDE_RECONNECT_DEADLINE_S", 1.5)
    gate = asyncio.Event()
    harness = _ScriptedHarnessClient([])
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, relaunch_gate=gate, harness_client=harness
    )
    terminals = app.state.session_resource_registry.terminal_registry
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        reconnected = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            ),
            timeout=5,
        )
        assert reconnected.status_code == 200, reconnected.text
        assert reconnected.json()["reconnect_error"].startswith(
            "Claude Code didn't start again in time."
        )
        # The cancelled relaunch brings no pane up once its launch could go on.
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)  # custom-lint: disable=sleep-then-assert -- let it run
        assert terminals.get(conv_id, "claude", "main") is None
        # Reconnect retries nothing after its deadline.
        assert relaunched == [conv_id]
        message = await asyncio.wait_for(
            client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE), timeout=5
        )
        assert message.status_code < 300, message.text
        await poll_until_async(lambda: bool(harness.posted_bodies), timeout_s=5)

    assert closes == [conv_id]


@pytest.mark.asyncio
async def test_a_reconnect_waiting_in_line_is_bounded_by_its_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Reconnect queued behind a slow one expires while it waits, and answers in time."""
    import omnigent.runner.native.claude_mcp_sign_in as sign_in_module

    conv_id = "aeb2c3d4e5f60718293a4b5c6d7e8f90"
    monkeypatch.setattr(runner_app, "_CLAUDE_TIMEOUT_ANSWER_S", 0.2)
    checking, release = _hold_restart_attach_check(monkeypatch)
    harness = _ScriptedHarnessClient([])
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness
    )
    statuses["linear"] = "connected"
    # Host inventory discovery that turns slow while the queued request answers.
    slow = threading.Event()
    discover = sign_in_module.harness_sign_in_by_live_name

    def _sometimes_slow(harness_name: str) -> dict[str, str]:
        if slow.is_set():
            time.sleep(1.0)
        return discover(harness_name)

    monkeypatch.setattr(sign_in_module, "harness_sign_in_by_live_name", _sometimes_slow)
    url = f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect"

    async with _runner_client(app) as client:
        # The head request, with a long budget, holds the line in its checks.
        monkeypatch.setattr(runner_app, "_CLAUDE_RECONNECT_DEADLINE_S", 30.0)
        first = asyncio.create_task(client.post(url, json={"name": "linear"}))
        await poll_until_async(checking.is_set, timeout_s=5)
        # The queued one, with a short budget, expires while still waiting behind it.
        monkeypatch.setattr(runner_app, "_CLAUDE_RECONNECT_DEADLINE_S", 1.0)
        slow.set()
        queued = await asyncio.wait_for(
            client.post(url, json={"name": "linear"}), timeout=1.0 + 0.2 + 0.5
        )
        slow.clear()
        assert not first.done(), "the head request should still hold the line"
        release.set()
        head = await asyncio.wait_for(first, timeout=5)
        message = await asyncio.wait_for(
            client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE), timeout=5
        )
        assert message.status_code < 300, message.text
        await poll_until_async(lambda: bool(harness.posted_bodies), timeout_s=5)

    # Answered without the slow inventory read, and it restarted nothing.
    assert queued.json() == {
        "running": True,
        "partial": True,
        "servers": [],
        "reconnect_error": "Reconnect took too long. Try again.",
    }
    assert "reconnect_error" not in head.json(), head.text
    # Only the head request restarted Claude Code.
    assert (closes, relaunched) == ([conv_id], [conv_id])


@pytest.mark.asyncio
async def test_a_message_is_admitted_when_its_restart_check_stalls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hung pane probe can't hold a message's place in line past the restart deadline."""
    conv_id = "afb2c3d4e5f60718293a4b5c6d7e8f90"
    monkeypatch.setattr(runner_app, "_CLAUDE_AUTO_RESTART_DEADLINE_S", 0.5)
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"
    pane = app.state.session_resource_registry.terminal_registry.get(conv_id, "claude", "main")
    hung = asyncio.Event()

    async def _hung_probe() -> bool:
        await hung.wait()
        return True

    pane.is_alive = _hung_probe  # type: ignore[method-assign]

    async with _runner_client(app) as client:
        try:
            first = await asyncio.wait_for(
                client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE), timeout=5
            )
            # The line is free: a later message is admitted too.
            later = await asyncio.wait_for(
                client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE), timeout=5
            )
        finally:
            hung.set()

    assert first.status_code < 300, first.text
    assert later.status_code < 300, later.text
    assert (closes, relaunched) == ([], [])


@pytest.mark.asyncio
async def test_a_cancelled_ensure_keeps_the_running_launch_sign_in_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ensure cancelled while another launch holds the lock leaves that launch's record."""
    conv_id = "b0b2c3d4e5f60718293a4b5c6d7e8f90"
    gate = asyncio.Event()
    app, _closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, relaunch_gate=gate
    )
    # linear makes the pane eligible; notion still needs sign-in as the relaunch starts.
    statuses.update({"linear": "connected", "notion": "sign_in_required"})
    ensure = {"terminal": "claude", "session_key": "main", "ensure_native_terminal": True}
    on_lock = _note_launch_lock_waits(app, conv_id)

    async with _runner_client(app) as client:
        reconnect = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect", json={"name": "linear"}
            )
        )
        await poll_until_async(lambda: relaunched == [conv_id], timeout_s=5)
        waiting = asyncio.create_task(
            client.post(f"/v1/sessions/{conv_id}/resources/terminals", json=ensure)
        )
        await asyncio.wait_for(on_lock.wait(), timeout=5)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        # Sign-in finishes before anything checks the new pane.
        statuses["notion"] = "connected"
        gate.set()
        assert (await asyncio.wait_for(reconnect, timeout=5)).status_code == 200
        status = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert [server["name"] for server in status.json()["servers"]] == ["notion"], status.text


@pytest.mark.asyncio
async def test_a_cancelled_reconnect_does_not_hold_up_later_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Reconnect cancelled while it waits in line gives up its place."""
    conv_id = "a4b2c3d4e5f60718293a4b5c6d7e8f90"
    gate = asyncio.Event()
    harness = _ScriptedHarnessClient([])
    app, _closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, relaunch_gate=gate, harness_client=harness
    )
    statuses["linear"] = "connected"
    url = f"/v1/sessions/{conv_id}/live-mcp-servers/reconnect"

    async with _runner_client(app) as client:
        first = asyncio.create_task(client.post(url, json={"name": "linear"}))
        await poll_until_async(lambda: relaunched == [conv_id], timeout_s=5)
        waiting = _note_line_waits(monkeypatch)
        second = asyncio.create_task(client.post(url, json={"name": "linear"}))
        await asyncio.wait_for(waiting.wait(), timeout=5)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        gate.set()
        await first
        message = await asyncio.wait_for(
            client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE), timeout=5
        )
        assert message.status_code < 300, message.text
        await poll_until_async(lambda: bool(harness.posted_bodies), timeout_s=5)


@pytest.mark.asyncio
async def test_mcp_still_answers_for_claude_native_after_an_agent_cache_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An MCP edit resets the runner's agent cache; /mcp must not lose the session's harness."""
    conv_id = "a5b2c3d4e5f60718293a4b5c6d7e8f90"
    app, _closes, _relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id
    )
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        reset = await client.post(f"/v1/sessions/{conv_id}/agent-cache/reset", json={})
        assert reset.status_code < 300, reset.text
        resp = await client.get(f"/v1/sessions/{conv_id}/live-mcp-servers")

    assert [server["name"] for server in resp.json()["servers"]] == ["linear"]


@pytest.mark.asyncio
async def test_a_message_never_restarts_a_claude_pane_left_from_another_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session now running claude-sdk can keep its old Claude pane, which isn't its own."""
    conv_id = "a6b2c3d4e5f60718293a4b5c6d7e8f90"
    harness = _ScriptedHarnessClient([])
    app, closes, relaunched, statuses = await _open_claude_native_session_before_sign_in(
        monkeypatch, tmp_path, conv_id, harness_client=harness, harness="claude-sdk"
    )
    # The old pane started before the sign-in, so only the harness keeps it running.
    old_pane = app.state.session_resource_registry.terminal_registry.get(conv_id, "claude", "main")
    app.state.claude_mcp_sign_in.signed_in_since_launch(conv_id, old_pane)
    statuses["linear"] = "connected"

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=_MESSAGE)
        assert resp.status_code < 300, resp.text
        await poll_until_async(lambda: bool(harness.posted_bodies), timeout_s=5)

    assert (closes, relaunched) == ([], [])
