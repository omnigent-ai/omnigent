"""E2E: the new-session agent picker binds the NEWEST version of an agent.

Regression for the picker selecting a stale agent version. Setup mirrors the
report:

* Agent A exists as a user-registered template (``builtin: false`` in
  ``GET /v1/agents``) — e.g. created via ``omnigent server --agent``.
* A newer ``omnigent run`` minted a session-scoped Agent A with a DISTINCT
  agent_id (discovered via ``GET /v1/sessions?kind=any``), created after the
  template.

The picker must offer (and bind) the newest — the upload — not the stale
template. A seeded built-in (``builtin: true``) is the control: a same-named
upload must NOT supersede it.

Route-stubbing approach (same as ``test_create_custom_agent.py``): the SPA is
served by the live server, but ``/v1/agents`` + ``/v1/sessions`` are faked so
the test pins the picker's resolution without provisioning real agents.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")

# Seeded built-in (protected), an older user-registered template, and the
# newer same-named upload discovered on a session.
_DEBBY_ID = "ag_debby_seeded"
_TEMPLATE_ID = "ag_agenta_template"  # builtin:false, older
_UPLOAD_ID = "ag_agenta_upload_v2"  # session-scoped, newer — must win
_SCAN_TEMPLATE_SESSION = "conv_bound_template"
_SCAN_UPLOAD_SESSION = "conv_upload_v2"


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


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


async def _register_routes(page, *, created_session_id: str, create_requests: list[dict]) -> None:
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
            # The picker's sessions scan (?kind=any).
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
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)


async def _seed_workspace(page) -> None:
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def test_picker_binds_newest_agent_version(seeded_session: tuple[str, str]) -> None:
    """Selecting Agent A binds the newer upload, not the stale template."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id))


async def _drive(base_url: str, created_session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict] = []
            await _register_routes(
                page, created_session_id=created_session_id, create_requests=create_requests
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

            # Agent A is offered as the NEWER upload id — the stale template id
            # must not appear (it was superseded). The seeded debby is present
            # and untouched.
            await expect(option(_UPLOAD_ID)).to_be_visible()
            await expect(option(_DEBBY_ID)).to_be_visible()
            assert await option(_TEMPLATE_ID).count() == 0, (
                "stale template version leaked into the picker"
            )

            # Select Agent A, send, and assert the create POST bound the upload.
            await option(_UPLOAD_ID).click()
            await page.get_by_test_id("new-chat-landing-input").fill("which version are you?")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_requests) == 1)
            assert create_requests[0].get("agent_id") == _UPLOAD_ID, (
                f"picker bound the wrong version: {create_requests[0]}"
            )
        finally:
            await browser.close()


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")
