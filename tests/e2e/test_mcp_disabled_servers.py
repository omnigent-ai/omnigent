"""Real native CLIs must omit disabled servers and retain the Omnigent relay.

CI pins Claude 2.1.295 and Codex 0.142.0 in setup-harness-cli/manifest.json.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import (
    compat_server_cwd,
    compat_server_python,
    resolve_server_version,
    server_executable,
    server_pythonpath,
)
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.session import send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_requests

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mcp_enabled_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Keep MCP release flags local to this journey, preserving old-server compatibility."""
    env = {
        "PYTHONPATH": server_pythonpath(_REPO_ROOT),
    }
    if compat_server_python() is None:
        env["OMNIGENT_FEATURES"] = (
            "mcp_disabled_servers,mcp_disabled_servers_claude,mcp_disabled_servers_codex"
        )
    with isolated_local_server(
        tmp_path_factory.mktemp("mcp_enabled_server"),
        env=env,
        executable=server_executable(),
        cwd=compat_server_cwd(),
        health_timeout=180.0,
    ) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def server_version(mcp_enabled_server: str) -> str:
    """Check compatibility against the same isolated server the journey uses."""
    return resolve_server_version(mcp_enabled_server)


@pytest.mark.min_server_version("0.22.0.dev0")
@pytest.mark.min_runner_version("0.22.0.dev0")
@pytest.mark.timeout(300)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_disabled_server_absent_from_real_native_session(
    mcp_enabled_server: str, mock_llm_server_url: str, tmp_path: Path, harness: str
) -> None:
    if shutil.which(harness) is None:
        pytest.skip(f"needs the {harness} CLI")
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = Path(__file__).resolve().parents[1] / "tools/fixtures/echo_stdio_mcp_server.py"
    # A visible control proves absence is selective, rather than failed MCP discovery.
    names = ("off_fixture", "on_fixture")
    config = {name: {"command": sys.executable, "args": [str(fixture)]} for name in names}
    if harness == "claude":
        (home / ".claude.json").write_text(json.dumps({"mcpServers": config}))
    else:
        codex = home / ".codex"
        codex.mkdir()
        text = "\n".join(
            f"[mcp_servers.{name}]\ncommand={json.dumps(sys.executable)}\nargs={json.dumps([str(fixture)])}\n"
            for name in names
        )
        (codex / "config.toml").write_text(text)
        # Exercise Codex's project layer as well as its private user-home copy.
        (workspace / ".codex").mkdir()
        (workspace / ".codex/config.toml").write_text(text)
    model = f"mcp-disable-{harness}-{tmp_path.name}"
    family = "anthropic" if harness == "claude" else "openai"
    provider = {
        "base_url": mock_llm_server_url if harness == "claude" else f"{mock_llm_server_url}/v1",
        "api_key": "mock-key",
        "models": {"default": model},
    }
    if harness == "codex":
        provider["wire_api"] = "responses"
    responses = [{"text": "MCP test complete."}]
    if harness == "claude":
        # Claude can send its first request while MCP servers are still connecting.
        responses.insert(
            0,
            {
                "tool_calls": [
                    {
                        "call_id": "wait_for_mcp",
                        "name": "WaitForMcpServers",
                        "arguments": "{}",
                    }
                ]
            },
        )
    configure_mock_llm(mock_llm_server_url, responses, key=model)
    with (
        httpx.Client(base_url=mcp_enabled_server, timeout=120) as client,
        isolated_host(
            tmp_path / "host",
            mcp_enabled_server,
            config={"providers": {"test": {"kind": "key", "default": [family], family: provider}}},
            env={
                "CLAUDE_CONFIG_DIR": None,
                "CODEX_HOME": None,
                "CLAUDE_ENV_FILE": None,
                "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "OMNIGENT_WRAPPER_BYPASS",
            },
            ready=lambda host: host.wait_online(client, timeout=90),
        ) as host,
    ):
        response = client.put(
            f"/v1/hosts/{host.host_id}/mcp-servers/enabled",
            json={
                "harness": harness,
                "server": "off_fixture",
                "enabled": False,
            },
        )
        response.raise_for_status()
        session = create_native_session(
            client,
            mcp_enabled_server,
            harness=harness,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
            },
            **({"model": model} if harness == "codex" else {}),
        )
        session_id = session["session_id"]
        try:
            send_user_text(client, session_id=session_id, text="Say MCP test complete.")
            tools: set[str] = set()

            def mcp_controls_advertised() -> bool:
                nonlocal tools
                tools = advertised_tool_names(get_mock_requests(mock_llm_server_url, key=model))
                return all(
                    any(marker in name for name in tools) for marker in ("on_fixture", "omnigent")
                )

            try:
                poll_until(mcp_controls_advertised, timeout_s=120)
            except AssertionError as exc:
                raise AssertionError(f"MCP controls never advertised: {sorted(tools)}") from exc
            assert any("on_fixture" in name for name in tools), tools
            assert not any("off_fixture" in name for name in tools), tools
            assert any("omnigent" in name for name in tools), sorted(tools)
        finally:
            client.delete(f"/v1/sessions/{session_id}")
