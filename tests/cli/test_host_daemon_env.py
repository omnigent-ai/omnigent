"""Host-daemon environment boundary regression tests."""

from __future__ import annotations

from typing import Final

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import (
    RUNNER_ENV_PASSTHROUGH_ENV_VAR,
    _build_runner_env,
)

_REMOTE_SERVER_URL: Final = "https://example.databricksapps.com"
_PROXY_ENV: Final = {
    "HTTP_PROXY": "http://upper-http-proxy.example.com:3128",
    "HTTPS_PROXY": "http://upper-https-proxy.example.com:3128",
    "ALL_PROXY": "socks5://upper-proxy.example.com:1080",
    "NO_PROXY": "localhost,127.0.0.1",
    "http_proxy": "http://lower-http-proxy.example.com:3128",
    "https_proxy": "http://lower-https-proxy.example.com:3128",
    "all_proxy": "socks5://lower-proxy.example.com:1080",
    "no_proxy": "localhost,127.0.0.2",
}


@pytest.mark.parametrize(
    ("server_url", "keeps_provider_secret"),
    [(None, True), (_REMOTE_SERVER_URL, False)],
)
def test_host_daemon_env_preserves_proxy_vars_and_provider_secret_split(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    keeps_provider_secret: bool,
) -> None:
    """Proxy selectors reach both daemon modes without widening provider secrets."""
    # Given
    for name, value in _PROXY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "corp")
    monkeypatch.setenv("OPENAI_API_KEY", "local-provider-secret")

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _PROXY_ENV} == _PROXY_ENV
    assert env["DATABRICKS_CONFIG_PROFILE"] == "corp"
    assert ("OPENAI_API_KEY" in env) is keeps_provider_secret


def test_runner_env_excludes_proxy_vars_by_default() -> None:
    """Daemon proxy credentials do not cross into runner subprocesses by default."""
    # Given
    base_env = {"PATH": "/usr/bin", **_PROXY_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert set(_PROXY_ENV).isdisjoint(env)


def test_runner_env_explicit_proxy_passthrough_remains_available() -> None:
    """Runner proxy forwarding remains opt-in through the existing passthrough."""
    # Given
    explicit_names = {"HTTPS_PROXY", "http_proxy"}
    base_env = {
        "PATH": "/usr/bin",
        **_PROXY_ENV,
        RUNNER_ENV_PASSTHROUGH_ENV_VAR: ",".join(explicit_names),
    }

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env[name] for name in explicit_names} == {
        name: _PROXY_ENV[name] for name in explicit_names
    }
    assert (set(_PROXY_ENV) - explicit_names).isdisjoint(env)


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
@pytest.mark.parametrize("enabled", ["0", "1"])
def test_host_slice_key_gate_reaches_daemon_and_runner(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    enabled: str,
) -> None:
    """The host and its spawned runner make the same slice-key decision."""
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", enabled)

    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_slice_key",
        binding_token="binding-slice-key",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    assert daemon_env["OMNIGENT_HOST_SLICE_KEY_ENABLED"] == enabled
    assert runner_env["OMNIGENT_HOST_SLICE_KEY_ENABLED"] == enabled


_CLAUDE_TOOL_SEARCH_ENV: Final = {
    "CLAUDE_CODE_USE_GATEWAY": "1",
    "ENABLE_TOOL_SEARCH": "true",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_claude_tool_search_flags(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the daemon in both modes (Gate 1)."""
    # Given
    for name, value in _CLAUDE_TOOL_SEARCH_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


def test_runner_env_preserves_claude_tool_search_flags() -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the runner subprocess (Gate 2)."""
    # Given
    base_env = {"PATH": "/usr/bin", **_CLAUDE_TOOL_SEARCH_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_tool_search",
        binding_token="binding-tool-search",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_claude_telemetry_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """CLAUDE_CODE_ENABLE_TELEMETRY survives the CLI→daemon strip in both modes."""
    # Given
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then: the opt-in flag travels with the OTEL exporter config it belongs to.
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp"
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1"


def test_runner_env_preserves_claude_telemetry_opt_in() -> None:
    """CLAUDE_CODE_ENABLE_TELEMETRY survives the daemon→runner strip."""
    # Given
    base_env = {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
    }

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_telemetry",
        binding_token="binding-telemetry",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp"
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1"
