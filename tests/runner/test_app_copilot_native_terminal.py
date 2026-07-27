"""Runner-level tests for copilot-native terminal auto-creation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.copilot_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    bridge_dir_for_session_id,
    read_tmux_info,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.runner.native.orchestration import _auto_create_copilot_terminal
from omnigent.runner.resource_registry import COPILOT_NATIVE_TERMINAL_ROLE
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "conv_copilot_native_1"


@dataclass
class _FakeTerminalInstance:
    """Minimal stand-in for the tmux-backed terminal the bridge is told about."""

    socket_path: Path
    tmux_target: str = "copilot:0.0"
    running: bool = True


@dataclass
class _FakeTerminalRegistry:
    """Terminal registry exposing only the lookup the copilot path performs."""

    instance: _FakeTerminalInstance | None

    def get(self, session_id: str, terminal_name: str, session_key: str) -> Any:
        """Return the single fake instance regardless of the lookup key."""
        del session_id, terminal_name, session_key
        return self.instance


@dataclass
class _FakeResourceRegistry:
    """Records the launch and any compensating close."""

    terminal_registry: _FakeTerminalRegistry | None
    captured: dict[str, Any] = field(default_factory=dict)
    closed: list[str] = field(default_factory=list)

    async def launch_required_terminal(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        resource_role: str | None = None,
        parent_os_env: Any = None,
    ) -> SessionResourceView:
        """Record the launch and return a terminal resource view."""
        del parent_os_env
        self.captured["terminal_name"] = terminal_name
        self.captured["session_key"] = session_key
        self.captured["resource_role"] = resource_role
        self.captured["spec"] = spec
        return SessionResourceView(
            id="terminal_copilot_main",
            type="terminal",
            session_id=session_id,
            name="copilot:main",
            metadata={"terminal_name": "copilot", "session_key": "main", "running": True},
        )

    async def close_terminal(self, session_id: str, terminal_id: str) -> bool:
        """Record a teardown request."""
        del session_id
        self.closed.append(terminal_id)
        return True


@pytest.fixture
def copilot_launch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bridge root at tmp_path and stub the binary + launch config."""
    import omnigent.copilot_native as copilot_native
    import omnigent.copilot_native_bridge as copilot_native_bridge
    from omnigent.runner.native import orchestration

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(copilot_native_bridge, "_BRIDGE_ROOT", tmp_path / "copilot-bridge")
    monkeypatch.setattr(copilot_native, "resolve_copilot_executable", lambda: "copilot")
    # No ambient/Omnigent credential unless a test opts in.
    monkeypatch.setattr(
        "omnigent.onboarding.copilot_auth.resolve_copilot_github_token", lambda: None
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_launch_config(**_kwargs: Any) -> Any:
        return orchestration._PiNativeLaunchConfig(
            workspace=workspace,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", _fake_launch_config)
    # The forwarder is exercised by its own tests; keep the launch path offline.
    monkeypatch.setattr(orchestration, "_register_auto_forwarder_task", lambda *_a, **_k: None)
    return tmp_path


@pytest.mark.asyncio
async def test_auto_create_copilot_terminal_publishes_tmux_target(
    copilot_launch_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch publishes a readable ``tmux.json`` carrying the session id.

    Regression guard: ``write_tmux_target`` is the only native bridge whose
    ``session_id`` is keyword-only with no default, and the call site was
    copied from a peer that has no such parameter. Omitting it raised
    ``TypeError`` after the pane was already live, so no session could ever
    inject. The executor's ``_session_is_active`` compares this exact id.
    """
    from omnigent.copilot_native_bridge import bridge_dir_for_session_id

    socket = copilot_launch_env / "tmux.sock"
    registry = _FakeResourceRegistry(
        terminal_registry=_FakeTerminalRegistry(_FakeTerminalInstance(socket_path=socket))
    )
    published: list[dict[str, Any]] = []

    await _auto_create_copilot_terminal(
        _SESSION_ID,
        registry,  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    assert registry.captured["terminal_name"] == "copilot"
    assert registry.captured["resource_role"] == COPILOT_NATIVE_TERMINAL_ROLE
    info = read_tmux_info(bridge_dir_for_session_id(_SESSION_ID))
    assert info is not None
    assert info["session_id"] == _SESSION_ID
    assert info["socket_path"] == str(socket)
    assert info["tmux_target"] == "copilot:0.0"
    assert any(evt.get("type") == "session.resource.created" for evt in published)
    assert registry.closed == []
    del monkeypatch


@pytest.mark.asyncio
async def test_auto_create_copilot_terminal_tears_down_on_publish_failure(
    copilot_launch_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bridge publication closes the pane instead of orphaning it.

    The terminal is registered before the bridge file is written, so a
    surviving pane would be found by a later ensure — which then skips the
    setup that failed and leaves the session permanently unable to inject.
    """
    from omnigent.runner.native import orchestration

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("bridge dir is read-only")

    monkeypatch.setattr(orchestration, "write_tmux_target", _boom, raising=False)
    monkeypatch.setattr("omnigent.copilot_native_bridge.write_tmux_target", _boom)

    socket = copilot_launch_env / "tmux.sock"
    registry = _FakeResourceRegistry(
        terminal_registry=_FakeTerminalRegistry(_FakeTerminalInstance(socket_path=socket))
    )

    with pytest.raises(OSError):
        await _auto_create_copilot_terminal(
            _SESSION_ID,
            registry,  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )

    assert registry.closed == ["terminal_copilot_main"]


@pytest.mark.asyncio
async def test_copilot_native_spawn_env_carries_the_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness process is spawned with the native bridge env vars.

    ``CopilotNativeExecutor`` raises on construction without
    ``HARNESS_COPILOT_NATIVE_BRIDGE_DIR``, so the first web turn died before
    anything reached tmux. The copilot branch cannot use the peer
    ``spawn_env is None`` idiom: the spec-derived dict is non-empty, so the
    bridge vars have to be merged in rather than substituted.
    """
    import omnigent.copilot_native_bridge as copilot_native_bridge

    monkeypatch.setattr(copilot_native_bridge, "_BRIDGE_ROOT", tmp_path / "copilot-bridge")
    session_id = "0f0d1c3b8a5e4f6c9d2b7a1e4c8f3b6d"
    spec = AgentSpec(
        spec_version=1,
        name="copilot-native-agent",
        executor=ExecutorSpec(config={"harness": "copilot-native"}),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=tmp_path)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    _conv, harness, env = pm.get_client_calls[-1]
    assert harness == "copilot-native"
    assert env is not None
    assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id(session_id))
    assert env[REQUEST_SESSION_ID_ENV_VAR] == session_id
    # The SDK harness's vars are not part of the native contract; the token in
    # particular must not be handed to a process that never reads it.
    assert not [key for key in env if key.startswith("HARNESS_COPILOT_GITHUB_TOKEN")]
