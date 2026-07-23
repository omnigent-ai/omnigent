"""Tests for native terminal auto-creation across supported harnesses."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import (
    claude_native_bridge,
    cursor_native_bridge,
    kiro_native_bridge,
)
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
)
from omnigent.antigravity_native_bridge import (
    prepare_bridge_dir as prepare_antigravity_bridge_dir,
)
from omnigent.antigravity_native_bridge import (
    write_bridge_state as write_antigravity_bridge_state,
)
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_permission_hook_config,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    ResolvedSpec,
    _agent_os_env_from_spec,
    _auto_create_claude_terminal,
    _auto_create_cursor_terminal,
    _auto_create_kiro_terminal,
    _auto_create_pi_terminal,
    _KiroNativeLaunchConfig,
    _load_claude_launch_metadata,
    _log_terminal_lookup_miss,
    _PiNativeLaunchConfig,
    _publish_native_terminal_start_error,
    _publish_terminal_pending,
    _refresh_claude_permission_hook_auth,
    _terminal_lookup_miss_log_state,
)
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    KIRO_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.runner.session_init_protocol import RunnerSessionInitEnvelope
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_claude_permission_hook_snapshot_refreshes_without_binding_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent runner refreshes delegated hook auth in the bridge file."""
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir("refresh-hook-auth", workspace=tmp_path)
    claude_native_bridge.build_hook_settings(
        bridge_dir,
        ap_server_url="https://omnigent.example.com",
        ap_auth_headers={"Authorization": "Bearer old-token"},
    )

    task = asyncio.create_task(
        _refresh_claude_permission_hook_auth(
            bridge_dir=bridge_dir,
            server_url="https://omnigent.example.com",
            auth_token_factory=lambda: "fresh-delegated-token",
            refresh_interval_s=0.01,
        )
    )
    try:

        async def _wait_for_refresh() -> None:
            while True:
                config = read_permission_hook_config(bridge_dir)
                if config.get("ap_auth_headers", {}).get("Authorization") == (
                    "Bearer fresh-delegated-token"
                ):
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait_for_refresh(), timeout=1.0)
    finally:
        task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_auth_headers"]["Authorization"] == "Bearer fresh-delegated-token"


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_launches_required_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native auto-create must launch a *required* terminal.

    Regression guard for a missed call site. The pi-native runtime *is* the
    terminal process (parity with claude-native), so when the lifecycle-aware
    launch API replaced ``launch_terminal`` with ``launch_required_terminal`` /
    ``launch_auxiliary_terminal``, ``_auto_create_pi_terminal`` had to move to
    ``launch_required_terminal``. The fake registry below exposes *only*
    ``launch_required_terminal`` (no ``launch_terminal``), so a stale call site
    raises ``AttributeError`` here in CI instead of crashing in production the
    moment a real pi-native session boots.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.pi_native as pi_native
    import omnigent.pi_native_bridge as pi_native_bridge
    import omnigent.pi_native_credentials as pi_native_credentials

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    # The lifecycle of the launch — not the binary or credentials — is under
    # test, so neither a real Pi install nor a configured provider is needed.
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    # Accept the ``model`` kwarg the runner now threads through (the spec model
    # → models.json path); None still skips provider injection here.
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    # Skip the GET /v1/sessions round-trip: hand the flow a ready launch
    # config pointing at the tmp workspace.
    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Records the launch; exposes ONLY the required-terminal launch API."""

        terminal_registry = None

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
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    await _auto_create_pi_terminal(
        "47f049b9d13df4db397c7f46859b825f",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Required lifecycle (parity with claude-native), correct terminal identity.
    assert captured["terminal_name"] == "pi"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == PI_NATIVE_TERMINAL_ROLE
    assert captured["spec"].command == "pi"
    # The fresh terminal is surfaced on the live stream for the Terminal toggle.
    assert any(evt.get("type") == "session.resource.created" for evt in published)


@pytest.mark.asyncio
async def test_auto_create_kiro_terminal_launches_required_terminal_with_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-native auto-create launches the TUI and session forwarder."""
    import omnigent.kiro_native as kiro_native

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(
        kiro_native,
        "resolve_kiro_executable",
        lambda **_kwargs: "/usr/bin/kiro-cli",
    )
    forwarder_calls: list[dict[str, Any]] = []
    permission_mirror_calls: list[dict[str, Any]] = []

    async def _fake_supervise_kiro_session_forwarder(**kwargs: Any) -> None:
        forwarder_calls.append(kwargs)

    async def _fake_supervise_kiro_permission_mirror(**kwargs: Any) -> None:
        permission_mirror_calls.append(kwargs)

    relay_calls: list[dict[str, Any]] = []

    async def _spy_ensure_relay(session_id: str, **kwargs: Any) -> None:
        relay_calls.append({"session_id": session_id, **kwargs})

    monkeypatch.setattr(
        "omnigent.kiro_native_session_forwarder.supervise_kiro_session_forwarder",
        _fake_supervise_kiro_session_forwarder,
    )
    monkeypatch.setattr(
        "omnigent.kiro_native_permissions.supervise_kiro_permission_mirror",
        _fake_supervise_kiro_permission_mirror,
    )

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path,
            terminal_launch_args=["--model", "auto", "--effort", "high", "hello"],
            external_session_id="kiro-session-123",
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Records the launch; exposes ONLY the required-terminal launch API."""

        terminal_registry = None

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
            del parent_os_env
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_kiro_main",
                type="terminal",
                session_id=session_id,
                name="kiro:main",
                metadata={"terminal_name": "kiro", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    await _auto_create_kiro_terminal(
        "823dbd1aab969b5a813fac59bb977a77",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
        ensure_comment_relay=_spy_ensure_relay,
    )
    for _ in range(20):
        if forwarder_calls and permission_mirror_calls:
            break
        await asyncio.sleep(0)

    spec = captured["spec"]
    assert captured["terminal_name"] == "kiro"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == KIRO_NATIVE_TERMINAL_ROLE
    assert spec.command == "/usr/bin/kiro-cli"
    assert spec.args == [
        "chat",
        "--tui",
        "--resume-id",
        "kiro-session-123",
        "--model",
        "auto",
        "--effort",
        "high",
        "hello",
    ]
    assert spec.inherit_env is False
    assert "OPENAI_API_KEY" not in spec.env
    assert "OPENAI_API_KEY" in spec.env_unset
    assert spec.env[kiro_native_bridge.KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] == str(
        kiro_native_bridge.bridge_dir_for_session_id("823dbd1aab969b5a813fac59bb977a77")
    )
    assert spec.env[kiro_native_bridge.KIRO_ACP_RECORD_PATH_ENV_VAR] == str(
        kiro_native_bridge.acp_record_path(
            kiro_native_bridge.bridge_dir_for_session_id("823dbd1aab969b5a813fac59bb977a77")
        )
    )
    assert any(evt.get("type") == "session.resource.created" for evt in published)
    assert forwarder_calls
    assert forwarder_calls[0]["base_url"] == "http://127.0.0.1:6767"
    assert forwarder_calls[0]["session_id"] == "823dbd1aab969b5a813fac59bb977a77"
    assert forwarder_calls[0]["agent_name"] == "kiro-native-ui"
    assert forwarder_calls[0]["workspace"] == str(tmp_path)
    assert permission_mirror_calls
    assert permission_mirror_calls[0]["base_url"] == "http://127.0.0.1:6767"
    assert permission_mirror_calls[0]["session_id"] == "823dbd1aab969b5a813fac59bb977a77"
    # The Omnigent MCP tool relay is seeded for this session's bridge dir.
    assert relay_calls == [
        {
            "session_id": "823dbd1aab969b5a813fac59bb977a77",
            "explicit_bridge_dir": kiro_native_bridge.bridge_dir_for_session_id(
                "823dbd1aab969b5a813fac59bb977a77"
            ),
            "await_notify": False,
        }
    ]
    # And the Omnigent MCP server is declared in the workspace-scoped kiro config.
    workspace_mcp = tmp_path / ".kiro" / "settings" / "mcp.json"
    assert workspace_mcp.exists()
    mcp_servers = json.loads(workspace_mcp.read_text())["mcpServers"]
    assert "serve-mcp" in mcp_servers["omnigent"]["args"]


@pytest.mark.asyncio
async def test_auto_create_kiro_terminal_skips_mcp_wiring_without_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a comment-relay callback, the Omnigent MCP is NOT wired.

    The workspace mcp.json write + relay seed are gated on ``server_client`` AND
    ``ensure_comment_relay`` together, so serve-mcp never launches with no relay
    to route calls back to. With ``ensure_comment_relay`` absent the gate must
    short-circuit: no workspace ``mcp.json`` is written.
    """
    import omnigent.kiro_native as kiro_native

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(
        kiro_native,
        "resolve_kiro_executable",
        lambda **_kwargs: "/usr/bin/kiro-cli",
    )

    async def _noop_supervise(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.kiro_native_session_forwarder.supervise_kiro_session_forwarder",
        _noop_supervise,
    )
    monkeypatch.setattr(
        "omnigent.kiro_native_permissions.supervise_kiro_permission_mirror",
        _noop_supervise,
    )
    mcp_writes: list[Any] = []
    monkeypatch.setattr(
        kiro_native_bridge,
        "write_kiro_workspace_mcp_config",
        lambda *args, **kwargs: mcp_writes.append((args, kwargs)),
    )

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path,
            terminal_launch_args=["hello"],
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, **_kwargs: Any
        ) -> SessionResourceView:
            return SessionResourceView(
                id="terminal_kiro_main",
                type="terminal",
                session_id=session_id,
                name="kiro:main",
                metadata={"terminal_name": "kiro", "session_key": "main", "running": True},
            )

    # No ``ensure_comment_relay`` argument -> the MCP wiring gate stays closed.
    await _auto_create_kiro_terminal(
        "17b3209fb5684c628f95edee0042e455",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    assert mcp_writes == []
    assert not (tmp_path / ".kiro" / "settings" / "mcp.json").exists()


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native auto-create must honour the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the pi-native auto-create path
    built a fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox``
    and passed no ``parent_os_env``, so ``launch_required_terminal`` fell back
    to ``_default_sandbox_for_platform`` (``linux_bwrap`` on Linux) and ignored
    the agent YAML.  A pi-native agent that declares
    ``os_env.sandbox.type: none`` was wrongly forced into bwrap, which failed
    on a hardened host.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's ``os_env``
    is threaded through as the launch ``parent_os_env`` so the rest of the
    policy (egress_rules / env_passthrough) is also inherited.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.pi_native as pi_native
    import omnigent.pi_native_bridge as pi_native_bridge
    import omnigent.pi_native_credentials as pi_native_credentials
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    # Accept the ``model`` kwarg the runner now threads through (the spec model
    # → models.json path); None still skips provider injection here.
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

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
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    # An agent that declares sandbox: none (runs unconfined; outer
    # container/VM is the security boundary).
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="pi_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi-native", "model": "pi-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_pi_terminal(
        "28a25c47fe4fd8ccde95c80bab47c1c7",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_required_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env


@pytest.mark.asyncio
@pytest.mark.parametrize("use_envelope", [False, True], ids=["legacy", "envelope"])
async def test_auto_create_claude_terminal_passes_session_effort(
    use_envelope: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch reads session effort and passes ``--effort``.

    When the Omnigent server returns a session with a persisted
    ``reasoning_effort``, the auto-create path must include
    ``--effort <value>`` in the Claude CLI args so the terminal
    starts at the user's chosen effort level.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec."""

        terminal_registry = None

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
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    # Fake Omnigent server client that returns a session with reasoning_effort.
    def _handle_request(_request: httpx.Request) -> httpx.Response:
        if use_envelope:
            raise AssertionError("envelope terminal startup made a legacy HTTP callback")
        return httpx.Response(
            200,
            json={"reasoning_effort": "high", "labels": {}},
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    session_id = "f89fd41f6eefee45b2117ac0fcbc73fa"
    session_init = (
        RunnerSessionInitEnvelope.model_validate(
            {
                "protocol_version": 2,
                "server_version": "0.6.0.dev0",
                "session_id": session_id,
                "agent_id": "agent",
                "snapshot": {
                    "created_at": 10,
                    "updated_at": 11,
                    "workspace": str(tmp_path),
                    "reasoning_effort": "high",
                    "labels": {},
                },
            }
        )
        if use_envelope
        else None
    )

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        session_init=session_init,
    )

    args = captured["spec"].args
    assert "--effort" in args
    effort_idx = args.index("--effort")
    assert args[effort_idx + 1] == "high"

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_claude_launch_metadata_envelope_never_calls_server() -> None:
    """Resume configuration comes entirely from a v2 envelope."""

    class _NoCallbackClient:
        async def get(self, path: str, **_kwargs: Any) -> Any:
            raise AssertionError(f"envelope path made legacy callback: {path}")

    envelope = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": "conv_resume",
            "agent_id": "agent_resume",
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": "/tmp/worktree",
                "reasoning_effort": "high",
                "model_override": "claude-opus-4-7",
                "terminal_launch_args": ["--verbose"],
                "external_session_id": "claude-session-id",
                "labels": {"omnigent.fork.carry_history": "1"},
            },
        }
    )

    metadata = await _load_claude_launch_metadata(
        server_client=_NoCallbackClient(),  # type: ignore[arg-type]
        session_id="conv_resume",
        session_init=envelope,
    )

    assert metadata.reasoning_effort == "high"
    assert metadata.model_override == "claude-opus-4-7"
    assert metadata.terminal_launch_args == ["--verbose"]
    assert metadata.external_session_id == "claude-session-id"
    assert metadata.fork_carry_history is True


def test_agent_os_env_from_spec_unwraps_resolved_and_handles_none() -> None:
    """
    ``_agent_os_env_from_spec`` reads ``os_env`` through the resolved wrapper.

    The auto-create terminals receive either a bare ``AgentSpec`` or a
    ``ResolvedSpec`` wrapping one (the codex path passes ``ResolvedSpec``).
    The helper must unwrap the latter and return the inner ``os_env``, and
    return ``None`` when there is no spec — so the launch falls back to the
    platform default only when there is genuinely no agent policy to honour.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    os_env = OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    bare = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
        os_env=os_env,
    )

    # Bare AgentSpec: returned directly.
    assert _agent_os_env_from_spec(bare) is os_env
    # ResolvedSpec wrapper: unwrapped to the inner spec's os_env.
    assert _agent_os_env_from_spec(ResolvedSpec(spec=bare, workdir=None)) is os_env
    # No spec at all: None (caller then uses the platform default).
    assert _agent_os_env_from_spec(None) is None
    # AgentSpec without an os_env block: None.
    no_os_env = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
    )
    assert _agent_os_env_from_spec(no_os_env) is None


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned Claude terminal honours the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the auto-create path built a
    fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox`` and
    passed no ``parent_os_env``, so ``launch_terminal`` fell back to
    ``_default_sandbox_for_platform`` (``linux_bwrap`` / ``darwin_seatbelt``)
    and ignored the agent YAML. A claude-native agent that declares
    ``os_env.sandbox.type: none`` (e.g. Polly's ``claude_code`` worker,
    which relies on the outer container/VM as the boundary) was wrongly
    forced into bwrap, which then failed to start in a hardened container.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's
    ``os_env`` is threaded through as the launch ``parent_os_env`` so the
    rest of the policy (egress_rules / env_passthrough) is inherited too.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

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
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    # An agent that declares sandbox: none (runs unconfined; the outer
    # container/VM is the boundary) — exactly Polly's coding sub-agents.
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="claude_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-native", "model": "claude-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_claude_terminal(
        "e27fc87ef2a8d798895ce8c1e66db82d",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_terminal will fall "
        "back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_injects_ucode_gateway_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned launch injects the ucode Databricks gateway config.

    On the daemon / web-UI path the runner — not the CLI — launches
    Claude, so it must reproduce the gateway auth the CLI normally
    injects: the ``ANTHROPIC_BASE_URL`` env, the ``apiKeyHelper`` token
    command, and the gateway default model. The runner derives this from
    the user's provider config (here the legacy global ``auth:`` block —
    the ambient ``DATABRICKS_CONFIG_PROFILE`` env var deliberately no
    longer steers credentials). Without it, Claude would launch with
    empty env and no token and could not reach the Databricks model —
    the exact regression that blocked daemon-routing.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.claude_native import ClaudeNativeUcodeConfig

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    # The supported credential source for a host-spawned runner: the
    # global config's ``auth:`` block (written by ``omnigent setup``),
    # isolated to a temp config home so the developer's real config
    # can't leak in.
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: databricks\n  profile: test-profile\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    gateway_env = {"ANTHROPIC_BASE_URL": "https://gw.example/anthropic"}
    ucode = ClaudeNativeUcodeConfig(
        env=dict(gateway_env),
        api_key_helper="databricks auth token --fake-helper",
        model="databricks-claude-opus-4-7",
    )
    # The runner imports ``_ucode_config_for_profile`` from
    # ``omnigent.claude_native`` per call, so patch it at the source.
    monkeypatch.setattr(
        "omnigent.claude_native._ucode_config_for_profile",
        lambda profile: ucode,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec."""

        terminal_registry = None

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
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )
    recorded_configs: dict[str, ClaudeNativeUcodeConfig | None] = {}

    await _auto_create_claude_terminal(
        "13efa494411f3ae60211e6be5635062a",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        record_launch_config=recorded_configs.__setitem__,
    )

    spec = captured["spec"]
    # The gateway env points ``claude`` at the Databricks gateway, and
    # ENABLE_TOOL_SEARCH forces Claude Code to defer MCP tool schemas
    # instead of loading all 200+ bridge tools into startup context.
    assert spec.env == {
        **gateway_env,
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
    }
    assert spec.command == "claude"
    # The gateway default model is applied (no per-session override here).
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "databricks-claude-opus-4-7"
    # The apiKeyHelper threaded into the Claude settings augment so the
    # gateway token command is registered.
    assert "databricks auth token --fake-helper" in " ".join(spec.args)
    assert recorded_configs == {"13efa494411f3ae60211e6be5635062a": ucode}

    await fake_client.aclose()


async def _run_auto_create_cursor_terminal(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_spec: AgentSpec | None,
    terminal_launch_args: list[str] | None,
) -> Any:
    """Drive ``_auto_create_cursor_terminal`` and return the captured launch spec.

    Stubs the cursor-agent binary lookup, the transcript forwarder, and the
    runner auth factory so the model-injection branch runs without a real
    ``cursor-agent`` install or Databricks credentials. The session snapshot
    (workspace + ``terminal_launch_args``) is served from an in-memory
    ``httpx.MockTransport``, and a fake registry records the ``spec`` passed to
    ``launch_required_terminal`` so the test can assert on ``spec.args``.
    """
    from omnigent.runner import _entry as _runner_entry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.cursor_native.resolve_cursor_executable", lambda: "cursor-agent")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.cursor_native_forwarder.supervise_cursor_forwarder",
        _no_op_forwarder,
    )
    # The forwarder is stubbed, so the auth it would carry is never used — keep
    # the factory from reaching for ambient Databricks credentials in tests.
    monkeypatch.setattr(_runner_entry, "_make_auth_token_factory", lambda *a, **k: None)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no real terminal registry."""

        terminal_registry = None

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
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key, resource_role, parent_os_env
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_cursor_main",
                type="terminal",
                session_id=session_id,
                name="cursor:main",
                metadata={"terminal_name": "cursor", "session_key": "main", "running": True},
            )

    snapshot = {"workspace": str(workspace), "terminal_launch_args": terminal_launch_args}
    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=snapshot)),
    )
    try:
        await _auto_create_cursor_terminal(
            "c42dbcb16fd3a87ee8f5d1fe4cabfdf8",
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=fake_client,
            ensure_comment_relay=None,
            agent_spec=agent_spec,
        )
    finally:
        await fake_client.aclose()
    return captured["spec"]


@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_injects_spec_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec-pinned cursor model is threaded into the cursor-agent launch args.

    The web-UI / daemon path launches ``cursor-agent`` from the runner, so the
    session's ``executor.model`` (from ``--model`` or config.yaml ``model:``)
    must reach the TUI as ``--model <id>`` — the regression #933 fixes.
    """
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=None,
    )
    assert spec.command == "cursor-agent"
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "sonnet-4-thinking"


@pytest.mark.parametrize(
    "passthrough",
    [
        # Both the split (``-- --model X``) and joined (``--model=X``) forms a
        # user can pass through must suppress injection — otherwise cursor-agent
        # sees two ``--model`` values and selection is ambiguous.
        ["--model", "gpt-5"],
        ["--model=gpt-5"],
        ["-m", "gpt-5"],
    ],
    ids=["split", "joined", "short"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_user_model_wins(
    passthrough: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-pinned passthrough model wins; the spec model is not injected."""
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=passthrough,
    )
    # Exactly the user's args survive — no second ``--model`` / spec model added.
    assert spec.args.count("--model") == passthrough.count("--model")
    assert "sonnet-4-thinking" not in spec.args


@pytest.mark.parametrize(
    "spec_model",
    [None, "", "databricks-claude-opus", "databricks/claude-opus"],
    ids=["none", "empty", "databricks-dash", "databricks-slash"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_omits_model_when_unusable(
    spec_model: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable cursor model id → no ``--model`` (cursor-agent keeps its default).

    Gateway-routed ``databricks-*`` ids are not cursor-agent model ids, so they
    are dropped rather than passed through (which would error on launch).
    """
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model=spec_model)
        ),
        terminal_launch_args=None,
    )
    assert "--model" not in spec.args


@pytest.mark.parametrize(
    ("snapshot_external_id", "expected_start_at_end"),
    [
        ("02857840-6362-408f-b41f-309e396ed7c6", True),
        (None, False),
    ],
)
@pytest.mark.asyncio
async def test_auto_create_claude_terminal_forwarder_skips_replayed_transcript_on_resume(
    snapshot_external_id: str | None,
    expected_start_at_end: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned resume starts the forwarder past the replayed transcript.

    On cold resume the runner synthesizes Claude's local transcript from
    AP's committed history and launches ``claude --resume``, so the
    transcript file already holds every item Omnigent has at offset 0. The
    forwarder must therefore start at the transcript end
    (``start_at_end=True``); starting at offset 0 would re-post the whole
    history as new ``external_conversation_item`` records — which carry no
    server-side dedup — duplicating the visible conversation on every
    resume. A fresh session has no ``--resume`` and an empty
    transcript, so it must forward from the beginning
    (``start_at_end=False``). This mirrors the CLI client's
    ``prepared.cold_resumed`` handling in ``claude_native.py``.

    :param snapshot_external_id: ``external_session_id`` returned in the AP
        session snapshot, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"`` for a resume, or
        ``None`` for a fresh session.
    :param expected_start_at_end: The ``start_at_end`` the forwarder must
        be launched with for that snapshot.
    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    # Pin the launch config to Claude's native auth so the test does not
    # depend on the runner process's ambient Databricks profile.
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    forwarder_kwargs: dict[str, Any] = {}

    async def _capture_forwarder(**kwargs: Any) -> None:
        """Record the forwarder launch kwargs without opening a stream."""
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _capture_forwarder,
    )

    # Transcript synthesis from Omnigent history has its own coverage; stub it to
    # return a path so the resume branch sets ``resume_external_session_id``
    # without a real item fetch. A non-None return mirrors the production
    # contract: it means ``--resume`` will be passed, which is precisely the
    # condition that makes the replayed transcript hazard real.
    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
    ) -> Path:
        """Record the resume id and return a transcript path."""
        del client, session_id, workspace
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.claude_native._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    snapshot: dict[str, Any] = {}
    if snapshot_external_id is not None:
        snapshot["external_session_id"] = snapshot_external_id

    class _SnapshotServerClient(NullServerClient):
        """Server client whose session snapshot carries the resume id."""

        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            """Return the session snapshot, or labels for the cleared-bridge check."""
            del kwargs

            # auto-create reads the bridge_id label to honour a /clear "-cleared"
            # re-key. Report none here so it falls back to session_id (no /clear).
            if url.endswith("/labels"):

                class _LabelsResponse(NullServerClient._Response):
                    """Empty labels → bridge_id resolves to session_id."""

                    def json(self) -> dict[str, Any]:
                        """Return an empty labels payload."""
                        return {"labels": {}}

                return _LabelsResponse()

            assert url == "/v1/sessions/5cdbea97a2fb0c659bc09605401e2bb2"

            class _SnapResponse(NullServerClient._Response):
                """Snapshot response carrying the parametrized resume id."""

                def json(self) -> dict[str, Any]:
                    """Return the session snapshot dict."""
                    return snapshot

            return _SnapResponse()

    launched_args: list[str] = []

    class _FakeResourceRegistry:
        """Resource registry that returns a terminal without launching."""

        terminal_registry = None

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
            """Return a terminal resource view without spawning a TTY."""
            del terminal_name, session_key
            launched_args.extend(spec.args)
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "5cdbea97a2fb0c659bc09605401e2bb2",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
    )

    # The forwarder runs as a scheduled task; yield so the stub records its
    # kwargs before asserting.
    await asyncio.sleep(0)

    # Crux of the fix: resume skips the replayed transcript, a fresh session
    # forwards from the start. A regression to the old hardcoded
    # ``start_at_end=False`` flips the resume case False and reintroduces
    # the duplicate-history bug; the ``is`` comparison also fails if a
    # truthy non-bool leaks through.
    assert forwarder_kwargs.get("start_at_end") is expected_start_at_end, (
        f"forwarder start_at_end={forwarder_kwargs.get('start_at_end')!r}; "
        f"expected {expected_start_at_end!r} for external_session_id="
        f"{snapshot_external_id!r}. False on resume means the whole "
        f"transcript is re-posted."
    )

    # ``start_at_end`` must be correct *because* the resume branch ran, not
    # by coincidence: synthesis happens exactly when (and only when) the
    # snapshot carried an external session id.
    if snapshot_external_id is None:
        assert synth_calls == []
    else:
        assert synth_calls == [snapshot_external_id]

    # Session titles are generated by an isolated background job, so neither
    # fresh nor resumed Claude terminals receive framework rename instructions.
    assert "--append-system-prompt" not in launched_args


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_cold_resume_fallback_uses_pre_wipe_bridge_sid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback fires when server GET omits external_session_id but local bridge has it.

    Simulates the workspace-scope miss (ES-2065116): the server snapshot
    returns no external_session_id, but the bridge state.json from the
    previous launch holds a claude_session_id. The runner must read it
    *before* prepare_bridge_dir wipes the file and use it as the resume
    hint, so _ensure_local_claude_resume_transcript is called with the
    local sid and --resume is passed.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    # Write the previous claude_session_id into the bridge state.json *before*
    # auto-create runs so the pre-wipe read can find it.
    session_id = "5cdbea97a2fb0c659bc09605401e2bb2"
    prior_claude_sid = "3d10247d-c3c0-4689-8cbd-862d7453bf70"
    pre_bridge_dir = bridge_dir_for_bridge_id(session_id)
    pre_bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pre_bridge_dir / "state.json").write_text(
        json.dumps({"claude_session_id": prior_claude_sid}), encoding="utf-8"
    )

    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
    ) -> Path:
        del client, session_id, workspace
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.claude_native._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    forwarder_kwargs: dict[str, Any] = {}

    async def _capture_forwarder(**kwargs: Any) -> None:
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _capture_forwarder,
    )

    class _NullBindingServerClient(NullServerClient):
        """Server client whose session snapshot omits external_session_id."""

        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            del kwargs
            if url.endswith("/labels"):

                class _LabelsResponse(NullServerClient._Response):
                    def json(self) -> dict[str, Any]:
                        return {"labels": {}}

                return _LabelsResponse()

            # Session snapshot has no external_session_id (workspace-scope miss).
            class _SnapResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {}

            return _SnapResponse()

    class _FakeResourceRegistry:
        terminal_registry = None

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
            del terminal_name, session_key, spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=_NullBindingServerClient(),  # type: ignore[arg-type]
    )

    await asyncio.sleep(0)

    # The fallback must have fired: synthesis is called with the local bridge
    # sid, not skipped (which would leave the user with no context).
    assert synth_calls == [prior_claude_sid], (
        f"Expected synthesis with prior claude sid {prior_claude_sid!r}; "
        f"got {synth_calls!r}. The fallback may be reading the bridge dir "
        "after prepare_bridge_dir already wiped state.json."
    )
    # The forwarder must start past the replayed transcript (same as a
    # normal cold resume where the server returned the binding directly).
    assert forwarder_kwargs.get("start_at_end") is True


@dataclass
class _PublishedEvent:
    """
    One event captured from the runner's per-session publisher.

    :param session_id: Routing session id the event was published under,
        e.g. ``"c74c7a36c4736e2153ed6046d16bcf76"``.
    :param event: The published SSE event dict.
    """

    session_id: str
    event: dict[str, Any]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_emits_resource_created_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch publishes a live ``session.resource.created``.

    The web UI sources its terminal list purely from SSE
    ``session.resource.created`` events, so the auto-create path must
    emit one (the agent-tool / REST launch paths already do via
    ``_emit_terminal_resource_event``). Without it the Terminal toggle
    stays gray until a refresh re-lists terminals via
    snapshot-on-connect.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _ViewResourceRegistry:
        """Returns a terminal resource view; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is None.
        terminal_registry = None

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
            """Return the terminal resource view the runner would launch."""
            del spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    await _auto_create_claude_terminal(
        "c74c7a36c4736e2153ed6046d16bcf76",
        _ViewResourceRegistry(),
        _capture,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Exactly one session.resource.created for the auto-created terminal.
    # 0 means the live publish was dropped (the bug: the toggle would stay
    # gray until a refresh triggers snapshot-on-connect).
    created = [p for p in published if p.event.get("type") == "session.resource.created"]
    assert len(created) == 1, (
        f"auto-create must publish exactly one session.resource.created; got {published}"
    )
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert created[0].session_id == "c74c7a36c4736e2153ed6046d16bcf76"
    resource = created[0].event["resource"]
    assert resource["type"] == "terminal"
    assert resource["id"] == "terminal_claude_main"
    # metadata.running is what the web rail reads for the live terminal.
    assert resource["metadata"]["running"] is True


def test_publish_terminal_pending_emits_pending_then_clear() -> None:
    """
    ``_publish_terminal_pending`` emits the wire shape the Omnigent relay
    consumes for the Terminal-pill spinner.

    The session-creation handler calls this with ``True`` before
    auto-creating a terminal-first session's terminal and ``False`` in a
    ``finally`` (so a failed launch also clears the spinner). The AP
    relay matches on ``type == "session.terminal_pending"`` and reads
    the ``pending`` flag, so both fields must be present and correct, or
    the spinner would never appear (or never clear).
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    _publish_terminal_pending(_capture, "7cef62c6518d5591cc7991974e33ec4c", True)
    _publish_terminal_pending(_capture, "7cef62c6518d5591cc7991974e33ec4c", False)

    assert [p.event for p in published] == [
        {"type": "session.terminal_pending", "pending": True},
        {"type": "session.terminal_pending", "pending": False},
    ]
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert all(p.session_id == "7cef62c6518d5591cc7991974e33ec4c" for p in published)


def test_publish_native_terminal_start_error_emits_failed_status_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Native terminal startup failure publishes a generic ``failed`` status.

    The runner must stay alive when terminal auto-create fails, but the
    affected session should only receive ``session.status: failed`` from
    this startup path. A bare ``response.error`` is turn-scoped; if the
    runner publishes one here, Omnigent can persist an orphan transcript error
    and then publish/persist a second error when the user message
    fast-fails against the same terminal.

    The published/returned message is a fixed, client-safe string — the raw
    exception text (which can embed paths/CLI details) is logged for
    operators, not surfaced on the session stream.

    :param caplog: Pytest log capture fixture, used to confirm the raw
        cause is logged server-side.
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    with caplog.at_level(logging.WARNING):
        error = _publish_native_terminal_start_error(
            _capture,
            "415c9954e2fe4b9276083a4d2c66f689",
            "Codex",
            ImportError("Native Codex requires the 'codex' CLI on PATH."),
        )

    # Generic, client-safe payload — no raw exception text.
    assert error == {
        "code": "native_terminal_start_failed",
        "message": "Native Codex terminal failed to start; see runner logs for details.",
    }
    # The raw cause must NOT leak into the surfaced message, but MUST be
    # logged for operators. If this fails, the redaction regressed (raw
    # text back in the payload) or the server-side log was dropped.
    assert "requires the 'codex' CLI" not in error["message"]
    assert "requires the 'codex' CLI on PATH." in caplog.text
    assert [p.event for p in published] == [
        {
            "type": "session.status",
            "status": "failed",
            "error": error,
        },
    ]
    assert all(p.session_id == "415c9954e2fe4b9276083a4d2c66f689" for p in published)


def test_terminal_lookup_miss_log_explains_stopped_registered_terminal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Terminal GET miss logs identify a stopped registered terminal.

    The CLI polls ``GET /resources/terminals/terminal_claude_main`` while
    waiting to attach. If a tmux pane was registered and then failed the
    liveness probe, the runner log must say that instead of looking like
    auto-create never ran.

    :param tmp_path: Temporary directory for the fake tmux socket path.
    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=False,
    )
    terminal_registry._by_conversation["49b1b4ef0f1c9ba81d232a6f31dfeb24"] = {
        ("claude", "main"): instance
    }
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    _terminal_lookup_miss_log_state.clear()
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
            _log_terminal_lookup_miss(
                resource_registry,
                "49b1b4ef0f1c9ba81d232a6f31dfeb24",
                "terminal_claude_main",
            )
            _log_terminal_lookup_miss(
                resource_registry,
                "49b1b4ef0f1c9ba81d232a6f31dfeb24",
                "terminal_claude_main",
            )
    finally:
        _terminal_lookup_miss_log_state.clear()

    messages = [
        record.getMessage()
        for record in caplog.records
        if "Terminal resource lookup miss" in record.getMessage()
    ]
    assert len(messages) == 1, (
        f"lookup miss logging should be throttled per reason; got {messages!r}"
    )
    assert "terminal_registered_but_not_running" in messages[0]
    assert "terminal_claude_main" in messages[0]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_resets_stale_bridge_id_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Auto-create corrects a stale ``bridge_id`` label on the Omnigent session.

    If a prior rotation left ``BRIDGE_ID_LABEL_KEY`` set to an older
    bridge id (e.g. ``"m0-bridge_from_prior_rotation"``),
    ``_auto_create_claude_terminal`` must PATCH the label to
    ``session_id`` before proceeding.  Without the correction,
    ``_ensure_comment_relay_started`` would later read the stale label
    and write ``tool_relay.json`` into the wrong bridge dir — the bridge
    MCP subprocess would never see it and the relay tools
    (``list_comments``, ``sys_session_list``, etc.) would be absent.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _FakeResourceRegistry:
        """Returns a minimal terminal view; no live terminal registry."""

        terminal_registry = None

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
            """Return a minimal terminal view so the launch doesn't error."""
            del spec
            # Guards that _auto_create_claude_terminal tags the agent
            # terminal with the claude-native role — the runner gates
            # PTY-activity → session.status emission on this role, so
            # dropping it would silently disable working-status updates.
            assert resource_role == CLAUDE_NATIVE_TERMINAL_ROLE
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    # Capture all HTTP requests made to the fake Omnigent server.
    recorded_requests: list[httpx.Request] = []

    def _handle(req: httpx.Request) -> httpx.Response:
        """Record every request; return 200 with minimal session payload."""
        recorded_requests.append(req)
        return httpx.Response(
            200,
            json={
                "reasoning_effort": None,
                "labels": {BRIDGE_ID_LABEL_KEY: "m0-bridge_from_prior_rotation"},
            },
            request=req,
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle),
    )

    await _auto_create_claude_terminal(
        "9a0bec6675dc7ac693d7bf6f53cfb984",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    await fake_client.aclose()

    # Exactly one PATCH request must have been sent to correct the label.
    # 0 means the fix was not applied and the relay would target the wrong dir.
    patch_requests = [r for r in recorded_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1, (
        f"Expected exactly one PATCH to correct the stale bridge_id label; "
        f"got {len(patch_requests)}. 0 means _auto_create_claude_terminal did "
        f"not update the label, so _ensure_comment_relay_started would write "
        f"tool_relay.json to a dir the bridge subprocess never reads."
    )

    import json as _json

    patch_body = _json.loads(patch_requests[0].content)
    assert (
        patch_body.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) == "9a0bec6675dc7ac693d7bf6f53cfb984"
    ), (
        f"PATCH must set {BRIDGE_ID_LABEL_KEY!r} to the session_id "
        f"'9a0bec6675dc7ac693d7bf6f53cfb984' so _ensure_comment_relay_started finds the "
        f"correct bridge dir; got {patch_body.get('labels', {})!r}"
    )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_honours_cleared_bridge_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A session re-keyed to "{id}-cleared" by /clear resumes in its OWN dir.

    The /clear rotation hands the live pane to the new session and re-keys the
    superseded session's bridge_id label to ``{session_id}-cleared``. When that
    session is later resumed, ``_auto_create_claude_terminal`` must honour the
    marker and prepare the isolated ``D({session_id}-cleared)`` — NOT the
    natural ``D(session_id)`` (the new session's live dir, which would
    double-mirror the transcript and trip the executor guard).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _FakeInstance:
        """Minimal live terminal instance for the tmux-target publish."""

        running = True
        socket_path = "/tmp/fake-claude.sock"
        tmux_target = "claude:0.0"

    class _FakeTerminalRegistry:
        """Returns the live instance for any (session, terminal, key) lookup."""

        def get(self, conversation_id: str, terminal_name: str, session_key: str) -> Any:
            """Return the fake live instance."""
            del conversation_id, terminal_name, session_key
            return _FakeInstance()

    class _FakeResourceRegistry:
        """Resource registry exposing a live terminal registry."""

        terminal_registry = _FakeTerminalRegistry()

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
            """Return a minimal terminal view so the launch doesn't error."""
            del spec, resource_role
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    recorded_requests: list[httpx.Request] = []

    def _handle(req: httpx.Request) -> httpx.Response:
        """Report the session's bridge_id label as the cleared marker."""
        recorded_requests.append(req)
        return httpx.Response(
            200,
            json={
                "reasoning_effort": None,
                "labels": {BRIDGE_ID_LABEL_KEY: "b3e788af0ecd4516439ee859b8c74536-cleared"},
            },
            request=req,
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle),
    )

    await _auto_create_claude_terminal(
        "b3e788af0ecd4516439ee859b8c74536",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )
    await fake_client.aclose()

    cleared_dir = claude_native_bridge.bridge_dir_for_bridge_id(
        "b3e788af0ecd4516439ee859b8c74536-cleared"
    )
    natural_dir = claude_native_bridge.bridge_dir_for_bridge_id("b3e788af0ecd4516439ee859b8c74536")
    # The isolated cleared dir is prepared; the natural (live-sibling) dir is not.
    assert cleared_dir.exists()
    assert not natural_dir.exists()
    # tmux.json (what the executor reads to inject) must land in the SAME dir the
    # executor + forwarder use — the cleared dir — NOT the natural session_id dir.
    # Hardcoding session_id there was the "tmux target not advertised yet" bug.
    assert (cleared_dir / "tmux.json").exists()
    assert not (natural_dir / "tmux.json").exists()

    import json as _json

    patch_requests = [r for r in recorded_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1
    patch_body = _json.loads(patch_requests[0].content)
    assert (
        patch_body.get("labels", {}).get(BRIDGE_ID_LABEL_KEY)
        == "b3e788af0ecd4516439ee859b8c74536-cleared"
    )


@dataclass
class _AutoCreateScenario:
    """
    One parametrized case for the claude-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param active_session_id: ``active_session_id`` to seed into the
        shared bridge config, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None`` seeds no
        bridge dir at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``claude:main``
        terminal under in the registry, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        ``BRIDGE_ID_LABEL_KEY`` label, e.g. ``"bridge_shared"`` for a
        rotation target (shares the original's bridge) or ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``
        for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_claude_terminal`` for the new session.
    """

    case_id: str
    active_session_id: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _LabelsAndEmptyHistoryServerClient:
    """
    Server-client stub for the auto-create guard route test.

    Answers the two GETs ``create_session`` issues for a claude-native
    session: the session snapshot (returns a ``BRIDGE_ID_LABEL_KEY``
    label so the guard can resolve the bridge id) and the items page
    (returns empty history so no crash-recovery turn starts). A real
    stub class — not ``MagicMock`` — so an unexpected call shape fails
    loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or empty items page for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` or
            ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d/items"``.
        :returns: A response object exposing ``status_code`` and
            ``json()`` matching the subset the runner reads.
        """
        del kwargs

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/items"):
            return _Response({"data": [], "has_more": False})
        return _Response({"labels": {BRIDGE_ID_LABEL_KEY: self._bridge_id_label}})


_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns
    # the live terminal that is about to be transferred onto conv_new.
    _AutoCreateScenario(
        case_id="clear_rotation_target_skips",
        active_session_id="3bb59abc6e20b834cbb2269f28880895",
        terminal_under="3bb59abc6e20b834cbb2269f28880895",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded active
    # session and no terminal, so it must bootstrap its own Claude.
    _AutoCreateScenario(
        case_id="fresh_session_creates",
        active_session_id=None,
        terminal_under=None,
        bridge_id_label="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        expect_auto_create=True,
    ),
    # The bridge's active session is conv_new itself (e.g. a relaunch
    # after the terminal died) — not a rotation, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="active_is_self_creates",
        active_session_id="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names an active sibling (conv_old) but no live terminal
    # exists under it — nothing to transfer in, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        active_session_id="3bb59abc6e20b834cbb2269f28880895",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", _AUTO_CREATE_SCENARIOS, ids=[s.case_id for s in _AUTO_CREATE_SCENARIOS]
)
@pytest.mark.parametrize("use_envelope", [False, True], ids=["legacy", "envelope"])
async def test_create_session_auto_create_guard_skips_rotation_targets(
    scenario: _AutoCreateScenario,
    use_envelope: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` or ``/fork`` rotation binds the runner to a fresh AP
    session, then transfers the existing Claude terminal onto it. The
    bind reaches the runner's ``POST /v1/sessions`` before the transfer
    runs, so the new session momentarily has no terminal. Previously,
    ``create_session`` always auto-created a second Claude here, which
    made the subsequent transfer 409 and looped the rotation into
    unbounded session/process spawning. The guard now skips auto-create
    when the new session's bridge already has a *different* session
    owning a live ``claude:main`` terminal — the one about to be
    transferred in.

    Drives the real route with the real guard. Each scenario seeds the
    shared bridge's ``active_session_id`` and the terminal registry, then
    asserts whether ``_auto_create_claude_terminal`` ran. Reverting the
    guard turns the ``clear_rotation_target_skips`` case red (auto-create
    fires for a rotation target again).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")

    # Seed the shared bridge config so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.active_session_id is not None:
        prepare_bridge_dir(
            scenario.active_session_id,
            bridge_id=scenario.bridge_id_label,
            workspace=tmp_path,
        )

    # Seed a live claude:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom (see tests/terminals/test_registry.py) — a real TerminalInstance
    # without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="claude",
            session_key="main",
            socket_path=tmp_path / "claude.sock",
            private_dir=tmp_path / "claude",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("claude", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real Claude.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _recording_auto_create)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the claude-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The claude-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabelsAndEmptyHistoryServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    payload: dict[str, Any] = {
        "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
    }
    if use_envelope:
        payload["session_init"] = {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": payload["session_id"],
            "agent_id": payload["agent_id"],
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": str(tmp_path),
                "labels": {BRIDGE_ID_LABEL_KEY: scenario.bridge_id_label},
            },
        }

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json=payload,
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own
        # Claude — the guard only suppresses true rotation targets. An
        # empty ``created`` here would mean the guard over-fired and a
        # host-spawned session would never get a terminal.
        assert created == ["2d1b1a96e3e08f2cd43c0cc4b695ac5d"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create
        # here is the regression: it 409s the transfer and loops the
        # rotation into unbounded session spawning.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@dataclass
class _AntigravityAutoCreateScenario:
    """
    One parametrized case for the antigravity-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param bridge_state_session: ``session_id`` to seed into the shared
        bridge state, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None`` seeds no bridge state
        at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``antigravity:main``
        terminal under in the registry, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        :data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY` label, e.g.
        ``"bridge_shared"`` for a rotation target (shares the original's
        bridge) or ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_antigravity_terminal`` for the new session.
    """

    case_id: str
    bridge_state_session: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _AntigravitySnapshotServerClient:
    """
    Server-client stub for the antigravity auto-create guard route test.

    Answers the two GETs the antigravity branch issues for that session: the
    session snapshot (``/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d`` — non-``None`` so
    ``_session_payload_for_host_spawn_check`` reports the session needs a
    terminal) and the labels lookup (``/v1/sessions/<id>/labels`` — returns
    the bridge-id label so the transfer-inbound check can resolve the shared
    bridge dir). A real stub class — not ``MagicMock`` — so an unexpected call
    shape fails loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or labels payload for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` or
            ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d/labels"``.
        :returns: A response object exposing ``status_code`` and ``json()``
            matching the subset the runner reads.
        """
        del kwargs
        labels = {ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: self._bridge_id_label}

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/labels"):
            return _Response({"labels": labels})
        # The session snapshot: non-None so the host-spawn check reports the
        # session needs a terminal, and carries the same labels.
        return _Response({"id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d", "labels": labels})


_ANTIGRAVITY_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns the
    # live agy terminal that is about to be transferred onto conv_new.
    _AntigravityAutoCreateScenario(
        case_id="clear_rotation_target_skips",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under="3bb59abc6e20b834cbb2269f28880895",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded state and no
    # terminal, so it must bootstrap (cold-start) its own agy.
    _AntigravityAutoCreateScenario(
        case_id="fresh_session_creates",
        bridge_state_session=None,
        terminal_under=None,
        bridge_id_label="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        expect_auto_create=True,
    ),
    # The bridge's recorded session is conv_new itself (e.g. a relaunch after
    # the terminal died) — not a rotation, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="active_is_self_creates",
        bridge_state_session="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names a sibling (conv_old) but no live terminal exists under
    # it — nothing to transfer in, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    _ANTIGRAVITY_AUTO_CREATE_SCENARIOS,
    ids=[s.case_id for s in _ANTIGRAVITY_AUTO_CREATE_SCENARIOS],
)
async def test_create_session_antigravity_auto_create_guard_skips_rotation_targets(
    scenario: _AntigravityAutoCreateScenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The antigravity-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` rotation binds the runner to a fresh Omnigent session, then
    transfers the existing agy terminal onto it — agy is one long-lived process
    hosting many cascades, so the rotation re-homes the SAME process. The bind
    reaches the runner's ``POST /v1/sessions`` before the transfer runs, so the
    new session momentarily has no terminal. Auto-creating a second agy here
    cold-starts a brand-new process whose own ``external_session_id`` then 400s
    the rotation's PATCH and loops it into unbounded session/process spawning
    (the bug found by live e2e). The guard now skips auto-create when the new
    session's bridge already has a *different* session owning a live
    ``antigravity:main`` terminal — the one about to be transferred in. Mirrors
    the claude-native guard test above.

    Drives the real route with the real guard. Each scenario seeds the shared
    bridge state's ``session_id`` and the terminal registry, then asserts whether
    ``_auto_create_antigravity_terminal`` ran. Reverting the guard turns the
    ``clear_rotation_target_skips`` case red (auto-create fires for a rotation
    target again).
    """
    monkeypatch.setattr(
        "omnigent.antigravity_native_bridge._BRIDGE_ROOT",
        tmp_path / "antigravity-native",
    )

    # Seed the shared bridge state so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.bridge_state_session is not None:
        seed_dir = prepare_antigravity_bridge_dir(scenario.bridge_id_label)
        write_antigravity_bridge_state(
            seed_dir,
            AntigravityNativeBridgeState(
                session_id=scenario.bridge_state_session,
                conversation_id="cascade_old",
            ),
        )

    # Seed a live antigravity:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom — a real TerminalInstance without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="antigravity",
            session_key="main",
            socket_path=tmp_path / "antigravity.sock",
            private_dir=tmp_path / "antigravity",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("antigravity", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real agy.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        "omnigent.runner.app._auto_create_antigravity_terminal", _recording_auto_create
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the antigravity-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The antigravity-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_AntigravitySnapshotServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own agy —
        # the guard only suppresses true rotation targets. An empty ``created``
        # here would mean the guard over-fired and a host-spawned session would
        # never get a terminal.
        assert created == ["2d1b1a96e3e08f2cd43c0cc4b695ac5d"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create here
        # is the regression: it cold-starts a redundant agy that 400s the
        # rotation's external_session_id PATCH and loops the rotation.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_registers_permission_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch wires the PermissionRequest hook.

    The runner's ``_auto_create_claude_terminal`` is the launch path
    used when a claude-native session is created with no CLI client
    present (web-UI sessions, the ``omnigent host`` host API). It
    must pass the Omnigent server URL into ``augment_claude_args`` so
    ``build_hook_settings`` registers the ``PermissionRequest`` command
    hook and writes permission_hook.json. Without it, approval prompts
    silently never reach the web UI even though every other hook is
    present (the regression observed in production: settings carried
    SessionStart/Stop/.../PreCompact + statusLine but no
    PermissionRequest).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    token_calls: list[int] = []

    def _shared_auth_factory() -> str:
        token_calls.append(1)
        return "shared-runner-token"

    def _unexpected_auth_resolution(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("terminal launch must reuse the runner auth factory")

    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory",
        _unexpected_auth_resolution,
    )

    # The real forwarder opens an HTTP stream to the server; stub it so
    # the auto-create flow runs without network. The created task is
    # scheduled and completes immediately. Capture the kwargs so the
    # test can assert the forwarder gets a refresh-capable auth.
    forwarder_kwargs: dict[str, Any] = {}

    async def _no_op_forwarder(**kwargs: Any) -> None:
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is
        # None, so the test doesn't need a real terminal instance.
        terminal_registry = None

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
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "4e92b5a0c0ee6db3f874f9c4a3f855a5",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        auth_token_factory=_shared_auth_factory,
    )

    spec = captured["spec"]
    assert spec.command == "claude"
    assert spec.env["ENABLE_TOOL_SEARCH"] == "true"
    # The claude-native terminal must opt into keeping its private tmux server
    # alive past an inner-CLI exit, so a sub-agent worker whose `claude` exits
    # early no longer cascades into "no server running" (#540).
    assert spec.keep_alive_after_exit is True
    args = spec.args
    assert "--append-system-prompt" not in args
    settings = json.loads(args[args.index("--settings") + 1])
    assert "PermissionRequest" in settings["hooks"]
    permission_hook = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert permission_hook["type"] == "command"
    assert "claude_native_hook permission-request" in permission_hook["command"]

    # The hook reads the server URL back out of this file at hook time,
    # so it must be written with the runner's Omnigent server URL.
    config = read_permission_hook_config(
        bridge_dir_for_bridge_id("4e92b5a0c0ee6db3f874f9c4a3f855a5")
    )
    assert config["ap_server_url"] == "http://127.0.0.1:8000"
    assert config["ap_auth_headers"]["Authorization"] == "Bearer shared-runner-token"
    assert token_calls == [1]

    # The forwarder must get a refresh-capable httpx.Auth (not just a
    # one-shot Authorization header) so a long-running host-spawned
    # session keeps forwarding after the ~1h OAuth token expires.
    # ``_auto_create_claude_terminal`` schedules the forwarder as a task;
    # yield once so the stub records its kwargs before asserting.
    from omnigent.runner._entry import _RunnerDatabricksAuth

    await asyncio.sleep(0)
    assert isinstance(forwarder_kwargs.get("auth"), _RunnerDatabricksAuth)
