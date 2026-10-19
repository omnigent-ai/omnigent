"""A managed session must run its first prompt without an open browser.

The only substitutes are local cloud allocation and scripted model output.
Creation, provisioning, host/runner processes, HTTP, persistence and the SPA
are real. Cover both a local host connection and a different replica, where
the absent browser cannot rescue delivery with a routed retry.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _persisted_messages,
    _poll,
    managed_replicas,  # noqa: F401 — shared real-process fixture
)

_PROMPT = "Please acknowledge the startup message after I close this browser tab."
_REPLY = "I received the startup message while the browser was closed."


@pytest.mark.timeout(240)
# The create stays on replica A; the host's tunnel joins it there, or (keying
# by its own id) lands on replica B, where no browser can rescue delivery.
@pytest.mark.parametrize(
    "managed_replicas",
    [(True, False), (True, True)],
    ids=["same-replica", "other-replica"],
    indirect=True,
)
def test_managed_first_input_runs_after_browser_closes(
    managed_replicas: ManagedReplicas,  # noqa: F811 — pytest injects the imported fixture
    mock_llm_server_url: str,
) -> None:
    """Close during provisioning; require completion before opening a new browser."""
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY, "stream": True}],
        key="managed-first-input-tab-close",
        match=_PROMPT,
    )
    run_in_fresh_loop(_drive(managed_replicas, mock_llm_server_url))


async def _drive(rig: ManagedReplicas, model_url: str) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 960})
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        session_path = ""
        try:
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_id = page.url.rsplit("/", 1)[-1]
            session_path = f"/v1/sessions/{session_id}"

            # A real accepted create, still blocked at the provider boundary.
            await _poll(client, f"{rig.a}/__test/state", lambda body: body["provisioning"])
            snapshot = (await client.get(rig.a + session_path)).json()
            assert snapshot["host_id"] is None and snapshot["runner_id"] is None
            await expect(page.get_by_test_id("runner-starting-indicator")).to_be_visible()
            await page.screenshot(path=str(rig.logs / "before-tab-close.png"))
            await context.tracing.stop(path=str(rig.logs / "before-tab-close.zip"))
            await page.close()
            await browser.close()

            # Only the cloud-allocation gate is released. No /events request,
            # tab reopen, synthetic turn, or transcript seed can deliver input.
            (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
            snapshot = await _poll(
                client,
                rig.b + session_path,
                lambda body: bool(
                    body.get("host_id")
                    and body.get("runner_id")
                    and body.get("host_online")
                    and body.get("runner_online")
                ),
                timeout=90,
            )
            assert snapshot["host_id"] and snapshot["runner_id"]
            snapshot = await _poll(
                client,
                rig.b + session_path,
                lambda body: len(_persisted_messages(body, "assistant", _REPLY)) == 1,
                timeout=30,
            )
            assert len(_persisted_messages(snapshot, "user", _PROMPT)) == 1
            (rig.logs / "completed-with-browser-closed.json").write_text(
                json.dumps(snapshot, indent=2)
            )

            # A new browser has no pending JS callback, draft or localStorage
            # from creation. It must simply display the already-completed turn.
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})
            await page.goto(f"{rig.ui}/c/{session_id}")
            for role, text in (("user", _PROMPT), ("assistant", _REPLY)):
                bubble = page.locator(
                    f'[data-testid="message-bubble"][data-role="{role}"]'
                ).filter(has_text=text)
                await expect(bubble).to_have_count(1, timeout=20_000)
            await page.reload()
            await expect(
                page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
                    has_text=_REPLY
                )
            ).to_have_count(1)
            await page.screenshot(path=str(rig.logs / "reopened-after-completion.png"))
            after_reload = (await client.get(rig.b + session_path)).json()
            for role, text in (("user", _PROMPT), ("assistant", _REPLY)):
                assert len(_persisted_messages(after_reload, role, text)) == 1
        finally:
            if session_path:
                snapshot = (await client.get(rig.b + session_path)).json()
                (rig.logs / "final-session.json").write_text(json.dumps(snapshot, indent=2))
            network = (await client.get(f"{rig.ingress}/__test/network")).json()
            (rig.logs / "tab-close-network.json").write_text(json.dumps(network, indent=2))
            requests = (await client.get(f"{model_url}/mock/requests")).json()
            (rig.logs / "tab-close-model-requests.json").write_text(json.dumps(requests, indent=2))
            await client.post(f"{rig.a}/__test/provision/release")
            if browser.is_connected():
                await browser.close()
