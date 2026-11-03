"""Close the browser with a saved attachment; run SDK and real native Codex turns."""

from __future__ import annotations

import json
import re
import shutil

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _persisted_messages,
    _poll,
)
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    managed_replicas as managed_replicas,
)

_PROMPT = "Use context.txt and reply with MANAGED_ATTACHMENT_RECEIVED."
_REPLY = "MANAGED_ATTACHMENT_RECEIVED"
_FILE = b"Saved attachment: managed-first-input-context."


@pytest.fixture(autouse=True)
def _minimal_native_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CODEX_MINIMAL_CONFIG", "1")


@pytest.mark.timeout(300)
# Create on replica A, host tunnel on replica B: delivery crosses replicas.
@pytest.mark.parametrize("managed_replicas", [(True, True)], indirect=True)
@pytest.mark.parametrize("agent_name", ["routing_probe", "codex-native-ui"])
def test_managed_initial_attachment_survives_browser_close(
    managed_replicas: ManagedReplicas, mock_llm_server_url: str, agent_name: str
) -> None:
    if agent_name == "codex-native-ui" and not all(
        shutil.which(tool) for tool in ("codex", "tmux")
    ):
        pytest.skip("native managed-input coverage requires the real Codex CLI and tmux")
    # The fresh host/runner reads this fixture-owned provider configuration.
    (managed_replicas.logs / "config" / "config.yaml").write_text(
        "providers:\n  mock-codex:\n    kind: key\n    default: openai\n    openai:\n"
        f'      base_url: "{mock_llm_server_url}/v1"\n'
        '      api_key: "mock-key"\n      wire_api: responses\n'
        "      models:\n        default: gpt-4o\n"
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": _REPLY, "stream": True}], key=agent_name, match=_PROMPT
    )
    run_in_fresh_loop(_drive(managed_replicas, mock_llm_server_url, agent_name))


async def _drive(rig: ManagedReplicas, model_url: str, agent_name: str) -> None:
    async with async_playwright() as playwright, httpx.AsyncClient(timeout=10) as client:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        await page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(agent["id"] for agent in agents if agent["name"] == agent_name)
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-file-input").set_input_files(
                {"name": "context.txt", "mimeType": "text/plain", "buffer": _FILE}
            )
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_id = page.url.rsplit("/", 1)[-1]
            path = f"/v1/sessions/{session_id}"
            snapshot = (await client.get(rig.a + path)).json()
            assert snapshot["host_id"] is None and snapshot["runner_id"] is None
            file_id = snapshot["pending_inputs"][0]["content"][0]["file_id"]
            await page.screenshot(path=str(rig.logs / "attachment-before-close.png"))
            await page.context.tracing.stop(path=str(rig.logs / "attachment-before-close.zip"))
            await browser.close()

            (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
            snapshot = await _poll(
                client,
                rig.b + path,
                lambda body: len(_persisted_messages(body, "assistant", _REPLY)) == 1,
                timeout=120,
            )
            assert snapshot["host_id"] and snapshot["runner_id"]
            assert len(_persisted_messages(snapshot, "user", _PROMPT)) == 1
            (rig.logs / "completed-with-attachment.json").write_text(
                json.dumps(snapshot, indent=2)
            )
            content = await client.get(f"{rig.b}{path}/resources/files/{file_id}/content")
            assert content.status_code == 200 and content.content == _FILE
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})
            await page.goto(f"{rig.ui}/c/{session_id}")
            for role, text in (("user", _PROMPT), ("assistant", _REPLY)):
                await expect(
                    page.locator(f'[data-testid="message-bubble"][data-role="{role}"]').filter(
                        has_text=text
                    )
                ).to_have_count(1, timeout=20_000)
            await page.screenshot(path=str(rig.logs / "attachment-reopened.png"))
        finally:
            network = (await client.get(f"{rig.ingress}/__test/network")).json()
            (rig.logs / "network.json").write_text(json.dumps(network, indent=2))
            requests = (await client.get(f"{model_url}/mock/requests")).json()
            (rig.logs / "model-requests.json").write_text(json.dumps(requests, indent=2))
            await client.post(f"{rig.a}/__test/provision/release")
            if browser.is_connected():
                await browser.close()
