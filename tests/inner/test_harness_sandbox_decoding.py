"""Harness startup must restore typed credential policies after JSON transport."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner import bwrap_sandbox
from omnigent.inner.credential_proxy import prepare_credential_proxy_runtime
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    DatabricksProfileBinding,
    DatabricksProxySpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
)
from omnigent.inner.os_env import _build_credential_proxy_parent_env
from omnigent.inner.sandbox import resolve_sandbox
from omnigent.runtime.workflow import _serialize_os_env

_HARNESSES = [
    "acp",
    "claude_sdk",
    "codex",
    "copilot",
    "cursor",
    "goose",
    "hermes",
    "kimi",
    "pi",
    "qwen",
]


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
