"""Sibling CUJs through real replicas, hosts and runners; local sandbox provider."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.browser import async_user_page
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _poll,
    managed_replicas,  # noqa: F401
)


@pytest.fixture(autouse=True)
def enable_siblings(monkeypatch):
    monkeypatch.setenv("OMNIGENT_FEATURES", "sibling_sessions")


@pytest.mark.timeout(300)
def test_sibling_composer_placement_and_survivor(
    managed_replicas: ManagedReplicas,  # noqa: F811
    mock_llm_server_url: str,
):
    for prompt in (
        "Research the API",
        "Implement the client",
        "Still here after deletion",
        "Still here after fork deletion",
    ):
        configure_mock_llm(mock_llm_server_url, [{"text": f"Done: {prompt}"}], match=prompt)
    run_in_fresh_loop(_drive(managed_replicas))


async def _drive(rig: ManagedReplicas) -> None:
    evidence = Path(os.environ.get("OMNIGENT_DEMO_DIR", str(rig.logs / "sibling-evidence")))
    evidence.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
        agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
        response = await client.post(
            f"{rig.ingress}/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_type": "managed",
                "title": "Coordinator",
            },
        )
        assert response.status_code == 201, response.text
        source_id = response.json()["id"]
        (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
        (await client.post(f"{rig.b}/__test/provision/release")).raise_for_status()
        source = await _poll(
            client,
            f"{rig.a}/v1/sessions/{source_id}",
            lambda row: bool(row.get("host_id") and row.get("runner_online")),
            timeout=60,
        )
        source_host = source["host_id"]
        (Path(source["workspace"]) / "keep.txt").write_text("Shared workspace must survive\n")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            async with async_user_page(
                browser,
                headers={},
                viewport={"width": 1440, "height": 960},
                record_video_dir=str(evidence),
                record_video_size={"width": 1440, "height": 960},
            ) as page:
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
                try:
                    await page.goto(f"{rig.ui}/c/{source_id}")

                    async def add_agent(
                        session_id: str, prompt: str, *, fresh: bool = False
                    ) -> str:
                        row = page.locator(f'li[data-sidebar-session-id="{session_id}"]').first
                        await expect(row).to_be_visible()
                        await row.hover()
                        await row.get_by_test_id("conversation-actions").click()
                        await page.screenshot(path=str(evidence / "add-agent-menu.png"))
                        await page.get_by_role(
                            "menuitem", name="Add nested agent", exact=True
                        ).click()
                        await expect(
                            page.get_by_role("heading", name="Add nested agent", exact=True)
                        ).to_be_visible()
                        await expect(
                            page.get_by_test_id("new-chat-landing-host-chip")
                        ).not_to_contain_text("New Sandbox")
                        if (
                            "Routing_probe"
                            not in await page.get_by_test_id(
                                "new-chat-landing-agent-select"
                            ).inner_text()
                        ):
                            await page.get_by_test_id("new-chat-landing-agent-select").click()
                            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
                        if fresh:
                            await page.get_by_test_id("new-chat-landing-host-chip").click()
                            await page.get_by_test_id("new-chat-landing-sandbox-option").click()
                        await page.get_by_test_id("new-chat-landing-input").fill(prompt)
                        await expect(
                            page.get_by_test_id("new-chat-landing-submit")
                        ).to_be_enabled()
                        async with page.expect_response(
                            lambda response: (
                                response.request.method == "POST"
                                and response.url.endswith("/v1/sessions")
                            )
                        ) as created:
                            await page.get_by_test_id("new-chat-landing-submit").click()
                        result = await created.value
                        assert result.status == 201, await result.text()
                        submitted = result.request.post_data_json
                        assert submitted["sibling_source_id"] == session_id
                        if not fresh:
                            assert submitted["host_id"] == source_host
                            assert submitted.get("host_type") != "managed"
                        new_id = (await result.json())["id"]
                        await expect(page).to_have_url(re.compile(f"/c/{new_id}$"), timeout=30000)
                        await expect(
                            page.get_by_text(f"Done: {prompt}", exact=True)
                        ).to_be_visible(timeout=60000)
                        return new_id

                    sibling_id = await add_agent(source_id, "Research the API")
                    sibling = (await client.get(f"{rig.a}/v1/sessions/{sibling_id}")).json()
                    assert sibling["host_id"] == source_host
                    assert sibling["parent_session_id"] is None
                    assert sibling["family"]["representative_id"] == source_id
                    await expect(
                        page.locator(f'li[data-sidebar-session-id="{sibling_id}"]')
                    ).to_have_attribute("data-sibling-session", "true")
                    fresh_id = await add_agent(sibling_id, "Implement the client", fresh=True)
                    fresh = (await client.get(f"{rig.a}/v1/sessions/{fresh_id}")).json()
                    assert fresh["host_id"] != source_host
                    assert fresh["family"]["id"] == sibling["family"]["id"]
                    await expect(
                        page.locator(f'li[data-sidebar-session-id="{fresh_id}"]')
                    ).to_have_attribute("data-sibling-session", "true")
                    representative_label = (
                        await page.locator(f'li[data-sidebar-session-id="{source_id}"]')
                        .get_by_text("Coordinator", exact=True)
                        .bounding_box()
                    )
                    sibling_label = (
                        await page.locator(f'li[data-sidebar-session-id="{sibling_id}"]')
                        .get_by_text("Research the API", exact=True)
                        .bounding_box()
                    )
                    assert representative_label and sibling_label
                    assert sibling_label["x"] >= representative_label["x"] + 16
                    await page.screenshot(path=str(evidence / "siblings-two-sandboxes.png"))
                    representative = page.locator(f'li[data-sidebar-session-id="{source_id}"]')
                    await page.mouse.move(1200, 900)
                    for action in ("quick-pin-conversation", "quick-archive-conversation"):
                        await expect(representative.get_by_test_id(action)).to_have_css(
                            "opacity", "0"
                        )
                    await representative.hover()
                    await expect(
                        representative.get_by_test_id("quick-pin-conversation")
                    ).to_have_css("opacity", "1")
                    await page.mouse.move(1200, 900)
                    await expect(
                        representative.get_by_test_id("quick-pin-conversation")
                    ).to_have_css("opacity", "0")
                    await page.screenshot(path=str(evidence / "family-actions-at-rest.png"))
                    # The clicked member's sandbox is the only ephemeral host in its picker.
                    row = page.locator(f'li[data-sidebar-session-id="{fresh_id}"]')
                    await row.hover()
                    await row.get_by_test_id("conversation-actions").click()
                    await page.get_by_role("menuitem", name="Add nested agent", exact=True).click()
                    await page.get_by_test_id("new-chat-landing-host-chip").click()
                    hosts = (await client.get(f"{rig.a}/v1/hosts")).json()["hosts"]
                    names = {host["host_id"]: host["name"] for host in hosts}
                    menu = page.get_by_test_id("new-chat-landing-host-menu")
                    await expect(menu).to_contain_text(names[fresh["host_id"]])
                    await expect(menu).not_to_contain_text(names[source_host])
                    await page.keyboard.press("Escape")
                    await page.goto(f"{rig.ui}/c/{sibling_id}")
                    row = page.locator(f'li[data-sidebar-session-id="{source_id}"]')
                    await row.hover()
                    await row.get_by_test_id("quick-pin-conversation").click()
                    await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: row.get("family", {}).get("pinned") is True,
                    )
                    # Pinned peers disappear immediately; Undo restores their family position.
                    for archived_id in (fresh_id, source_id):
                        row = page.locator(f'li[data-sidebar-session-id="{archived_id}"]')
                        await row.hover()
                        await row.get_by_test_id("conversation-actions").click()
                        await page.get_by_test_id("archive-conversation").click()
                        await expect(row).to_have_count(0)
                        representative_id = sibling_id if archived_id == source_id else source_id
                        await expect(
                            page.locator(
                                f'li[data-sidebar-session-id="{representative_id}"]'
                            ).get_by_role("button", name="Collapse session family")
                        ).to_be_visible()
                        await (
                            page.get_by_test_id("archive-undo-toast-item")
                            .get_by_role("button", name="Undo")
                            .click()
                        )
                        await expect(row).to_have_count(1)
                        await expect(
                            page.locator(f'li[data-sidebar-session-id="{source_id}"]').get_by_role(
                                "button", name="Collapse session family"
                            )
                        ).to_be_visible()
                    # Delete through the UI and prove the same-host sibling can still work.
                    row = page.locator(f'li[data-sidebar-session-id="{source_id}"]')
                    await row.hover()
                    await row.get_by_test_id("conversation-actions").click()
                    await page.get_by_test_id("delete-conversation").click()
                    release_delete = asyncio.Event()

                    async def delay_delete(route):
                        if route.request.method == "DELETE":
                            await release_delete.wait()
                        await route.continue_()

                    delete_url = f"**/v1/sessions/{source_id}*"
                    await page.route(delete_url, delay_delete)
                    await (
                        page.get_by_role("dialog")
                        .get_by_role("button", name="Delete", exact=True)
                        .click()
                    )
                    await expect(
                        page.locator(f'li[data-sidebar-session-id="{source_id}"]')
                    ).to_have_count(0)
                    try:
                        await expect(
                            page.locator(f'li[data-sidebar-session-id="{fresh_id}"]')
                        ).to_have_attribute("data-sibling-session", "true")
                        await expect(
                            page.locator(
                                f'li[data-sidebar-session-id="{sibling_id}"]'
                            ).get_by_role("button", name="Collapse session family")
                        ).to_be_visible()
                        await page.screenshot(path=str(evidence / "family-delete-pending.png"))
                    finally:
                        release_delete.set()
                        await page.unroute(delete_url, delay_delete)
                    promoted = await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: row.get("family", {}).get("representative_id") == sibling_id,
                    )
                    await expect(
                        page.locator(f'li[data-sidebar-session-id="{sibling_id}"]').get_by_role(
                            "button", name="Collapse session family"
                        )
                    ).to_be_visible()
                    assert promoted["host_id"] == source_host
                    assert (
                        Path(source["workspace"]) / "keep.txt"
                    ).read_text() == "Shared workspace must survive\n"
                    await page.get_by_label("Message the agent").fill("Still here after deletion")
                    await page.get_by_label("Message the agent").press("Enter")
                    await expect(
                        page.get_by_text("Done: Still here after deletion", exact=True)
                    ).to_be_visible(timeout=60000)
                    await page.screenshot(path=str(evidence / "survivor.png"))
                    row = page.locator(f'li[data-sidebar-session-id="{sibling_id}"]')
                    await row.hover()
                    await row.get_by_test_id("quick-pin-conversation").click()
                    await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: row.get("family", {}).get("pinned") is False,
                    )

                    async def menu(session_id: str) -> None:
                        row = page.locator(f'li[data-sidebar-session-id="{session_id}"]').first
                        await row.hover()
                        await row.get_by_test_id("conversation-actions").click()

                    # Only the representative offers pin actions.
                    await menu(fresh_id)
                    await expect(page.get_by_test_id("move-to-project")).to_have_count(0)
                    await expect(page.get_by_test_id("pin-conversation")).to_have_count(0)
                    await page.keyboard.press("Escape")
                    member_row = page.locator(f'li[data-sidebar-session-id="{fresh_id}"]')
                    await expect(
                        member_row.get_by_test_id("quick-pin-conversation")
                    ).to_have_count(0)
                    await page.locator(f'li[data-sidebar-session-id="{sibling_id}"]').first.hover()
                    await (
                        page.locator(f'li[data-sidebar-session-id="{sibling_id}"]')
                        .first.get_by_test_id("quick-pin-conversation")
                        .click()
                    )
                    await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: row.get("family", {}).get("pinned") is True,
                    )
                    await page.screenshot(path=str(evidence / "family-pinned.png"))
                    await expect(member_row).to_have_attribute("data-sibling-session", "true")
                    await expect(
                        member_row.get_by_test_id("quick-pin-conversation")
                    ).to_have_count(0)
                    await page.locator(f'li[data-sidebar-session-id="{sibling_id}"]').first.hover()
                    await (
                        page.locator(f'li[data-sidebar-session-id="{sibling_id}"]')
                        .first.get_by_test_id("quick-pin-conversation")
                        .click()
                    )
                    await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: row.get("family", {}).get("pinned") is False,
                    )
                    # Project moves are family-wide.
                    await menu(sibling_id)
                    await page.get_by_test_id("move-to-project").hover()
                    await page.get_by_placeholder("Search or create project").fill("Sibling demo")
                    await page.get_by_role(
                        "menuitem", name=re.compile("Create.*Sibling demo")
                    ).click()
                    filed = await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{sibling_id}",
                        lambda row: bool(row.get("project_id")),
                    )
                    assert (await client.get(f"{rig.a}/v1/sessions/{fresh_id}")).json()[
                        "project_id"
                    ] == filed["project_id"]
                    await page.screenshot(path=str(evidence / "family-project.png"))
                    # Pinning an expanded folder's family removes every row from that folder.
                    project_header = page.get_by_role("button", name="Sibling demo", exact=True)
                    if await project_header.get_attribute("aria-expanded") == "false":
                        await project_header.click()
                    project_section = project_header.locator("xpath=ancestor::section[1]")
                    member_selector = f'li[data-sidebar-session-id="{fresh_id}"]'
                    member_row = project_section.locator(member_selector)
                    await expect(member_row).to_have_attribute("data-sibling-session", "true")
                    await expect(
                        member_row.get_by_test_id("quick-pin-conversation")
                    ).to_have_count(0)
                    for pinned in (True, False):
                        representative = page.locator(
                            f'li[data-sidebar-session-id="{sibling_id}"]'
                        )
                        await representative.hover()
                        await representative.get_by_test_id("quick-pin-conversation").click()
                        await _poll(
                            client,
                            f"{rig.a}/v1/sessions/{sibling_id}",
                            lambda row, expected=pinned: (
                                row.get("family", {}).get("pinned") is expected
                            ),
                        )
                        for member_id in (sibling_id, fresh_id):
                            selector = f'li[data-sidebar-session-id="{member_id}"]'
                            await expect(page.locator(selector)).to_have_count(1)
                            await expect(project_section.locator(selector)).to_have_count(
                                0 if pinned else 1
                            )
                    # Ordinary Fork stays standalone and can reuse this sandbox.
                    await menu(sibling_id)
                    await page.get_by_test_id("fork-conversation").click()
                    await expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()
                    await page.get_by_test_id("fork-session-host-select").click()
                    await expect(
                        page.get_by_role("option", name="Same sandbox online")
                    ).to_be_visible()
                    await page.screenshot(path=str(evidence / "fork-same-sandbox.png"))
                    await page.get_by_role("option", name="Same sandbox online").click()
                    await page.get_by_test_id("fork-session-submit").click()
                    await expect(page).not_to_have_url(
                        re.compile(f"/c/{sibling_id}$"), timeout=30000
                    )
                    fork_id = page.url.rsplit("/c/", 1)[-1]
                    fork = await _poll(
                        client,
                        f"{rig.a}/v1/sessions/{fork_id}",
                        lambda row: bool(row.get("runner_online")),
                    )
                    assert not fork.get("family")
                    assert fork["host_id"] == source_host
                    await menu(fork_id)
                    await page.get_by_role("menuitem", name="Delete", exact=True).click()
                    await (
                        page.get_by_role("dialog")
                        .get_by_role("button", name="Delete", exact=True)
                        .click()
                    )
                    await expect(
                        page.locator(f'li[data-sidebar-session-id="{fork_id}"]')
                    ).to_have_count(0)
                    assert (Path(source["workspace"]) / "keep.txt").is_file()
                    await page.goto(f"{rig.ui}/c/{sibling_id}")
                    await page.get_by_label("Message the agent").fill(
                        "Still here after fork deletion"
                    )
                    await page.get_by_label("Message the agent").press("Enter")
                    await expect(
                        page.get_by_text("Done: Still here after fork deletion", exact=True)
                    ).to_be_visible(timeout=60000)
                    # Desktop header and mobile header/sidebar use the same composer.
                    await page.goto(f"{rig.ui}/c/{sibling_id}")
                    await page.locator(f'li[data-sidebar-session-id="{sibling_id}"]').first.click(
                        button="right"
                    )
                    await page.get_by_role("menuitem", name="Add nested agent", exact=True).click()
                    await expect(page.get_by_test_id("sibling-composer-context")).to_be_visible()
                    await page.goto(f"{rig.ui}/c/{sibling_id}")
                    await page.get_by_test_id("header-conversation-actions").click()
                    await page.get_by_role("menuitem", name="Add nested agent", exact=True).click()
                    await expect(page.get_by_test_id("sibling-composer-context")).to_be_visible()
                    async with async_user_page(
                        browser,
                        headers={},
                        viewport={"width": 390, "height": 844},
                        has_touch=True,
                        is_mobile=True,
                        record_video_dir=str(evidence),
                        record_video_size={"width": 390, "height": 844},
                    ) as phone:
                        await phone.goto(f"{rig.ui}/c/{sibling_id}")
                        await phone.get_by_test_id("header-conversation-actions").click()
                        await phone.get_by_role(
                            "menuitem", name="Add nested agent", exact=True
                        ).click()
                        await expect(
                            phone.get_by_test_id("sibling-composer-context")
                        ).to_be_visible()
                        await phone.screenshot(path=str(evidence / "mobile-composer.png"))
                        await phone.get_by_role("button", name="Open sidebar", exact=True).click()
                        project = phone.get_by_role("button", name="Sibling demo", exact=True)
                        if await project.get_attribute("aria-expanded") == "false":
                            await project.click()
                        row = phone.locator(f'li[data-sidebar-session-id="{fresh_id}"]').first
                        await row.scroll_into_view_if_needed()
                        box = await row.bounding_box()
                        assert box is not None
                        cdp = await phone.context.new_cdp_session(phone)
                        await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
                        await cdp.send(
                            "Input.dispatchTouchEvent",
                            {
                                "type": "touchStart",
                                "touchPoints": [
                                    {
                                        "x": box["x"] + box["width"] / 2,
                                        "y": box["y"] + box["height"] / 2,
                                    }
                                ],
                            },
                        )
                        await phone.wait_for_timeout(850)
                        await cdp.send(
                            "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}
                        )
                        await phone.screenshot(path=str(evidence / "mobile-add-agent-menu.png"))
                        await phone.get_by_role(
                            "menuitem", name="Add nested agent", exact=True
                        ).click()
                        await expect(
                            phone.get_by_test_id("sibling-composer-context")
                        ).to_contain_text("Implement the client")
                        await expect(
                            phone.get_by_test_id("new-chat-landing-host-chip")
                        ).to_have_attribute("aria-label", re.compile("Host: .+, Online"))
                        await phone.screenshot(path=str(evidence / "mobile-siblings.png"))
                    assert not errors, errors
                finally:
                    await page.screenshot(path=str(evidence / "last-screen.png"))
                    await page.context.tracing.stop(path=str(evidence / "trace.zip"))
            await browser.close()
