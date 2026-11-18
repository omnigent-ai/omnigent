"""Startup follow-ups must outlive the browser, using real product processes."""

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

_PROMPTS = [f"STARTUP_ORDER_{number}: acknowledge this instruction." for number in range(3)]
_REPLY = "STARTUP_MESSAGES_RECEIVED"


@pytest.fixture(autouse=True)
def _minimal_native_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CODEX_MINIMAL_CONFIG", "1")


@pytest.mark.timeout(300)
# Create on replica A, host tunnel on replica B: delivery crosses replicas.
@pytest.mark.parametrize("managed_replicas", [(True, True)], indirect=True)
@pytest.mark.parametrize("agent_name", ["routing_probe", "codex-native-ui"])
def test_startup_followups_survive_browser_close(
    managed_replicas: ManagedReplicas, mock_llm_server_url: str, agent_name: str
) -> None:
    """Send three messages before allocation, close the browser, then reopen fresh."""
    if agent_name == "codex-native-ui" and not all(
        shutil.which(tool) for tool in ("codex", "tmux")
    ):
        pytest.skip("requires the real Codex CLI and tmux")
    (managed_replicas.logs / "config" / "config.yaml").write_text(
        "providers:\n  mock-codex:\n    kind: key\n    default: openai\n    openai:\n"
        f'      base_url: "{mock_llm_server_url}/v1"\n'
        '      api_key: "mock-key"\n      wire_api: responses\n'
        "      models:\n        default: gpt-4o\n"
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY, "stream": True}] * 6,
        key="startup-followups",
        match="STARTUP_ORDER_",
    )
    run_in_fresh_loop(_drive(managed_replicas, mock_llm_server_url, agent_name))


async def _drive(rig: ManagedReplicas, model_url: str, agent_name: str) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        await page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        session_path = ""
        try:
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                timeout=20_000
            )
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(agent["id"] for agent in agents if agent["name"] == agent_name)
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPTS[0])
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_id = page.url.rsplit("/", 1)[-1]
            session_path = f"/v1/sessions/{session_id}"
            await _poll(client, f"{rig.a}/__test/state", lambda state: state["provisioning"])
            await expect(page.get_by_test_id("runner-starting-indicator")).to_be_visible()

            # Real composer actions with the default queue preference. No route
            # interception, synthetic /events POST, or localStorage workaround.
            composer = page.get_by_label("Message the agent")
            for prompt in _PROMPTS[1:]:
                await composer.fill(prompt)
                await page.get_by_role("button", name="Send", exact=True).click()
                await expect(composer).to_have_value("")
            await page.screenshot(path=str(rig.logs / "three-startup-messages.png"))
            snapshot = await _poll(
                client,
                rig.a + session_path,
                lambda data: len(data["pending_inputs"]) == len(_PROMPTS),
                timeout=10,
            )
            assert snapshot["host_id"] is None and snapshot["runner_id"] is None
            assert [
                entry["content"][-1]["text"] for entry in snapshot["pending_inputs"]
            ] == _PROMPTS
            network = (await client.get(f"{rig.ingress}/__test/network")).json()
            accepted = [row for row in network if row["path"] == session_path + "/events"]
            assert len(accepted) == 2 and all(row["status"] == 202 for row in accepted)
            await page.context.tracing.stop(path=str(rig.logs / "startup-followups.zip"))
            await browser.close()

            (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
            snapshot = await _poll(
                client,
                rig.b + session_path,
                lambda data: (
                    not data["pending_inputs"]
                    and all(
                        len(_persisted_messages(data, "user", prompt)) == 1 for prompt in _PROMPTS
                    )
                    and bool(_persisted_messages(data, "assistant", _REPLY))
                ),
                timeout=120,
            )
            requests = await _poll(
                client,
                f"{model_url}/mock/requests",
                lambda data: all(prompt in json.dumps(data) for prompt in _PROMPTS),
            )
            # Every instruction reaches an actual harness turn. History may be
            # included on later turns; the first appearance must retain order.
            turns = [
                json.dumps(row.get("input", row.get("messages", [])))
                for row in requests["requests"]
            ]
            first_seen = [
                next(i for i, turn in enumerate(turns) if prompt in turn) for prompt in _PROMPTS
            ]
            assert first_seen == sorted(first_seen)
            users = [
                item for item in snapshot["items"] if item.get("data", {}).get("role") == "user"
            ]
            assert [item["data"]["content"][-1]["text"] for item in users] == _PROMPTS
            (rig.logs / "startup-completed-without-browser.json").write_text(
                json.dumps(snapshot, indent=2)
            )

            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})
            await page.goto(f"{rig.ui}/c/{session_id}")
            for prompt in _PROMPTS:
                await expect(
                    page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
                        has_text=prompt
                    )
                ).to_have_count(1, timeout=20_000)
            await page.reload()
            for prompt in _PROMPTS:
                await expect(
                    page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
                        has_text=prompt
                    )
                ).to_have_count(1)
            await page.screenshot(path=str(rig.logs / "startup-followups-reopened.png"))
        finally:
            if session_path:
                snapshot = (await client.get(rig.b + session_path)).json()
                (rig.logs / "final-session.json").write_text(json.dumps(snapshot, indent=2))
            network = (await client.get(f"{rig.ingress}/__test/network")).json()
            (rig.logs / "network.json").write_text(json.dumps(network, indent=2))
            requests = (await client.get(f"{model_url}/mock/requests")).json()
            (rig.logs / "model-requests.json").write_text(json.dumps(requests, indent=2))
            await client.post(f"{rig.a}/__test/provision/release")
            if browser.is_connected():
                await page.screenshot(path=str(rig.logs / "final.png"))
                await browser.close()
