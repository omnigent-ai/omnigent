"""A namespaced override launches the selected ACP agent end to end."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online
from tests.e2e_ui.harness_override.conftest import GEMINI_REPLY, GOOSE_OVERRIDE, GOOSE_REPLY


@pytest.fixture
def acp_override_session(
    live_server: str,
    runner_id: str,
    two_acp_agents_config: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a non-ACP session overridden to the second configured agent."""
    agents_resp = httpx.get(f"{live_server}/v1/agents", timeout=10.0)
    agents_resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in agents_resp.json()["data"] if a["name"] == "hello_world"),
        None,
    )
    assert agent_id is not None, "hello_world agent is not registered on the server"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        json={"agent_id": agent_id, "harness_override": GOOSE_OVERRIDE},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["id"]

    respawned_runner: subprocess.Popen[bytes] | None = None
    try:
        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned_runner is not None:
                respawned_runner.terminate()
                try:
                    respawned_runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned_runner.kill()
                    respawned_runner.wait(timeout=5)


def test_acp_slug_override_launches_named_agent(
    page: Page,
    acp_override_session: tuple[str, str],
) -> None:
    base_url, session_id = acp_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("Say hello")
    composer.press("Enter")

    expect(page.get_by_text(GOOSE_REPLY)).to_be_visible(timeout=90_000)
    expect(page.get_by_text(GEMINI_REPLY)).to_have_count(0)
