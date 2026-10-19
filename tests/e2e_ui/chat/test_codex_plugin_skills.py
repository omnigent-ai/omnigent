"""Both composers discover cached Codex skills through real host processes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.codex_plugins import install_codex_plugin
from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session


def test_plugin_skill_in_both_composers_after_inventory_failure(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    output_path: str,
) -> None:
    codex = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI required for real plugin discovery")
    home, workspace, marketplace = (tmp_path / name for name in ("codex", "workspace", "market"))
    home.mkdir()
    workspace.mkdir()
    install_codex_plugin(
        codex,
        home,
        workspace,
        marketplace,
        marketplace_name="dev-market",
        plugin_name="dev-productivity",
        skill_name="xray",
    )
    marketplace.rename(tmp_path / "market-moved")
    inventory = subprocess.run(
        [codex, "plugin", "list", "--json"],
        cwd=workspace,
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inventory.returncode != 0
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "inventory-failure.txt").write_text(
        f"exit={inventory.returncode}\n{inventory.stdout}\n{inventory.stderr}"
    )
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            name="Plugin skill test host",
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "0",
                "OMNIGENT_CODEX_PATH": codex,
                "CODEX_HOME": str(home),
                "OPENAI_API_KEY": "mock-key",
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            },
        ) as host,
    ):
        host.wait_online(client, timeout=60, poll_interval=0.2)
        session_id = create_native_session(
            client,
            live_server,
            harness="codex",
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )["session_id"]
        try:
            page = request.getfixturevalue("page")
            page.add_init_script(
                f"localStorage.setItem('omnigent:imports-reviewed:{host.host_id}', 'true')"
            )
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_label("Message the agent")
            expect(composer).to_be_visible(timeout=60_000)
            composer.fill("$xray")
            row = page.get_by_test_id("slash-menu-item-dev-productivity:xray")
            expect(row).to_be_visible(timeout=30_000)
            page.screenshot(path=str(evidence / "session-composer.png"))
            row.click()
            expect(composer).to_have_value("$dev-productivity:xray ")

            agents = client.get("/v1/agents")
            agents.raise_for_status()
            codex_agent = next(
                agent for agent in agents.json()["data"] if agent["harness"] == "codex-native"
            )
            # Restore the user's previous choice; picker interaction is covered separately.
            preferences = {
                "omnigent:recent-workspaces": json.dumps({host.host_id: [str(workspace)]}),
                "omnigent:last-agent-id": codex_agent["id"],
                "omnigent:last-host-choice": host.host_id,
            }
            page.add_init_script(
                f"for (const [key, value] of Object.entries({json.dumps(preferences)})) "
                "localStorage.setItem(key, value);"
            )
            page.goto(live_server)
            landing = page.get_by_test_id("new-chat-landing-input")
            expect(landing).to_be_visible(timeout=30_000)
            landing.fill("$xray")
            expect(row).to_be_visible(timeout=30_000)
            page.screenshot(path=str(evidence / "new-session-composer.png"))
            row.click()
            expect(landing).to_have_value("$dev-productivity:xray ")
            page.context.close()
        finally:
            client.delete(f"/v1/sessions/{session_id}", timeout=10)
