"""Exercise Claude terminal authority through REST and terminal tools."""

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
    omit_harness: bool = False,
    omit_spec_resolver: bool = False,
) -> AsyncIterator[tuple[httpx.AsyncClient, TerminalRegistry, SessionResourceRegistry]]:
    async def snapshot(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_test"
        body = {"id": "conv_test", "agent_id": "agent_test"}
        if not omit_harness:
            body["harness"] = override if override is not None else spec.executor.harness_kind
        return httpx.Response(200, json=body)

    async def resolve(agent_id: str, session_id: str) -> ResolvedSpec:
        assert (agent_id, session_id) == ("agent_test", "conv_test")
        return ResolvedSpec(spec=spec, workdir=bundle)

    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(snapshot), base_url="http://server.example.test"
    ) as server:
        app = create_runner_app(
            resource_registry=resources,
            terminal_registry=terminals,
            server_client=server,
            spec_resolver=None if omit_spec_resolver else resolve,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner.example.test"
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
@pytest.mark.parametrize(
    ("harness", "sandbox_type", "inherited", "override_none", "rejects"),
    [
        ("claude-sdk", "linux_bwrap", True, False, True),
        ("claude-sdk", None, False, False, True),
        ("claude", None, True, False, True),
        ("claude-sdk", "none", False, False, False),
        ("claude-sdk", "linux_bwrap", True, True, False),
        ("claude-native", None, True, False, False),
        ("codex-native", None, True, False, False),
        ("openai-agents", "linux_bwrap", False, False, False),
    ],
    ids=[
        "sdk-inherited-sandbox",
        "sdk-own-implicit-sandbox",
        "sdk-alias-inherited-implicit-sandbox",
        "sdk-explicit-optout",
        "sdk-call-optout",
        "claude-native-default",
        "codex-native-default",
        "other-harness-explicit-sandbox",
    ],
)
async def test_terminal_tool_without_resolver_rejects_only_sdk_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    harness: str,
    sandbox_type: str | None,
    inherited: bool,
    override_none: bool,
    rejects: bool,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness=harness)
    environment = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(type=sandbox_type) if sandbox_type is not None else None,
        fork=rejects,
    )
    spec.os_env = environment if inherited else None
    spec.terminals = {
        "shell": TerminalEnvSpec(
            command="bash",
            os_env="inherit" if inherited else environment,
            allow_sandbox_override=override_none,
        )
    }
    resolve_access = Mock(return_value=None)
    monkeypatch.setattr(workflow, "claude_terminal_model_access", resolve_access)
    terminals = TerminalRegistry()
    tool = SysTerminalLaunchTool(spec=spec, registry=terminals)
    context = ToolContext(task_id="task", agent_id="agent", conversation_id="conv_test")
    arguments = {"terminal": "shell", "session": "main"}
    if override_none:
        arguments["sandbox"] = "none"
    try:
        result = json.loads(await asyncio.to_thread(tool.invoke, json.dumps(arguments), context))
        if rejects:
            assert result == {
                "error": (
                    "launch failed: Claude SDK terminals require a session model-access resolver"
                )
            }
            assert not processes.created  # Reject before allocating or copying a fork.
            assert not processes.launched
            assert terminals.list_for_conversation("conv_test") == []
        else:
            assert result["status"] == "launched"
            instance = terminals.get("conv_test", "shell", "main")
            assert instance is not None and instance.model_access is None
            assert (instance.sandbox_policy is not None) == (
                sandbox_type == "linux_bwrap" and not override_none
            )
            repeated = json.loads(
                await asyncio.to_thread(tool.invoke, json.dumps(arguments), context)
            )
            assert repeated["status"] == "already_running"
            assert len(processes.launched) == 1
        resolve_access.assert_not_called()
    finally:
        await terminals.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "override"), [("codex", "claude-sdk"), ("claude-sdk", "claude-native")]
)
async def test_terminal_tool_session_resolver_controls_harness_and_bundle(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    harness: str,
    override: str,
) -> None:
    spec = _agent(tmp_path / "workspace", harness=harness)
    assert spec.os_env is not None
    spec.os_env.sandbox = None
    async with _runner(spec, bundle, override=override) as (_, terminals, _resources):
        tool = SysTerminalLaunchTool(spec=spec, registry=terminals)
        context = ToolContext(task_id="task", agent_id="agent", conversation_id="conv_test")
        result = json.loads(
            await asyncio.to_thread(
                tool.invoke, json.dumps({"terminal": "shell", "session": "main"}), context
            )
        )
        assert result["status"] == "launched"
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None
        assert (instance.model_access is not None) == (override == "claude-sdk")
        assert (instance.sandbox_policy is not None) == (override == "claude-sdk")
        if instance.model_access is not None:
            assert bundle.resolve() in instance.model_access.source_paths
        assert len(processes.launched) == 1


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
        assert bundle / "config.yaml" not in access.source_paths
        assert bundle / ".mcp.json" not in access.source_paths
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
@pytest.mark.parametrize(
    ("override", "omit_harness"),
    [(None, False), (None, True), ("auto", False), ("codex", False)],
)
async def test_rest_terminal_effective_harness_controls_model_access(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    override: str | None,
    omit_harness: bool,
) -> None:
    spec = _agent(tmp_path / "workspace")
    async with _runner(spec, bundle, override=override, omit_harness=omit_harness) as (
        client,
        _,
        _resources,
    ):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        assert response.status_code == 200, response.text
        assert len(processes.launched) == 1
        assert (processes.launched[0].model_access is not None) == (override != "codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "codex-native"])
async def test_unresolved_session_spec_blocks_sdk_terminal_only(
    tmp_path: Path,
    processes: _TerminalProcesses,
    bundle: Path,
    harness: str,
) -> None:
    spec = _agent(tmp_path / "workspace", harness=harness)
    async with _runner(spec, bundle, omit_spec_resolver=True) as (_, terminals, _):
        if harness == "claude-sdk":
            with pytest.raises(
                RuntimeError, match="Claude SDK terminals require a resolved session spec"
            ):
                await terminals.launch(
                    "conv_test",
                    "shell",
                    "main",
                    spec.terminals["shell"],
                    parent_os_env=spec.os_env,
                )
            assert not processes.created
            assert not processes.launched
        else:
            instance = await terminals.launch(
                "conv_test", "shell", "main", spec.terminals["shell"], parent_os_env=spec.os_env
            )
            assert instance.model_access is None
            assert processes.launched == [instance]


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
    child = workspace / "child"
    (child / "child").mkdir(parents=True)
    env_spec = OSEnvSpec(type="caller_process", cwd=str(workspace))
    if inherited:
        spec.os_env = env_spec
        spec.terminals["shell"].allow_cwd_override = True
    else:
        spec.terminals = {
            "shell": TerminalEnvSpec(command="bash", os_env=env_spec, allow_cwd_override=True)
        }
    effective = terminal_mod.build_terminal_os_env_spec(
        spec.terminals["shell"], parent_os_env_spec=spec.os_env
    )
    assert effective.sandbox is None
    assert env_spec.sandbox is None
    async with _runner(spec, bundle, override="claude-sdk") as (client, terminals, _):
        resolved_cwds: list[Path] = []
        original_resolver = terminals.model_access_resolver
        assert original_resolver is not None

        async def record_resolver(session_id: str, cwd: Path):
            resolved_cwds.append(cwd)
            return await original_resolver(session_id, cwd)

        terminals.model_access_resolver = record_resolver
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main", "cwd": "child"},
        )
        assert response.status_code == 200, response.text
        instance = terminals.get("conv_test", "shell", "main")
        assert instance is not None and instance.model_access is not None
        assert instance.sandbox_policy is not None and instance.sandbox_policy.active
        assert bundle in instance.sandbox_policy.credential_source_paths
        assert instance.model_access.credential() == "parent-provider-secret"
        assert resolved_cwds == [child]
        assert Path(instance.launch_cwd) == child
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
    ("declared_harness", "effective_harness", "rejects"),
    [("codex", "claude-sdk", True), ("claude-sdk", "codex", False)],
)
async def test_rest_terminal_own_policy_follows_effective_harness_for_bundle_grants(
    tmp_path: Path,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    declared_harness: str,
    effective_harness: str,
    rejects: bool,
) -> None:
    workspace = tmp_path / "workspace"
    spec = _agent(workspace, harness=declared_harness)
    terminal_env = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", read_paths=[], write_paths=[]),
    )
    assert terminal_env.sandbox is not None
    terminal_env.sandbox.read_paths = [str(bundle)]
    spec.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=terminal_env)}
    # The terminal's independent policy exposes the credential-bearing bundle.
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.read_paths == []
    async with _runner(spec, bundle, override=effective_harness) as (client, terminals, resources):
        response = await client.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={"terminal": "shell", "session_key": "main"},
        )
        if rejects:
            assert response.status_code == 500, response.text
            assert response.json()["error"]["code"] == "terminal_launch_failed"
            assert not processes.launched
            assert len(processes.created) == len(processes.closed) == 1
            assert terminals.list_for_conversation("conv_test") == []
            assert not resources._terminal_lifecycles
            assert "literal-bundle-secret" not in response.text
        else:
            assert response.status_code == 200, response.text
            instance = terminals.get("conv_test", "shell", "main")
            assert instance is not None and instance.model_access is None
            assert instance.sandbox_policy is not None
            assert bundle in instance.sandbox_policy.read_roots
            assert len(processes.launched) == 1


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
@pytest.mark.parametrize("source", ["bundle", "inherited-bundle", "bundle-default-sandbox"])
async def test_rest_terminal_rejects_protected_fork_before_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    processes: _TerminalProcesses,
    provider: None,
    bundle: Path,
    source: str,
) -> None:
    spec = _agent(tmp_path / "workspace", harness="codex")
    fork_env = OSEnvSpec(
        type="caller_process",
        cwd=str(bundle),
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
