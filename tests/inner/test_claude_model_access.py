"""Inference authority must never turn into a general credential oracle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import sys
import time
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.claude_model_access import (
    _MAX_REQUEST_BODY_BYTES,
    HELPER_ENV,
    ClaudeAuthRequired,
    ClaudeModelAccess,
    _ClaudeCredential,
    _ClaudeRelay,
    resolve_claude_model_access,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle
from omnigent.inner.egress.certs import HostCertCache
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.egress.rules import parse_rules
from omnigent.inner.model_signer_service import _parse_strict_request_headers
from omnigent.inner.model_signing import SigningRejected
from omnigent.inner.sandbox import SandboxPolicy, _default_sandbox_for_platform
from omnigent.inner.terminal import TerminalInstance
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.registry import TerminalRegistry


def _request(access: ClaudeModelAccess, **overrides):
    kwargs = {
        "bearer_token": "parent-only-key",
        "method": "POST",
        "connect_host": "model.test",
        "sni_host": "model.test",
        "request_host": "model.test",
        "target": "/gateway/v1/messages?beta=true",
        "headers": [
            ("Authorization", f"Bearer {access.placeholder}"),
            ("Content-Type", "application/json"),
        ],
        "body": b'{"model":"claude-test","messages":[]}',
    }
    kwargs.update(overrides)
    return access.reconstruct(**kwargs)


@pytest.mark.parametrize("header", ["authorization", "x-api-key"])
@pytest.mark.parametrize(
    "client_header", ["Authorization", "aUtHoRiZaTiOn", "x-api-key", "X-Api-Key"]
)
def test_reconstructs_only_model_headers(header: str, client_header: str) -> None:
    access = ClaudeModelAccess("https://model.test/gateway", header, lambda: "parent-only-key")
    request = _request(
        access,
        headers=[
            (
                client_header,
                f"Bearer {access.placeholder}"
                if client_header.lower() == "authorization"
                else access.placeholder,
            ),
            ("Content-Type", "application/json"),
            ("anthropic-beta", "interleaved-thinking-2025-05-14"),
            ("X-Untrusted", "arbitrary"),
            ("Cookie", "session=worker"),
        ],
    )
    expected = "Bearer parent-only-key" if header == "authorization" else "parent-only-key"
    assert request.header(header) == expected
    assert request.header("x-api-key" if header == "authorization" else "authorization") is None
    assert all(access.placeholder not in value for _, value in request.headers)
    assert request.header("Cookie") is None
    assert request.header("X-Untrusted") is None
    assert request.path == "/gateway/v1/messages?beta=true"
    assert "parent-only-key" not in repr(request)
    assert "parent-only-key" not in repr(access)


@pytest.mark.parametrize("gateway", [True, False])
def test_gateway_attribution_comes_from_parent_metadata(
    monkeypatch: pytest.MonkeyPatch, gateway: bool
) -> None:
    from omnigent.inner import claude_model_access as module

    base_url = "https://gateway.example.test/anthropic"
    monkeypatch.setattr(module, "is_databricks_ai_gateway_url", lambda _: gateway)
    via_ug = Mock(return_value=True)
    monkeypatch.setattr(module, "is_ug_route", via_ug)
    monkeypatch.setenv("OMNIGENT_CLIENT_SURFACE", "desktop")
    access = ClaudeModelAccess(base_url, "authorization", lambda: "parent-only-key")
    monkeypatch.setenv("OMNIGENT_CLIENT_SURFACE", "ios")
    request = _request(
        access,
        connect_host=access.host,
        sni_host=access.host,
        request_host=access.host,
        target=access.paths[0],
        headers=[
            ("Authorization", f"Bearer {access.placeholder}"),
            ("Content-Type", "application/json"),
            ("Databricks-Custom-Coding-Agent", "worker-agent"),
            ("Databricks-Custom-Client-Surfaces", "worker-surface"),
            ("x-databricks-use-coding-agent-mode", "false"),
        ],
    )
    assert request.header("x-databricks-use-coding-agent-mode") is None
    assert request.header("Databricks-Custom-Coding-Agent") == ("claude-code" if gateway else None)
    assert request.header("Databricks-Custom-Client-Surfaces") == (
        "omnigent-desktop, ug" if gateway else None
    )
    if gateway:
        via_ug.assert_called_once_with(base_url)
    else:
        via_ug.assert_not_called()


@pytest.mark.parametrize(
    "auth_headers",
    [
        [],
        [("Authorization", "Bearer wrong")],
        [("x-api-key", "wrong")],
        [("Authorization", "bearer {placeholder}")],
        [("x-api-key", "Bearer {placeholder}")],
        [("Authorization", "Bearer {placeholder}, Bearer wrong")],
        [("x-api-key", "{placeholder}, wrong")],
        [("Authorization", "Bearer {placeholder}"), ("x-api-key", "wrong")],
        [("Authorization", "Bearer wrong"), ("x-api-key", "{placeholder}")],
        [("Authorization", "Bearer wrong"), ("x-api-key", "wrong")],
        [("Authorization", "Bearer {placeholder}"), ("authorization", "Bearer {placeholder}")],
        [("x-api-key", "{placeholder}"), ("X-Api-Key", "{placeholder}")],
    ],
)
def test_rejects_missing_wrong_or_ambiguous_placeholder(
    auth_headers: list[tuple[str, str]],
) -> None:
    access = ClaudeModelAccess("https://model.test/gateway", "authorization", lambda: "secret")
    headers = [
        (name, value.format(placeholder=access.placeholder)) for name, value in auth_headers
    ]
    with pytest.raises(SigningRejected):
        _request(access, headers=[*headers, ("Content-Type", "application/json")])


@pytest.mark.parametrize("upstream_header", ["authorization", "x-api-key"])
@pytest.mark.parametrize("reverse", [False, True])
def test_matching_dual_placeholders_rebuild_one_parent_credential(
    upstream_header: str, reverse: bool
) -> None:
    access = ClaudeModelAccess(
        "https://model.test/gateway", upstream_header, lambda: "parent-only-key"
    )
    auth = [
        ("Authorization", f"Bearer {access.placeholder}"),
        ("X-Api-Key", access.placeholder),
    ]
    if reverse:
        auth.reverse()
    signed = _request(access, headers=[*auth, ("Content-Type", "application/json")])
    assert [
        (name, value)
        for name, value in signed.headers
        if name.lower() in ("authorization", "x-api-key")
    ] == [
        (
            upstream_header,
            "Bearer parent-only-key" if upstream_header == "authorization" else "parent-only-key",
        )
    ]


@pytest.mark.parametrize("client_header", ["Authorization", "x-api-key"])
@pytest.mark.parametrize(
    "extra_headers",
    [
        [("Host", "{placeholder}")],
        [("X-Debug", "prefix-{placeholder}")],
        [("Bad Header", "value")],
        [("X-Debug", "value\r\nInjected: yes")],
        [("X-Debug", "value\x00")],
        [("Proxy-Authorization", "other")],
        [("X-Forwarded-Host", "evil.test")],
        [("Connection", "upgrade")],
        [("Transfer-Encoding", "chunked")],
        [("Content-Encoding", "gzip")],
        [("Connection", "close"), ("connection", "close")],
    ],
)
def test_placeholder_forms_keep_shared_header_validation(
    client_header: str, extra_headers: list[tuple[str, str]]
) -> None:
    access = ClaudeModelAccess("https://model.test/gateway", "authorization", lambda: "secret")
    auth = (
        f"Bearer {access.placeholder}" if client_header == "Authorization" else access.placeholder
    )
    with pytest.raises(SigningRejected):
        _request(
            access,
            headers=[
                (client_header, auth),
                ("Content-Type", "application/json"),
                *[
                    (name, value.format(placeholder=access.placeholder))
                    for name, value in extra_headers
                ],
            ],
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "TRACE"},
        {"method": "GET"},
        {"target": "/gateway/v1/models"},
        {"target": "/gateway/v1/messages?redirect=https://evil.test"},
        {"target": "/gateway/v1/messages?beta=true&beta=true"},
        {"target": "/gateway/v1/../messages"},
        {"target": "https://model.test/gateway/v1/messages"},
        {"sni_host": "evil.test"},
        {"request_host": "evil.test"},
        {"connect_host": "evil.test"},
        {"headers": [("Content-Type", "application/json")]},
        {"body": b"{}"},
        {"body": b"[]"},
        {"body": b'{"model":NaN}'},
    ],
)
def test_rejects_out_of_scope_requests(overrides: dict) -> None:
    access = ClaudeModelAccess("https://model.test/gateway", "authorization", lambda: "secret")
    with pytest.raises(SigningRejected):
        _request(access, **overrides)


def test_relay_accepts_provider_sized_bodies_without_raising_the_shared_limit() -> None:
    access = ClaudeModelAccess("https://model.test/gateway", "authorization", lambda: "secret")
    attachment = "A" * (12 * 1024 * 1024)
    body = json.dumps({"model": "claude-test", "messages": [{"content": attachment}]}).encode()
    assert _request(access, body=body).body == body
    headers = f"Host: model.test\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    assert _ClaudeRelay._request_headers(headers)[1] == len(body)
    assert _ClaudeRelay._parse_inner_content_length(headers) == len(body)
    with pytest.raises(SigningRejected):
        _parse_strict_request_headers(headers)
    oversized = _MAX_REQUEST_BODY_BYTES + 1
    with pytest.raises(SigningRejected):
        _ClaudeRelay._request_headers(
            f"Host: model.test\r\nContent-Length: {oversized}\r\n\r\n".encode()
        )
    with pytest.raises(SigningRejected):
        _request(access, body=b" " * oversized)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://model.test",
        "https://model.test:444",
        "https://user:password@model.test",
        "https://model.test/prefix?query=true",
        "https://model.test/prefix#fragment",
        "https://model.test/%2e%2e",
        "https://model.test/../path",
        "https://*.model.test",
    ],
)
def test_rejects_ambiguous_model_authority(endpoint: str) -> None:
    with pytest.raises(ValueError):
        ClaudeModelAccess(endpoint, "authorization", lambda: "secret")


def test_worker_gets_only_placeholder_and_private_state(tmp_path: Path) -> None:
    env = {
        "ANTHROPIC_API_KEY": "secret-key",
        "ANTHROPIC_AUTH_TOKEN": "secret-bearer",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth",
        "ANTHROPIC_CUSTOM_HEADERS": "Authorization: secret-custom",
        "DATABRICKS_TOKEN": "secret-gateway",
        "OMNIGENT_DATABRICKS_TOKEN": "secret-gateway-alias",
        "HARNESS_CLAUDE_SDK_API_KEY_HELPER": "printf %s secret-helper",
        HELPER_ENV: "printf %s secret-helper",
        "NO_PROXY": "*",
        "OMNIGENT_ANTHROPIC_API_KEY": "secret-alias",
        "ANTHROPIC_FOUNDRY_API_KEY": "secret-inactive-provider",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "secret-client-passphrase",
        "OMNIGENT_CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "secret-client-alias",
        "CLAUDE_FUTURE_AUTH_TOKEN_FILE": "secret-token-file",
        "ANTHROPIC_FUTURE_SECRET": "secret-future",
        "CUSTOM_CREDENTIAL": "secret-custom-ref",
        "OMNIGENT_CUSTOM_CREDENTIAL": "secret-custom-alias",
        "CUSTOM_ENDPOINT": "https://model.test",
        "LANG": "en_US.UTF-8",
        "ANTHROPIC_MODEL": "claude-test",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000",
    }
    access = ClaudeModelAccess(
        "https://model.test",
        "x-api-key",
        lambda: "secret-key",
        source_env_names=frozenset({"CUSTOM_CREDENTIAL"}),
    )
    access.worker_environment(env, tmp_path)
    assert "secret-" not in json.dumps(env)
    assert env["ANTHROPIC_AUTH_TOKEN"] == access.placeholder
    assert env["HOME"] == str(tmp_path)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude")
    assert env["CUSTOM_ENDPOINT"] == "https://model.test"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["ANTHROPIC_MODEL"] == "claude-test"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1000"


def test_credentials_inside_granted_workspace_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / ".credentials.json"
    source.write_text("private")
    access = ClaudeModelAccess("https://model.test", "authorization", lambda: "secret", (source,))
    policy = SandboxPolicy("linux_bwrap", True, [tmp_path], [tmp_path], [], False)
    with pytest.raises(ValueError, match="outside sandbox") as error:
        access.protect_sources(policy, tmp_path)
    assert repr(str(source)) in str(error.value)
    assert repr(str(tmp_path.resolve())) in str(error.value)


@pytest.mark.parametrize(
    "backend_type,active",
    [
        pytest.param("linux_bwrap", True, id="linux_bwrap"),
        pytest.param("darwin_seatbelt", True, id="darwin_seatbelt"),
        pytest.param("windows_jobobject", True, id="windows_jobobject"),
        pytest.param("unknown", True, id="unknown"),
        pytest.param("linux_bwrap", False, id="inactive"),
    ],
)
def test_model_access_requires_backend_isolation_even_without_file_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_type: str, active: bool
) -> None:
    from omnigent.inner import claude_model_access as module

    credentials = Mock(return_value="parent-only-key")
    access = ClaudeModelAccess("https://model.test", "authorization", credentials)
    policy = SandboxPolicy(backend_type, active, [], [tmp_path], [], False)
    discover = Mock(return_value=())
    monkeypatch.setattr("omnigent.onboarding.ambient.claude_code_managed_mcp_paths", discover)
    monkeypatch.setattr(module, "_claude_managed_preferences_paths", lambda: ())

    # Environment-only credentials still require filesystem and network isolation.
    if active and backend_type in ("linux_bwrap", "darwin_seatbelt"):
        protected = access.protect_sources(policy, tmp_path)
        assert protected.backend_type == backend_type
        assert protected.credential_source_paths == []
    else:
        expected_error = (
            "requires filesystem and network isolation" if active else "active sandbox policy"
        )
        with pytest.raises(ValueError, match=expected_error) as error:
            access.protect_sources(policy, tmp_path)
        assert backend_type in str(error.value)
        discover.assert_not_called()

    credentials.assert_not_called()
    assert policy.credential_source_paths is None


def test_static_helper_never_executes_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr("subprocess.run", run)
    access = resolve_claude_model_access({HELPER_ENV: "printf %s 'private-key'"})
    assert access.credential() == "private-key"
    assert access.auth_header == "x-api-key"
    with pytest.raises(ValueError, match="shell auth helper"):
        resolve_claude_model_access({HELPER_ENV: "./workspace-helper.sh"})
    run.assert_not_called()


def test_custom_provider_config_cannot_be_granted_to_the_worker(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("providers: {private: {api_key: parent-only}}")
    access = resolve_claude_model_access(
        {"ANTHROPIC_API_KEY": "parent-only", "OMNIGENT_CONFIG_HOME": str(tmp_path)}
    )
    policy = SandboxPolicy("linux_bwrap", True, [tmp_path], [], [], False)
    with pytest.raises(ValueError, match="outside sandbox-readable paths") as error:
        access.protect_sources(policy, tmp_path)
    assert repr(str(config)) in str(error.value)
    assert repr(str(tmp_path.resolve())) in str(error.value)


@pytest.mark.parametrize("custom_home", [False, True])
@pytest.mark.parametrize("alias", ["symlink", "hardlink", "grant"])
def test_file_backed_provider_secrets_cannot_alias_worker_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_home: bool, alias: str
) -> None:
    home = tmp_path / "host-home"
    monkeypatch.setattr(Path, "home", lambda: home)
    config_home = tmp_path / "provider-config" if custom_home else home / ".omnigent"
    config_home.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secrets = config_home / "secrets.json"
    exposed = workspace / "provider-secrets.json"
    exposed.write_text('{"anthropic":"parent-only-secret"}')
    if alias == "symlink":
        secrets.symlink_to(exposed)
    elif alias == "hardlink":
        os.link(exposed, secrets)
    else:
        secrets.write_text(exposed.read_text())
    env = {"ANTHROPIC_AUTH_TOKEN": "parent-only-secret"}
    if custom_home:
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    access = resolve_claude_model_access(env)
    policy = SandboxPolicy("linux_bwrap", True, [], [workspace], [], False)
    if alias == "grant":
        policy.read_roots = [secrets]
    with pytest.raises(ValueError, match=r"outside sandbox|hard-linked"):
        access.protect_sources(policy, workspace)


def test_bundle_sources_include_config_aliases_without_exporting_skills(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "skills/probe").mkdir(parents=True)
    (bundle / "skills/probe/SKILL.md").write_text("supporting skill")
    (bundle / "config.yaml").write_text("api_key: private")
    (bundle / ".mcp.json").write_text("private MCP configuration")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exposed = workspace / "other-config.yaml"
    exposed.write_text("api_key: private")
    (bundle / "linked-config.yaml").symlink_to(exposed)
    original = ClaudeModelAccess("https://model.test", "authorization", lambda: "private")
    assert original.with_bundle_sources(None) is original
    access = original.with_bundle_sources(bundle)
    assert set(access.source_paths) == {
        bundle,
        bundle / "config.yaml",
        bundle / ".mcp.json",
        bundle / "linked-config.yaml",
    }
    assert original.source_paths == ()
    policy = SandboxPolicy("linux_bwrap", True, [], [workspace], [], False)
    with pytest.raises(ValueError, match="outside sandbox"):
        access.protect_sources(policy, workspace)


@pytest.mark.parametrize(
    "grant_kind,grant_target",
    [
        ("read_root", "directory"),
        ("read_root", "child"),
        ("write_root", "directory"),
        ("write_root", "child"),
        ("write_file", "secret"),
        ("cwd", "child"),
    ],
)
def test_bundle_directory_symlink_targets_cannot_be_granted(
    tmp_path: Path, grant_kind: str, grant_target: str
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    external = tmp_path / "external-config"
    child = external / "nested"
    child.mkdir(parents=True)
    secret = child / "credentials.json"
    secret.write_text('{"api_key":"parent-only-key"}')
    alias = bundle / "private-config"
    alias.symlink_to(external, target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    access = ClaudeModelAccess(
        "https://model.example.test", "authorization", lambda: "parent-only-key"
    ).with_bundle_sources(bundle)
    target = {"directory": external, "child": child, "secret": secret}[grant_target]
    policy = SandboxPolicy("linux_bwrap", True, [], [workspace], [], False)
    cwd = workspace
    if grant_kind == "read_root":
        policy.read_roots.append(target)
    elif grant_kind == "write_root":
        policy.write_roots.append(target)
    elif grant_kind == "write_file":
        policy.write_files.append(target)
    else:
        cwd = target

    with pytest.raises(ValueError, match="outside sandbox") as error:
        access.protect_sources(policy, cwd)

    assert repr(str(alias)) in str(error.value)
    assert repr(str(target)) in str(error.value)
    assert alias in access.source_paths


def test_create_proxy_requires_upstream_ca_bundle_before_loading_certificates(
    tmp_path: Path,
) -> None:
    access = ClaudeModelAccess(
        "https://model.example.test", "authorization", lambda: "parent-only-key"
    )

    with pytest.raises(ValueError, match="upstream"):
        access.create_proxy([], tmp_path / "ca.pem", tmp_path / "ca-key.pem")


def _write_oauth(path: Path, token: str = "initial") -> None:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "parent-only-refresh",
                    "expiresAt": (time.time() + 300) * 1000,
                }
            }
        )
    )


@pytest.fixture
def oauth_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_oauth(config / ".credentials.json")
    return config, workspace


@pytest.mark.parametrize("grant_kind", ["read_root", "write_root", "write_file", "cwd"])
@pytest.mark.parametrize("alias", [False, True])
def test_grants_nested_in_protected_directories_are_rejected(
    oauth_paths: tuple[Path, Path], grant_kind: str, alias: bool
) -> None:
    config, workspace = oauth_paths
    nested = config / "nested"
    nested.mkdir()
    if alias:
        link = workspace.parent / "grant-alias"
        link.symlink_to(nested, target_is_directory=True)
        nested = link
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    cwd = workspace
    if grant_kind == "read_root":
        policy.read_roots.append(nested)
    elif grant_kind == "write_root":
        policy.write_roots.append(nested)
    elif grant_kind == "write_file":
        policy.write_files.append(nested / "credential-copy")
    else:
        cwd = nested
    access = resolve_claude_model_access({})
    with pytest.raises(ValueError, match="outside sandbox"):
        access.protect_sources(policy, cwd)


@pytest.mark.parametrize("custom_config", [False, True])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_actual_oauth_files_cannot_alias_a_worker_grant(
    oauth_paths: tuple[Path, Path], custom_config: bool, alias_kind: str
) -> None:
    config, workspace = oauth_paths
    source = config / ".credentials.json"
    exposed = workspace / "credential-alias"
    if alias_kind == "symlink":
        source.replace(exposed)
        source.symlink_to(exposed)
    else:
        os.link(source, exposed)
    # The default CLI credential file stays private even with a custom config dir.
    env = {"CLAUDE_CONFIG_DIR": str(config.parent / "custom")} if custom_config else {}
    access = resolve_claude_model_access(env)
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    with pytest.raises(ValueError, match=r"outside sandbox|hard-linked"):
        access.protect_sources(policy, workspace)


@pytest.mark.parametrize("entrypoint", ["cli_callback", "terminal", "terminal_override"])
def test_relative_config_dir_provenance_uses_session_cwd(
    oauth_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    from omnigent.inner import claude_sdk_executor as claude
    from omnigent.runtime import workflow

    config, workspace = oauth_paths
    parent = workspace.parent / "daemon" / "cwd"
    parent.mkdir(parents=True)
    monkeypatch.chdir(parent)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    for name in tuple(os.environ):
        if name.startswith(("CLAUDE_", "ANTHROPIC_", "HARNESS_")):
            monkeypatch.delenv(name)
    env = {"CLAUDE_CONFIG_DIR": os.path.relpath(config, workspace)}
    # Parent settings must not replace the session's credential provenance.
    settings = {"env": {"ANTHROPIC_AUTH_TOKEN": "session-only-key"}}
    (config / "settings.json").write_text(json.dumps(settings))
    parent_config = parent / env["CLAUDE_CONFIG_DIR"]
    parent_config.mkdir(parents=True)
    (parent_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "wrong-parent-key"}})
    )
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    if entrypoint == "cli_callback":
        env["ANTHROPIC_AUTH_TOKEN"] = "session-only-key"
        monkeypatch.setattr(claude, "resolve_sandbox", lambda *_: policy)
        backend = Mock()
        backend.wrap_launcher_argv.side_effect = lambda argv, *args, **kwargs: ["bwrap", *argv]
        monkeypatch.setattr(claude, "get_backend", lambda _: backend)
        proxy = Mock(
            relay_port=34567,
            socket_path=workspace / "relay.sock",
            ca_bundle_path=workspace / "ca.pem",
        )
        start_proxy = Mock(return_value=proxy)
        monkeypatch.setattr(claude, "start_egress_proxy", start_proxy)
        launcher = workspace / "launcher"
        launcher.touch()
        launch = Mock(return_value=str(launcher))
        monkeypatch.setattr(claude, "create_exec_launcher", launch)
        spec = OSEnvSpec(cwd=".", sandbox=OSEnvSandboxSpec(type="linux_bwrap"))
        prepared = claude.prepare_claude_cli_path("/bin/claude", spec, worker_env=dict(env))
        try:
            access = prepared.model_access
            assert access is not None
            assert prepared.sandbox is not None
            assert config in (prepared.sandbox.credential_source_paths or [])
        finally:
            prepared.close()
        start_proxy.reset_mock()
        launch.reset_mock()
        policy.read_roots.append(config)
        with pytest.raises(ValueError, match="outside sandbox"):
            claude.prepare_claude_cli_path("/bin/claude", spec, worker_env=dict(env))
        start_proxy.assert_not_called()
        launch.assert_not_called()
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
        monkeypatch.setattr(workflow, "_build_claude_sdk_spawn_env", lambda _: {})
        spec = AgentSpec(
            spec_version=1,
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
            os_env=OSEnvSpec(cwd=str(parent) if entrypoint == "terminal_override" else "."),
        )
        access = workflow.claude_terminal_model_access(
            spec, cwd=workspace if entrypoint == "terminal_override" else None
        )
        assert access is not None
        protected = access.protect_sources(policy, workspace)
        assert config in (protected.credential_source_paths or [])
        policy.read_roots.append(config)
        with pytest.raises(ValueError, match="outside sandbox"):
            access.protect_sources(policy, workspace)
    assert config in access.source_paths
    assert access.credential() == "session-only-key"


def test_private_oauth_symlink_is_protected_and_atomic_rotation_works(
    oauth_paths: tuple[Path, Path],
) -> None:
    config, workspace = oauth_paths
    source = config / ".credentials.json"
    target = config.parent / "private-oauth.json"
    source.replace(target)
    source.symlink_to(target)
    access = resolve_claude_model_access({})
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    protected = access.protect_sources(policy, workspace)
    assert target in protected.credential_source_paths
    assert policy.credential_source_paths is None
    assert access.credential() == "initial"
    replacement = config.parent / "rotated-oauth.json"
    _write_oauth(replacement, "rotated")
    replacement.replace(target)
    assert access.credential() == "rotated"


@pytest.mark.parametrize("mutation", ["file_symlink", "directory_symlink", "hardlink", "fifo"])
async def test_oauth_reread_revalidates_aliases_and_file_type(
    oauth_paths: tuple[Path, Path], mutation: str
) -> None:
    config, workspace = oauth_paths
    access = resolve_claude_model_access({})
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    access.protect_sources(policy, workspace)
    credential = _ClaudeCredential(access.credential)
    assert await credential.token_for_request() == "initial"
    source = config / ".credentials.json"
    exposed = workspace / ".credentials.json"
    _write_oauth(exposed, "worker-controlled-secret")
    if mutation == "file_symlink":
        source.unlink()
        source.symlink_to(exposed)
    elif mutation == "directory_symlink":
        source.unlink()
        config.rmdir()
        config.symlink_to(workspace, target_is_directory=True)
    elif mutation == "hardlink":
        os.link(source, workspace / "hardlink")
    else:
        source.unlink()
        os.mkfifo(source)
    credential.invalidate_after_unauthorized()
    with pytest.raises(ClaudeAuthRequired, match="authentication is unavailable") as error:
        await credential.token_for_request()
    assert "worker-controlled-secret" not in str(error.value)
    assert "parent-only-refresh" not in str(error.value)


async def test_inference_works_without_rules_and_broad_rules_cannot_bypass_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise CONNECT, TLS, signing, streaming, and rejection on real sockets."""
    cert, key = ensure_ca(cache_dir=tmp_path)
    bundle = ensure_ca_bundle(cert, cache_dir=tmp_path)
    server_context = HostCertCache(cert, key).get_ssl_context("model.test")
    received: list[bytes] = []

    async def provider(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        body = await reader.readexactly(length)
        received.append(head + body)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Set-Cookie: parent-only-cookie\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"b\r\ndata: test\n\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=server_context)
    upstream_port = upstream.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def connect(host, port, **kwargs):
        assert host == "127.0.0.1" or host == "model.test"
        return await original_connect(
            "127.0.0.1", upstream_port if port == 443 else port, **kwargs
        )

    async def allow_destination(self, host, port):
        return "127.0.0.1"

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(EgressProxy, "_assert_destination_allowed", allow_destination)
    credentials = Mock(return_value="parent-only-key")
    access = ClaudeModelAccess("https://model.test/gateway", "x-api-key", credentials)
    relay = access.create_proxy([], cert, key, upstream_ca_bundle=bundle)
    token_for_request = AsyncMock(wraps=relay._credential_source.token_for_request)
    monkeypatch.setattr(relay._credential_source, "token_for_request", token_for_request)
    proxy_port = await relay.start_tcp()
    context = ssl.create_default_context(cafile=str(bundle))
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}", verify=context, trust_env=False
        ) as client:
            for count, client_header in enumerate(
                ("Authorization", "x-api-key", "both", "both-reversed"), start=1
            ):
                headers = {
                    "Authorization": "Bearer " + access.placeholder,
                    "x-api-key": access.placeholder,
                }
                if client_header == "both-reversed":
                    headers = dict(reversed(list(headers.items())))
                elif client_header != "both":
                    headers = {client_header: headers[client_header]}
                response = await client.post(
                    "https://model.test/gateway/v1/messages?beta=true",
                    json={"model": "claude-test", "messages": []},
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.text == "data: test\n"
                assert "set-cookie" not in response.headers
                assert b"x-api-key: parent-only-key\r\n" in received[-1]
                assert b"authorization:" not in received[-1].lower()
                assert access.placeholder.encode() not in received[-1]
                assert len(received) == count
            assert token_for_request.await_count == 4
            token_for_request.reset_mock()
            relay._rules = parse_rules(["* model.test/**"])
            for method, path in (
                ("POST", "/gateway/v1/messages?redirect=evil"),
                ("POST", "/gateway/v1/models"),
                ("TRACE", "/gateway/v1/messages"),
            ):
                denied = await client.request(method, "https://model.test" + path, headers=headers)
                assert denied.status_code == 403
            assert len(received) == 4
            token_for_request.assert_not_awaited()
            for auth_headers in (
                [("Authorization", "Bearer wrong")],
                [
                    ("Authorization", "Bearer " + access.placeholder),
                    ("x-api-key", "wrong"),
                ],
                [("x-api-key", access.placeholder), ("X-Api-Key", access.placeholder)],
            ):
                denied = await client.post(
                    "https://model.test/gateway/v1/messages",
                    json={"model": "test"},
                    headers=auth_headers,
                )
                assert denied.status_code == 403
            token_for_request.assert_not_awaited()
            assert len(received) == 4
            denied = await client.post(
                "http://model.test/gateway/v1/messages", json={"model": "test"}, headers=headers
            )
            assert denied.status_code == 403
            token_for_request.assert_not_awaited()
            credentials.assert_called_once_with()
    finally:
        await relay.stop()
        upstream.close()
        await upstream.wait_closed()


async def test_explicit_invalidation_refreshes_cached_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(
        "omnigent.inner.claude_model_access.time", SimpleNamespace(monotonic=clock)
    )
    resolve = Mock(side_effect=["first", "expired", "refreshed"])
    source = _ClaudeCredential(resolve)
    assert await source.token_for_request() == "first"
    clock.return_value = 129.999
    assert await source.token_for_request() == "first"
    clock.return_value = 130.0
    assert await source.token_for_request() == "expired"
    source.invalidate_after_unauthorized()
    assert await source.token_for_request() == "refreshed"


async def test_upstream_401_refreshes_credential_on_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real upstream response must invalidate the relay's credential cache."""
    cert, key = ensure_ca(cache_dir=tmp_path)
    bundle = ensure_ca_bundle(cert, cache_dir=tmp_path)
    received: list[bytes] = []

    async def provider(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        await reader.readexactly(length)
        received.append(head)
        status = b"401 Unauthorized" if len(received) == 1 else b"200 OK"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(
        provider, "127.0.0.1", 0, ssl=HostCertCache(cert, key).get_ssl_context("model.test")
    )
    upstream_port = upstream.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def connect(host, port, **kwargs):
        assert host in ("127.0.0.1", "model.test")
        return await original_connect(
            "127.0.0.1", upstream_port if port == 443 else port, **kwargs
        )

    async def allow_destination(self, host, port):
        return "127.0.0.1"

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(EgressProxy, "_assert_destination_allowed", allow_destination)
    credentials = Mock(side_effect=["first", "refreshed"])
    access = ClaudeModelAccess("https://model.test/gateway", "authorization", credentials)
    relay = access.create_proxy([], cert, key, upstream_ca_bundle=bundle)
    proxy_port = await relay.start_tcp()
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}",
            verify=ssl.create_default_context(cafile=str(bundle)),
            trust_env=False,
        ) as client:
            for index, expected_status in enumerate((401, 200, 200)):
                response = await client.post(
                    "https://model.test/gateway/v1/messages",
                    json={"model": "claude-test", "messages": []},
                    headers={"x-api-key": access.placeholder},
                )
                assert response.status_code == expected_status
                assert credentials.call_count == min(index + 1, 2)
        assert len(received) == 3
        assert b"authorization: Bearer first\r\n" in received[0]
        assert all(b"authorization: Bearer refreshed\r\n" in head for head in received[1:])
        assert all(access.placeholder.encode() not in head for head in received)
    finally:
        await relay.stop()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.parametrize(
    ("sandbox_type", "sdk_owned", "fork", "inherited", "use_factory"),
    [
        ("linux_bwrap", True, False, False, True),
        ("none", True, False, True, False),
        (None, True, False, True, False),
        (None, True, False, False, True),
        (None, False, False, False, False),
        (None, False, False, True, True),
        (None, True, True, False, False),
    ],
    ids=[
        "explicit-sdk-factory",
        "sdk-optout-inherited",
        "implicit-sdk-resolver-inherited",
        "implicit-sdk-factory",
        "other-harness-resolver",
        "other-harness-factory-inherited",
        "protected-sdk-fork",
    ],
)
async def test_terminal_inherits_automatic_model_access_only_when_sandboxed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sandbox_type: str | None,
    sdk_owned: bool,
    fork: bool,
    inherited: bool,
    use_factory: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = (workspace if fork else tmp_path) / "provider.json"
    source.write_text("{}")
    access = ClaudeModelAccess("https://model.test", "x-api-key", lambda: "secret", (source,))

    def resolve(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        assert spec.sandbox is not None
        expected_type = sandbox_type or _default_sandbox_for_platform().type
        assert spec.sandbox.type == expected_type
        return SandboxPolicy(spec.sandbox.type, True, [cwd], [cwd], [], False)

    # Keep construction and credential protection real, without starting platform processes.
    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    monkeypatch.setattr(terminal_mod, "create_os_environment", lambda *args, **kwargs: None)
    monkeypatch.setattr(terminal_mod, "resolve_sandbox", resolve)
    launch_process = AsyncMock()
    monkeypatch.setattr(TerminalInstance, "launch", launch_process)
    monkeypatch.setattr(TerminalInstance, "is_alive", AsyncMock(return_value=True))
    if fork:
        monkeypatch.setattr(
            terminal_mod,
            "_copy_tree",
            Mock(side_effect=AssertionError("credential sources must be rejected before copying")),
        )
    registry = TerminalRegistry()
    resolver = AsyncMock(return_value=access if sdk_owned else None)
    factory = Mock(return_value=access if sdk_owned else None)
    if not use_factory:
        registry.model_access_resolver = resolver
        factory.side_effect = AssertionError("the installed resolver must take precedence")
    environment_spec = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(type=sandbox_type) if sandbox_type is not None else None,
        fork=fork,
    )
    terminal_spec = TerminalEnvSpec(os_env="inherit" if inherited else environment_spec)
    try:
        launch = registry.launch(
            "conversation",
            "bash",
            "s1",
            terminal_spec,
            parent_os_env=environment_spec if inherited else None,
            model_access_factory=factory,
        )
        if fork:
            with pytest.raises(RuntimeError, match="terminal model access rejected"):
                await launch
            launch_process.assert_not_awaited()
            resolver.assert_awaited_once_with("conversation", workspace)
            factory.assert_not_called()
            return
        instance = await launch
        sandboxed = sdk_owned and sandbox_type != "none"
        assert (instance.sandbox_policy is not None) == sandboxed
        if sandboxed:
            assert instance.model_access is access
            assert instance.sandbox_policy is not None
            assert instance.sandbox_policy.active
            assert instance.sandbox_policy.credential_source_paths is not None
            assert source in instance.sandbox_policy.credential_source_paths
        else:
            assert instance.model_access is None
        if sandbox_type == "none":
            resolver.assert_not_called()
            factory.assert_not_called()
        elif use_factory:
            factory.assert_called_once_with()
            resolver.assert_not_called()
        else:
            resolver.assert_awaited_once_with("conversation", workspace)
            factory.assert_not_called()
        if sandbox_type is None:
            assert environment_spec.sandbox is None
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("other_harness", ["claude-native", "codex-native", "codex"])
def test_terminal_uses_selected_provider_and_does_not_grant_other_harnesses(
    monkeypatch: pytest.MonkeyPatch,
    other_harness: str,
) -> None:
    from omnigent.runtime import workflow

    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    build = Mock(
        return_value={
            "HARNESS_CLAUDE_SDK_GATEWAY": "true",
            "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL": "https://model.test/gateway",
            "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND": "printf %s provider-secret",
        }
    )
    monkeypatch.setattr(workflow, "_build_claude_sdk_spawn_env", build)
    access = workflow.claude_terminal_model_access(spec)
    assert access is not None
    assert access.base_url == "https://model.test/gateway"
    assert access.credential() == "provider-secret"
    build.reset_mock()
    spec.executor.config["harness"] = other_harness
    assert workflow.claude_terminal_model_access(spec) is None
    build.assert_not_called()


@pytest.mark.parametrize(
    ("field", "reference"),
    [
        ("api_key_ref", "env:CUSTOM_CREDENTIAL"),
        ("api_key_ref", "$CUSTOM_CREDENTIAL"),
        ("api_key_ref", "${CUSTOM_CREDENTIAL}"),
        ("api_key", "$CUSTOM_CREDENTIAL"),
        ("api_key", "${CUSTOM_CREDENTIAL}"),
    ],
)
@pytest.mark.parametrize("alias_only", [False, True])
def test_provider_env_refs_survive_helper_mapping_and_are_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, reference: str, alias_only: bool
) -> None:
    from omnigent.onboarding import provider_config
    from omnigent.runtime import workflow

    config = {
        "providers": {
            "custom": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    field: reference,
                    "base_url": "$CUSTOM_ENDPOINT",
                    "models": {"default": "claude-test"},
                },
            }
        }
    }
    monkeypatch.setattr(workflow, "load_config", lambda: config)
    monkeypatch.setattr(provider_config, "_load_config", lambda: config)
    monkeypatch.setattr(workflow, "effective_config_with_detected", lambda config: config)
    monkeypatch.delenv("CUSTOM_CREDENTIAL", raising=False)
    if not alias_only:
        monkeypatch.setenv("CUSTOM_CREDENTIAL", "parent-provider-secret")
    monkeypatch.setenv("OMNIGENT_CUSTOM_CREDENTIAL", "parent-alias-secret")
    monkeypatch.setenv("CUSTOM_ENDPOINT", "https://model.test/gateway")
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    launch_env = workflow._build_claude_sdk_spawn_env(spec)
    names = json.loads(launch_env["HARNESS_CLAUDE_SDK_CREDENTIAL_ENV_NAMES"])
    assert set(names) == {"CUSTOM_CREDENTIAL", "OMNIGENT_CUSTOM_CREDENTIAL"}
    assert "secret" not in json.dumps(names)
    access = workflow.claude_terminal_model_access(spec)
    assert access is not None
    assert access.credential() == (
        "parent-alias-secret" if alias_only else "parent-provider-secret"
    )
    env = {
        "CUSTOM_CREDENTIAL": "parent-provider-secret",
        "OMNIGENT_CUSTOM_CREDENTIAL": "parent-alias-secret",
        "CUSTOM_ENDPOINT": "https://model.test/gateway",
        "DEPLOYMENT_NAME": "staging",
        **launch_env,
    }
    access.worker_environment(env, tmp_path / "worker")
    assert "CUSTOM_CREDENTIAL" not in env
    assert "OMNIGENT_CUSTOM_CREDENTIAL" not in env
    assert "secret" not in json.dumps(env)
    assert env["CUSTOM_ENDPOINT"] == "https://model.test/gateway"
    assert env["DEPLOYMENT_NAME"] == "staging"


def test_provider_env_refs_include_local_named_auth_under_managed_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inference_config import inference_config_scope
    from omnigent.onboarding import provider_config
    from omnigent.runtime import workflow
    from omnigent.spec.types import ProviderAuth

    local_config = {
        "providers": {
            "local-named": {
                "kind": "gateway",
                "anthropic": {
                    "api_key_ref": "env:LOCAL_CREDENTIAL",
                    "base_url": "https://model.test/gateway",
                    "models": {"default": "claude-test"},
                },
            }
        }
    }
    monkeypatch.setattr(provider_config, "_load_config", lambda: local_config)
    monkeypatch.setattr(workflow, "effective_config_with_detected", lambda config: config)
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", "scope-supplies-overlay")
    monkeypatch.setenv("LOCAL_CREDENTIAL", "parent-only-local-secret")
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
            auth=ProviderAuth(name="local-named"),
        ),
    )
    with inference_config_scope({"providers": {}}):
        access = workflow.claude_terminal_model_access(spec)
    assert access is not None
    assert access.credential() == "parent-only-local-secret"
    env = {"LOCAL_CREDENTIAL": "parent-only-local-secret", "OMNIGENT_LOCAL_CREDENTIAL": "alias"}
    access.worker_environment(env, tmp_path / "worker")
    assert "LOCAL_CREDENTIAL" not in env
    assert "OMNIGENT_LOCAL_CREDENTIAL" not in env


@pytest.mark.parametrize("config_path", ["absolute", "relative", "literal_tilde"])
def test_host_oauth_file_is_reread_without_exposing_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_path: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / "~" / "state" if config_path == "literal_tilde" else tmp_path / "state"
    config.mkdir(parents=True)
    source = config / ".credentials.json"
    monkeypatch.chdir(parent)

    _write_oauth(source, "initial")
    configured = str(config) if config_path == "absolute" else os.path.relpath(config, workspace)
    access = resolve_claude_model_access({"CLAUDE_CONFIG_DIR": configured}, cwd=workspace)
    assert config in access.source_paths
    assert access.oauth
    assert access.credential() == "initial"
    monkeypatch.chdir(workspace)
    _write_oauth(source, "renewed")
    assert access.credential() == "renewed"
    env: dict[str, str] = {}
    access.worker_environment(env, tmp_path / "worker")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == access.placeholder
    assert "parent-only-refresh" not in json.dumps(env)
    assert "renewed" not in json.dumps(env)


def test_databricks_auth_pins_the_trusted_executable_before_worker_start(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import claude_model_access as module
    from omnigent.inner.model_auth import _TrustedExecutable

    interpreter, _, _ = auth_runtime
    path = tmp_path / "profile-auth"
    path.write_text(f"#!{interpreter}\nprint('unused')\n")
    info = path.stat()
    executable = _TrustedExecutable(path, (info.st_dev, info.st_ino))
    monkeypatch.setattr(module, "_resolve_auth_executable", lambda: executable)
    mint = AsyncMock(return_value="parent-bearer")
    monkeypatch.setattr(module, "mint_profile_token", mint)
    access = resolve_claude_model_access(
        {HELPER_ENV: "trusted-generated-auth", "ANTHROPIC_BASE_URL": "https://model.test/gateway"},
        databricks_profile="selected-profile",
    )
    assert access.credential() == "parent-bearer"
    assert access.auth_executable is not None
    assert access.auth_executable.path == executable.path
    assert access.auth_executable.interpreter == interpreter
    mint.assert_awaited_once_with(
        host="https://model.test",
        profile="selected-profile",
        _executable=access.auth_executable,
        _config_file=(Path.home() / ".databrickscfg").resolve(),
    )
    policy = SandboxPolicy("linux_bwrap", True, [tmp_path], [tmp_path], [], False)
    with pytest.raises(ValueError, match=r"auth executable.*outside sandbox-writable"):
        access.protect_sources(policy, tmp_path)


@pytest.fixture
def auth_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    from omnigent.inner import claude_model_access as module_access
    from omnigent.inner.model_auth import _TrustedExecutable

    runtime = tmp_path / "auth-venv"
    # Linux CI's shared Python can be world-writable; keep its binary private.
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "linux").create(runtime)
    interpreter = runtime / "bin/python"
    if sys.platform == "linux":
        interpreter.unlink()
        interpreter.symlink_to(f"python{sys.version_info.major}.{sys.version_info.minor}")
        # CI's base Python can be world-writable; the venv metadata needs a trusted copy.
        base_executable = tmp_path / "trusted-base-python"
        shutil.copy2(sys._base_executable, base_executable)
        base_executable.chmod(0o500)
        config = runtime / "pyvenv.cfg"
        config.write_text(
            "\n".join(
                f"executable = {base_executable}" if line.startswith("executable = ") else line
                for line in config.read_text().splitlines()
            )
            + "\n"
        )
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    module = packages / "profile_auth_impl.py"
    module.write_text("print('separate-runtime-token')\n")
    bin_dir = tmp_path / "trusted-bin"
    bin_dir.mkdir()
    executable = bin_dir / "profile-auth"
    executable.write_text(f"#!{interpreter}\nimport profile_auth_impl\n")
    executable.chmod(0o500)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    info = executable.stat()
    pinned_executable = _TrustedExecutable(executable, (info.st_dev, info.st_ino))
    monkeypatch.setattr(module_access, "_resolve_auth_executable", lambda: pinned_executable)
    return interpreter, module, workspace


def _profile_auth_access(env: dict[str, str] | None = None) -> ClaudeModelAccess:
    return resolve_claude_model_access(
        {
            HELPER_ENV: "trusted-generated-auth",
            "ANTHROPIC_BASE_URL": "https://model.test/gateway",
            **(env or {}),
        },
        databricks_profile="selected-profile",
    )


@pytest.mark.parametrize(
    "target,grant_kind",
    [
        ("module", "write_files"),
        ("packages", "write_roots"),
        ("interpreter", "write_files"),
        ("resolved-interpreter", "write_files"),
        ("venv-config", "write_files"),
        ("venv", "write_roots"),
    ],
)
def test_auth_runtime_writes_are_rejected_before_refresh(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path, target: str, grant_kind: str
) -> None:
    interpreter, module, workspace = auth_runtime
    access = _profile_auth_access()
    assert access.credential() == "separate-runtime-token"
    marker = tmp_path / "poisoned-import-ran"
    module.write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
    (module.parent / "sitecustomize.py").write_text(module.read_text())
    grant = {
        "module": module,
        "packages": module.parent,
        "interpreter": interpreter,
        "resolved-interpreter": interpreter.resolve(),
        "venv-config": interpreter.parent.parent / "pyvenv.cfg",
        "venv": interpreter.parent.parent,
    }[target]
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    getattr(policy, grant_kind).append(grant)
    with pytest.raises(ValueError, match=r"auth executable and runtime.*outside sandbox-writable"):
        access.protect_sources(policy, workspace)
    assert not marker.exists()


@pytest.mark.parametrize("target", ["helper", "startup-import"])
def test_profile_auth_rejects_workspace_hardlink_to_trusted_code(
    auth_runtime: tuple[Path, Path, Path], target: str
) -> None:
    _, module, workspace = auth_runtime
    trusted = workspace.parent / "trusted-bin" / "profile-auth" if target == "helper" else module
    if target == "helper":
        trusted.chmod(0o700)
    access = _profile_auth_access()
    original_identity = (trusted.stat().st_dev, trusted.stat().st_ino)
    alias = workspace / "editable.py"
    os.link(trusted, alias)
    alias.write_text("print('modified in place')\n")
    assert (trusted.stat().st_dev, trusted.stat().st_ino) == original_identity

    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    with pytest.raises(ValueError, match=r"auth executable and runtime.*outside sandbox-writable"):
        access.protect_sources(policy, workspace)


def test_profile_auth_rejects_hardlinked_pth_before_startup_probe(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, module, workspace = auth_runtime
    startup = module.parent / "startup.pth"
    startup.write_text("# clean startup\n")
    alias = workspace / "editable.pth"
    os.link(startup, alias)
    marker = tmp_path / "startup-ran"
    alias.write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    access = _profile_auth_access()
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)

    with pytest.raises(
        ValueError, match=r"auth executable and runtime.*outside sandbox-writable"
    ) as error:
        access.protect_sources(policy, workspace)
    assert error.value.__cause__ is not None
    assert "hard-link alias" in str(error.value.__cause__)
    assert not marker.exists(), "the isolated startup probe must not run poisoned .pth code"


def test_profile_auth_protects_pth_imports_without_rerunning_startup_hooks(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, module, workspace = auth_runtime
    external = tmp_path / "external-imports"
    external.mkdir()
    (module.parent / "external.pth").write_text(str(external) + "\n")
    access = _profile_auth_access()
    access.protect_sources(
        SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False), workspace
    )
    marker = tmp_path / "startup-hook-ran"
    (external / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    )
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [external], [], False)
    with pytest.raises(ValueError, match=r"auth executable and runtime.*outside sandbox-writable"):
        access.protect_sources(policy, workspace)
    assert not marker.exists()


def test_profile_auth_readonly_separate_runtime_ignores_hostile_pythonpath(
    auth_runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, workspace = auth_runtime
    (workspace / "profile_auth_impl.py").write_text("print('workspace-controlled-token')\n")
    monkeypatch.setenv("PYTHONPATH", str(workspace))
    access = _profile_auth_access()
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    access.protect_sources(policy, workspace)
    assert access.credential() == "separate-runtime-token"


def test_profile_auth_rejects_unsupported_script_runtime(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    executable = tmp_path / "trusted-bin/profile-auth"
    executable.chmod(0o700)
    executable.write_text("#!/bin/sh\nprintf 'shell-token\\n'\n")
    with pytest.raises(ValueError, match="requires an absolute Python interpreter shebang"):
        _profile_auth_access()


def test_profile_auth_snapshots_and_protects_selected_databricks_config(
    auth_runtime: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, module, workspace = auth_runtime
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    default = home / ".databrickscfg"
    default.write_text("[selected-profile]\ntoken=wrong-default-token\n")
    selected = tmp_path / "selected.databrickscfg"
    selected.write_text("[selected-profile]\ntoken=approved-selected-token\n")
    module.write_text(
        "import configparser, os\n"
        "from pathlib import Path\n"
        "config = configparser.ConfigParser()\n"
        "config.read(os.environ.get('DATABRICKS_CONFIG_FILE', Path.home() / '.databrickscfg'))\n"
        "print(config['selected-profile']['token'])\n"
    )
    env = {
        HELPER_ENV: "trusted-generated-auth",
        "ANTHROPIC_BASE_URL": "https://model.test/gateway",
        "DATABRICKS_CONFIG_FILE": str(selected),
    }
    access = resolve_claude_model_access(env, databricks_profile="selected-profile")
    env["DATABRICKS_CONFIG_FILE"] = str(default)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(default))
    assert access.credential() == "approved-selected-token"
    policy = SandboxPolicy("linux_bwrap", True, [workspace], [workspace], [], False)
    protected = access.protect_sources(policy, workspace)
    assert selected.resolve() in (protected.credential_source_paths or [])
    policy.read_roots = [selected]
    with pytest.raises(ValueError, match="outside sandbox"):
        access.protect_sources(policy, workspace)
