"""Browser coverage for the workspace rail before a session is created."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Request, Route, WebSocket, async_playwright, expect

_HOST_ALPHA = ("host_e2e_alpha", "e2e-host-alpha")
_HOST_BETA = ("host_e2e_beta", "e2e-host-beta")
_ALPHA_WORKSPACE = "/alpha/project"
_BETA_WORKSPACE = "/beta/project"
_BETA_OTHER_WORKSPACE = "/beta/other"
_SESSION_RESOURCE_PATH = re.compile(r"^/v1/sessions/(?:[^/]*)/resources(?:/|$)")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run async Playwright away from pytest-playwright's sync event loop."""
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


def _hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {"host_id": host_id, "name": name, "owner": "e2e", "status": "online"}
                for host_id, name in (_HOST_ALPHA, _HOST_BETA)
            ]
        }
    )


def _agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_e2e",
                    "name": "hello-world",
                    "display_name": "Hello World",
                    "description": "Deterministic pre-chat agent",
                    "harness": None,
                    "skills": [],
                }
            ]
        }
    )


async def _fulfill_json(route: Route, body: object, *, status: int = 200) -> None:
    await route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(body),
    )


def test_prechat_workspace_files_agents_and_target_switching(live_server: str) -> None:
    """The landing rail follows host/folder picks without creating a chat."""
    _run_in_fresh_loop(_drive_prechat_workspace(live_server))


async def _drive_prechat_workspace(base_url: str) -> None:
    requested_targets: list[tuple[str, str, str]] = []
    session_posts: list[str] = []
    premature_session_requests: list[str] = []
    files = {
        (_HOST_ALPHA[0], _ALPHA_WORKSPACE): "alpha-visible.txt",
        (_HOST_BETA[0], _BETA_WORKSPACE): "beta-project-visible.txt",
        (_HOST_BETA[0], _BETA_OTHER_WORKSPACE): "beta-other-visible.txt",
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_hosts_body(),
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_agents_body(),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    await _fulfill_json(route, {"detail": "unexpected session create"}, status=500)
                    return
                await _fulfill_json(route, {"data": [], "has_more": False})

            async def handle_worktrees(route: Route) -> None:
                await _fulfill_json(route, {"object": "list", "data": [], "has_more": False})

            async def handle_workspace_resource(route: Route) -> None:
                parsed = urlparse(route.request.url)
                host_id = unquote(
                    parsed.path.split("/v1/hosts/", 1)[1].split("/workspace/resources", 1)[0]
                )
                suffix = parsed.path.split("/workspace/resources/", 1)[1]
                workspace = parse_qs(parsed.query)["workspace"][0]
                requested_targets.append((host_id, workspace, suffix))

                if suffix == "environments/default":
                    await _fulfill_json(
                        route,
                        {
                            "object": "session.environment",
                            "id": "default",
                            "metadata": {
                                "root": workspace,
                                "home": "/home/e2e",
                                "reachable": {
                                    "unconfined": True,
                                    "roots": [
                                        {
                                            "path": workspace,
                                            "access": "write",
                                            "origin": "cwd",
                                        }
                                    ],
                                },
                            },
                        },
                    )
                    return
                if suffix.startswith("environments/default/filesystem"):
                    filename = files[(host_id, workspace)]
                    await _fulfill_json(
                        route,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": filename,
                                    "name": filename,
                                    "path": filename,
                                    "type": "file",
                                    "bytes": 12,
                                    "modified_at": 0,
                                }
                            ],
                            "has_more": False,
                            "base": workspace,
                        },
                    )
                    return
                if suffix == "environments/default/changes":
                    await _fulfill_json(
                        route,
                        {"object": "list", "data": [], "has_more": False},
                    )
                    return
                if suffix == "github":
                    await _fulfill_json(
                        route,
                        {
                            "object": "session.github.info",
                            "available": False,
                            "reason": "not_a_git_repo",
                        },
                    )
                    return
                await _fulfill_json(route, {"detail": f"unexpected resource {suffix}"}, status=404)

            def note_session_request(request: Request) -> None:
                request_path = urlparse(request.url).path
                if request.method == "POST" and request_path == "/v1/sessions":
                    session_posts.append(request.url)
                if _SESSION_RESOURCE_PATH.match(request_path):
                    premature_session_requests.append(f"{request.method} {request_path}")

            def note_session_socket(socket: WebSocket) -> None:
                socket_path = urlparse(socket.url).path
                if _SESSION_RESOURCE_PATH.match(socket_path):
                    premature_session_requests.append(f"WS {socket_path}")

            page.on("request", note_session_request)
            page.on("websocket", note_session_socket)
            await page.route(re.compile(r"/v1/hosts(?:\?.*)?$"), handle_hosts)
            await page.route(re.compile(r"/v1/agents(?:\?.*)?$"), handle_agents)
            await page.route(re.compile(r"/v1/sessions(?:\?.*)?$"), handle_sessions)
            await page.route(re.compile(r"/v1/hosts/[^/]+/worktrees(?:\?.*)?$"), handle_worktrees)
            await page.route(
                re.compile(r"/v1/hosts/[^/]+/workspace/resources(?:/.*)?(?:\?.*)?$"),
                handle_workspace_resource,
            )

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{
                        "{_HOST_ALPHA[0]}": ["{_ALPHA_WORKSPACE}"],
                        "{_HOST_BETA[0]}": [
                            "{_BETA_WORKSPACE}",
                            "{_BETA_OTHER_WORKSPACE}"
                        ]
                    }})
                );"""
            )
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            toggle = page.locator(
                'button[aria-label="Expand right panel"], '
                'button[aria-label="Collapse right panel"]'
            ).first
            await expect(toggle).to_be_visible(timeout=30_000)
            if await toggle.get_attribute("aria-label") == "Expand right panel":
                await toggle.click()

            rail = page.get_by_role("complementary", name="Workspace")
            await expect(rail).to_be_visible()
            files_tab = rail.get_by_role("tab", name="Files", exact=True)
            agents_tab = rail.get_by_role("tab", name="Agents 0", exact=True)
            await expect(files_tab).to_have_attribute("aria-selected", "true")
            await expect(rail.get_by_text("alpha-visible.txt", exact=True)).to_be_visible()

            await agents_tab.click()
            await expect(agents_tab).to_have_attribute("aria-selected", "true")
            await expect(
                rail.get_by_text("No agents yet. Start a chat to add an agent.", exact=True)
            ).to_be_visible()
            await files_tab.click()

            host_chip = page.get_by_test_id("new-chat-landing-host-chip")
            await host_chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_BETA[0]}").click()
            await expect(host_chip).to_have_attribute("aria-label", re.compile(_HOST_BETA[1]))
            await expect(rail.get_by_text("beta-project-visible.txt", exact=True)).to_be_visible()

            workspace_chip = page.get_by_test_id("new-chat-landing-workspace-chip")
            await workspace_chip.click()
            await page.get_by_test_id("new-chat-landing-workspace-recent-1").click()
            await expect(workspace_chip).to_contain_text("other")
            await expect(rail.get_by_text("beta-other-visible.txt", exact=True)).to_be_visible()

            requested = {(host_id, workspace) for host_id, workspace, _ in requested_targets}
            assert (_HOST_ALPHA[0], _ALPHA_WORKSPACE) in requested
            assert (_HOST_BETA[0], _BETA_WORKSPACE) in requested
            assert (_HOST_BETA[0], _BETA_OTHER_WORKSPACE) in requested
            assert session_posts == []
            assert premature_session_requests == []
            assert urlparse(page.url).path == "/"
        finally:
            await browser.close()
