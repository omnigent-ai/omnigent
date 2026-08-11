"""Host-daemon environment boundary regression tests."""

from __future__ import annotations

from typing import Final

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import _build_runner_env

_REMOTE_SERVER_URL: Final = "https://example.databricksapps.com"

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
