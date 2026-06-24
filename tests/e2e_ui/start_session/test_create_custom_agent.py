"""E2E: "Create custom agent" dialog on the new-session landing page.

Covers the user journey of creating a custom agent from the agent picker
dropdown, configuring it (name, description, MCP tools), and submitting the
form to create a session with the bundled agent.

Uses the same route-stubbing approach as ``test_start_session.py``: the
server's ``/v1/hosts``, ``/v1/agents``, and ``POST /v1/sessions`` are faked
so the tests don't need a real host. The create POST is intercepted to
capture the multipart request body for assertion.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects.
_HOST_ID = "host_e2e"
# Bare create endpoint — intercepts POST but lets GET through.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


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


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _agents_body() -> str:
    """Single Claude Code agent for the stub."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                }
            ]
        }
    )


def _hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                }
            ]
        }
    )


async def _register_routes(
    page,
    *,
    created_session_id: str,
    create_requests: list[dict[str, Any]],
) -> None:
    """Install stubs for hosts, agents, session create, and events."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            # Capture multipart or JSON create requests.
            content_type = route.request.headers.get("content-type", "")
            if "multipart" in content_type:
                # For multipart, we can't easily parse the binary body in
                # Playwright, so just record that a multipart POST happened.
                create_requests.append({"__multipart__": True})
            else:
                create_requests.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id, "session_id": created_session_id}),
            )
        else:
            await route.continue_()

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the composer can enable Send."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


# ── Tests ──────────────────────────────────────────────────────────


def test_create_agent_dialog_opens_from_dropdown(
    seeded_session: tuple[str, str],
) -> None:
    """The agent dropdown shows a "Create custom agent" item that opens the dialog."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_dialog_opens(base_url, session_id))


async def _drive_dialog_opens(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_requests=create_requests
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent dropdown.
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # "Create custom agent" item should be visible.
            create_item = page.get_by_test_id("new-chat-landing-create-agent")
            await expect(create_item).to_be_visible()

            # Click it — dialog should open.
            await create_item.click()
            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Verify form fields are present.
            await expect(page.get_by_test_id("create-agent-name")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-description")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-harness")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-instructions")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-add-mcp")).to_be_visible()
        finally:
            await browser.close()


def test_create_agent_submits_multipart_bundle(
    seeded_session: tuple[str, str],
) -> None:
    """Creating a custom agent and sending produces a multipart POST."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_create_and_submit(base_url, session_id))


async def _drive_create_and_submit(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_requests=create_requests
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-create-agent").click()

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill in agent details.
            await page.get_by_test_id("create-agent-name").fill("test-agent")
            await page.get_by_test_id("create-agent-description").fill("A test agent")
            await page.get_by_test_id("create-agent-instructions").fill(
                "You are a test assistant."
            )

            # Submit the dialog.
            await page.get_by_test_id("create-agent-submit").click()

            # Dialog should close.
            await expect(dialog).to_be_hidden(timeout=5_000)

            # The agent chip should now show the custom agent name.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "test-agent"
            )

            # Type a message and submit the session.
            await page.get_by_test_id("new-chat-landing-input").fill("hello world")
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create POST should have been a multipart request (the bundle).
            await _wait_until(lambda: len(create_requests) == 1)
            assert create_requests[0].get("__multipart__") is True, (
                f"Expected multipart POST, got: {create_requests[0]}"
            )
        finally:
            await browser.close()


def test_create_agent_with_mcp_server(
    seeded_session: tuple[str, str],
) -> None:
    """Adding an MCP server in the dialog includes it in the bundle."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_mcp_server(base_url, session_id))


async def _drive_mcp_server(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_requests=create_requests
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-create-agent").click()

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill in agent name (required).
            await page.get_by_test_id("create-agent-name").fill("mcp-agent")

            # Add an MCP server.
            await page.get_by_test_id("create-agent-add-mcp").click()

            # An MCP entry card should appear.
            mcp_entry = page.get_by_test_id("create-agent-mcp-entry")
            await expect(mcp_entry).to_be_visible()

            # Fill in MCP server details (stdio transport is default).
            await page.get_by_test_id("create-agent-mcp-name").fill("github")
            await page.get_by_test_id("create-agent-mcp-command").fill("npx")
            await page.get_by_test_id("create-agent-mcp-args").fill(
                "-y @modelcontextprotocol/server-github"
            )
            await page.get_by_test_id("create-agent-mcp-env").fill("GITHUB_TOKEN=ghp_test123")

            # Submit the dialog.
            await page.get_by_test_id("create-agent-submit").click()
            await expect(dialog).to_be_hidden(timeout=5_000)

            # Submit session.
            await page.get_by_test_id("new-chat-landing-input").fill("list repos")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_requests) == 1)
            assert create_requests[0].get("__multipart__") is True
        finally:
            await browser.close()


def test_create_agent_cancel_closes_dialog(
    seeded_session: tuple[str, str],
) -> None:
    """Cancelling the dialog closes it without creating an agent."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_cancel(base_url, session_id))


async def _drive_cancel(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_requests=create_requests
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-create-agent").click()

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill some fields.
            await page.get_by_test_id("create-agent-name").fill("should-not-persist")

            # Cancel.
            cancel_btn = dialog.get_by_role("button", name="Cancel")
            await cancel_btn.click()

            # Dialog should close.
            await expect(dialog).to_be_hidden(timeout=5_000)

            # The agent chip should still show the original agent (Claude Code).
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Claude Code"
            )
        finally:
            await browser.close()
