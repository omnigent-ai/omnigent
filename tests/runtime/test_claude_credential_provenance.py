"""Keep legacy API-key env references private after production spec resolution."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.inner import terminal as terminal_module
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.inner.terminal import TerminalInstance
from omnigent.runtime import workflow
from omnigent.spec import load
from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec


@pytest.fixture
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "parent-home"
    home.mkdir()
    config_home = home / ".omnigent"
    config_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    monkeypatch.setenv("CUSTOM_CLAUDE_CREDENTIAL", "parent-only-custom-key")
    monkeypatch.setenv("OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL", "parent-only-alias-key")
    monkeypatch.setenv("UNRELATED_ENV", "keep-me")
    monkeypatch.setenv("UNRELATED_TOKEN", "also-keep-me")
    monkeypatch.setattr(workflow, "effective_config_with_detected", lambda config: config)
    return config_home


def _load_auth_spec(tmp_path: Path, config_home: Path, origin: str, reference: str) -> AgentSpec:
    auth = {
        "type": "api_key",
        "api_key": reference,
        "base_url": "https://model.test/gateway",
    }
    executor: dict[str, object] = {
        "type": "omnigent",
        "config": {"harness": "claude-sdk"},
        "model": "claude-test",
    }
    if origin in ("spec", "compat"):
        executor["auth"] = auth
    else:
        (config_home / "config.yaml").write_text(yaml.safe_dump({"auth": auth}))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    if origin == "compat":
        executor.pop("type")
        executor.pop("config")
        executor["harness"] = "claude-sdk"
        path = bundle / "agent.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "name": "private_auth",
                    "prompt": "Test credential isolation.",
                    "executor": executor,
                }
            )
        )
        return load(path)
    (bundle / "config.yaml").write_text(
        yaml.safe_dump({"spec_version": 1, "name": "private-auth", "executor": executor})
    )
    return load(bundle)


@pytest.mark.parametrize("origin", ["spec", "compat", "global"])
@pytest.mark.parametrize("reference", ["$CUSTOM_CLAUDE_CREDENTIAL", "${CUSTOM_CLAUDE_CREDENTIAL}"])
def test_resolved_api_key_refs_reach_cli_and_terminal_policy(
    tmp_path: Path,
    isolated_auth: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    reference: str,
) -> None:
    spec = _load_auth_spec(tmp_path, isolated_auth, origin, reference)
    auth = spec.executor.auth if origin != "global" else workflow._load_global_auth()
    assert isinstance(auth, ApiKeyAuth)
    assert auth.api_key == "parent-only-custom-key"
    assert set(auth.source_env_names) == {
        "CUSTOM_CLAUDE_CREDENTIAL",
        "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL",
    }
    assert "providers" not in workflow.load_config()

    # Provenance must survive parsing even if the original env value changes.
    monkeypatch.setenv("CUSTOM_CLAUDE_CREDENTIAL", "rotated-parent-key")
    launch = workflow._build_claude_sdk_spawn_env(spec)
    names = set(json.loads(launch["HARNESS_CLAUDE_SDK_CREDENTIAL_ENV_NAMES"]))
    assert {"CUSTOM_CLAUDE_CREDENTIAL", "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL"} <= names
    assert "UNRELATED_TOKEN" not in names
    access = workflow.claude_terminal_model_access(spec)
    assert access is not None
    expected = "rotated-parent-key" if origin == "global" else "parent-only-custom-key"
    assert access.credential() == expected

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_dir = tmp_path / "terminal"
    private_dir.mkdir()
    policy = SandboxPolicy("linux_bwrap", True, [], [workspace], [], True)
    instance = TerminalInstance("shell", "s1", private_dir / "tmux.sock", private_dir)
    instance.model_access = access
    instance.launch_cwd = str(workspace)
    stopped = Mock()
    handle = SimpleNamespace(
        relay_port=18373,
        socket_path=tmp_path / "relay.sock",
        ca_bundle_path=tmp_path / "ca.pem",
        stop=stopped,
    )
    start = Mock(return_value=handle)
    monkeypatch.setattr(terminal_module, "start_egress_proxy", start)
    # Exercise the terminal's actual launch-environment sanitization wiring.
    env = dict(os.environ)
    try:
        protected = instance._bootstrap_egress_proxy(policy, env)
        assert not protected.allow_network
        assert "CUSTOM_CLAUDE_CREDENTIAL" not in env
        assert "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL" not in env
        assert env["UNRELATED_ENV"] == "keep-me"
        assert env["UNRELATED_TOKEN"] == "also-keep-me"
        assert env["ANTHROPIC_AUTH_TOKEN"] == access.placeholder
        assert start.call_args.kwargs["model_access"] is access
    finally:
        if instance._egress_handle is not None:
            instance._egress_handle.stop()
        if instance._egress_tmpdir is not None:
            shutil.rmtree(instance._egress_tmpdir, ignore_errors=True)


def test_auth_provenance_keeps_dataclass_and_json_compatibility() -> None:
    auth = ApiKeyAuth("parent-only-key", "https://model.test", "api_key")
    stamped = replace(auth, source_env_names=("CUSTOM_TOKEN", "OMNIGENT_CUSTOM_TOKEN"))
    assert stamped == auth
    assert "CUSTOM_TOKEN" not in repr(stamped)
    assert replace(stamped, base_url="https://other.test").source_env_names == (
        "CUSTOM_TOKEN",
        "OMNIGENT_CUSTOM_TOKEN",
    )
    assert json.loads(json.dumps(asdict(stamped)))["source_env_names"] == [
        "CUSTOM_TOKEN",
        "OMNIGENT_CUSTOM_TOKEN",
    ]


@pytest.mark.parametrize("expand_env", [True, False])
def test_resolved_spec_json_provenance_survives_production_reload(
    tmp_path: Path,
    isolated_auth: Path,
    monkeypatch: pytest.MonkeyPatch,
    expand_env: bool,
) -> None:
    spec = _load_auth_spec(tmp_path, isolated_auth, "spec", "${CUSTOM_CLAUDE_CREDENTIAL}")
    assert isinstance(spec.executor.auth, ApiKeyAuth)
    serialized = json.dumps(asdict(spec))
    restored_bundle = tmp_path / "restored-bundle"
    restored_bundle.mkdir()
    (restored_bundle / "config.yaml").write_text(serialized)
    # Only the serialized names can identify the original source after resolution.
    monkeypatch.delenv("CUSTOM_CLAUDE_CREDENTIAL")
    monkeypatch.delenv("OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL")
    restored = load(restored_bundle, expand_env=expand_env)
    assert isinstance(restored.executor.auth, ApiKeyAuth)
    assert restored.executor.auth.api_key == "parent-only-custom-key"
    assert restored.executor.auth.source_env_names == spec.executor.auth.source_env_names
    access = workflow.claude_terminal_model_access(restored)
    assert access is not None
    env = {
        "CUSTOM_CLAUDE_CREDENTIAL": "rotated-parent-key",
        "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL": "rotated-parent-alias",
        "UNRELATED_TOKEN": "keep-me",
    }
    access.worker_environment(env, tmp_path / "worker-home")
    assert "CUSTOM_CLAUDE_CREDENTIAL" not in env
    assert "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL" not in env
    assert env["UNRELATED_TOKEN"] == "keep-me"


@pytest.mark.parametrize("expand_env", [True, False])
def test_serialized_auth_provenance_unions_raw_refs_and_aliases(
    tmp_path: Path, isolated_auth: Path, expand_env: bool
) -> None:
    spec = _load_auth_spec(tmp_path, isolated_auth, "spec", "${CUSTOM_CLAUDE_CREDENTIAL}")
    payload = asdict(spec)
    payload["executor"]["auth"]["api_key"] = "$CUSTOM_CLAUDE_CREDENTIAL"
    payload["executor"]["auth"]["source_env_names"] = ["ADDITIONAL_CREDENTIAL"] * 2
    bundle = tmp_path / "bundle"
    (bundle / "config.yaml").write_text(json.dumps(payload))
    restored = load(bundle, expand_env=expand_env)
    assert isinstance(restored.executor.auth, ApiKeyAuth)
    assert set(restored.executor.auth.source_env_names) == {
        "CUSTOM_CLAUDE_CREDENTIAL",
        "OMNIGENT_CUSTOM_CLAUDE_CREDENTIAL",
        "ADDITIONAL_CREDENTIAL",
        "OMNIGENT_ADDITIONAL_CREDENTIAL",
    }


@pytest.mark.parametrize(
    "names",
    [
        "CUSTOM_TOKEN",
        {"CUSTOM_TOKEN": True},
        None,
        42,
        [True],
        [None],
        [""],
        ["bad-name"],
        ["CUSTOM_TOKEN\n"],
        ["$CUSTOM_TOKEN"],
        ["${CUSTOM_TOKEN}"],
        ["env:CUSTOM_TOKEN"],
        ["1INVALID"],
    ],
)
def test_reloading_auth_rejects_malformed_serialized_provenance(
    tmp_path: Path, isolated_auth: Path, names: object
) -> None:
    spec = _load_auth_spec(tmp_path, isolated_auth, "spec", "${CUSTOM_CLAUDE_CREDENTIAL}")
    payload = asdict(spec)
    payload["executor"]["auth"]["source_env_names"] = names
    bundle = tmp_path / "bundle"
    (bundle / "config.yaml").write_text(json.dumps(payload))
    with pytest.raises(OmnigentError, match=r"executor\.auth\.source_env_names"):
        load(bundle)


@pytest.mark.parametrize("explicit_gateway", [False, True])
def test_terminal_model_access_uses_parent_settings_only_without_explicit_gateway(
    tmp_path: Path,
    isolated_auth: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_gateway: bool,
) -> None:
    from omnigent.inner import claude_sdk_executor
    from omnigent.inner.claude_model_access import HELPER_ENV

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(HELPER_ENV, raising=False)
    settings_dir = Path.home() / ".claude"
    settings_dir.mkdir()
    settings_env = {
        "ANTHROPIC_BASE_URL": "https://host-settings.test/gateway",
        "ANTHROPIC_AUTH_TOKEN": "parent-only-settings-token",
    }
    (settings_dir / "settings.json").write_text(json.dumps({"env": settings_env}))
    if explicit_gateway:
        spec = _load_auth_spec(tmp_path, isolated_auth, "spec", "$CUSTOM_CLAUDE_CREDENTIAL")
    else:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "spec_version": 1,
                    "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
                }
            )
        )
        spec = load(bundle)
    spec.os_env = OSEnvSpec(cwd=str(tmp_path / "workspace"))
    read_settings = Mock(wraps=claude_sdk_executor._claude_user_settings_env_with_sources)
    monkeypatch.setattr(
        claude_sdk_executor, "_claude_user_settings_env_with_sources", read_settings
    )

    access = workflow.claude_terminal_model_access(spec)
    assert access is not None
    if explicit_gateway:
        read_settings.assert_not_called()
        assert access.base_url == "https://model.test/gateway"
        assert access.credential() == "parent-only-custom-key"
    else:
        read_settings.assert_called_once_with(spec.os_env.cwd)
        assert access.base_url == "https://host-settings.test/gateway"
        assert access.credential() == "parent-only-settings-token"
    env = dict(settings_env)
    access.worker_environment(env, tmp_path / "worker-home")
    assert env["ANTHROPIC_AUTH_TOKEN"] == access.placeholder
    assert "parent-only-settings-token" not in env.values()


@pytest.mark.parametrize("entrypoint", ["sdk", "terminal"])
def test_redirected_user_settings_source_is_rejected_before_launch(
    tmp_path: Path,
    isolated_auth: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    from omnigent.inner import claude_sdk_executor as claude

    workspace = tmp_path / "selected-workspace"
    original = workspace / "claude-config"
    original.mkdir(parents=True)
    redirected = tmp_path / "redirected-config"
    redirected.mkdir()
    daemon = tmp_path / "daemon"
    daemon.mkdir()
    monkeypatch.chdir(daemon)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "claude-config")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (original / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "parent-only-settings-token",
                    "CLAUDE_CONFIG_DIR": str(redirected),
                }
            }
        )
    )
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(
            cwd=str(tmp_path / "configured-workspace"),
            sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
        ),
    )
    if entrypoint == "terminal":
        access = workflow.claude_terminal_model_access(spec, cwd=workspace)
        assert access is not None
        assert access.credential() == "parent-only-settings-token"
        with pytest.raises(ValueError, match="sandbox-writable paths"):
            access.protect_sources(policy, workspace)
        assert original in access.source_paths
        assert original / "settings.json" in access.source_paths
    else:
        no_launch = Mock(side_effect=AssertionError("worker startup must not begin"))
        monkeypatch.setattr(claude, "_find_system_claude", lambda: "/bin/claude")
        monkeypatch.setattr(claude, "resolve_sandbox", lambda *_: policy)
        monkeypatch.setattr(claude, "get_backend", no_launch)
        monkeypatch.setattr(claude, "create_private_tmpdir", no_launch)
        with pytest.raises(ValueError, match="sandbox-writable paths"):
            claude.ClaudeSDKExecutor(
                cwd=str(workspace),
                os_env=spec.os_env,
                bundle_dir=tmp_path / "bundle",
            )
        no_launch.assert_not_called()
