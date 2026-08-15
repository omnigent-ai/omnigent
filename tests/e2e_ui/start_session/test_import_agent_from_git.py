"""E2E: "Import from Git" dialog on the new-session landing page.

Covers the user journey of importing a custom agent from a git repository:
open the agent picker, drill into the "Create custom agent" submenu, choose
"Import from Git", fill in the host / repo URL / branch / path, and submit.

Uses the same route-stubbing approach as ``test_create_custom_agent.py``: the
server's ``/v1/hosts``, ``/v1/agents``, and ``POST /v1/agents/import-git`` are
faked so the test needs no real connected host (the clone normally runs on the
host). The import POST is intercepted to capture and assert the JSON body, and
to return a git-backed agent so the picker refreshes.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects and the import clone "runs on".
_HOST_ID = "host_e2e"
# Import endpoint — intercept POST, capture the JSON body.
_IMPORT_RE = re.compile(r"/v1/agents/import-git$")


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
    """Single Claude Code agent for the initial picker state."""
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


def _imported_agent_body() -> str:
    """The git-backed agent the import POST returns."""
    return json.dumps(
        {
            "id": "ag_imported_e2e",
            "name": "imported-agent",
            "version": 1,
            "git_url": "https://github.com/org/repo",
            "git_ref": "main",
            "git_subpath": None,
            "git_commit": "a" * 40,
            "git_host_id": _HOST_ID,
        }
    )


async def _register_routes(
    page,
    *,
    import_requests: list[dict[str, Any]],
) -> None:
    """Install stubs for hosts, agents list, and the git-import POST."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Keep the "Custom agents" group empty so "Create custom agent" stays a
        # top-level submenu (mirrors test_create_custom_agent.py).
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    async def handle_import(route: Route) -> None:
        if route.request.method == "POST":
            import_requests.append(route.request.post_data_json)
            await route.fulfill(
                status=200, content_type="application/json", body=_imported_agent_body()
            )
        else:
            await route.continue_()

    # Import glob first, then the broad agents route, so the POST endpoint wins.
    await page.route(_IMPORT_RE, handle_import)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the composer selects the stubbed host."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


async def _open_import_from_git(page) -> None:
    """Open the picker, the "Create custom agent" submenu, then "Import from Git"."""
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await page.get_by_test_id("new-chat-landing-create-agent-group").click()
    await page.get_by_test_id("new-chat-landing-import-git").click()


# ── Tests ──────────────────────────────────────────────────────────


def test_import_from_git_opens_dialog(seeded_session: tuple[str, str]) -> None:
    """The "Import from Git" submenu item opens the import dialog."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_dialog_opens(base_url, session_id))


async def _drive_dialog_opens(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            import_requests: list[dict[str, Any]] = []
            await _register_routes(page, import_requests=import_requests)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await _open_import_from_git(page)

            dialog = page.get_by_test_id("import-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)
            # The repo URL field and Import button are present.
            await expect(dialog.get_by_label("Repository URL")).to_be_visible()
            await expect(page.get_by_test_id("import-agent-submit")).to_be_visible()
        finally:
            await browser.close()


def test_import_from_git_submits_request(seeded_session: tuple[str, str]) -> None:
    """Filling the form and importing posts the expected git-import body."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_submit(base_url, session_id))


async def _drive_submit(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            import_requests: list[dict[str, Any]] = []
            await _register_routes(page, import_requests=import_requests)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await _open_import_from_git(page)
            dialog = page.get_by_test_id("import-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Select the stubbed host, then fill URL + branch.
            await dialog.get_by_label("Host").select_option(_HOST_ID)
            await dialog.get_by_label("Repository URL").fill("https://github.com/org/repo")
            await dialog.get_by_label("Branch").fill("main")

            await page.get_by_test_id("import-agent-submit").click()

            # The dialog should close and the import POST should carry the form.
            await expect(dialog).to_be_hidden(timeout=5_000)
            await _wait_until(lambda: len(import_requests) == 1)
            body = import_requests[0]
            assert body["git_url"] == "https://github.com/org/repo", body
            assert body["git_ref"] == "main", body
            assert body["host_id"] == _HOST_ID, body
        finally:
            await browser.close()


def test_import_from_git_sends_agent_name(seeded_session: tuple[str, str]) -> None:
    """An explicit agent name is sent, so one repo can be imported per branch."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_named_submit(base_url, session_id))


async def _drive_named_submit(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            import_requests: list[dict[str, Any]] = []
            await _register_routes(page, import_requests=import_requests)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await _open_import_from_git(page)
            dialog = page.get_by_test_id("import-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            await dialog.get_by_label("Host").select_option(_HOST_ID)
            await dialog.get_by_label("Repository URL").fill("https://github.com/org/repo")
            await dialog.get_by_label("Branch").fill("dev")
            # Naming the agent is what allows a second import of the same repo.
            await dialog.get_by_label("Agent name").fill("myagent-dev")

            await page.get_by_test_id("import-agent-submit").click()
            await expect(dialog).to_be_hidden(timeout=5_000)

            await _wait_until(lambda: len(import_requests) == 1)
            body = import_requests[0]
            assert body["name"] == "myagent-dev", body
            assert body["git_ref"] == "dev", body
        finally:
            await browser.close()


def test_import_from_git_shows_server_error(seeded_session: tuple[str, str]) -> None:
    """A rejected import surfaces the server error inline without closing."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_error(base_url, session_id))


async def _drive_error(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            import_requests: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            async def handle_import_error(route: Route) -> None:
                if route.request.method == "POST":
                    import_requests.append(route.request.post_data_json)
                    await route.fulfill(
                        status=400,
                        content_type="application/json",
                        body=json.dumps(
                            {"error": {"code": "INVALID_INPUT", "message": "Not a valid git URL."}}
                        ),
                    )
                else:
                    await route.continue_()

            await page.route(_IMPORT_RE, handle_import_error)
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await _open_import_from_git(page)
            dialog = page.get_by_test_id("import-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            await dialog.get_by_label("Host").select_option(_HOST_ID)
            await dialog.get_by_label("Repository URL").fill("file:///etc/passwd")
            await page.get_by_test_id("import-agent-submit").click()

            # The error is shown inline (role="alert") and the dialog stays open.
            await expect(dialog.get_by_role("alert")).to_contain_text("valid git URL")
            await expect(dialog).to_be_visible()
        finally:
            await browser.close()
