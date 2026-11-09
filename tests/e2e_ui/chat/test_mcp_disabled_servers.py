"""Real native CLIs must omit disabled servers and retain the Omnigent relay.

CI pins Claude 2.1.295 and Codex 0.142.0 in setup-harness-cli/manifest.json.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session
from tests._helpers.session import send_user_text
from tests.e2e_ui.chat.test_sidecar_mcp_allowlist import _wait_for_advertised_tools
from tests.e2e_ui.conftest import configure_mock_llm


@pytest.mark.min_server_version("0.22.0.dev0")
@pytest.mark.min_runner_version("0.22.0.dev0")
@pytest.mark.timeout(300)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_disabled_server_absent_from_real_native_session(
    live_server: str, mock_llm_server_url: str, tmp_path: Path, harness: str
) -> None:
    if shutil.which(harness) is None:
        pytest.skip(f"needs the {harness} CLI")
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = Path(__file__).resolve().parents[2] / "tools/fixtures/echo_stdio_mcp_server.py"
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
    configure_mock_llm(mock_llm_server_url, [{"text": "MCP test complete."}], key=model)
    with (
        httpx.Client(base_url=live_server, timeout=120) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
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
            live_server,
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
            tools = _wait_for_advertised_tools(mock_llm_server_url, model, timeout=120)
            assert any("on_fixture" in name for name in tools), tools
            assert not any("off_fixture" in name for name in tools), tools
            assert any("omnigent" in name for name in tools), sorted(tools)
        finally:
            client.delete(f"/v1/sessions/{session_id}")
