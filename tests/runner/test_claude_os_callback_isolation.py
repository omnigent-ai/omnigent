"""Runner OS callbacks must retain the Claude session's credential boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import credential_proxy as credential_module
from omnigent.inner import os_env as os_module
from omnigent.inner.claude_model_access import ClaudeAuthRequired, ClaudeModelAccess
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    DatabricksProfileBinding,
    DatabricksProxySpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
)
from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.tool_dispatch import _execute_os_env_tool, execute_tool
from omnigent.spec.types import AgentSpec, ExecutorSpec


@pytest.fixture
def callback_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from omnigent.runtime import workflow

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = tmp_path / "private-bundle"
    bundle.mkdir()
    (bundle / "config.yaml").write_text("api_key: parent-only-provider-secret")
    config_home = tmp_path / "parent-config"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-only-host-secret")
    monkeypatch.setenv("CALLBACK_PROVIDER_KEY", "parent-only-provider-secret")
    monkeypatch.setenv("OMNIGENT_CALLBACK_PROVIDER_KEY", "parent-only-alias-secret")
    build = Mock(
        return_value={
            "HARNESS_CLAUDE_SDK_API_KEY_HELPER": "printf %s parent-only-provider-secret",
            "HARNESS_CLAUDE_SDK_CREDENTIAL_ENV_NAMES": '["CALLBACK_PROVIDER_KEY"]',
        }
    )
    monkeypatch.setattr(workflow, "_build_claude_sdk_spawn_env", build)
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap",
                write_paths=["."],
                allow_network=True,
                env_passthrough=[
                    "ANTHROPIC_AUTH_TOKEN",
                    "CALLBACK_PROVIDER_KEY",
                    "OMNIGENT_CALLBACK_PROVIDER_KEY",
                ],
            ),
        ),
    )
    return spec, workspace, bundle, build


def _fake_policy(spec, cwd):
    return SandboxPolicy("darwin_seatbelt", True, [], [cwd], [], True)


async def test_isolation_restarts_the_helper_only_when_the_credential_boundary_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_module, "resolve_sandbox", _fake_policy)
    environment = os_module.create_os_environment(OSEnvSpec(cwd=str(tmp_path)))
    assert environment is not None
    process = Mock()
    process.poll.return_value = None
    environment._helper._proc = process
    secret = tmp_path.parent / "private-callback-credential"

    def session_access(
        base_url: str = "https://model.test", token: str = "parent-only-first"
    ) -> ClaudeModelAccess:
        return ClaudeModelAccess(base_url, "authorization", lambda: token, (secret,))

    access = session_access()
    cert, key = ensure_ca(cache_dir=tmp_path)
    relay = access.create_proxy(
        [], cert, key, upstream_ca_bundle=ensure_ca_bundle(cert, cache_dir=tmp_path)
    )
    try:
        environment.isolate_model_credentials(access)
        process.terminate.assert_called_once()
        assert environment._helper._proc is None
        assert secret in environment.sandbox.credential_source_paths
        assert environment._helper._model_access is access
        assert await relay._credential_source.token_for_request() == "parent-only-first"

        # Each callback derives a new access object with a fresh placeholder.
        process.reset_mock()
        environment._helper._proc = process
        rederived = session_access(token="parent-only-replacement")
        assert rederived.placeholder != access.placeholder
        environment.isolate_model_credentials(rederived)
        process.terminate.assert_not_called()
        assert environment._helper._proc is process
        assert environment._helper._model_access is access
        assert environment._helper._model_access.placeholder == access.placeholder
        assert access.credential() == "parent-only-replacement"
        assert await relay._credential_source.token_for_request() == "parent-only-replacement"

        rejected = Mock(return_value="parent-only-rejected")
        environment.sandbox.read_roots.append(secret)
        try:
            with pytest.raises(ValueError, match="outside sandbox"):
                environment.isolate_model_credentials(
                    ClaudeModelAccess(access.base_url, "authorization", rejected, (secret,))
                )
        finally:
            environment.sandbox.read_roots.remove(secret)
        rejected.assert_not_called()
        assert access.credential() == "parent-only-replacement"
        assert await relay._credential_source.token_for_request() == "parent-only-replacement"

        unavailable = Mock(
            side_effect=[RuntimeError("parent-only-error"), "parent-only-recovered"]
        )
        environment.isolate_model_credentials(
            ClaudeModelAccess(access.base_url, "authorization", unavailable, (secret,))
        )
        with pytest.raises(ClaudeAuthRequired) as error:
            await relay._credential_source.token_for_request()
        assert "parent-only" not in str(error.value)
        assert error.value.__suppress_context__
        assert await relay._credential_source.token_for_request() == "parent-only-recovered"
        assert unavailable.call_count == 2
        unavailable.side_effect = None
        unavailable.return_value = "parent-only-next-callback"
        environment.isolate_model_credentials(access)
        assert await relay._credential_source.token_for_request() == "parent-only-next-callback"
        process.terminate.assert_not_called()
        assert environment._helper._proc is process

        changed = session_access("https://other-model.test")
        with pytest.raises(ValueError, match="unchanged authority"):
            access._adopt_credential(changed)
        environment.isolate_model_credentials(changed)
        process.terminate.assert_called_once()
        assert environment._helper._model_access is changed
    finally:
        environment.close()


@pytest.mark.parametrize("retired_fails", [False, True], ids=["retired-key", "retired-error"])
async def test_equal_boundary_replacement_fences_an_outstanding_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired_fails: bool
) -> None:
    monkeypatch.setattr(os_module, "resolve_sandbox", _fake_policy)
    environment = os_module.create_os_environment(OSEnvSpec(cwd=str(tmp_path)))
    assert environment is not None
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def retired() -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "credential replacement never released the old resolution"
        if retired_fails:
            raise RuntimeError("parent-only-retired-error")
        return "parent-only-retired"

    access = ClaudeModelAccess("https://model.test", "authorization", retired)
    replacement = Mock(return_value="parent-only-current")
    cert, key = ensure_ca(cache_dir=tmp_path)
    relay = access.create_proxy(
        [], cert, key, upstream_ca_bundle=ensure_ca_bundle(cert, cache_dir=tmp_path)
    )
    environment.isolate_model_credentials(access)
    pending = asyncio.create_task(relay._credential_source.token_for_request())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        environment.isolate_model_credentials(
            ClaudeModelAccess(access.base_url, "authorization", replacement)
        )
        release.set()
        assert await asyncio.wait_for(pending, timeout=5) == "parent-only-current"
        assert await relay._credential_source.token_for_request() == "parent-only-current"
        replacement.assert_called_once_with()
    finally:
        release.set()
        try:
            if not pending.done():
                await asyncio.wait_for(pending, timeout=5)
        finally:
            environment.close()


async def test_callback_spawn_has_private_env_protected_bundle_and_authenticated_relay(
    callback_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, workspace, bundle, _ = callback_session
    monkeypatch.setattr(os_module, "resolve_sandbox", _fake_policy)
    captured = {}
    stopped = Mock()
    handle = SimpleNamespace(
        _proxy=object(),
        _loop=object(),
        _thread=object(),
        auth_token="private-proxy-auth",
        relay_port=19671,
        socket_path=tmp_path / "relay.sock",
        ca_bundle_path=tmp_path / "ca.pem",
        stop=stopped,
    )
    start = Mock(return_value=handle)
    monkeypatch.setattr("omnigent.inner.egress.start_egress_proxy", start)
    monkeypatch.setattr(
        os_module,
        "get_backend",
        lambda _: SimpleNamespace(
            wrap_launcher_argv=lambda argv, policy, *args, **kwargs: argv,
        ),
    )

    class SpawnCaptured(Exception):
        pass

    def popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        captured["config"] = json.loads(os.read(kwargs["pass_fds"][0], 65536))
        raise SpawnCaptured("captured before executing any payload")

    monkeypatch.setattr(os_module.subprocess, "Popen", popen)
    result = await execute_tool(
        tool_name="sys_os_shell",
        arguments='{"command":"printf ran"}',
        agent_spec=spec,
        runner_workspace=workspace,
        local_tool_workdir=bundle,
    )
    assert "captured before executing" in result
    env, config = captured["env"], captured["config"]
    assert "parent-only-" not in json.dumps(captured)
    assert env["ANTHROPIC_AUTH_TOKEN"].startswith("oa_cred_")
    assert env["HOME"] != str(Path.home())
    assert env["CLAUDE_CONFIG_DIR"] == str(Path(env["HOME"]) / ".claude")
    assert "CALLBACK_PROVIDER_KEY" not in env
    assert "OMNIGENT_CALLBACK_PROVIDER_KEY" not in env
    assert "private-proxy-auth" not in json.dumps(env)
    assert config["egress_auth_token"] == "private-proxy-auth"
    policy = SandboxPolicy.from_jsonable(config["sandbox"])
    assert not policy.allow_network
    assert bundle in policy.credential_source_paths
    assert bundle / "config.yaml" in policy.credential_source_paths
    from omnigent.inner.seatbelt_sandbox import _build_profile

    profile = _build_profile(policy, workspace)
    assert '(global-name "com.apple.SecurityServer")' in profile
    assert "securityd|secd|keychain" in profile
    assert start.call_args.kwargs["rules"] == []
    assert start.call_args.kwargs["require_auth"] is True
    assert start.call_args.kwargs["model_access"].credential() == "parent-only-provider-secret"
    assert start.call_args.kwargs["request_body_transform"] is not None
    stopped.assert_called_once()
    assert not Path(env["HOME"]).exists(), "failed startup left its private home behind"


async def test_callback_keeps_synthetic_proxy_env_and_config_after_model_scrubbing(
    callback_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, workspace, bundle, _ = callback_session
    config_file = tmp_path / "parent-databrickscfg"
    config_file.write_text("[parent]\ntoken = parent-only-config-secret\n")
    monkeypatch.setenv("DATABRICKS_TOKEN", "parent-only-tool-secret")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "parent-profile")
    original_home = os.environ.get("HOME")
    proxy_spec = CredentialProxySpec(
        entries=[
            CredentialProxyEntry(
                host="api.tool.test",
                scheme="bearer",
                source=CredentialSourceSpec(kind="env", env="DATABRICKS_TOKEN"),
                inject_env=["DATABRICKS_TOKEN"],
            )
        ],
        databricks=DatabricksProxySpec(
            profiles=[DatabricksProfileBinding(profile="tool-profile")],
            default="tool-profile",
        ),
    )
    spec.os_env.sandbox.credential_proxy = proxy_spec
    spec.os_env.sandbox.env_passthrough.extend(
        ["DATABRICKS_TOKEN", "DATABRICKS_CONFIG_FILE", "DATABRICKS_CONFIG_PROFILE"]
    )

    def resolve_policy(spec, cwd):
        policy = _fake_policy(spec, cwd)
        policy.credential_proxy = proxy_spec
        policy.env_passthrough = list(spec.sandbox.env_passthrough)
        return policy

    monkeypatch.setattr(os_module, "resolve_sandbox", resolve_policy)
    monkeypatch.setattr(
        credential_module,
        "DatabricksProfileTokenProvider",
        lambda profile: SimpleNamespace(
            hostname="profile.tool.test",
            workspace_url="https://profile.tool.test",
            resolve=lambda: "parent-only-profile-secret",
        ),
    )
    prepare = Mock(wraps=os_module.prepare_credential_proxy_runtime)
    monkeypatch.setattr(os_module, "prepare_credential_proxy_runtime", prepare)
    stopped = Mock()
    start = Mock(
        return_value=SimpleNamespace(
            _proxy=object(),
            _loop=object(),
            _thread=object(),
            auth_token="private-proxy-auth",
            relay_port=19671,
            socket_path=tmp_path / "relay.sock",
            ca_bundle_path=tmp_path / "ca.pem",
            stop=stopped,
        )
    )
    monkeypatch.setattr("omnigent.inner.egress.start_egress_proxy", start)
    monkeypatch.setattr(
        os_module,
        "get_backend",
        lambda _: SimpleNamespace(wrap_launcher_argv=lambda argv, *args, **kwargs: argv),
    )
    captured = {}

    def popen(argv, **kwargs):
        env = kwargs["env"]
        captured["env"] = env
        captured["config"] = json.loads(os.read(kwargs["pass_fds"][0], 65536))
        pointer = env.get("DATABRICKS_CONFIG_FILE")
        if pointer is not None:
            path = Path(pointer)
            captured["generated_config"] = path.read_text()
            captured["config_mode"] = path.stat().st_mode & 0o777
        raise RuntimeError("captured callback credential proxy before spawn")

    monkeypatch.setattr(os_module.subprocess, "Popen", popen)
    result = await execute_tool(
        tool_name="sys_os_shell",
        arguments='{"command":"printf ran"}',
        agent_spec=spec,
        runner_workspace=workspace,
        local_tool_workdir=bundle,
    )
    assert "captured callback credential proxy before spawn" in result
    parent_env = prepare.call_args.kwargs["parent_env"]
    assert parent_env["HOME"] == original_home
    assert parent_env["DATABRICKS_TOKEN"] == "parent-only-tool-secret"
    assert parent_env["DATABRICKS_CONFIG_FILE"] == str(config_file)
    rewrites = {rule.host: rule for rule in start.call_args.kwargs["credential_rewrites"]}
    env = captured["env"]
    assert env["DATABRICKS_TOKEN"] == rewrites["api.tool.test"].synthetic
    assert env["DATABRICKS_TOKEN"].startswith("oa_cred_")
    assert rewrites["api.tool.test"].resolve_secret() == "parent-only-tool-secret"
    assert rewrites["profile.tool.test"].resolve_secret() == "parent-only-profile-secret"
    assert env["DATABRICKS_CONFIG_PROFILE"] == "tool-profile"
    assert env["DATABRICKS_CONFIG_FILE"] != str(config_file)
    assert "[tool-profile]" in captured["generated_config"]
    assert f"token = {rewrites['profile.tool.test'].synthetic}" in captured["generated_config"]
    assert captured["config_mode"] == 0o600
    assert "parent-only-" not in json.dumps(captured)
    assert env["ANTHROPIC_AUTH_TOKEN"] == start.call_args.kwargs["model_access"].placeholder
    assert "private-proxy-auth" not in json.dumps(env)
    assert captured["config"]["egress_auth_token"] == "private-proxy-auth"
    stopped.assert_called_once()
    assert not Path(env["DATABRICKS_CONFIG_FILE"]).exists()


@pytest.mark.parametrize("effective_harness", ["claude-sdk", "codex"])
async def test_callback_uses_effective_harness_over_spec(
    callback_session, monkeypatch: pytest.MonkeyPatch, effective_harness: str
) -> None:
    spec, workspace, bundle, build = callback_session
    spec.executor.config["harness"] = (
        "codex" if effective_harness == "claude-sdk" else "claude-sdk"
    )
    environment = SimpleNamespace(shell=AsyncMock(return_value={"stdout": "ran"}), close=Mock())
    create = Mock(return_value=environment)
    monkeypatch.setattr(os_module, "create_os_environment", create)
    result = await execute_tool(
        tool_name="sys_os_shell",
        arguments='{"command":"printf ran"}',
        agent_spec=spec,
        runner_workspace=workspace,
        local_tool_workdir=bundle,
        effective_harness=effective_harness,
    )
    assert json.loads(result)["stdout"] == "ran"
    access = create.call_args.kwargs["model_access"]
    if effective_harness == "claude-sdk":
        assert access is not None
        assert bundle in access.source_paths
        assert access.credential() == "parent-only-provider-secret"
    else:
        assert access is None
        build.assert_not_called()


async def test_shared_callback_environment_is_protected_before_execution(
    callback_session,
) -> None:
    spec, workspace, bundle, _ = callback_session
    spec.os_env.sandbox.type = "linux_bwrap"
    spec.os_env.sandbox.write_paths = [{"path": ".", "copy_on_write": True}]
    order = []
    environment = SimpleNamespace(
        isolate_model_credentials=Mock(side_effect=lambda access: order.append(access)),
        shell=AsyncMock(side_effect=lambda **kwargs: {"protected": len(order) == 1}),
        close=Mock(),
    )
    registry = SimpleNamespace(resolve_environment=Mock(return_value=environment))
    result = await _execute_os_env_tool(
        "sys_os_shell",
        {"command": "printf ran"},
        agent_spec=spec,
        runner_workspace=workspace,
        bundle_dir=bundle,
        conversation_id="session",
        resource_registry=registry,
    )
    assert json.loads(result)["protected"] is True
    assert bundle in order[0].source_paths
    environment.close.assert_not_called()


@pytest.mark.parametrize(
    "fork,active",
    [
        pytest.param(False, True, id="False"),
        pytest.param(True, True, id="True"),
        pytest.param(False, False, id="inactive"),
    ],
)
async def test_callback_rejects_bundle_credentials_before_execution(
    callback_session, monkeypatch: pytest.MonkeyPatch, fork: bool, active: bool
) -> None:
    spec, workspace, bundle, _ = callback_session
    spec.os_env.fork = fork
    spec.os_env.sandbox.read_paths = [str(bundle)]
    monkeypatch.setattr(
        os_module,
        "resolve_sandbox",
        lambda spec, cwd: SandboxPolicy("linux_bwrap", active, [bundle], [cwd], [], False),
    )
    copy = Mock(side_effect=AssertionError("must reject before copying credentials"))
    spawn = Mock(side_effect=AssertionError("must reject before executing payload"))
    monkeypatch.setattr(os_module, "_copy_tree", copy)
    monkeypatch.setattr(os_module.subprocess, "Popen", spawn)
    result = await execute_tool(
        tool_name="sys_os_shell",
        arguments='{"command":"cat config.yaml"}',
        agent_spec=spec,
        runner_workspace=workspace,
        local_tool_workdir=bundle,
    )
    expected_error = "outside sandbox" if active else "active sandbox policy"
    assert expected_error in json.loads(result)["error"]
    copy.assert_not_called()
    spawn.assert_not_called()


@pytest.fixture
def real_callback_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("requires Seatbelt or bubblewrap")
    if sys.platform == "linux" and shutil.which("bwrap") is None:
        pytest.skip("requires bubblewrap")
    cert, key = ensure_ca(cache_dir=tmp_path / "ca")
    bundle = ensure_ca_bundle(cert, cache_dir=tmp_path / "ca")
    monkeypatch.setattr("omnigent.inner.egress.controller.ensure_ca", lambda: (cert, key))
    monkeypatch.setattr("omnigent.inner.egress.controller.ensure_ca_bundle", lambda _: bundle)


async def test_real_callback_child_cannot_read_secrets_or_use_direct_network(
    callback_session, real_callback_sandbox, tmp_path: Path
) -> None:
    spec, workspace, bundle, _ = callback_session
    outside = tmp_path / "outside-write"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        code = f"""
import json, os, pathlib, socket, subprocess, sys
child = '''import json, os; print(json.dumps(dict(os.environ)))'''
result = {{'env': json.loads(subprocess.check_output([sys.executable, '-c', child]))}}
try:
    result['private'] = pathlib.Path({str(bundle / "config.yaml")!r}).read_text()
except OSError:
    result['private'] = None
try:
    with socket.create_connection(('127.0.0.1', {port}), timeout=1):
        result['network'] = 'allowed'
except OSError:
    result['network'] = 'blocked'
try:
    pathlib.Path({str(outside)!r}).write_text('forbidden')
except OSError:
    pass
pathlib.Path('callback-marker').write_text('executed')
print(json.dumps(result))
"""
        result = json.loads(
            await execute_tool(
                tool_name="sys_os_shell",
                arguments=json.dumps(
                    {
                        "command": shlex.join([sys.executable, "-c", code]),
                        "timeout": 15,
                    }
                ),
                agent_spec=spec,
                runner_workspace=workspace,
                local_tool_workdir=bundle,
            )
        )
    assert result.get("exit_code") == 0, result
    report = json.loads(result["stdout"])
    assert report["private"] is None
    assert report["network"] == "blocked"
    assert "parent-only-" not in json.dumps(report)
    assert report["env"]["ANTHROPIC_AUTH_TOKEN"].startswith("oa_cred_")
    assert (workspace / "callback-marker").read_text() == "executed"
    assert not outside.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain IPC")
async def test_real_callback_cannot_read_parent_keychain(
    callback_session, real_callback_sandbox, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    spec, workspace, bundle, _ = callback_session
    keychain = str(tmp_path / "callback.keychain-db")
    subprocess.run(
        ["/usr/bin/security", "create-keychain", "-p", "synthetic", keychain],
        check=True,
        capture_output=True,
        timeout=10,
    )
    request.addfinalizer(
        lambda: subprocess.run(
            ["/usr/bin/security", "delete-keychain", keychain], capture_output=True, timeout=10
        )
    )
    subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-a",
            "probe",
            "-s",
            "callback-probe",
            "-w",
            "parent-only-keychain-canary",
            keychain,
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    retrieve = [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "probe",
        "-s",
        "callback-probe",
        "-w",
        keychain,
    ]
    parent = subprocess.run(retrieve, check=True, capture_output=True, text=True, timeout=10)
    assert parent.stdout.strip() == "parent-only-keychain-canary"
    result = json.loads(
        await execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": shlex.join(retrieve)}),
            agent_spec=spec,
            runner_workspace=workspace,
            local_tool_workdir=bundle,
        )
    )
    assert result.get("exit_code") not in (None, 0), result
    assert "parent-only-keychain-canary" not in result["stdout"] + result["stderr"]
