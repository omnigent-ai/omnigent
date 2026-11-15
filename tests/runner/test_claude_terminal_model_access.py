"""Exercise Claude terminal authority through the runner's REST launch route."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.inner import terminal as terminal_mod
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.inner.terminal import TerminalInstance
from omnigent.onboarding import provider_config
from omnigent.runner import create_runner_app
from omnigent.runner.native.orchestration import ResolvedSpec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime import workflow
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import registry as registry_mod
from omnigent.terminals.registry import TerminalRegistry
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool


@dataclass
class _TerminalProcesses:
    created: list[TerminalInstance] = field(default_factory=list)
    launched: list[TerminalInstance] = field(default_factory=list)
    closed: list[TerminalInstance] = field(default_factory=list)


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> _TerminalProcesses:
    """Keep construction and source validation real; replace platform/process setup."""
    state = _TerminalProcesses()
    create = terminal_mod.create_terminal_instance

    def resolve(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        assert spec.sandbox is not None
        return SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=[Path(p) for p in spec.sandbox.read_paths or []],
            write_roots=[cwd, *(Path(p.path) for p in spec.sandbox.write_path_specs)],
            write_files=[],
            allow_network=False,
        )

    def construct(*args, **kwargs):
        result = create(*args, **kwargs)
        state.created.append(result.instance)
        return result

    async def launch(instance: TerminalInstance, *, cwd: Path | None = None) -> None:
        instance.launch_cwd = str(cwd)
        instance.running = True
        state.launched.append(instance)

    async def alive(instance: TerminalInstance) -> bool:
        return instance.running

    async def close(instance: TerminalInstance) -> None:
        instance.running = False
        state.closed.append(instance)
        shutil.rmtree(instance.private_dir, ignore_errors=True)

    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    monkeypatch.setattr(terminal_mod, "create_os_environment", lambda *a, **kw: None)
    monkeypatch.setattr(terminal_mod, "resolve_sandbox", resolve)
    monkeypatch.setattr(registry_mod, "create_terminal_instance", construct)
    monkeypatch.setattr(TerminalInstance, "launch", launch)
    monkeypatch.setattr(TerminalInstance, "is_alive", alive)
    monkeypatch.setattr(TerminalInstance, "close", close)
    monkeypatch.setattr(
        SessionResourceRegistry, "_start_terminal_activity_watcher", lambda *a, **kw: None
    )
    return state


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use a custom env-ref provider without consulting host config or credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CUSTOM_CREDENTIAL", "parent-provider-secret")
    monkeypatch.setenv("OMNIGENT_CUSTOM_CREDENTIAL", "parent-alias-secret")
    config = {
        "providers": {
            "custom": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "api_key_ref": "env:CUSTOM_CREDENTIAL",
                    "base_url": "https://model.test/gateway",
                    "models": {"default": "claude-test"},
                },
            }
        }
    }
    monkeypatch.setattr(workflow, "load_config", lambda: config)
    monkeypatch.setattr(provider_config, "_load_config", lambda: config)
    monkeypatch.setattr(workflow, "effective_config_with_detected", lambda value: value)


def _agent(workspace: Path, *, harness: str = "claude-sdk") -> AgentSpec:
    workspace.mkdir(exist_ok=True)
    return AgentSpec(
        spec_version=1,
        name="terminal-owner",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", read_paths=[], write_paths=[]),
        ),
        terminals={"shell": TerminalEnvSpec(command="bash", os_env="inherit")},
    )


@asynccontextmanager
async def _runner(
    spec: AgentSpec,
    bundle: Path,
    *,
    override: str | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, TerminalRegistry, SessionResourceRegistry]]:
    async def snapshot(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_test"
        return httpx.Response(
            200,
            json={
                "id": "conv_test",
                "agent_id": "agent_test",
                "harness_override": override,
            },
        )

    async def resolve(agent_id: str, session_id: str) -> ResolvedSpec:
        assert (agent_id, session_id) == ("agent_test", "conv_test")
        return ResolvedSpec(spec=spec, workdir=bundle)

    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(snapshot), base_url="http://server"
    ) as server:
        app = create_runner_app(
            resource_registry=resources,
            terminal_registry=terminals,
            server_client=server,
            spec_resolver=resolve,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            try:
                yield client, terminals, resources
            finally:
                await terminals.cleanup_conversation("conv_test")


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "private-bundle"
    root.mkdir()
    (root / "config.yaml").write_text("api_key: literal-bundle-secret\n")
    (root / ".mcp.json").write_text('{"env": {"TOKEN": "plugin-secret"}}')
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text("# Public skill\n")
    return root


@pytest.mark.asyncio
async def test_rest_terminal_cold_harness_override_keeps_bundle_and_credentials_private(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
) -> None:
    # No SDK turn has initialized this runner: the override comes from the stored row.
    spec = _agent(tmp_path / "workspace", harness="codex")
    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, _):
        for session_key in ("main", "second"):
            response = await client.post(
                "/v1/sessions/conv_test/resources/terminals",
                json={"terminal": "shell", "session_key": session_key},
            )
            assert response.status_code == 200, response.text
            instance = terminals.get("conv_test", "shell", session_key)
            assert instance is not None and instance.model_access is not None
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is not None
        access = instance.model_access
        assert access.base_url == "https://model.test/gateway"
        assert access.credential() == "parent-provider-secret"
        assert bundle.resolve() in access.source_paths
        assert bundle / "config.yaml" in access.source_paths
        assert bundle / ".mcp.json" in access.source_paths
        assert bundle / "skills" / "SKILL.md" not in access.source_paths
        assert instance.sandbox_policy is not None
        assert bundle.resolve() in instance.sandbox_policy.credential_source_paths
        assert not instance.egress_rules  # Automatic inference needs no user egress rule.
        env = {
            "CUSTOM_CREDENTIAL": "parent-provider-secret",
            "OMNIGENT_CUSTOM_CREDENTIAL": "parent-alias-secret",
            "NONSECRET": "keep-me",
        }
        access.worker_environment(env, tmp_path / "worker-home")
        assert "CUSTOM_CREDENTIAL" not in env
        assert "OMNIGENT_CUSTOM_CREDENTIAL" not in env
        assert env["ANTHROPIC_AUTH_TOKEN"] == access.placeholder
        assert env["NONSECRET"] == "keep-me"
    assert spec.executor.harness_kind == "codex"
    assert len(processes.launched) == 2
    assert len({instance.model_access.placeholder for instance in processes.launched}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "auto", "codex"])
async def test_rest_terminal_effective_harness_controls_model_access(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    override: str | None,
) -> None:
    spec = _agent(tmp_path / "workspace")
    async with _runner(spec, bundle, override=override) as (client, _, _resources):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        assert len(processes.launched) == 1
        assert (processes.launched[0].model_access is not None) == (override != "codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [False, True])
async def test_rest_terminal_omitted_sandbox_uses_platform_default_and_model_access(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    inherited: bool,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness="codex")
    env_spec = OSEnvSpec(type="caller_process", cwd=str(workspace))
    if inherited:
        spec.os_env = env_spec
    else:
        spec.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=env_spec)}
    effective = terminal_mod.build_terminal_os_env_spec(
        spec.terminals["shell"], parent_os_env_spec=spec.os_env
    )
    assert effective.sandbox is not None
    assert effective.sandbox.type == terminal_mod._default_sandbox_for_platform().type
    assert env_spec.sandbox is None
    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, _):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is not None
        assert instance.sandbox_policy is not None and instance.sandbox_policy.active
        assert bundle in instance.sandbox_policy.credential_source_paths
        assert instance.model_access.credential() == "parent-provider-secret"
        assert len(processes.launched) == 1


@pytest.mark.asyncio
async def test_rest_terminal_without_os_env_keeps_unsandboxed_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    bundle: Path,
) -> None:
    spec = _agent(tmp_path / "workspace")
    spec.os_env = None
    resolve_access = Mock(side_effect=AssertionError("no OS environment needs no model auth"))
    monkeypatch.setattr(workflow, "claude_terminal_model_access", resolve_access)
    async with _runner(spec, bundle) as (client, terminals, _):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.sandbox_policy is None
        assert instance.model_access is None
        resolve_access.assert_not_called()
        assert len(processes.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grant", ["read-root", "write-root", "nested-read", "cwd", "symlink", "hardlink"]
)
async def test_rest_terminal_own_policy_rejects_original_bundle_grants(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    grant: str,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness="codex")
    terminal_env = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", read_paths=[], write_paths=[]),
    )
    assert terminal_env.sandbox is not None
    if grant == "read-root":
        terminal_env.sandbox.read_paths = [str(bundle)]
    elif grant == "write-root":
        terminal_env.sandbox.write_paths = [str(bundle)]
    elif grant == "nested-read":
        terminal_env.sandbox.read_paths = [str(bundle / "skills")]
    elif grant == "cwd":
        terminal_env.cwd = str(bundle / "skills")
    elif grant == "symlink":
        (workspace / "alias.json").symlink_to(bundle / ".mcp.json")
        terminal_env.sandbox.read_paths = [str(workspace / "alias.json")]
    else:
        (workspace / "alias.yaml").hardlink_to(bundle / "config.yaml")
    spec.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=terminal_env)}
    # The SDK policy is safe; only this terminal's independent policy exposes the bundle.
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.read_paths == []
    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, resources):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 500, response.text
        assert response.json()["error"]["code"] == "terminal_launch_failed"
        assert not processes.launched
        assert len(processes.created) == len(processes.closed) == 1
        assert terminals.list_for_conversation("conv_test") == []
        assert not resources._terminal_lifecycles
        assert "literal-bundle-secret" not in response.text


@pytest.mark.asyncio
async def test_rest_terminal_safe_independent_policy_gets_automatic_model_access(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
) -> None:
    spec = _agent(tmp_path / "workspace")
    separate = tmp_path / "terminal-workspace"
    separate.mkdir()
    spec.terminals = {
        "shell": TerminalEnvSpec(
            command="bash",
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(separate),
                sandbox=OSEnvSandboxSpec(
                    type="linux_bwrap", read_paths=[str(separate)], write_paths=[str(separate)]
                ),
            ),
        )
    }
    async with _runner(spec, bundle) as (client, terminals, _):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is not None
        assert instance.launch_cwd == str(separate)
        assert instance.sandbox_policy is not None
        assert separate in instance.sandbox_policy.read_roots
        assert bundle in instance.sandbox_policy.credential_source_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["bundle", "inherited-bundle", "symlink", "hardlink", "bundle-default-sandbox"]
)
async def test_rest_terminal_rejects_protected_fork_before_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    source: str,
) -> None:
    spec = _agent(tmp_path / "workspace", harness="codex")
    source_dir = bundle
    if source == "symlink":
        source_dir = tmp_path / "bundle-alias"
        source_dir.symlink_to(bundle, target_is_directory=True)
    elif source == "hardlink":
        source_dir = tmp_path / "fork-source"
        source_dir.mkdir()
        (source_dir / "config.yaml").hardlink_to(bundle / "config.yaml")
    fork_env = OSEnvSpec(
        type="caller_process",
        cwd=str(source_dir),
        fork=True,
        sandbox=(
            None
            if source == "bundle-default-sandbox"
            else OSEnvSandboxSpec(type="linux_bwrap", read_paths=[], write_paths=[])
        ),
    )
    if source == "inherited-bundle":
        spec.os_env = fork_env
    else:
        spec.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=fork_env)}
    copy_tree = Mock(wraps=terminal_mod._copy_tree)
    monkeypatch.setattr(terminal_mod, "_copy_tree", copy_tree)

    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, resources):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 500, response.text
        assert response.json()["error"]["code"] == "terminal_launch_failed"
        copy_tree.assert_not_called()
        assert not processes.created
        assert not processes.launched
        assert terminals.list_for_conversation("conv_test") == []
        assert not resources._terminal_lifecycles
        assert "literal-bundle-secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [False, True])
async def test_rest_terminal_safe_fork_copies_workspace_with_model_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    inherited: bool,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness="codex")
    source = tmp_path / "fork-source"
    source.mkdir()
    (source / "marker.txt").write_text("safe workspace content")
    fork_env = OSEnvSpec(
        type="caller_process",
        cwd=str(source),
        fork=True,
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", read_paths=[], write_paths=[]),
    )
    if inherited:
        spec.os_env = fork_env
    else:
        spec.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=fork_env)}
    copy_tree = Mock(wraps=terminal_mod._copy_tree)
    monkeypatch.setattr(terminal_mod, "_copy_tree", copy_tree)

    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, _):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is not None
        fork_root = instance.private_dir / "root"
        copy_tree.assert_called_once_with(source, fork_root)
        assert instance.launch_cwd == str(fork_root)
        assert (fork_root / "marker.txt").read_text() == "safe workspace content"
        assert not (fork_root / "config.yaml").exists()
        assert not (fork_root / ".mcp.json").exists()
        assert instance.model_access.credential() == "parent-provider-secret"
        assert len(processes.launched) == 1
    assert not fork_root.exists()
    assert (source / "marker.txt").read_text() == "safe workspace content"


@pytest.mark.asyncio
@pytest.mark.parametrize("override_none", [False, True])
async def test_rest_terminal_unsandboxed_fork_does_not_resolve_model_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    bundle: Path,
    override_none: bool,
) -> None:
    spec = _agent(tmp_path / "workspace")
    spec.terminals = {
        "shell": TerminalEnvSpec(
            command="bash",
            allow_sandbox_override=override_none,
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(bundle),
                fork=True,
                sandbox=OSEnvSandboxSpec(type="linux_bwrap" if override_none else "none"),
            ),
        )
    }
    resolve_access = Mock(side_effect=AssertionError("unsandboxed terminals need no model auth"))
    monkeypatch.setattr(workflow, "claude_terminal_model_access", resolve_access)
    copy_tree = Mock(wraps=terminal_mod._copy_tree)
    monkeypatch.setattr(terminal_mod, "_copy_tree", copy_tree)

    async with _runner(spec, bundle) as (client, terminals, _):
        body = {"terminal": "shell", "session_key": "main"}
        if override_none:
            body["sandbox"] = "none"
        response = await client.post("/v1/sessions/conv_test/resources/terminals", json=body)
        assert response.status_code == 200, response.text
        resolve_access.assert_not_called()
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is None
        assert instance.sandbox_policy is None
        fork_root = instance.private_dir / "root"
        copy_tree.assert_called_once_with(bundle, fork_root)
        assert (fork_root / "config.yaml").read_text() == "api_key: literal-bundle-secret\n"
        assert len(processes.launched) == 1


@pytest.mark.parametrize("expose_bundle", [False, True])
def test_tool_fallback_preserves_bundle_and_effective_harness(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    expose_bundle: bool,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness="codex")
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    if expose_bundle:
        spec.os_env.sandbox.read_paths = [str(bundle)]
    registry = TerminalRegistry()
    tool = SysTerminalLaunchTool(spec, registry, bundle_dir=bundle, effective_harness="claude-sdk")
    try:
        result = json.loads(
            tool.invoke(
                json.dumps({"terminal": "shell", "session": "main"}),
                ToolContext(
                    task_id="task_test",
                    agent_id="agent_test",
                    workspace=workspace,
                    conversation_id="conv_test",
                ),
            )
        )
        if expose_bundle:
            assert "error" in result
            assert not processes.launched
            assert len(processes.closed) == 1
        else:
            assert result["status"] == "launched"
            instance = registry.get("conv_test", "shell", "main")
            assert instance is not None and instance.model_access is not None
            assert bundle in instance.model_access.source_paths
            assert instance.model_access.credential() == "parent-provider-secret"
        assert spec.executor.harness_kind == "codex"
    finally:
        asyncio.run(registry.cleanup_conversation("conv_test"))
