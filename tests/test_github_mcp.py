"""Tests for the per-launch GitHub MCP proxy injection (omnigent.github_mcp)."""

from __future__ import annotations

import pytest

import omnigent.github_mcp as gh
from omnigent.github_mcp import (
    GITHUB_MCP_NAME,
    github_mcp_available,
    github_mcp_server_config,
    github_mcp_token,
    inject_session_link,
    open_in_omnigent_link,
)
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_TOKEN_ENV_VAR

_SESSION_URL = "https://omni.example/c/sess123"
_SERVER = "https://srv.example"
_HOST_ID = "host1"
_HOST_TOKEN = "launch-tok"
# Broker coordinates a managed sandbox has in its environment.
_COORD_VARS = ("RUNNER_SERVER_URL", HOST_ID_ENV_VAR, HOST_TOKEN_ENV_VAR)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*_COORD_VARS, "OMNIGENT_SESSION_URL", "OMNIGENT_PR_BUTTON_IMAGE_URL"):
        monkeypatch.delenv(var, raising=False)


def _set_coords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", _SERVER)
    monkeypatch.setenv(HOST_ID_ENV_VAR, _HOST_ID)
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, _HOST_TOKEN)


def _connect(monkeypatch: pytest.MonkeyPatch, token: str | None = "ghu_example") -> None:
    """Simulate a managed sandbox whose owner has (or hasn't) connected GitHub."""
    _set_coords(monkeypatch)
    monkeypatch.setattr(gh, "fetch_broker_token", lambda *a, **k: token)


def test_unavailable_without_broker_coords() -> None:
    # No broker coordinates → plain local run, nothing to fetch.
    assert github_mcp_available() is False
    assert github_mcp_token() is None
    assert github_mcp_server_config() is None


def test_not_connected_still_declares_server_no_network_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Config build must NOT hit the network (no event-loop stall): the server is
    # declared whenever broker coords are present; the proxy degrades to an empty
    # tool set at connect time if the owner isn't connected.
    _set_coords(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("github_mcp_server_config must not fetch at build time")

    monkeypatch.setattr(gh, "fetch_broker_token", _boom)
    assert github_mcp_server_config() is not None


def test_available_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _connect(monkeypatch)
    assert github_mcp_available() is True
    assert github_mcp_token() == "ghu_example"


def test_server_config_is_stdio_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _connect(monkeypatch)
    cfg = github_mcp_server_config(session_url=_SESSION_URL, python_executable="/py")
    assert cfg is not None
    assert cfg.name == GITHUB_MCP_NAME
    assert cfg.transport == "stdio"
    assert cfg.command == "/py"
    assert cfg.args == ["-m", "omnigent.github_mcp_proxy"]
    # Broker coordinates (not the GitHub token) + session URL reach the proxy.
    assert cfg.env["RUNNER_SERVER_URL"] == _SERVER
    assert cfg.env[HOST_ID_ENV_VAR] == _HOST_ID
    assert cfg.env[HOST_TOKEN_ENV_VAR] == _HOST_TOKEN
    assert cfg.env["OMNIGENT_SESSION_URL"] == _SESSION_URL
    # The GitHub token itself is never written into the harness MCP config.
    assert "ghu_example" not in cfg.env.values()


def test_server_config_session_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _connect(monkeypatch)
    monkeypatch.setenv("OMNIGENT_SESSION_URL", _SESSION_URL)
    cfg = github_mcp_server_config()
    assert cfg is not None and cfg.env["OMNIGENT_SESSION_URL"] == _SESSION_URL


def test_inject_session_link_appends_idempotently() -> None:
    args = inject_session_link({"title": "x", "body": "hello"}, _SESSION_URL)
    assert open_in_omnigent_link(_SESSION_URL) in args["body"]
    assert args["title"] == "x"
    # Idempotent: a second pass doesn't duplicate.
    again = inject_session_link(args, _SESSION_URL)
    assert again["body"].count(_SESSION_URL) == 1


def test_inject_session_link_noop_without_url() -> None:
    args = {"body": "hello"}
    assert inject_session_link(args, None) == args


def test_inject_session_link_empty_body() -> None:
    args = inject_session_link({"title": "x"}, _SESSION_URL)
    assert args["body"] == open_in_omnigent_link(_SESSION_URL)


def test_open_in_omnigent_link_default_image_is_camo_reachable() -> None:
    from omnigent.github_mcp import _DEFAULT_BUTTON_IMAGE_URL

    link = open_in_omnigent_link(_SESSION_URL)
    # Fixed image on a camo-reachable CDN (not the deployment's own origin).
    assert f'src="{_DEFAULT_BUTTON_IMAGE_URL}"' in link
    assert "raw.githubusercontent.com" in link
    # The session URL stays verbatim in the href exactly once → detection intact.
    assert f'href="{_SESSION_URL}"' in link
    assert link.count(_SESSION_URL) == 1


def test_open_in_omnigent_link_honors_image_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_PR_BUTTON_IMAGE_URL", "https://cdn.example/star.svg")
    link = open_in_omnigent_link(_SESSION_URL)
    assert 'src="https://cdn.example/star.svg"' in link


def test_open_in_omnigent_link_empty_override_falls_back_to_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_PR_BUTTON_IMAGE_URL", "")
    link = open_in_omnigent_link(_SESSION_URL)
    assert link == f"[Open in Omnigent]({_SESSION_URL})"


def test_opencode_block_translates_stdio_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.opencode_native_provider import build_opencode_mcp_block

    _connect(monkeypatch)
    cfg = github_mcp_server_config(session_url=_SESSION_URL, python_executable="/py")
    block = build_opencode_mcp_block([cfg])
    entry = block[GITHUB_MCP_NAME]
    assert entry["type"] == "local"
    assert entry["command"] == ["/py", "-m", "omnigent.github_mcp_proxy"]
    assert entry["environment"][HOST_TOKEN_ENV_VAR] == _HOST_TOKEN


def test_claude_mcp_config_includes_proxy_when_connected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from omnigent.claude_native_bridge import build_mcp_config

    _connect(monkeypatch)
    monkeypatch.setenv("OMNIGENT_SESSION_URL", _SESSION_URL)
    cfg = build_mcp_config(tmp_path)
    gh_entry = cfg["mcpServers"][GITHUB_MCP_NAME]
    assert gh_entry["args"] == ["-m", "omnigent.github_mcp_proxy"]
    assert gh_entry["env"][HOST_TOKEN_ENV_VAR] == _HOST_TOKEN
    assert "omnigent" in cfg["mcpServers"]  # relay still present


def test_claude_mcp_config_omits_github_when_not_connected(tmp_path) -> None:
    from omnigent.claude_native_bridge import build_mcp_config

    cfg = build_mcp_config(tmp_path)
    assert GITHUB_MCP_NAME not in cfg["mcpServers"]
