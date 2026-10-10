"""Codex on a managed sandbox without an inference catalog offers a model choice like Claude Code.

Drives this checkout's real server (``agent_sandbox`` provider, no ``inference`` block);
only the final create POST is intercepted because no cluster exists here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.live_server import isolated_local_server
from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import (
    _close_entry_models,
    _open_entry_models,
    _wait_until,
)

_REPO_URL = "https://github.com/omnigent-ai/omnigent.git"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
_VIEWPORT = {"width": 1440, "height": 960}


@pytest.fixture(scope="module")
def sandbox_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """This checkout's server with only an agent_sandbox target and no inference block."""
    root = tmp_path_factory.mktemp("sandbox_no_inference")
    config = root / "server.yaml"
    # The provider's server_url is only contacted when a sandbox is provisioned,
    # which this journey never reaches.
    config.write_text("sandbox:\n  provider: agent_sandbox\n  server_url: http://127.0.0.1:9\n")
    with isolated_local_server(
        root,
        extra_args=["--config", str(config)],
        env={
            "OMNIGENT_CONFIG_HOME": str(root / "config-home"),
            "OMNIGENT_DATA_DIR": str(root / "data"),
        },
    ) as base_url:
        yield base_url


def builtin_agent_ids(base_url: str) -> dict[str, str]:
    """Confirm the server offers the managed target without inference, keyed by agent name."""
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        info = client.get(f"{base_url}/v1/info").json()
        assert info["managed_sandboxes_enabled"] is True, info
        assert info["sandbox_provider"] == "agent_sandbox", info
        capabilities = info["sandbox_provider_capabilities"]["agent_sandbox"]
        assert "inference_models" not in capabilities, info
        agents = client.get(f"{base_url}/v1/agents").json()["data"]
    return {agent["name"]: agent["id"] for agent in agents}


def test_sandbox_codex_offers_harness_default_and_starts_without_inference_config(
    sandbox_server: str, tmp_path: Path
) -> None:
    """Codex lists a selectable default like Claude Code, and Start sends a bare managed launch."""
    agents = builtin_agent_ids(sandbox_server)
    run_in_fresh_loop(
        _drive_journey(
            sandbox_server, agents["codex-native-ui"], agents["claude-native-ui"], tmp_path
        )
    )


async def _open_composer(page: Any, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    # The sandbox repository chip only renders while the Sandbox target is selected.
    await expect(page.get_by_test_id("new-chat-landing-repo-chip")).to_be_visible()


async def _dismiss_entry_models(page: Any) -> None:
    await _close_entry_models(page)
    # The closed menu's dismissal layer swallows the next click until it unmounts.
    await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


async def _settled_model_menu(page: Any, agent_id: str) -> Any:
    """Open *agent_id*'s model list and wait until it shows rows or the unavailable note."""
    await _open_entry_models(page, agent_id)
    models = page.get_by_test_id("new-chat-landing-agent-models")
    await expect(models).to_be_visible()
    await expect(models).not_to_contain_text("Loading models")
    rows = models.get_by_role("menuitemcheckbox")
    await expect(rows.first.or_(models.get_by_text("Models unavailable"))).to_be_visible()
    return models


async def _drive_journey(base_url: str, codex_id: str, claude_id: str, evidence_dir: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport=_VIEWPORT)
        page = await context.new_page()
        try:
            creates: list[dict[str, Any]] = []

            async def handle_sessions(route: Route) -> None:
                if route.request.method != "POST":
                    await route.continue_()
                    return
                creates.append(route.request.post_data_json)
                await route.fulfill(status=200, json={"id": "conv_sandbox_codex"})

            await page.route(_SESSIONS_RE, handle_sessions)
            await _open_composer(page, base_url)
            picker = page.get_by_test_id("new-chat-landing-agent-select")

            models = await _settled_model_menu(page, codex_id)
            codex_rows = await models.get_by_role("menuitemcheckbox").count()
            codex_menu = " ".join((await models.inner_text()).split())
            codex_trigger = " ".join((await picker.inner_text()).split())
            await page.screenshot(path=evidence_dir / "codex-sandbox-models.png")
            await _dismiss_entry_models(page)

            models = await _settled_model_menu(page, claude_id)
            claude_rows = await models.get_by_role("menuitemcheckbox").count()
            await page.screenshot(path=evidence_dir / "claude-sandbox-models.png")
            await _dismiss_entry_models(page)

            assert claude_rows > 0, "Claude Code offers no models on the Sandbox target"
            assert codex_rows > 0 and "Models unavailable" not in codex_menu, (
                f"Codex offers no model choice on the Sandbox target: menu={codex_menu!r}, "
                f"trigger={codex_trigger!r}, rows={codex_rows}"
            )

            await select_landing_agent(page, codex_id)
            await page.get_by_test_id("new-chat-landing-repo-chip").click()
            await page.get_by_test_id("new-chat-landing-repo-input").fill(_REPO_URL)
            await page.get_by_test_id("new-chat-landing-repo-add").click()
            await page.keyboard.press("Escape")
            await page.get_by_test_id("new-chat-landing-input").fill("Reply with READY.")
            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit).to_be_enabled()
            await page.screenshot(path=evidence_dir / "codex-sandbox-start.png")
            await submit.click()
            await _wait_until(lambda: len(creates) == 1)
            body = creates[0]
            assert body["agent_id"] == codex_id, body
            assert body["host_type"] == "managed", body
            assert body["workspaces"] == [_REPO_URL], body
            assert body.get("model_override") is None, body
        finally:
            await context.close()
            await browser.close()
