"""A live-but-unadvertised Claude pane must be re-advertised, not injected blind.

Pane liveness does not imply deliverability: a starved runner can leave the
Claude pane alive while its ``tmux.json`` advertisement is missing (the
launch-time write lagged or was lost). The turn/inject-time self-heal used to
return early on ``is_alive()`` without checking the advertisement, so the
injection raced an absent target and hard-failed after the advertisement wait
("Claude terminal tmux target is not advertised yet"). These tests plant
exactly that state -- a live registry instance with no ``tmux.json`` -- and
assert the heal rewrites the advertisement from the live instance instead of
failing the injection, and never tears down the live pane to do it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    _TMUX_FILE,
    bridge_dir_for_conversation_id,
    tmux_target_advertised,
)
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


def _plant_live_unadvertised_pane(
    registry: TerminalRegistry,
    conv_id: str,
    tmp_path: Path,
    bridge_dir: Path,
) -> TerminalInstance:
    """Register a live Claude pane whose tmux target is not advertised.

    This is the reproduced gap: the registry instance is alive, but the
    bridge directory carries no ``tmux.json`` for the injection to read.

    :returns: The planted live instance.
    """
    live_sock = tmp_path / "omnigent-terminal-live" / "tmux.sock"
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=live_sock,
        private_dir=tmp_path / "live_private",
    )
    instance.running = True
    (tmp_path / "live_private").mkdir(exist_ok=True)

    async def _alive() -> bool:
        return True

    instance.is_alive = _alive  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("claude", "main"): instance}
        registry._instance_locks[(conv_id, "claude", "main")] = threading.Lock()
    # The fault: nothing advertises the live pane's tmux target.
    (bridge_dir / _TMUX_FILE).unlink(missing_ok=True)
    return instance


async def _open_claude_native_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conv_id: str,
    auto_create_calls: list[str],
) -> tuple[Any, TerminalRegistry]:
    """Build a runner with a real terminal registry and a claude-native session.

    ``_auto_create_claude_terminal`` is stubbed so session create does not
    spawn a real Claude TUI; recording its calls lets tests assert a live
    pane was never recreated.
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
async def test_live_unadvertised_pane_is_readvertised_before_inject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event: dict[str, Any],
    expected_command: str,
) -> None:
    """Live pane + missing tmux.json must re-advertise and inject, not fail.

    ``inject_slash_command`` keeps the real advertisement dependency (it
    waits on ``tmux.json`` exactly like the production inject), so without
    the re-advertise heal the handler answers 503 -- the same
    "tmux target is not advertised" hard-fail the web turn surfaced.
    """
    conv_id = "e5f60718293a4b5c6d7e8f901a2b3c4d"
    if event["type"] == "effort_change":
        conv_id = "f60718293a4b5c6d7e8f901a2b3c4d5e"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_unadvertised_pane(registry, conv_id, tmp_path, bridge_dir)

    captured: list[tuple[str, dict[str, str]]] = []

    def _inject_requires_advertisement(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint
        # Keep the real inject's advertisement dependency: this raises
        # TmuxSessionNotAdvertised when tmux.json is still missing.
        info = claude_native_bridge._wait_for_tmux_info(inject_bridge_dir, timeout_s=0.2)
        captured.append((command, info))

    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_requires_advertisement
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=event)

    assert resp.status_code == 204, (
        f"live-but-unadvertised pane must be re-advertised before inject; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert auto_create_calls == [], (
        "a live pane must be re-advertised in place, never torn down and recreated"
    )
    assert captured == [
        (
            expected_command,
            {"socket_path": str(instance.socket_path), "tmux_target": instance.tmux_target},
        )
    ], f"inject must see the live pane's re-advertised target; got {captured!r}"
    # The heal is durable: the advertisement survives for subsequent injects.
    assert tmux_target_advertised(bridge_dir)


@pytest.mark.asyncio
async def test_live_advertised_pane_advertisement_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live pane whose advertisement is present must not be rewritten."""
    conv_id = "0718293a4b5c6d7e8f901a2b3c4d5e6f"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_unadvertised_pane(registry, conv_id, tmp_path, bridge_dir)
    # Advertise a valid target up front; the heal must keep it verbatim.
    claude_native_bridge.write_tmux_target(
        bridge_dir,
        socket_path=instance.socket_path,
        tmux_target=instance.tmux_target,
    )
    advertised_before = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")

    def _inject_records(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del inject_bridge_dir, command, timeout_s, auto_confirm, confirm_hint

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _inject_records)

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == []
    assert (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8") == advertised_before, (
        "a valid advertisement must not be rewritten on every inject"
    )
