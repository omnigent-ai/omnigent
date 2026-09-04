"""E2E: the home composer deep-links directly to a host via ``?host=<host_id>``.

Visiting ``/?host=<host_id>`` pre-selects that host in the new-session
composer (``web/src/shell/NewChatDialog.tsx``) when it resolves to a
currently *online* host — ahead of both the persisted last-choice/first-online
default and a ``?project=``'s stored host default. This lets a dashboard tile
on a shared, multi-host ``omnigent-server`` deployment deep-link straight to
one specific registered host instead of making the user pick it out of a
growing list every time (issue #5869).

Heavy ``page.route`` stubbing mirrors ``test_start_session``/
``test_project_config_prefill`` and is required for the same reason: the
e2e_ui harness's tunneled runner registers no *host*, so ``/v1/hosts``,
``/v1/agents``, the project config, and the create ``POST`` are faked (the
POST handler *captures the body* — the thing under test — and returns a real
seeded session id so post-send navigation lands somewhere real).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Two online hosts: without ``?host=``, the composer auto-selects the FIRST
# one (alpha) when there's no persisted pick — so selecting beta via the
# query param, rather than alpha, is the proof the deep link won.
_HOST_ALPHA = ("host_e2e_alpha", "e2e-host-alpha")
_HOST_BETA = ("host_e2e_beta", "e2e-host-beta")
_PROJECT_ID = "proj_e2e_hostlink"
_PROJECT_NAME = "HostLinkProject"
# Bare create endpoint (POST captured); NOT the /{id}/... sub-routes.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# One project config endpoint: /v1/projects/<id> (not the bare list).
_PROJECT_CFG_RE = re.compile(r"/v1/projects/[^/?]+")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own loop (see test_start_session)."""
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


def _two_hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {"host_id": hid, "name": name, "owner": "e2e", "status": "online"}
                for hid, name in (_HOST_ALPHA, _HOST_BETA)
            ]
        }
    )


def _agents_body() -> str:
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


def _projects_list_body() -> str:
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body(*, host_id: str) -> str:
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": host_id},
        }
    )


async def _register_routes(
    page,
    *,
    created_session_id: str,
    create_bodies: list[dict[str, Any]],
    project_host_id: str | None = None,
) -> None:
    """Register the host/agent/create/events stubs shared by both tests.

    :param page: The Playwright page to install routes on.
    :param created_session_id: Real pre-seeded session id the faked create
        returns, so post-send navigation lands on a real page.
    :param create_bodies: Sink the create ``POST /v1/sessions`` body is
        appended to — the assertion target.
    :param project_host_id: When set, also stubs ``?project=`` resolution
        (``/v1/sessions/projects`` + ``/v1/projects/{id}``) with this host
        pinned as the project's stored default.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_two_hosts_body())

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
            create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id}),
            )
        else:
            await route.continue_()

    # Neutralize agent discovery so a leaked native agent from another test
    # can't switch the picker mid-flow.
    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

    if project_host_id is not None:

        async def handle_projects_list(route: Route) -> None:
            await route.fulfill(
                status=200, content_type="application/json", body=_projects_list_body()
            )

        async def handle_project_config(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_project_config_body(host_id=project_host_id),
            )

        await page.route("**/v1/sessions/projects", handle_projects_list)
        await page.route(_PROJECT_CFG_RE, handle_project_config)


async def _seed_recent_workspaces(page) -> None:
    """Seed a recent workspace for both stub hosts so the working-directory
    chip auto-fills and Send is enabled without opening the (host-less) file
    browser (mirrors ``test_start_session``'s two-host test)."""
    alpha_id, _ = _HOST_ALPHA
    beta_id, _ = _HOST_BETA
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{
                "{alpha_id}": ["/work/repo"],
                "{beta_id}": ["/work/repo"]
            }})
        );"""
    )


def test_host_query_param_selects_named_host_over_default(
    seeded_session: tuple[str, str],
) -> None:
    """``?host=<beta_id>`` selects beta, not the first-online default (alpha)."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_host_param(base_url, session_id))


async def _drive_host_param(base_url: str, session_id: str) -> None:
    beta_id, beta_name = _HOST_BETA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )
            await _seed_recent_workspaces(page)

            await page.goto(f"{base_url}/?host={beta_id}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(chip).to_contain_text(beta_name, timeout=15_000)

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == beta_id, body
        finally:
            await browser.close()


def test_host_query_param_wins_over_project_config_host(
    seeded_session: tuple[str, str],
) -> None:
    """``?host=<beta_id>&project=<name>`` selects beta, not the project's
    stored default host (alpha) — the deep link is more specific than a
    project's generic stored config, so it wins.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_host_beats_project(base_url, session_id))


async def _drive_host_beats_project(base_url: str, session_id: str) -> None:
    alpha_id, _alpha_name = _HOST_ALPHA
    beta_id, beta_name = _HOST_BETA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                project_host_id=alpha_id,
            )
            await _seed_recent_workspaces(page)

            await page.goto(f"{base_url}/?host={beta_id}&project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(chip).to_contain_text(beta_name, timeout=15_000)

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == beta_id, body
        finally:
            await browser.close()
