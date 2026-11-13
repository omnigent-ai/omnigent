"""Harness startup must restore typed credential policies after JSON transport."""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omnigent.inner import bwrap_sandbox
from omnigent.inner.credential_proxy import prepare_credential_proxy_runtime
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    DatabricksProfileBinding,
    DatabricksProxySpec,
    GitSshBinding,
    OSEnvSandboxSpec,
    OSEnvSpec,
    TcpEgressRoute,
)
from omnigent.inner.os_env import _build_credential_proxy_parent_env
from omnigent.inner.os_env_serialization import resolve_harness_os_env
from omnigent.inner.sandbox import HarnessSandboxValidationError, resolve_sandbox
from omnigent.runtime.workflow import _serialize_os_env

_HARNESSES = {
    "acp": None,
    "claude_sdk": "ClaudeSDKExecutor",
    "codex": "CodexExecutor",
    "copilot": None,
    "cursor": "CursorExecutor",
    "goose": "GooseExecutor",
    "hermes": None,
    "kimi": "KimiExecutor",
    "pi": "PiExecutor",
    "qwen": None,
}


@pytest.fixture(params=_HARNESSES)
def decode_sandbox(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec]:
    harness = importlib.import_module(f"omnigent.inner.{request.param}_harness")

    def decode(sandbox: OSEnvSandboxSpec) -> OSEnvSandboxSpec:
        payload = _serialize_os_env(OSEnvSpec(sandbox=sandbox))
        assert payload is not None
        monkeypatch.setenv(harness._ENV_OS_ENV, payload)
        resolved = harness._resolve_os_env()
        assert resolved.sandbox is not None
        return resolved.sandbox

    return decode


def test_harness_credential_proxy_resolves_parent_secret(
    decode_sandbox: Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = OSEnvSandboxSpec(
        type="linux_bwrap",
        write_paths=["."],
        egress_rules=["* api.example.com/**"],
        credential_proxy=CredentialProxySpec(
            entries=[
                CredentialProxyEntry(
                    host="api.example.com",
                    scheme="bearer",
                    source=CredentialSourceSpec(kind="env", env="TEST_PROXY_TOKEN"),
                    inject_env=["API_TOKEN"],
                )
            ]
        ),
    )
    decoded = decode_sandbox(sandbox)
    # Only resolve the policy; no bwrap process is spawned in this unit test.
    monkeypatch.setattr(bwrap_sandbox, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(bwrap_sandbox, "shutil", SimpleNamespace(which=lambda _: "/usr/bin/bwrap"))
    policy = resolve_sandbox(OSEnvSpec(sandbox=decoded), tmp_path)
    assert policy.active
    assert decoded == sandbox
    assert policy.credential_proxy is not None
    parent_env = _build_credential_proxy_parent_env(
        helper_env={},
        parent_env={"TEST_PROXY_TOKEN": "test-secret", "UNRELATED": "excluded"},
        spec=policy.credential_proxy,
    )
    assert parent_env == {"TEST_PROXY_TOKEN": "test-secret"}
    runtime = prepare_credential_proxy_runtime(policy.credential_proxy, parent_env=parent_env)
    assert runtime.rewrites[0].real_secret == "test-secret"
    assert runtime.helper_env_updates["API_TOKEN"].startswith("oa_cred_")
    assert "test-secret" not in runtime.helper_env_updates.values()


def test_harness_preserves_nested_sources_and_databricks_profiles(
    decode_sandbox: Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec],
) -> None:
    sources = [
        CredentialSourceSpec(kind="file", path="/tmp/token", refresh_interval_seconds=60),
        CredentialSourceSpec(kind="command", command="example-token-provider"),
        CredentialSourceSpec(
            kind="unix_socket", path="/tmp/token.sock", refresh_interval_seconds=30
        ),
    ]
    sandbox = OSEnvSandboxSpec(
        type="linux_bwrap",
        credential_proxy=CredentialProxySpec(
            entries=[
                CredentialProxyEntry(
                    host="api.example.com", scheme="basic", source=source, username="test-user"
                )
                for source in sources
            ],
            databricks=DatabricksProxySpec(
                profiles=[DatabricksProfileBinding(profile="test-workspace")],
                default="test-workspace",
                config_env="TEST_DATABRICKS_CONFIG",
            ),
        ),
    )
    assert decode_sandbox(sandbox) == sandbox


@pytest.mark.parametrize("proxy", [None, CredentialProxySpec(entries=[])])
def test_harness_preserves_absent_and_empty_credential_proxy(
    decode_sandbox: Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec],
    proxy: CredentialProxySpec | None,
) -> None:
    sandbox = OSEnvSandboxSpec(type="linux_bwrap", credential_proxy=proxy)
    assert decode_sandbox(sandbox) == sandbox


def test_harness_preserves_git_ssh_binding(
    decode_sandbox: Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec], tmp_path: Path
) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    sandbox = OSEnvSandboxSpec(type="linux_bwrap", git_ssh=[binding])
    assert decode_sandbox(sandbox) == sandbox


def test_harness_preserves_tcp_egress_route(
    decode_sandbox: Callable[[OSEnvSandboxSpec], OSEnvSandboxSpec],
) -> None:
    route = TcpEgressRoute(
        name="analytics_db",
        host="db.example.test",
        port=5432,
        allowed_cidrs=("10.42.0.0/16",),
    )
    sandbox = OSEnvSandboxSpec(type="linux_bwrap", tcp_egress=[route])
    assert decode_sandbox(sandbox) == sandbox


@pytest.mark.parametrize("harness_name", _HARNESSES)
def test_harness_rejects_invalid_nested_credential_policy(
    harness_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = importlib.import_module(f"omnigent.inner.{harness_name}_harness")
    monkeypatch.setenv(
        harness._ENV_OS_ENV,
        '{"sandbox": {"type": "linux_bwrap", "credential_proxy": {"entries": "invalid"}}}',
    )
    with pytest.raises(ValueError, match=r"credential_proxy\.entries"):
        harness._resolve_os_env()


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        "",
        "null",
        "[]",
        "false",
        '{"sandbox": false}',
        '{"sandbox": []}',
        '{"cwd": []}',
        '{"fork": "false"}',
        '{"start_in_scratch": "false"}',
        '{"sandbx": {"type": "linux_bwrap"}}',
        '{"sandbox": {"allow_network": "false"}}',
        '{"sandbox": {"cwd_hidden_scan_overflow": "erorr"}}',
        '{"sandbox": {"cwd_hidden_scan_max_entries": 0}}',
        '{"sandbox": {"cwd_hidden_scan_max_entries": -1}}',
        '{"sandbox": {"allow_netwrok": false}}',
        '{"sandbox": {"write_paths": [{"path": "/workspace", "copy_on_writ": true}]}}',
        '{"sandbox": {"credential_proxy": {"entry": []}}}',
        '{"sandbox": {"credential_proxy": {"entries": [{"host": "api.example.com", '
        '"source": {"kind": "env", "env": "API_TOKEN", "refresh_interval_second": 60}}]}}}',
    ],
)
def test_shared_decoder_rejects_malformed_os_policy(
    payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_HARNESS_OS_ENV", payload)
    with pytest.raises(HarnessSandboxValidationError):
        resolve_harness_os_env("TEST_HARNESS_OS_ENV")


@pytest.mark.parametrize("harness_name", _HARNESSES)
def test_harness_rejects_malformed_os_policy(
    harness_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = importlib.import_module(f"omnigent.inner.{harness_name}_harness")
    for name in list(os.environ):
        if name.startswith(f"HARNESS_{harness_name.upper()}_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(harness._ENV_OS_ENV, "{not-json")
    executor_name = _HARNESSES[harness_name]
    if executor_name is None:
        with pytest.raises(ValueError, match=harness._ENV_OS_ENV):
            harness._resolve_os_env()
        return
    with patch.object(getattr(harness, executor_name), "__init__") as constructor:
        with pytest.raises(ValueError, match=harness._ENV_OS_ENV):
            getattr(harness, f"_build_{harness_name}_executor")()
    constructor.assert_not_called()


@pytest.mark.parametrize("harness_name", _HARNESSES)
def test_harness_preserves_complete_os_policy(
    harness_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = importlib.import_module(f"omnigent.inner.{harness_name}_harness")
    spec = OSEnvSpec(
        cwd="/workspace",
        start_in_scratch=True,
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap", read_paths=[], write_paths=[], allow_network=False
        ),
    )
    monkeypatch.setenv(harness._ENV_OS_ENV, _serialize_os_env(spec))
    assert harness._resolve_os_env() == spec


@pytest.mark.parametrize("harness_name", _HARNESSES)
def test_harness_default_requires_absent_policy(
    harness_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = importlib.import_module(f"omnigent.inner.{harness_name}_harness")
    monkeypatch.delenv(harness._ENV_OS_ENV, raising=False)
    resolved = harness._resolve_os_env()
    assert resolved.type == "caller_process"
    assert resolved.sandbox == OSEnvSandboxSpec(type="none")
    monkeypatch.setenv(harness._ENV_OS_ENV, json.dumps({"sandbox": {"type": "none"}}))
    assert harness._resolve_os_env().sandbox == OSEnvSandboxSpec(type="none")
