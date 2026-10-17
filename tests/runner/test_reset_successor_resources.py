"""A reset closes its captured resources while a fresh launch keeps ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import terminal_resource_view
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner.native import orchestration
from omnigent.terminals import registry as registry_module
from omnigent.terminals.registry import TerminalListEntry, TerminalRegistry
from tests.runner.conftest import _runner_client
from tests.runner.helpers import RunningFlagTerminalInstance
from tests.runner.test_reset_state_fences_session_init_terminal_start import _build_app
from tests.runner.test_terminal_reset_fence import _RecordingTerminal


@pytest.mark.parametrize("include_receipt", [False, True])
@pytest.mark.asyncio
async def test_stale_view_cannot_close_a_new_instance_reusing_the_same_tmux_coordinates(
    tmp_path: Path, include_receipt: bool
) -> None:
    registry = TerminalRegistry()
    old = _RecordingTerminal(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "shared.sock",
        private_dir=tmp_path / "old",
        running=True,
    )
    fresh = _RecordingTerminal(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "shared.sock",
        private_dir=tmp_path / "fresh",
        running=True,
    )
    registry._by_conversation["session-a"] = {("codex", "main"): old}
    view = terminal_resource_view("session-a", TerminalListEntry("codex", "main", old))
    assert view.metadata["terminal_instance_id"] == old.diagnostic_id
    assert old.diagnostic_id != fresh.diagnostic_id
    if not include_receipt:
        view.metadata.pop("terminal_instance_id", None)
    [detached] = registry.detach_conversation("session-a")
    assert detached.instance is old
    registry._by_conversation["session-a"] = {("codex", "main"): fresh}
    events: list[dict[str, Any]] = []
    from omnigent.runner.resource_registry import SessionResourceRegistry

    context = orchestration.NativeLaunchContext(
        session_id="session-a",
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        publish_event=lambda session_id, event: events.append(event),
    )
    await orchestration._discard_terminal_reset_mid_launch(
        context, terminal_name="codex", view=view
    )
    assert registry.get("session-a", "codex", "main") is fresh
    assert fresh.running
    assert events == []


@pytest.mark.parametrize("old_backend", [False, True])
@pytest.mark.asyncio
async def test_reset_cannot_close_resources_installed_during_its_old_pane_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_backend: bool
) -> None:
    session_id = "session-reset-successor"
    closing, release = asyncio.Event(), asyncio.Event()
    created: list[RunningFlagTerminalInstance] = []

    class Terminal(RunningFlagTerminalInstance):
        async def launch(self, *, cwd: Path | None = None) -> None:
            self.running = True

        async def close(self) -> None:
            if self is created[0]:
                closing.set()
                await release.wait()
            self.running = False

    class Backend:
        closed = False

        async def close(self) -> None:
            self.closed = True

    def create(name: str, key: str, spec: Any, **kwargs: Any) -> TerminalCreateResult:
        instance = Terminal(
            name=name,
            session_key=key,
            socket_path=tmp_path / f"pane-{len(created)}.sock",
            private_dir=tmp_path / f"pane-{len(created)}",
            running=False,
        )
        created.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_module, "create_terminal_instance", create)
    app, registry, resources = _build_app(tmp_path, "codex-native", {})
    spec = TerminalEnvSpec(command="bash")
    await registry.launch(session_id, "bash", "main", spec)
    old, fresh = Backend(), Backend()
    if old_backend:
        orchestration._AUTO_CODEX_APP_SERVERS[session_id] = old  # type: ignore[assignment]
    reset: asyncio.Task[Any] | None = None
    try:
        async with _runner_client(app) as client:
            reset = asyncio.create_task(client.post(f"/v1/sessions/{session_id}/reset-state"))
            await asyncio.wait_for(closing.wait(), 5)
            pane = await registry.launch(session_id, "bash", "main", spec)
            orchestration._AUTO_CODEX_APP_SERVERS[session_id] = fresh  # type: ignore[assignment]
            release.set()
            response = await asyncio.wait_for(reset, 5)
            assert response.status_code == 200
            assert {
                "pane_is_fresh": registry.get(session_id, "bash", "main") is pane,
                "pane_running": pane.running,
                "backend_is_fresh": orchestration._AUTO_CODEX_APP_SERVERS.get(session_id) is fresh,
                "backend_running": not fresh.closed,
                "old_backend_closed": old.closed,
            } == {
                "pane_is_fresh": True,
                "pane_running": True,
                "backend_is_fresh": True,
                "backend_running": True,
                "old_backend_closed": old_backend,
            }
    finally:
        release.set()
        if reset is not None:
            await asyncio.gather(reset, return_exceptions=True)
        await orchestration.teardown_codex_native_app_server(session_id)
        await resources.cleanup_session(session_id)
