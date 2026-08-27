"""Host-daemon environment boundary regression tests."""

from __future__ import annotations

from typing import Final

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import _build_runner_env

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


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_proxy_vars_without_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """Proxy selectors reach both daemon modes without provider credentials."""
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
    assert "OPENAI_API_KEY" not in env


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


def test_runner_env_removed_passthrough_does_not_forward_proxy_credentials() -> None:
    """The removed generic passthrough cannot inject proxy values into runners."""
    # Given
    explicit_names = {"HTTPS_PROXY", "http_proxy"}
    base_env = {
        "PATH": "/usr/bin",
        **_PROXY_ENV,
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": ",".join(explicit_names),
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
    assert set(_PROXY_ENV).isdisjoint(env)
    assert "OMNIGENT_RUNNER_ENV_PASSTHROUGH" not in env


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
