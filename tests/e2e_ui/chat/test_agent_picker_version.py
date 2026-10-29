"""The picker binds the current owned agent; old servers retain session discovery."""

from __future__ import annotations

import json
import re

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests._helpers.picker_routes import OWN_AGENTS
from tests._helpers.polling import poll_until_async
from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_HOST_ID = "host_e2e"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")

# Seeded built-in (protected), an older user-registered template, and the
# newer same-named upload discovered on a session.
_DEBBY_ID = "ag_debby_seeded"
_TEMPLATE_ID = "ag_agenta_template"  # builtin:false, older
_UPLOAD_ID = "ag_agenta_upload_v2"  # session-scoped, newer — must win
_OLD_UPLOAD_ID = "ag_agenta_upload_v1"
_SCAN_TEMPLATE_SESSION = "conv_bound_template"
_SCAN_UPLOAD_SESSION = "conv_upload_v2"


def _agents_body() -> str:
    """Catalog: a seeded built-in plus an older user-registered Agent A."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _DEBBY_ID,
                    "name": "debby",
                    "description": "Seeded built-in",
                    "harness": "claude-sdk",
                    "skills": [],
                    "builtin": True,
                    "created_at": 100,
                },
                {
                    "id": _TEMPLATE_ID,
                    "name": "agent-a",
                    "description": "Agent A version 1 (template)",
                    "harness": "claude-sdk",
                    "skills": [],
                    "builtin": False,
                    "created_at": 200,
                },
            ]
        }
    )


def _scan_body() -> str:
    """Sessions scan: one bound the template, one is the newer upload."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {
                    "id": _SCAN_UPLOAD_SESSION,
                    "agent_id": _UPLOAD_ID,
                    "agent_name": "agent-a",
                    "created_at": 300,
                },
                {
                    "id": _SCAN_TEMPLATE_SESSION,
                    "agent_id": _TEMPLATE_ID,
                    "agent_name": "agent-a",
                    "created_at": 250,
                },
            ],
            "has_more": False,
        }
    )


def _hosts_body() -> str:
    return json.dumps(
        {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
    )


async def _register_routes(
    page, *, created_session_id: str, create_requests: list[dict], agent_install: bool
) -> None:
    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_upload_agent(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": _UPLOAD_ID,
                    "object": "agent",
                    "name": "agent-a",
                    "description": "Agent A version 2 (upload)",
                    "harness": "claude-sdk",
                    "skills": [],
                }
            ),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            create_requests.append(route.request.post_data_json or {"__multipart__": True})
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id, "session_id": created_session_id}),
            )
        else:
            # The picker's sessions scan (?visibility=mine).
            await route.fulfill(status=200, content_type="application/json", body=_scan_body())

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    # Enrich fetch for the winning upload must be routed BEFORE the broad
    # sessions matcher so it is not captured as a create/scan.
    await page.route(f"**/v1/sessions/{_SCAN_UPLOAD_SESSION}/agent", handle_upload_agent)
    await page.route("**/v1/hosts", handle_hosts)
    await stub_empty_host_picker_data(page, _HOST_ID)
    await page.route("**/v1/agents", handle_agents)
    await page.route(
        OWN_AGENTS,
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": agent_id,
                        "name": "agent-a",
                        "harness": "claude-sdk",
                        "created_at": created,
                    }
                    for agent_id, created in [(_OLD_UPLOAD_ID, 200), (_UPLOAD_ID, 300)]
                ]
                if agent_install
                else []
            }
        ),
    )

    async def info(route: Route) -> None:
        response = await route.fetch()
        body = await response.json()
        body["agent_install"] = agent_install
        await route.fulfill(response=response, json=body)

    await page.route("**/v1/info", info)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)


async def _seed_workspace(page) -> None:
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


@pytest.mark.parametrize("agent_install", [False, True], ids=["legacy", "owned-catalog"])
def test_picker_binds_newest_agent_version(
    seeded_session: tuple[str, str], agent_install: bool
) -> None:
    """Selecting Agent A binds its current owned ID or the legacy session upload."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id, agent_install))


async def _drive(base_url: str, created_session_id: str, agent_install: bool) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict] = []
            await _register_routes(
                page,
                created_session_id=created_session_id,
                create_requests=create_requests,
                agent_install=agent_install,
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent dropdown.
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            def option(agent_id: str):
                return page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")

            # The seeded (built-in) debby lists inline. Agent A is a custom
            # agent, so it lives in the custom-agent "Other..." submenu — drill in.
            await expect(option(_DEBBY_ID)).to_be_visible()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            # Server and owned names are separate identities on current servers.
            await expect(option(_UPLOAD_ID)).to_be_visible()
            await expect(option(_OLD_UPLOAD_ID)).to_have_count(0)
            await expect(option(_TEMPLATE_ID)).to_have_count(1 if agent_install else 0)
            if agent_install:
                await expect(option(_UPLOAD_ID)).to_contain_text("agent-a (yours)")
                await expect(option(_TEMPLATE_ID)).to_contain_text("(server)")

            # Select Agent A, send, and assert the create POST bound the upload.
            await option(_UPLOAD_ID).click()
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await page.get_by_test_id("new-chat-landing-input").fill("which version are you?")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await poll_until_async(lambda: len(create_requests) == 1)
            assert create_requests[0].get("agent_id") == _UPLOAD_ID, (
                f"picker bound the wrong version: {create_requests[0]}"
            )
        finally:
            await browser.close()
