"""E2E: the home composer prefills from a project's stored ``config``.

Visiting ``/?project=<name>`` seeds the new-session composer from that project's
stored defaults (``web/src/shell/projectPrefill.ts`` +
``web/src/shell/NewChatDialog.tsx``): host, working directory, and agent all
come from ``config``, silently falling back to the generic defaults for any
field the config leaves unset. This replaced the old newest-session inference —
stored config is now the single project-driven prefill source.

This drives the real chain: the composer resolves the project NAME → id via
``GET /v1/sessions/projects``, fetches ``GET /v1/projects/{id}`` for its config,
and seeds the host / workspace / agent, which then ride along to the create
``POST /v1/sessions``.

Heavy ``page.route`` stubbing mirrors ``test_start_session`` and is required for
the same reason: the e2e_ui harness's tunneled runner registers no *host* and
the host filesystem endpoint has nothing to browse, so ``/v1/hosts``,
``/v1/agents``, the project config, and the create ``POST`` are faked (the POST
handler *captures the body* — the thing under test — and returns a real seeded
session id so post-send navigation lands somewhere real).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e_cfg"
_PROJECT_ID = "proj_e2e_cfg"
_PROJECT_NAME = "ConfiguredProject"
_CONFIG_WORKSPACE = "/work/configured-repo"
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


def _hosts_body() -> str:
    return json.dumps(
        {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
    )


def _agents_body() -> str:
    """Two agents: the default-ranked Claude Code and a second the config pins."""
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
                },
                {
                    "id": "ag_pinned_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Multi-agent coding",
                    "harness": "claude-sdk",
                    "skills": [],
                },
            ]
        }
    )


def _projects_list_body() -> str:
    """``GET /v1/sessions/projects`` — the composer resolves the ?project= name
    to this id. Returns a bare ``ProjectSummary[]`` (``{id, name}``)."""
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body() -> str:
    """``GET /v1/projects/{id}`` — the stored defaults the composer seeds from."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {
                "host_id": _HOST_ID,
                "workspace": _CONFIG_WORKSPACE,
                "agent_id": "ag_pinned_e2e",
            },
        }
    )


def test_composer_prefills_from_project_config(seeded_session: tuple[str, str]) -> None:
    """A ``?project=`` visit seeds host / workspace / agent from stored config.

    The pinned agent (``ag_pinned_e2e``) and workspace (``/work/configured-repo``)
    come from ``config`` — NOT from the default-ranked Claude Code or a recent
    workspace — and ride along to ``POST /v1/sessions``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_prefill(base_url, session_id))


async def _drive_prefill(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

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
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # Neutralize the agent-discovery scan so only the stubbed catalog
            # feeds the picker (a leftover native agent would rank ahead).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The agent picker trigger reflects the config-pinned agent (Polly),
            # not the default-ranked Claude Code — proof the config seed won.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Polly", timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_pinned_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == _CONFIG_WORKSPACE, body
        finally:
            await browser.close()


# The sandbox sentinel a project can store as its default host (mirrors
# SANDBOX_HOST_CHOICE in web/src/lib/hostPreferences.ts).
_SANDBOX_CHOICE = "__sandbox__"


def _managed_info_body() -> str:
    """``GET /v1/info`` for a managed deployment that offers a sandbox."""
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": True,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "lakebox",
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def _sandbox_config_body() -> str:
    """``GET /v1/projects/{id}`` whose stored default host is the sandbox."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _SANDBOX_CHOICE},
        }
    )


def test_composer_prefills_sandbox_default_from_project_config(
    seeded_session: tuple[str, str],
) -> None:
    """A project whose stored default host is the sandbox selects the sandbox.

    Regression: the settings dialog offers (and persists) the sandbox sentinel as
    a default host, but prefill only seeded real online hosts, so the sandbox
    default was silently dropped and the composer fell back to a connected host.
    With the sandbox handled in prefill, a ``?project=`` visit selects the
    sandbox — the create posts ``host_type: "managed"`` (no ``host_id``).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_sandbox_prefill(base_url, session_id))


async def _drive_sandbox_prefill(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_sandbox_config_body()
                )

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
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/info", handle_info)
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The host chip shows the sandbox — proof the stored sandbox default
            # was honored rather than dropped for a connected host.
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_contain_text(
                "Sandbox", timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            # A sandbox create is managed (server provisions the host); no host_id.
            assert body.get("host_type") == "managed", body
            assert "host_id" not in body or body["host_id"] is None, body
        finally:
            await browser.close()


def test_composer_files_new_session_into_project(seeded_session: tuple[str, str]) -> None:
    """A ``?project=`` visit files the new session at create time (born filed).

    The sidebar's per-project "new session" pencil lands here with the project
    pre-scoped. On Send the session must be *born filed*: the create
    ``POST /v1/sessions`` carries the legacy ``omni_project`` label so the
    sidebar groups the new row under its project from its first appearance —
    instead of briefly showing it under the ungrouped "Sessions" section until
    the follow-up ``project_id`` move (``moveConversationToProject``) catches up
    in the search-indexed session list. The sidebar, the ``?project=`` folder
    query, and the project list all dual-read membership from this label OR the
    first-class ``project_id`` the move then sets, so there is no ungrouped
    window either way.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_born_filed(base_url, session_id))


async def _drive_born_filed(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

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
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # The per-project pencil destination: the composer lands pre-scoped
            # to this project (no interaction needed to file into it).
            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("write the docs")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # Born filed: the create carries the legacy omni_project label so the
            # sidebar files the new row under its project immediately, rather than
            # flashing under "Sessions" until the follow-up project_id move lands.
            labels = body.get("labels") or {}
            assert labels.get("omni_project") == _PROJECT_NAME, body
        finally:
            await browser.close()


# The worktree-list endpoint the composer probes for the seeded workspace.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")
# The user's most-recent workspace on the host — a LINKED worktree of the repo
# whose main tree lives at _MAIN_REPO. Seeded into localStorage so the auto-seed
# would land here (the pre-fix behavior) unless the fork-fresh redirect fires.
_MAIN_REPO = "/work/gamma"
_LINKED_WORKTREE = "/work/gamma-worktrees/feature-x"


def _base_branch_config_body() -> str:
    """``GET /v1/projects/{id}`` whose stored config sets a default base branch."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _HOST_ID, "base_branch": "develop"},
        }
    )


def _worktrees_body() -> str:
    """``GET /v1/hosts/{id}/worktrees`` — the repo's main tree plus one linked
    worktree (the recent workspace). The composer's fork-fresh probe reads this
    to decide the recent path is a worktree and redirect to the main tree."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {"path": _MAIN_REPO, "branch": "main", "is_main": True, "detached": False},
                {
                    "path": _LINKED_WORKTREE,
                    "branch": "feature/x",
                    "is_main": False,
                    "detached": False,
                },
            ],
        }
    )


def test_composer_forks_fresh_from_project_default_over_last_worktree(
    seeded_session: tuple[str, str],
) -> None:
    """A project default base branch forks fresh instead of reusing the last worktree.

    Regression: the composer auto-seeds the working directory from the user's
    most-recent workspace. When that path is an existing (linked) worktree, the
    branch prefilled from it and the project's stored default base branch was
    silently dropped — a fresh new-chat continued in the old worktree instead of
    forking off the default. With a default configured, the composer now probes
    the recent path, redirects the seed to the repo's MAIN work tree, and
    auto-names a fresh worktree branch so the create forks off the default:
    ``git: {branch_name: worktree-<hex>, base_branch: "develop"}`` at the main
    repo — NOT an ``existing_worktree`` bind in the linked worktree.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_fork_fresh(base_url, session_id))


async def _drive_fork_fresh(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_base_branch_config_body()
                )

            async def handle_worktrees(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_worktrees_body()
                )

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
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await page.route(_WORKTREES_RE, handle_worktrees)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # The most-recent workspace is a LINKED worktree — the pre-fix
            # auto-seed would land here and reuse it.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["{_LINKED_WORKTREE}"] }})
                );"""
            )

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The workspace chip shows the MAIN repo (gamma), not the linked
            # worktree — proof the fork-fresh redirect fired.
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "gamma", timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start fresh")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # Seeded at the MAIN repo, not the linked worktree.
            assert body["workspace"] == _MAIN_REPO, body
            git = body.get("git") or {}
            # A fresh worktree forked off the project default — not a bind.
            assert re.fullmatch(r"worktree-[0-9a-f]{8}", git.get("branch_name", "")), body
            assert git.get("base_branch") == "develop", body
            assert "existing_worktree" not in git, body
        finally:
            await browser.close()
