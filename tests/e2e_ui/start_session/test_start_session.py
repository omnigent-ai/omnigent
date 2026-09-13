"""E2E: starting a new session from the home composer ("/").

The landing composer (``NewChatLandingScreen`` in
``web/src/shell/NewChatDialog.tsx``) owns session creation end to end:
the textarea is the new session's first message and the footer chips —
host, working directory, git worktree — plus the unified agent/harness
picker supply every create parameter. The picker selects the agent and offers
model/effort controls under Edit; native permission modes live in the composer's
hand dropdown. Advanced settings remains for configurable agents' underlying
harness (see :func:`_open_entry_config`). Hitting Send POSTs ``/v1/sessions``
and navigates to the new session.

These tests cover the three configuration affordances the user reaches
before sending:

1. **Permission mode** — native permission/approval choices, in the
   hand dropdown. A non-default pick rides along as
   ``terminal_launch_args``.
2. **Working directory** — the file-browser popover behind the working-
   directory chip. Browsing into a folder sets the session's
   ``workspace``.
3. **Git worktree** — the branch chip's popover. Naming a branch attaches
   a ``git`` worktree spec to the create.

Why the heavy ``page.route`` stubbing (mirrors
``sessions/test_initial_prompt_session_switch.py``): the e2e harness's
runner is directly tunneled into the server and registers no *host*, and
the host filesystem endpoint has nothing to browse. The composer needs an
online host, an agent catalog, and (for the folder test) a directory
listing the headless harness can't produce, so ``/v1/hosts``,
``/v1/agents``, and ``/v1/hosts/{id}/filesystem`` are faked. The create
``POST /v1/sessions`` is intercepted too: rather than really launch a
session, the handler *captures the request body* — which is the thing
under test (that each selection reached the create call) — and returns a
real pre-seeded session id so the post-send navigation lands somewhere
real. ``/events`` is stubbed so the auto-sent first prompt never dispatches
a real LLM turn.

The async-in-a-fresh-thread shape is inherited from
``test_initial_prompt_session_switch`` for the same reason documented
there: once a pytest-playwright *sync* test has run in the session,
pytest-asyncio can't start a loop on the main thread, so each async body
runs in its own thread via :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

import pytest
from playwright.async_api import Request, Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import (
    commit_landing_workspace_picker,
    open_landing_workspace_picker,
)

# Stubbed host the composer auto-selects (the tunneled runner registers no
# host). Keyed identically in the recent-workspaces localStorage seed.
_HOST_ID = "host_e2e"
# Bare create endpoint: ``/v1/sessions`` with an optional query, but NOT
# ``/v1/sessions/{id}/...`` — so the GET conversation list and the
# agent-discovery scan pass through to the real server while only the POST
# create is faked.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# Any host filesystem listing, base (home) or a nested path. ``search``
# matches the substring, so it catches both ``…/filesystem`` and
# ``…/filesystem/home/e2e/projects``; it never matches the bare
# ``/v1/hosts`` list (no ``/filesystem`` segment).
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")
# The worktree-list endpoint the branch combobox queries for the picked repo.
# Distinct ``/worktrees`` segment, so it never collides with ``/filesystem``.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured
    and re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
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
    """Poll ``predicate`` on the event loop until true or timeout.

    :param predicate: Zero-arg callable returning truthy when satisfied.
    :param timeout_s: Max seconds to wait before failing the test.
    :raises AssertionError: If the predicate never becomes truthy.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _agents_body() -> str:
    """Stub body for ``GET /v1/agents``: a single Claude Code agent.

    ``claude-native-ui`` is the only built-in the picker needs here — its
    name is what gates the permission-mode UI (``isClaudeNativeAgent``) and,
    ranked first by display name, it auto-selects so no explicit pick is
    required. ``harness: null`` keeps the "needs setup" badge off regardless
    of the (stubbed) host's readiness map.
    """
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


def _codex_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Codex agent.

    ``codex-native-ui`` + ``harness: "codex-native"`` is what the frontend
    maps (via ``nativeCodingAgents``) to the ``approvalMode`` capability,
    gating the Codex approval-mode pill. Sole agent, so it auto-selects and
    no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                }
            ]
        }
    )


def _forked_codex_first_page_body() -> str:
    """First ``GET /v1/agents`` page with stale Codex forks only.

    Mirrors an older deployment where fork clones leaked into the built-in
    catalog before the server-side forward fix. The canonical Codex row is on
    page 2, so the picker must paginate before deduping native rows.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_fork_1",
                    "name": "codex-native-ui (fork ag_old1)",
                    "display_name": "Codex",
                    "description": "Stale Codex fork",
                    "harness": "codex-native",
                    "skills": [],
                },
                {
                    "id": "ag_codex_fork_2",
                    "name": "codex-native-ui (fork ag_old2)",
                    "display_name": "Codex",
                    "description": "Another stale Codex fork",
                    "harness": "codex-native",
                    "skills": [],
                },
            ],
            "has_more": True,
            "last_id": "ag_codex_fork_2",
        }
    )


def _canonical_codex_second_page_body() -> str:
    """Second ``GET /v1/agents`` page containing canonical Codex."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                }
            ],
            "has_more": False,
        }
    )


def _bundle_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the two harness-overridable bundle agents.

    Polly and Debby are multi-agent bundles, not native terminal wrappers, so
    their spec declares a brain harness (``harness: "claude-sdk"``) that lands
    them in ``BRAIN_HARNESS_LABELS``. That — and the fact that neither is named
    ``claude-native-ui`` — is what makes the config modal render the harness
    picker (an **Agent Harness** select) instead of Claude Code's permission
    select. Polly is
    ranked ahead of Debby by ``AGENT_DISPLAY_ORDER``, so it auto-selects and no
    explicit agent pick is needed. ``harness: null`` would suppress the section
    entirely, so it must be a real harness id here.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_polly_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Multi-agent coding",
                    "harness": "claude-sdk",
                    "skills": [],
                },
                {
                    "id": "ag_debby_e2e",
                    "name": "debby",
                    "display_name": "Debby",
                    "description": "Multi-agent debate",
                    "harness": "claude-sdk",
                    "skills": [],
                },
            ]
        }
    )


def _pi_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Pi agent.

    ``name: "pi-native-ui"`` + ``harness: "pi-native"`` is what the frontend
    maps (via ``nativeCodingAgents``) to the display label **"Pi"** and the
    pi-native wrapper labels. The wire ``display_name`` is deliberately set to
    the raw ``"pi-native-ui"`` to prove the picker derives "Pi" itself
    (``displayNameForAgent`` ignores the wire value) rather than echoing the
    server — the regression showed the raw "Pi-native-ui" here. Sole agent, so
    it auto-selects and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_pi_e2e",
                    "name": "pi-native-ui",
                    "display_name": "pi-native-ui",
                    "description": "Pi coding agent",
                    "harness": "pi-native",
                    "skills": [],
                }
            ]
        }
    )


def _antigravity_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Antigravity agent.

    ``name: "antigravity-native-ui"`` + ``harness: "antigravity-native"`` is what
    the frontend maps (via ``nativeCodingAgents``) to the display label
    **"Antigravity"** and the antigravity-native wrapper labels. The wire
    ``display_name`` is deliberately the raw ``"antigravity-native-ui"`` to prove
    the picker derives "Antigravity" itself (``nativeDisplayNameForAgent`` ignores
    the wire value) rather than echoing the server. Sole agent, so it auto-selects
    and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_antigravity_e2e",
                    "name": "antigravity-native-ui",
                    "display_name": "antigravity-native-ui",
                    "description": "Google's Gemini coding agent (agy CLI)",
                    "harness": "antigravity-native",
                    "skills": [],
                }
            ]
        }
    )


def _opencode_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native OpenCode agent.

    ``name: "opencode-native-ui"`` + ``harness: "opencode-native"`` is what the
    frontend maps (via ``nativeCodingAgents``) to the display label
    **"OpenCode"** and the opencode-native wrapper labels. As with the Pi stub,
    the wire ``display_name`` is deliberately the raw ``"opencode-native-ui"``
    to prove the picker derives "OpenCode" itself (the harness→display mapping
    wins) rather than echoing the server's raw value. Sole agent, so it
    auto-selects and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_opencode_e2e",
                    "name": "opencode-native-ui",
                    "display_name": "opencode-native-ui",
                    "description": "OpenCode coding agent",
                    "harness": "opencode-native",
                    "skills": [],
                }
            ]
        }
    )


def _kimi_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Kimi agent.

    ``name: "kimi-native-ui"`` + ``harness: "kimi-native"`` is what the frontend
    maps (via ``nativeCodingAgents``) to the display label **"Kimi"** and the
    kimi-native wrapper labels. The wire ``display_name`` is deliberately the raw
    ``"kimi-native-ui"`` to prove the picker derives "Kimi" itself
    (``nativeDisplayNameForAgent`` ignores the wire value) rather than echoing the
    server. Sole agent, so it auto-selects and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_kimi_native_e2e",
                    "name": "kimi-native-ui",
                    "display_name": "kimi-native-ui",
                    "description": "Moonshot's Kimi Code agent",
                    "harness": "kimi-native",
                    "skills": [],
                }
            ]
        }
    )


def _kimi_with_sdk_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Kimi agent AND the SDK kimi.

    The headless SDK ``kimi`` harness is kept (sub-agents use it) but is hidden
    from the new-session picker via ``NEW_SESSION_HIDDEN_AGENTS`` so there is one
    "Kimi" to pick — the native TUI agent (``kimi-native-ui``). Returning both
    here drives that dedup: the picker must offer only the native row and drop
    the SDK ``kimi`` row by name.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_kimi_native_e2e",
                    "name": "kimi-native-ui",
                    "display_name": "kimi-native-ui",
                    "description": "Moonshot's Kimi Code agent",
                    "harness": "kimi-native",
                    "skills": [],
                },
                {
                    # SDK kimi harness — present in the catalog, hidden from the
                    # picker by NEW_SESSION_HIDDEN_AGENTS (name == "kimi").
                    "id": "ag_kimi_sdk_e2e",
                    "name": "kimi",
                    "display_name": "Kimi",
                    "description": "Headless Kimi Code (SDK)",
                    "harness": "kimi",
                    "skills": [],
                },
            ]
        }
    )


def _hosts_body() -> str:
    """Stub body for ``GET /v1/hosts``: one online host the composer picks."""
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


# Two online hosts for the sticky-default test. The composer auto-selects the
# FIRST online host (alpha) when there's no stored pick; the test then picks
# beta and asserts it's restored after a reload.
_HOST_ALPHA = ("host_e2e_alpha", "e2e-host-alpha")
_HOST_BETA = ("host_e2e_beta", "e2e-host-beta")


def _two_hosts_body() -> str:
    """Stub body for ``GET /v1/hosts``: two online, user-connected hosts."""
    return json.dumps(
        {
            "hosts": [
                {"host_id": hid, "name": name, "owner": "e2e", "status": "online"}
                for hid, name in (_HOST_ALPHA, _HOST_BETA)
            ]
        }
    )


async def _register_common_routes(
    page,
    *,
    created_session_id: str,
    create_bodies: list[dict[str, Any]],
    agents_body: str | None = None,
) -> None:
    """Register the host/agent/create/events stubs shared by every test.

    :param page: The Playwright page to install routes on.
    :param created_session_id: Real pre-seeded session id the faked create
        returns, so the post-send navigation lands on a real page.
    :param create_bodies: Sink the create ``POST /v1/sessions`` body is
        appended to — the assertion target for each test.
    :param agents_body: Override for the ``GET /v1/agents`` stub body;
        defaults to the single Claude Code agent (:func:`_agents_body`).
    """
    resolved_agents_body = agents_body if agents_body is not None else _agents_body()

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=resolved_agents_body)

    async def handle_events(route: Route) -> None:
        # Swallow the auto-sent initial prompt so no real LLM turn runs.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        # Capture ONLY the composer's create POST (the thing under test) and
        # return a real session id so navigation lands somewhere real.
        # Everything else (GET conversation list, agent-discovery scan) goes
        # to the real server.
        if route.request.method == "POST":
            create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id}),
            )
        else:
            await route.continue_()

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)


async def _open_entry_models(page, agent_id: str) -> None:
    """Select a harness and open its primary model and effort picker."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(picker).to_have_attribute("aria-expanded", "true")
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        await page.get_by_test_id("new-chat-landing-harness-more").click()
    await (
        page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
        .get_by_text("Edit", exact=True)
        .click()
    )


async def _close_entry_models(page) -> None:
    """Dismiss the primary picker after immediate selection."""
    await page.keyboard.press("Escape")
    if (
        await page.get_by_test_id("new-chat-landing-agent-select").get_attribute("aria-expanded")
        == "true"
    ):
        await page.keyboard.press("Escape")


async def _expect_model_menu_without_advanced_settings(page) -> None:
    """Model/effort menus have no redundant Advanced settings row or divider."""
    model_menu = page.get_by_role("menu").filter(
        has=page.get_by_test_id("new-chat-landing-agent-models")
    )
    await expect(model_menu).to_be_visible()
    await expect(page.get_by_test_id("new-chat-landing-config-gear")).to_have_count(0)
    await expect(model_menu.locator(':scope > [role="separator"]:last-child')).to_have_count(0)


async def _open_entry_config(page, agent_id: str) -> None:
    """Select a configurable agent and open Edit > Advanced settings."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(picker).to_have_attribute("aria-expanded", "true")
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        await page.get_by_test_id("new-chat-landing-harness-more").click()
    await (
        page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
        .get_by_text("Edit", exact=True)
        .click()
    )
    await page.get_by_test_id("new-chat-landing-config-gear").click()


async def _save_config(page) -> None:
    """Commit the config modal's draft by clicking Save."""
    await page.get_by_test_id("new-chat-landing-config-save").click()


def test_start_session_select_permission_mode(seeded_session: tuple[str, str]) -> None:
    """A launched permission mode reaches create and seeds the next session.

    Selecting "Accept edits" in Claude Code's hand dropdown
    must (a) update the permission chip as immediate feedback and
    (b) reach ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--permission-mode", "acceptEdits"]``, then
    (c) remain selected when the user opens the next New Session screen.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_permission_mode(base_url, session_id))


async def _drive_permission_mode(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Hide agents left by other tests so Claude remains the selected harness.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory for the stubbed host so the
            # working-directory chip auto-fills and Send can enable without
            # touching the (host-less) file browser. Set before the SPA boots
            # so the landing composer reads it on mount.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Claude Code auto-selects; the hand menu offers all six permission modes.
            perm = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(perm).to_be_visible()
            await perm.click()
            perm_labels = (
                "Manual",
                "Auto",
                "Accept edits",
                "Plan",
                "Don't ask",
                "Bypass permissions",
            )
            for label in perm_labels:
                await expect(page.get_by_role("menuitem", name=label, exact=True)).to_be_visible()
            await page.get_by_test_id("new-chat-landing-permission-option-acceptEdits").click()
            await expect(perm).to_contain_text("Accept edits")

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_claude_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("terminal_launch_args") == ["--permission-mode", "acceptEdits"], body

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(perm).to_contain_text("Accept edits")
        finally:
            await browser.close()


def test_start_session_navigates_while_create_is_pending(seeded_session: tuple[str, str]) -> None:
    """Send immediately opens a temporary chat, then hydrates its real id.

    The create response is held so the test can verify the navigate-first
    window: the landing composer is already gone, the URL uses a client-only
    ``temp:`` id, and the optimistic prompt is visible in a read-only chat.
    Releasing the response must replace that temporary URL with the real id.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_send_busy_spinner(base_url, session_id))


async def _drive_send_busy_spinner(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            temp_scoped_requests: list[str] = []

            def capture_temp_scoped_request(request: Request) -> None:
                if re.search(r"/v1/sessions/temp(?::|%3A)", request.url, re.IGNORECASE):
                    temp_scoped_requests.append(request.url)

            page.on("request", capture_temp_scoped_request)
            # A gate the create handler awaits before responding, so the POST
            # stays pending long enough to observe the temporary chat.
            release_create = asyncio.Event()

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
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
                    # Hold the create open so the temp-id chat remains visible.
                    await release_create.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Keep the agent-discovery scan empty so only the stubbed Claude
            # agent feeds the picker (see _drive_permission_mode for why).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create is still in flight, but the landing screen is gone and
            # the optimistic prompt is already visible under a temporary URL.
            await _wait_until(lambda: len(create_bodies) == 1)
            await expect(page).to_have_url(
                re.compile(rf"{re.escape(base_url)}/c/temp:[0-9a-f]{{32}}")
            )
            assert "id" not in create_bodies[0]
            assert re.fullmatch(
                r"[0-9a-f]{32}",
                create_bodies[0]["labels"]["omnigent.client_create_token"],
            )
            await expect(page.get_by_test_id("new-chat-landing-input")).to_have_count(0)
            composer = page.get_by_role("textbox", name="Message the agent")
            await expect(composer).to_be_disabled()
            await expect(composer).to_have_attribute("placeholder", "Starting the session…")
            await expect(
                page.get_by_test_id("message-bubble").get_by_text("set up the project", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_role("navigation", name="Conversation").get_by_text(
                    "set up the project", exact=True
                )
            ).to_be_visible()
            header = page.locator("header.chat-header")
            for action_name in (
                "Add to project",
                "Agent tools and policies",
                "Chat view",
                "Terminal view",
                "Conversation actions",
                "Share session",
            ):
                await expect(
                    header.get_by_role("button", name=action_name, exact=True)
                ).to_be_disabled()
            await expect(
                header.get_by_role("button", name="Collapse right panel", exact=True)
            ).to_be_enabled()
            workspace = page.get_by_role("complementary", name="Workspace")
            await expect(workspace).to_be_visible()
            for tab_name in ("Files", "Changes", "GitHub", "Agents"):
                await expect(
                    workspace.get_by_role("tab", name=re.compile(tab_name))
                ).to_be_disabled()
            await expect(workspace.get_by_role("button", name="Full screen")).to_be_disabled()
            await expect(
                workspace.get_by_role("separator", name="Resize panel")
            ).to_have_attribute("aria-disabled", "true")
            await expect(workspace.get_by_text("Starting workspace…", exact=True)).to_be_visible()
            assert temp_scoped_requests == []

            # Release the create: the same chat hydrates onto the real id.
            release_create.set()
            await expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
            await expect(workspace.get_by_text("Starting workspace…", exact=True)).to_have_count(0)
            await expect(workspace.get_by_role("tab", name=re.compile("Agents"))).to_be_enabled()
        finally:
            await browser.close()


def test_start_session_ignores_uncorrelated_announcement_while_create_pending(
    seeded_session: tuple[str, str],
) -> None:
    """Only the exact create-token announcement resolves the temporary chat.

    An uncorrelated top-level row leaves the temporary route in place. A row
    carrying the POST's token resolves it while the HTTP response is pending.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_ignore_uncorrelated_announcement(base_url, session_id))


async def _drive_ignore_uncorrelated_announcement(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_seen = asyncio.Event()
            release_create = asyncio.Event()
            create_body: dict[str, Any] = {}
            sockets: list[Any] = []

            def handle_updates(ws: Any) -> None:
                # Mocked (never connected to the server), so this test owns
                # exactly what the page receives on the stream.
                sockets.append(ws)

            await page.route_web_socket(re.compile(r"/v1/sessions/updates"), handle_updates)

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_body.update(route.request.post_data_json)
                    create_seen.set()
                    await release_create.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _wait_until(lambda: len(sockets) == 1)

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            # The create is in flight and will stay that way.
            await _wait_until(create_seen.is_set)

            announced_id = session_id
            sockets[0].send(
                json.dumps(
                    {
                        "type": "changed",
                        "items": [
                            {
                                "id": announced_id,
                                "object": "conversation",
                                "agent_id": "ag_claude_e2e",
                                "host_id": _HOST_ID,
                                "parent_session_id": None,
                                "title": None,
                                "created_at": 1_800_000_000,
                                "updated_at": 1_800_000_000,
                                "labels": {},
                                "archived": False,
                            }
                        ],
                    }
                )
            )

            # Stay on the already-open temp chat; never guess that the pushed
            # row belongs to this request.
            await expect(page).to_have_url(
                re.compile(rf"{re.escape(base_url)}/c/temp:[0-9a-f]{{32}}")
            )
            await expect(page.get_by_test_id("new-chat-landing-input")).to_have_count(0)
            assert not release_create.is_set(), "the create must still be unanswered here"

            sockets[0].send(
                json.dumps(
                    {
                        "type": "changed",
                        "items": [
                            {
                                "id": announced_id,
                                "object": "conversation",
                                "parent_session_id": None,
                                "title": None,
                                "created_at": 1_800_000_000,
                                "updated_at": 1_800_000_000,
                                "labels": {
                                    "omnigent.client_create_token": create_body["labels"][
                                        "omnigent.client_create_token"
                                    ]
                                },
                                "archived": False,
                            }
                        ],
                    }
                )
            )
            await expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
            assert not release_create.is_set(), "the matching push must win before HTTP"

            release_create.set()
        finally:
            release_create.set()
            await browser.close()


def test_start_session_no_redirect_after_navigating_away(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A create that lands after the user left must not yank them into it.

    The create POST is slow (session bootstrap + runner launch), so the
    user often opens another session while it is still in flight. When
    the response finally arrives, following it would tear the user out
    of the session they deliberately navigated to — a hijacked view,
    seconds after the fact. The redirect belongs to the landing screen:
    it may only fire while the user is still sitting on it.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_no_redirect_after_navigating_away(base_url, session_a, session_b))


async def _drive_no_redirect_after_navigating_away(
    base_url: str, session_a: str, session_b: str
) -> None:
    """Async body of the late-create redirect test.

    :param base_url: Spawned server base URL.
    :param session_a: Pre-seeded session the faked create returns — the
        one the late redirect would wrongly jump to.
    :param session_b: Pre-seeded session the user opens mid-create, and
        must still be on once the create lands.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            # Holds the create POST open so the navigate-away happens
            # strictly inside the in-flight window, as it does for a real
            # multi-second create.
            release_create = asyncio.Event()
            # Set when the browser receives the create response, so the
            # settle window below starts only once the redirect could fire.
            create_answered = asyncio.Event()

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
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
                    await release_create.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_a}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Keep the agent-discovery scan empty so only the stubbed Claude
            # agent feeds the picker (see _drive_permission_mode for why).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            def note_create_response(response) -> None:
                if response.request.method == "POST" and _SESSIONS_RE.search(response.url):
                    create_answered.set()

            page.on("response", note_create_response)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            # The create is in flight and parked on the gate.
            await _wait_until(lambda: len(create_bodies) == 1)

            # Leave for another session via the sidebar — a client-side
            # navigation, so the create fetch keeps running (a reload would
            # abort it and the race would never happen).
            await page.locator(f'a[href="/c/{session_b}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}$"))

            # Let the create land. Waiting on the response (rather than a
            # bare sleep) keeps the assertion honest: a create that never
            # completed would fail here instead of passing vacuously.
            release_create.set()
            await asyncio.wait_for(create_answered.wait(), timeout=30.0)

            # Watch the URL for a while: the regression navigates within a
            # tick of the response, but `expect` would happily pass on the
            # pre-redirect URL, so sample instead of retrying-until-true.
            visited: list[str] = []
            for _ in range(50):
                url = page.url
                if not visited or visited[-1] != url:
                    visited.append(url)
                await asyncio.sleep(0.05)

            assert all(f"/c/{session_a}" not in url for url in visited), visited
            assert page.url.endswith(f"/c/{session_b}"), page.url
        finally:
            await browser.close()


def test_start_session_landing_clears_after_navigating_away(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Coming back to the landing screen mid-create must not restore the draft.

    The composer stashes its half-composed draft on unmount so a detour
    into another session doesn't lose a half-typed thought. But a draft
    that has already been *submitted* is spent: it belongs to the session
    now being created. Restoring it hands the user a composer pre-filled
    with the message they just sent — and a second Send would create a
    duplicate session.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_landing_clears_after_navigating_away(base_url, session_a, session_b))


async def _drive_landing_clears_after_navigating_away(
    base_url: str, session_a: str, session_b: str
) -> None:
    """Async body of the spent-draft test.

    :param base_url: Spawned server base URL.
    :param session_a: Pre-seeded session the faked create returns.
    :param session_b: Pre-seeded session the user detours through before
        returning to the landing screen.
    """
    message = "set up the project"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            release_create = asyncio.Event()
            create_answered = asyncio.Event()

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
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
                    await release_create.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_a}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Keep the agent-discovery scan empty so only the stubbed Claude
            # agent feeds the picker (see _drive_permission_mode for why).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            def note_create_response(response) -> None:
                if response.request.method == "POST" and _SESSIONS_RE.search(response.url):
                    create_answered.set()

            page.on("response", note_create_response)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            landing_input = page.get_by_test_id("new-chat-landing-input")
            await landing_input.wait_for(state="visible", timeout=30_000)

            await landing_input.fill(message)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)

            # Detour through another session and come straight back, all
            # while the create is still bootstrapping.
            await page.locator(f'a[href="/c/{session_b}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}$"))
            await page.get_by_test_id("new-chat-button").click()
            await landing_input.wait_for(state="visible", timeout=30_000)

            # A spent draft must not come back — not while the create is
            # still running, nor once it lands.
            await expect(landing_input).to_have_value("")
            release_create.set()
            await asyncio.wait_for(create_answered.wait(), timeout=30.0)
            for _ in range(20):
                assert await landing_input.input_value() == "", "submitted draft was restored"
                await asyncio.sleep(0.05)
        finally:
            await browser.close()


def test_start_session_remembers_last_picked_host(seeded_session: tuple[str, str]) -> None:
    """The host chip restores the last explicitly-picked host after a reload.

    With no stored pick the composer auto-selects the first online host
    (alpha). After the user picks a different host (beta), that choice must be
    persisted (``omnigent:last-host-choice`` in localStorage) and restored on
    the next visit — instead of reverting to the first-online default. This is
    the OSS mirror of the managed complaint where the picker always reverted to
    the "Databricks Sandbox" default.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_remembers_last_picked_host(base_url, session_id))


async def _drive_remembers_last_picked_host(base_url: str, session_id: str) -> None:
    alpha_id, alpha_name = _HOST_ALPHA
    beta_id, beta_name = _HOST_BETA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Override the single-host stub with the two-host body (registered
            # after the common routes so this handler wins).
            async def handle_two_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_two_hosts_body()
                )

            await page.route("**/v1/hosts", handle_two_hosts)

            # Neutralize agent discovery so a leaked native agent from another
            # test can't switch the picker mid-flow (see _drive_permission_mode).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed recents for both hosts so the working-directory chip auto-fills
            # and the composer never blocks on the (host-less) file browser.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{
                        "{alpha_id}": ["/work/repo"],
                        "{beta_id}": ["/work/repo"]
                    }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-host-chip")
            # No stored pick yet → auto-selects the first online host (alpha).
            await expect(chip).to_have_attribute("aria-label", re.compile(alpha_name))

            # Explicitly pick the second host.
            await chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{beta_id}").click()
            await expect(chip).to_have_attribute("aria-label", re.compile(beta_name))

            # Reload: a full document load resets the in-memory landing draft, so
            # the only thing that can restore the pick is the persisted
            # preference. The chip must come back on beta, NOT the alpha default.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(chip).to_have_attribute("aria-label", re.compile(beta_name))
        finally:
            await browser.close()


@pytest.mark.parametrize("managed", [False, True], ids=["connected-host", "managed-sandbox"])
def test_start_session_keeps_offline_host_selected(
    seeded_session: tuple[str, str], managed: bool
) -> None:
    """An offline last pick stays selected and blocks sending until an explicit switch."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_keeps_offline_host_selected(base_url, session_id, managed))


async def _drive_keeps_offline_host_selected(
    base_url: str, session_id: str, managed: bool
) -> None:
    alpha_id, _alpha_name = _HOST_ALPHA
    beta_id, beta_name = _HOST_BETA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        hosts = json.loads(_two_hosts_body())
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(json=hosts)

            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(json={"data": []})

            await page.route("**/v1/hosts", handle_hosts)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            if managed:
                await page.route("**/v1/info", handle_info)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{
                        "{alpha_id}": ["/work/repo"],
                        "{beta_id}": ["/work/repo"]
                    }})
                );"""
            )

            await page.goto(f"{base_url}/")
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{beta_id}").click()
            await expect(chip).to_have_attribute("aria-label", f"Host: {beta_name}, Online")

            # Reload without the in-memory draft after the chosen host disconnects.
            hosts["hosts"][1]["status"] = "offline"
            await page.reload()
            await expect(chip).to_have_attribute("aria-label", f"Host: {beta_name}, Offline")
            prompt = page.get_by_test_id("new-chat-landing-input")
            await prompt.fill("Work on this repository")
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_have_attribute(
                "aria-label", "Working directory: /work/repo"
            )
            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit).to_be_disabled()
            await prompt.press("Enter")
            await chip.click()
            await expect(
                page.get_by_test_id(f"new-chat-landing-host-{beta_id}")
            ).to_have_attribute("data-active", "true")
            assert create_bodies == []

            # Only an explicit destination change permits creation elsewhere.
            replacement = (
                "new-chat-landing-sandbox-option"
                if managed
                else f"new-chat-landing-host-{alpha_id}"
            )
            await page.get_by_test_id(replacement).click()
            await expect(submit).to_be_enabled()
            await submit.click()
            await _wait_until(lambda: len(create_bodies) == 1)
            if managed:
                assert create_bodies[0]["host_type"] == "managed"
                assert "host_id" not in create_bodies[0]
            else:
                assert create_bodies[0]["host_id"] == alpha_id
        finally:
            await browser.close()


def test_start_session_preserves_unavailable_remembered_host(
    seeded_session: tuple[str, str],
) -> None:
    """Mac-only host snapshots do not displace a remembered VM."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_preserves_unavailable_remembered_host(base_url, session_id))


async def _drive_preserves_unavailable_remembered_host(base_url: str, session_id: str) -> None:
    alpha_id, _alpha_name = _HOST_ALPHA
    beta_id, beta_name = _HOST_BETA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        route_state = {"include_remembered": False, "requests": 0}
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_hosts(route: Route) -> None:
                route_state["requests"] += 1
                body = (
                    _two_hosts_body()
                    if route_state["include_remembered"]
                    else json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": alpha_id,
                                    "name": _HOST_ALPHA[1],
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    )
                )
                await route.fulfill(status=200, content_type="application/json", body=body)

            # Registered after the common route so this stateful handler wins.
            await page.route("**/v1/hosts", handle_hosts)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:last-host-choice",
                    "{beta_id}"
                );"""
            )

            # ChatPage and Sidebar warm the shared host-query cache while the
            # stub exposes only the Mac. No landing draft exists on this path.
            await page.goto(f"{base_url}/c/{session_id}")
            await page.get_by_test_id("new-chat-button").wait_for(state="visible", timeout=30_000)
            await _wait_until(lambda: route_state["requests"] >= 2)

            # NewChat mounts with cached Mac-only data and completes another
            # Mac-only request. Neither snapshot may silently replace the saved
            # VM with the local default.
            requests_before_open = route_state["requests"]
            await page.get_by_test_id("new-chat-button").click()
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await _wait_until(lambda: route_state["requests"] > requests_before_open)
            await expect(chip).to_have_attribute("aria-label", re.compile("Choose host"))

            # A later host refresh reports the continuously preferred VM again.
            # The empty slot lets that saved choice heal automatically.
            route_state["include_remembered"] = True
            requests_before_focus = route_state["requests"]
            await page.evaluate("window.dispatchEvent(new Event('visibilitychange'))")
            await _wait_until(lambda: route_state["requests"] > requests_before_focus)
            await expect(chip).to_have_attribute("aria-label", re.compile(beta_name))
        finally:
            await browser.close()


def _managed_info_body() -> str:
    """Stub body for ``GET /v1/info``: a managed deployment offering a sandbox.

    ``managed_sandboxes_enabled: true`` + ``sandbox_provider: "lakebox"`` makes
    the picker offer (and default to) the "Databricks Sandbox" option, exactly
    the deployment shape behind the original complaint. Every field the SPA
    reads is supplied so the boot probe resolves to a fully-managed capability
    set rather than the fail-closed sentinel.
    """
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


def test_start_session_managed_remembers_host_over_sandbox_default(
    seeded_session: tuple[str, str],
) -> None:
    """In a managed deployment, a picked host survives reload — not the sandbox.

    This is the original complaint end-to-end: the managed picker defaults to
    "Databricks Sandbox", so a user who picks a connected host used to lose it
    on the next visit (the picker reverted to the sandbox default). With the
    last-host preference persisted, picking the host and reloading must restore
    the host, NOT snap back to the sandbox default.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_managed_remembers_host(base_url, session_id))


async def _drive_managed_remembers_host(base_url: str, session_id: str) -> None:
    host_id, host_name = _HOST_ALPHA
    # The loopback E2E server exposes exactly one online host, so the landing
    # footer intentionally replaces its raw hostname with an OS-aware local label.
    host_display_name = re.compile(r"This (?:Mac|Windows|Android|iPhone|iPad|machine)")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Managed capability probe: makes the sandbox the offered default.
            # `/v1/info` is fetched once per document load and module-cached, so
            # a full reload re-hits this stub and re-enters managed mode.
            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            await page.route("**/v1/info", handle_info)

            # One connected online host alongside the managed sandbox default.
            async def handle_one_host(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": host_id,
                                    "name": host_name,
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            await page.route("**/v1/hosts", handle_one_host)

            # Neutralize agent discovery so a leaked native agent from another
            # test can't switch the picker mid-flow (see _drive_permission_mode).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{host_id}": ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-host-chip")
            # Managed default with no stored pick: the sandbox, labeled by its
            # provider ("Databricks Sandbox").
            await expect(chip).to_have_attribute("aria-label", re.compile("Databricks Sandbox"))

            # Explicitly pick the connected host instead.
            await chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()
            await expect(chip).to_have_attribute("aria-label", re.compile(host_display_name))

            # Reload: the host must be restored, NOT reverted to the sandbox
            # default — the exact regression this change fixes.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(chip).to_have_attribute("aria-label", re.compile(host_display_name))
            await expect(chip).not_to_have_attribute(
                "aria-label", re.compile("Databricks Sandbox")
            )
        finally:
            await browser.close()


def _multi_provider_info_body() -> str:
    """``GET /v1/info`` for a server offering SEVERAL sandbox providers.

    ``sandbox_providers: ["modal", "e2b"]`` makes the picker render one row per
    provider; ``sandbox_provider: "modal"`` names the default (first
    launch-capable) so an older SPA still shows a single labeled option.
    """
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": True,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "modal",
            "sandbox_providers": ["modal", "e2b"],
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def test_start_session_managed_multi_provider_picks_and_persists(
    seeded_session: tuple[str, str],
) -> None:
    """A multi-provider server offers one row per provider, and the pick is sticky.

    Covers the user-facing behavior added for multiple sandbox providers: the
    picker shows a row per provider, choosing a non-default one (E2B) rides into
    the create ``POST`` as ``sandbox_provider`` and labels the chip, and the
    choice survives a reload (the composer reopens on the last provider used).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_managed_multi_provider(base_url, session_id))


async def _drive_managed_multi_provider(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_multi_provider_info_body(),
                )

            await page.route("**/v1/info", handle_info)

            # No connected hosts, so the sandbox is unambiguously the default.
            async def handle_no_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"hosts": []})
                )

            await page.route("**/v1/hosts", handle_no_hosts)

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-host-chip")
            # Default is the first launch-capable provider: "Modal Sandbox".
            await expect(chip).to_have_attribute("aria-label", re.compile("Modal Sandbox"))

            # Open the picker: one row per provider, the first keeping the
            # original testid and later rows a provider-scoped one.
            await chip.click()
            await expect(page.get_by_test_id("new-chat-landing-sandbox-option")).to_contain_text(
                "Modal Sandbox"
            )
            e2b_row = page.get_by_test_id("new-chat-landing-sandbox-option-e2b")
            await expect(e2b_row).to_contain_text("E2B Sandbox")

            # Pick the non-default provider; the chip reflects it.
            await e2b_row.click()
            await expect(chip).to_have_attribute("aria-label", re.compile("E2B Sandbox"))

            # A managed create carries the chosen provider in its body.
            await page.get_by_test_id("new-chat-landing-input").fill("audit the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_type"] == "managed", body
            assert body["sandbox_provider"] == "e2b", body
            assert "host_id" not in body, body

            # Reload: the pick is sticky — the composer reopens on E2B, not the
            # default Modal.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(chip).to_have_attribute("aria-label", re.compile("E2B Sandbox"))
        finally:
            await browser.close()


def test_start_session_managed_sandbox_appears_after_slow_info_probe(
    seeded_session: tuple[str, str],
) -> None:
    """A slow ``/v1/info`` still surfaces the managed-sandbox host option.

    The boot probe paints a fail-closed fallback (managed sandboxes OFF) if
    ``/v1/info`` hasn't answered within 1.5s, so the chat UI never hangs on a
    slow or proxied probe. The regression this guards: the SPA then *pinned*
    that fallback for the tab's lifetime, so on a slow-but-successful probe the
    "Databricks Sandbox" host option never appeared until a full reload — the
    managed complaint, where a proxied ``/v1/info`` behind a busy server
    routinely exceeds 1.5s. With the fix the boot code adopts the real
    ``/v1/info`` value when it finally lands.

    Here ``/v1/info`` is delayed past the 1.5s budget; once it resolves, the
    sandbox option must appear on its own (no reload). Pre-fix it never does.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_managed_sandbox_after_slow_info(base_url, session_id))


async def _drive_managed_sandbox_after_slow_info(base_url: str, session_id: str) -> None:
    host_id, host_name = _HOST_ALPHA
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Managed capability probe, but SLOW: held past the 1.5s boot budget
            # so the SPA first paints the fail-closed fallback (sandboxes off),
            # then must adopt this real value when it finally lands.
            async def handle_slow_info(route: Route) -> None:
                await asyncio.sleep(2.5)
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            await page.route("**/v1/info", handle_slow_info)

            # One connected online host alongside the managed sandbox option.
            async def handle_one_host(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": host_id,
                                    "name": host_name,
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            await page.route("**/v1/hosts", handle_one_host)

            # Neutralize agent discovery so a leaked native agent from another
            # test can't switch the picker mid-flow (see _drive_permission_mode).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{host_id}": ["/work/repo"] }})
                );"""
            )

            # Load and wait for the slow probe to actually answer (~2.5s). The
            # landing composer paints earlier, at the 1.5s fallback; the real
            # /v1/info lands after, and the fix re-renders with it.
            async with page.expect_response(lambda r: "/v1/info" in r.url):
                await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the host picker: the "Databricks Sandbox" option must be
            # present, proving the SPA adopted the late /v1/info rather than
            # staying pinned to the fail-closed fallback (where it never appears).
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            sandbox_option = page.get_by_test_id("new-chat-landing-sandbox-option")
            await expect(sandbox_option).to_be_visible(timeout=15_000)
            await expect(sandbox_option).to_contain_text("Databricks Sandbox")
        finally:
            await browser.close()


def test_start_session_select_model_and_effort(seeded_session: tuple[str, str]) -> None:
    """Picking a model + reasoning effort rides along to the create call.

    For the Claude-native agent the Edit menu shows model/effort
    choices that start with NOTHING selected — no model/effort default is
    forced, so an untouched picker omits the override and Claude Code keeps its
    own configured model. Explicitly selecting "Opus" and "High" must (a) update
    those selects as immediate feedback and (b) reach ``POST /v1/sessions`` as
    ``model_override: "opus"`` + ``reasoning_effort: "high"`` (the runner reads
    them as ``--model`` / ``--effort`` at terminal launch).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_model_effort(base_url, session_id))


async def _drive_model_effort(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Neutralize agent discovery so ONLY the stubbed Claude agent feeds
            # the picker (see _drive_permission_mode for the full rationale): a
            # leaked native agent auto-selecting ahead of Claude would open the
            # wrong agent's config modal. Registered after _register_common_routes
            # so it wins the kind=any scan.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # This isolated host fixture has no live harness bridge, so provide
            # the catalog the new-session picker now resolves through the host.
            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "models": [
                                {"id": "opus", "displayName": "Opus 4.8"},
                                {"id": "sonnet", "displayName": "Sonnet 4.6"},
                                {"id": "haiku", "displayName": "Haiku 4.5"},
                            ]
                        }
                    ),
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                handle_model_options,
            )

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # No override is forced until the user selects a model or effort.
            await _open_entry_models(page, "ag_claude_e2e")
            await _expect_model_menu_without_advanced_settings(page)
            model = page.locator(
                '[data-testid^="new-chat-landing-agent-model-"][aria-checked="true"]'
            )
            effort = page.locator(
                '[data-testid^="new-chat-landing-agent-effort-"][aria-checked="true"]'
            )
            await expect(model).to_contain_text("Harness default")
            await expect(effort).to_contain_text("Default")

            # Model and effort picks commit immediately using the live host catalog.
            await page.get_by_role("menuitemcheckbox", name="Opus 4.8", exact=True).click()
            await expect(model).to_contain_text("Opus 4.8")
            await page.get_by_role("menuitemcheckbox", name="High", exact=True).click()
            await expect(effort).to_contain_text("High")
            await _close_entry_models(page)

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_claude_e2e", body
            assert body.get("model_override") == "opus", body
            assert body.get("reasoning_effort") == "high", body

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_entry_models(page, "ag_claude_e2e")
            await expect(
                page.locator('[data-testid^="new-chat-landing-agent-model-"][aria-checked="true"]')
            ).to_contain_text("Opus 4.8")
            await expect(
                page.locator(
                    '[data-testid^="new-chat-landing-agent-effort-"][aria-checked="true"]'
                )
            ).to_contain_text("High")
        finally:
            await browser.close()


def test_start_session_select_codex_model(seeded_session: tuple[str, str]) -> None:
    """The host-resolved Codex model reaches the create request."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_model(base_url, session_id))


async def _drive_codex_model(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "models": [
                                {
                                    "id": "gpt-live-default",
                                    "displayName": "GPT Live Default",
                                    "isDefault": True,
                                },
                                {"id": "gpt-live-fast", "displayName": "GPT Live Fast"},
                            ]
                        }
                    ),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_model_options,
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_entry_models(page, "ag_codex_e2e")
            model = page.locator(
                '[data-testid^="new-chat-landing-agent-model-"][aria-checked="true"]'
            )
            # The Default row names the catalog's default by its DISPLAY name —
            # the same shared labeling the in-session gear uses.
            await expect(model).to_contain_text("GPT Live Default")
            # Codex options render decorated display names (same as claude),
            # so pick by the display name; the create still sends the id.
            await page.get_by_role("menuitemcheckbox", name="GPT Live Fast", exact=True).click()
            await _close_entry_models(page)

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            assert body.get("model_override") == "gpt-live-fast", body
            assert body.get("reasoning_effort") is None, body
        finally:
            await browser.close()


def test_start_session_select_approval_mode(seeded_session: tuple[str, str]) -> None:
    """A safer approval preset clears bypass and reaches the create call.

    Selecting "Full access" in the composer's permissions dropdown must clear
    a previous bypass choice and reach
    ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--sandbox", "danger-full-access",
    "--ask-for-approval", "never"]``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_approval_mode(base_url, session_id))


def test_start_session_agent_picker_paginates_and_dedupes_native_forks(
    seeded_session: tuple[str, str],
) -> None:
    """The new-session picker recovers canonical Codex from page 2.

    Older servers leaked fork clones into ``GET /v1/agents``. New servers stop
    creating those rows, but existing databases can still have enough stale
    forked native agents to push canonical built-ins off page 1. The picker must
    follow pagination and then collapse all ``codex-native`` rows to the
    canonical ``codex-native-ui`` row, so users see one top-level Codex choice.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_agent_picker_pagination_dedupe(base_url, session_id))


async def _drive_agent_picker_pagination_dedupe(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_forked_codex_first_page_body(),
            )

            async def handle_agents_page_2(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_canonical_codex_second_page_body(),
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route("**/v1/agents?after=ag_codex_fork_2", handle_agents_page_2)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(picker).to_have_attribute("aria-label", re.compile("Codex"))
            await picker.click()

            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e")
            ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_codex_fork_1")
            ).to_have_count(0)
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_codex_fork_2")
            ).to_have_count(0)

            await page.keyboard.press("Escape")
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
        finally:
            await browser.close()


async def _drive_approval_mode(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed Codex agent
            # feeds the picker.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Codex auto-selects; permissions are changed directly in the hand menu.
            approval = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(approval).to_be_visible()
            await approval.click()
            for label in ("Default", "Full access", "Read only", "Bypass approvals & sandbox"):
                await expect(page.get_by_role("menuitem", name=label, exact=True)).to_be_visible()
            await page.get_by_test_id("new-chat-landing-permission-option-bypass").click()
            await expect(approval).to_contain_text("Bypass approvals & sandbox")
            await expect(
                page.get_by_test_id("new-chat-landing-permission-menu")
            ).not_to_be_visible()
            await approval.click()
            await page.get_by_role("menuitem", name="Full access", exact=True).click()
            await expect(approval).to_contain_text("Full access")

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("terminal_launch_args") == [
                "--sandbox",
                "danger-full-access",
                "--ask-for-approval",
                "never",
            ], body
            assert "omnigent.codex_native.bypass_sandbox" not in (body.get("labels") or {}), body
        finally:
            await browser.close()


def test_start_session_bypass_sandbox(seeded_session: tuple[str, str]) -> None:
    """Codex full-bypass reaches create and seeds the next session.

    Bypass is the most-permissive option in the composer's permissions
    dropdown — Codex's ``--dangerously-bypass-approvals-and-sandbox`` stance.
    It reads back like Claude's "Bypass permissions": a plain dropdown pick with
    no warning banner. When armed, the create ``POST /v1/sessions`` must carry
    the ``omnigent.codex_native.bypass_sandbox: "1"`` conversation label so the
    runner launches Codex with the bypass flag. After returning to New Session,
    the same dropdown must still show bypass rather than resetting to Default.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_bypass_sandbox(base_url, session_id))


async def _drive_bypass_sandbox(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed Codex agent
            # feeds the picker.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Codex's Edit menu contains models and effort, without a redundant footer.
            await _open_entry_models(page, "ag_codex_e2e")
            await _expect_model_menu_without_advanced_settings(page)
            await _close_entry_models(page)

            approval = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(approval).to_contain_text("Default")
            await approval.click()
            bypass = page.get_by_test_id("new-chat-landing-permission-option-bypass")
            await expect(bypass).to_have_text("Bypass approvals & sandbox")
            await bypass.click()
            await expect(approval).to_contain_text("Bypass approvals & sandbox")
            await expect(page.get_by_test_id("new-chat-landing-permission-menu")).to_have_count(0)

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The dangerous opt-in rides along as the canonical conversation
            # label alongside the codex-native wrapper labels.
            labels = body.get("labels") or {}
            assert labels.get("omnigent.codex_native.bypass_sandbox") == "1", body

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(approval).to_contain_text("Bypass approvals & sandbox")
            await approval.click()
            await expect(bypass).to_be_visible()
        finally:
            await browser.close()


def test_start_session_select_harness(seeded_session: tuple[str, str]) -> None:
    """For a bundle agent (Polly/Debby), the composer offers an agent-harness pick.

    Unlike Claude Code — whose modal shows permission/model knobs — Polly and
    Debby declare a brain harness, so their config modal renders an "Agent
    Harness" select. Selecting a dynamically registered community harness
    must (a) show the label from ``/v1/harnesses`` and (b) reach
    ``POST /v1/sessions`` as ``harness_override: "community-brain"``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_select_harness(base_url, session_id))


async def _drive_select_harness(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_bundle_agents_body(),
            )

            async def handle_harness_catalog(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {"data": [{"id": "community-brain", "label": "Community Brain"}]}
                    ),
                )

            await page.route("**/v1/harnesses", handle_harness_catalog)

            # Neutralize agent discovery so only the stubbed bundle agents
            # (Polly/Debby) feed the picker. The landing picker merges
            # `/v1/agents` with agents found by scanning the caller's sessions
            # (`/v1/sessions?kind=any`); on the shared e2e_ui server, a native
            # fork another test left behind sorts ahead of bundle agents and
            # auto-selects, so the composer would show a permission-mode pill
            # (or nothing) instead of Polly's harness picker. Registered after
            # _register_common_routes so it wins the kind=any scan.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so the working-directory chip
            # auto-fills and Send can enable without touching the file browser.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Polly auto-selects (ranked ahead of Debby); its brain-harness
            # override select lives in the gear-icon config modal.
            await _open_entry_config(page, "ag_polly_e2e")
            await page.get_by_test_id("new-chat-landing-config-harness").click()
            # The built-in brain harnesses render as options, in registry order
            # (openai-agents is intentionally not offered in the picker).
            for harness in ("claude-sdk", "codex", "pi"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-harness-{harness}")
                ).to_be_visible()
            # Dynamic harness labels from `/v1/harnesses` extend the built-in
            # fallback catalog in the user-visible picker.
            community_harness = page.get_by_test_id("new-chat-landing-harness-community-brain")
            await expect(community_harness).to_be_visible()
            await expect(community_harness).to_contain_text("Community Brain")
            # Picking a harness updates the select; Save commits the override
            # (the agent chip keeps the bare agent label "Polly").
            await community_harness.click()
            await _save_config(page)

            await page.get_by_test_id("new-chat-landing-input").fill("debate the design")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_polly_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("harness_override") == "community-brain", body
        finally:
            await browser.close()


def test_start_session_pi_native_picker_and_wrapper_labels(
    seeded_session: tuple[str, str],
) -> None:
    """Native Pi: the picker shows "Pi" and create carries terminal-first labels.

    Covers the user-facing Pi native-agent flow this PR adds:

    1. **Picker label/icon** — the agent chip renders the harness-derived
       display label **"Pi"** (via ``nativeCodingAgents``), NOT the raw agent
       name ``"pi-native-ui"`` the server sends. (The pre-fix bug surfaced the
       raw name capitalized as "Pi-native-ui".)
    2. **Session-creation wrapper labels** — selecting Pi and sending must POST
       ``/v1/sessions`` with the terminal-first wrapper labels
       (``omnigent.ui: terminal`` + ``omnigent.wrapper: pi-native-ui``) that
       make the runner launch the Pi TUI and the web UI render the
       Chat/Terminal view.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_pi_native_start(base_url, session_id))


async def _drive_pi_native_start(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_pi_native_agents_body(),
            )

            # Neutralize agent discovery so the picker shows ONLY the stubbed
            # built-in Pi. The landing picker merges `/v1/agents` with agents
            # found by scanning the caller's sessions (`/v1/sessions?kind=any`);
            # on the shared e2e_ui server, sessions other tests left behind
            # (e.g. a claude-native fork) would otherwise leak in and — ranking
            # ahead of Pi — auto-select, so the chip would read "Claude Code".
            # Registered after _register_common_routes so it wins for the
            # kind=any scan; the bare POST /v1/sessions create still falls
            # through to the capturing handler.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so the working-directory chip
            # auto-fills and Send can enable without touching the file browser.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Pi auto-selects (sole agent). The chip shows the derived label
            # "Pi" — and crucially NOT "...native...": the regression rendered
            # the raw agent name "Pi-native-ui" when the harness→display
            # mapping was missing.
            agent_chip = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(agent_chip).to_have_attribute("aria-label", re.compile("Pi"))
            await expect(agent_chip).not_to_have_attribute("aria-label", re.compile("native"))

            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_pi_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The terminal-first wrapper labels are the contract that drives the
            # runner-owned Pi TUI and the web UI's Chat/Terminal view.
            assert body.get("labels") == {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "pi-native-ui",
                "omnigent.client_create_token": body["labels"]["omnigent.client_create_token"],
            }, body
            assert re.fullmatch(r"[0-9a-f]{32}", body["labels"]["omnigent.client_create_token"])
        finally:
            await browser.close()


def test_start_session_antigravity_native_picker_and_wrapper_labels(
    seeded_session: tuple[str, str],
) -> None:
    """Native Antigravity: the picker shows "Antigravity" and create carries terminal labels.

    Covers the user-facing Antigravity native-agent flow this PR adds:

    1. **Picker label/icon** — the agent chip renders the harness-derived display
       label **"Antigravity"** (via ``nativeCodingAgents``), NOT the raw agent name
       ``"antigravity-native-ui"`` the server sends.
    2. **Session-creation wrapper labels** — selecting Antigravity and sending must
       POST ``/v1/sessions`` with the terminal-first wrapper labels
       (``omnigent.ui: terminal`` + ``omnigent.wrapper: antigravity-native-ui``)
       that make the runner launch the agy TUI and the web UI render the
       Chat/Terminal view.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_antigravity_native_start(base_url, session_id))


async def _drive_antigravity_native_start(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_antigravity_native_agents_body(),
            )

            # Neutralize agent discovery so the picker shows ONLY the stubbed
            # built-in Antigravity (sessions other tests left behind on the shared
            # e2e_ui server would otherwise leak in and, ranking ahead, auto-select).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Antigravity auto-selects (sole agent). The chip shows the derived
            # label "Antigravity" — and NOT "...native...": the raw agent name
            # would surface "antigravity-native-ui" without the harness→display map.
            agent_chip = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(agent_chip).to_have_attribute("aria-label", re.compile("Antigravity"))
            await expect(agent_chip).not_to_have_attribute("aria-label", re.compile("native"))

            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_antigravity_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The terminal-first wrapper labels drive the runner-owned agy TUI and
            # the web UI's Chat/Terminal view.
            assert body.get("labels") == {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "antigravity-native-ui",
                "omnigent.client_create_token": body["labels"]["omnigent.client_create_token"],
            }, body
            assert re.fullmatch(r"[0-9a-f]{32}", body["labels"]["omnigent.client_create_token"])
        finally:
            await browser.close()


def test_start_session_opencode_native_picker_and_wrapper_labels(
    seeded_session: tuple[str, str],
) -> None:
    """Native OpenCode: the picker shows "OpenCode" and create carries labels.

    Covers the user-facing OpenCode native-agent flow this PR adds (mirrors
    the Codex / Pi native rows):

    1. **Picker label/icon** — the agent chip renders the harness-derived
       display label **"OpenCode"** (via ``nativeCodingAgents``), NOT the raw
       agent name ``"opencode-native-ui"`` the server sends.
    2. **Session-creation wrapper labels** — selecting OpenCode and sending
       must POST ``/v1/sessions`` with the terminal-first wrapper labels
       (``omnigent.ui: terminal`` + ``omnigent.wrapper: opencode-native-ui``)
       that make the runner launch the OpenCode TUI and the web UI render the
       Chat/Terminal view.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_opencode_native_start(base_url, session_id))


async def _drive_opencode_native_start(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_opencode_native_agents_body(),
            )

            # Neutralize agent discovery so the picker shows ONLY the stubbed
            # built-in OpenCode. The landing picker merges `/v1/agents` with
            # agents found by scanning the caller's sessions
            # (`/v1/sessions?kind=any`); on the shared e2e_ui server, sessions
            # other tests left behind (e.g. a claude-native fork) would
            # otherwise leak in and — ranking ahead of OpenCode — auto-select,
            # so the chip would read the wrong label. Registered after
            # _register_common_routes so it wins for the kind=any scan; the
            # bare POST /v1/sessions create still falls through to the
            # capturing handler.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so the working-directory chip
            # auto-fills and Send can enable without touching the file browser.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # OpenCode auto-selects (sole agent). The chip shows the derived
            # label "OpenCode" — and crucially NOT "...native...": the raw
            # agent name "opencode-native-ui" must never surface.
            agent_chip = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(agent_chip).to_have_attribute("aria-label", re.compile("OpenCode"))
            await expect(agent_chip).not_to_have_attribute("aria-label", re.compile("native"))

            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_opencode_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The terminal-first wrapper labels are the contract that drives the
            # runner-owned OpenCode TUI and the web UI's Chat/Terminal view.
            assert body.get("labels") == {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "opencode-native-ui",
                "omnigent.client_create_token": body["labels"]["omnigent.client_create_token"],
            }, body
            assert re.fullmatch(r"[0-9a-f]{32}", body["labels"]["omnigent.client_create_token"])
        finally:
            await browser.close()


def test_start_session_kimi_native_picker_and_wrapper_labels(
    seeded_session: tuple[str, str],
) -> None:
    """Native Kimi: the picker shows "Kimi" and create carries terminal labels.

    Covers the user-facing Kimi native-agent flow this PR adds (mirrors the
    Codex / Pi / OpenCode native rows):

    1. **Picker label/icon** — the agent chip renders the harness-derived
       display label **"Kimi"** (via ``nativeCodingAgents``), NOT the raw agent
       name ``"kimi-native-ui"`` the server sends.
    2. **Session-creation wrapper labels** — selecting Kimi and sending must POST
       ``/v1/sessions`` with the terminal-first wrapper labels
       (``omnigent.ui: terminal`` + ``omnigent.wrapper: kimi-native-ui``) that
       make the runner launch the Kimi TUI and the web UI render the
       Chat/Terminal view.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_kimi_native_start(base_url, session_id))


async def _drive_kimi_native_start(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_kimi_native_agents_body(),
            )

            # Neutralize agent discovery so the picker shows ONLY the stubbed
            # built-in Kimi. The landing picker merges `/v1/agents` with agents
            # found by scanning the caller's sessions (`/v1/sessions?kind=any`);
            # on the shared e2e_ui server, sessions other tests left behind would
            # otherwise leak in and — ranking ahead of Kimi — auto-select.
            # Registered after _register_common_routes so it wins the kind=any
            # scan; the bare POST /v1/sessions create still falls through.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Kimi auto-selects (sole agent). The chip shows the derived label
            # "Kimi" — and crucially NOT "...native...": the raw agent name
            # "kimi-native-ui" must never surface in the picker.
            agent_chip = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(agent_chip).to_have_attribute("aria-label", re.compile("Kimi"))
            await expect(agent_chip).not_to_have_attribute("aria-label", re.compile("native"))

            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_kimi_native_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The terminal-first wrapper labels are the contract that drives the
            # runner-owned Kimi TUI and the web UI's Chat/Terminal view.
            assert body.get("labels") == {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "kimi-native-ui",
                "omnigent.client_create_token": body["labels"]["omnigent.client_create_token"],
            }, body
            assert re.fullmatch(r"[0-9a-f]{32}", body["labels"]["omnigent.client_create_token"])
        finally:
            await browser.close()


def test_start_session_picker_hides_sdk_kimi(
    seeded_session: tuple[str, str],
) -> None:
    """The new-session picker offers only the native Kimi, not the SDK kimi.

    The headless SDK ``kimi`` harness is retained for sub-agents but hidden from
    the landing picker (``NEW_SESSION_HIDDEN_AGENTS``) so there is exactly one
    "Kimi" to start — the native TUI agent (``kimi-native-ui``), which opens in
    the user's workspace. This drives that dedup against the rendered picker: with
    both rows in the catalog, only ``kimi-native-ui`` is offered and the SDK
    ``kimi`` row is dropped (the regression surfaced two "Kimi" entries, and
    picking the SDK one launched headless in a /tmp spec dir).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_kimi_picker_dedup(base_url, session_id))


async def _drive_kimi_picker_dedup(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_kimi_with_sdk_agents_body(),
            )

            # Only the built-in catalog feeds the picker for this test.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent picker dropdown.
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # The native Kimi row is offered...
            await page.get_by_test_id("new-chat-landing-harness-more").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_kimi_native_e2e")
            ).to_be_visible(timeout=30_000)
            # ...and the SDK kimi row is dropped (hidden by NEW_SESSION_HIDDEN_AGENTS).
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_kimi_sdk_e2e")
            ).to_have_count(0)
            # Two menu items total: the one native Kimi + the "Create custom
            # agent" action — no second "Kimi" sneaks in via the SDK row.
            await expect(page.locator("[data-harness-menu-row]")).to_have_count(1)
        finally:
            await browser.close()


def test_start_session_select_folder(seeded_session: tuple[str, str]) -> None:
    """Browsing into a folder sets the new session's working directory.

    The composer seeds the working directory to the host's home, then the
    user opens the file browser and navigates into a subfolder. The chip
    label must follow the navigation and the picked path must reach
    ``POST /v1/sessions`` as ``workspace``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_folder_selection(base_url, session_id))


async def _drive_folder_selection(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_filesystem(route: Route) -> None:
                # Home ("/home/e2e") and the bare home listing both show the
                # two top-level folders; "/home/e2e/projects" shows its child.
                # Absolute paths let the picker pass entries straight through.
                path_part = route.request.url.split("?")[0]
                if path_part.endswith("/filesystem/home/e2e/projects"):
                    entries = [
                        {
                            "name": "src",
                            "path": "/home/e2e/projects/src",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        }
                    ]
                else:
                    entries = [
                        {
                            "name": "projects",
                            "path": "/home/e2e/projects",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        },
                        {
                            "name": "repo",
                            "path": "/home/e2e/repo",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        },
                    ]
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"object": "list", "data": entries, "has_more": False}),
                )

            # Registered last so it wins over the broader **/v1/hosts glob for
            # filesystem URLs.
            await page.route(_FILESYSTEM_RE, handle_filesystem)

            # No recent seed here: with no recent, the composer derives the
            # host's home from the filesystem listing and seeds the working
            # directory to it, so the chip starts at "e2e" (basename of
            # /home/e2e) and the test changes it by browsing.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Working directory auto-fills to the derived home.
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "e2e"
            )

            # Open the file browser and navigate into the "projects" folder.
            await open_landing_workspace_picker(page)
            await page.get_by_test_id("workspace-picker-entry-projects").click()
            # The child listing confirms we navigated in.
            await expect(page.get_by_test_id("workspace-picker-entry-src")).to_be_visible()
            await commit_landing_workspace_picker(page)

            # The explicit Select action commits the navigated folder.
            await page.get_by_test_id("new-chat-landing-input").fill("explore the project")
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "projects"
            )

            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/home/e2e/projects", body
        finally:
            await browser.close()


def test_start_session_create_folder(seeded_session: tuple[str, str]) -> None:
    """Creating a folder in the picker makes it the session's workspace.

    The user opens the file browser, navigates into a folder, clicks "New
    folder", names it, and confirms. The picker POSTs
    ``/v1/hosts/{id}/directories``, drops into the freshly created
    directory, and the working-directory chip follows. On Send the new
    folder's path must reach ``POST /v1/sessions`` as ``workspace`` — i.e.
    the agent's working directory is the folder the user just made.

    Like the other tests here, the tunneled runner registers no host, so
    ``/v1/hosts/{id}/directories`` is faked: the handler captures the
    requested path and echoes it back as the created absolute path (the
    real ``os.makedirs`` never runs in this harness).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_create_folder(base_url, session_id))


async def _drive_create_folder(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_filesystem(route: Route) -> None:
                # Home shows "projects"; "/home/e2e/projects" shows its child;
                # the freshly created "/home/e2e/projects/new-app" lists empty.
                # Deepest match first so the new folder isn't shadowed.
                path_part = route.request.url.split("?")[0]
                if path_part.endswith("/filesystem/home/e2e/projects/new-app"):
                    entries: list[dict[str, Any]] = []
                elif path_part.endswith("/filesystem/home/e2e/projects"):
                    entries = [
                        {
                            "name": "src",
                            "path": "/home/e2e/projects/src",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        }
                    ]
                else:
                    entries = [
                        {
                            "name": "projects",
                            "path": "/home/e2e/projects",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        }
                    ]
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"object": "list", "data": entries, "has_more": False}),
                )

            create_dir_bodies: list[dict[str, Any]] = []

            async def handle_create_dir(route: Route) -> None:
                # Mirror the server's success shape: echo the requested path
                # back as the created absolute path. Capturing the body lets
                # the test assert the picker sent the joined parent + name.
                body = json.loads(route.request.post_data or "{}")
                create_dir_bodies.append(body)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"object": "directory", "path": body["path"]}),
                )

            # Registered after the broad globs so these win for their URLs.
            await page.route(_FILESYSTEM_RE, handle_filesystem)
            await page.route(re.compile(r"/v1/hosts/[^/]+/directories$"), handle_create_dir)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "e2e"
            )

            # Open the picker and navigate into "projects" so the new folder
            # has a resolved absolute parent to be created under.
            await open_landing_workspace_picker(page)
            await page.get_by_test_id("workspace-picker-entry-projects").click()
            await expect(page.get_by_test_id("workspace-picker-entry-src")).to_be_visible()

            # Create a new folder under /home/e2e/projects.
            await page.get_by_test_id("workspace-picker-new-folder").click()
            await page.get_by_test_id("workspace-picker-new-folder-input").fill("new-app")
            await page.get_by_test_id("workspace-picker-new-folder-create").click()

            # The picker POSTs the joined path and drops into the new folder.
            await _wait_until(lambda: len(create_dir_bodies) == 1)
            assert create_dir_bodies[0]["path"] == "/home/e2e/projects/new-app", create_dir_bodies
            await commit_landing_workspace_picker(page)

            # The explicit Select action commits the folder we just created.
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "new-app"
            )

            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/home/e2e/projects/new-app", body
        finally:
            await browser.close()


def test_start_session_type_tilde_path(seeded_session: tuple[str, str]) -> None:
    """Typing a ``~/…`` path in the workspace picker navigates there.

    The picker opens at the composer's seeded working directory — an
    *absolute* path (the host's home). Because it never lands on the empty
    "home" view, it used to never resolve the host's home dir, so a typed
    ``~/Desktop`` couldn't be expanded and the path bar silently snapped back
    to the previous directory (the reported bug: ``~/…`` "just reverts").

    This drives that gesture end to end: open the browser (seeded at
    ``/home/e2e``), type ``~/Desktop`` in the path bar, press Enter, and assert
    the listing navigates into ``/home/e2e/Desktop`` and the picked path reaches
    ``POST /v1/sessions`` as ``workspace``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_type_tilde_path(base_url, session_id))


async def _drive_type_tilde_path(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_filesystem(route: Route) -> None:
                # Home ("/home/e2e", listed as "~" and as the absolute path)
                # shows "Desktop"; "/home/e2e/Desktop" shows its child. The
                # entries carry absolute paths so home resolves to "/home/e2e"
                # from any listing's parent.
                path_part = route.request.url.split("?")[0]
                if path_part.endswith("/filesystem/home/e2e/Desktop"):
                    entries = [
                        {
                            "name": "notes",
                            "path": "/home/e2e/Desktop/notes",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        }
                    ]
                else:
                    entries = [
                        {
                            "name": "Desktop",
                            "path": "/home/e2e/Desktop",
                            "type": "directory",
                            "bytes": None,
                            "modified_at": 0,
                        }
                    ]
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"object": "list", "data": entries, "has_more": False}),
                )

            # Registered last so it wins over the broader **/v1/hosts glob.
            await page.route(_FILESYSTEM_RE, handle_filesystem)

            # No recent seed: the composer derives home from the listing and
            # seeds the working directory to it, so the picker opens at the
            # absolute "/home/e2e" — never the empty home view.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "e2e"
            )

            # Open the browser and type a ~-relative path, then commit with Enter.
            await open_landing_workspace_picker(page)
            path_input = page.get_by_test_id("workspace-picker-path-input")
            await path_input.fill("~/Desktop")
            await path_input.press("Enter")

            # The listing navigated into the tilde-expanded directory — its
            # child confirms we're inside /home/e2e/Desktop (pre-fix the bar
            # reverted to /home/e2e and this row never appeared).
            await expect(page.get_by_test_id("workspace-picker-entry-notes")).to_be_visible()
            await commit_landing_workspace_picker(page)

            # The explicit Select action commits the tilde-expanded directory.
            await page.get_by_test_id("new-chat-landing-input").fill("explore the desktop")
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "Desktop"
            )

            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/home/e2e/Desktop", body
        finally:
            await browser.close()


def test_start_session_type_nonexistent_path(seeded_session: tuple[str, str]) -> None:
    """Typing a path that doesn't exist shows an error, not the old listing.

    Previously a typed path the host 404s on left the picker showing the
    *previous* valid directory's contents: the filesystem query kept the old
    listing on screen as placeholder data while it burned through its default
    retries on the deterministic 404, so for several seconds nothing signalled
    that the path was bad (the reported bug). With the 404 no longer retried,
    the picker drops the stale rows immediately and surfaces a "doesn't exist"
    message.

    This drives that end to end: open the browser (seeded at ``/home/e2e``,
    showing ``Desktop``), type a nonexistent ``~/does-not-exist``, press Enter,
    and assert the picker shows the doesn't-exist error and no longer lists the
    previous directory's ``Desktop`` entry.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_type_nonexistent_path(base_url, session_id))


async def _drive_type_nonexistent_path(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_filesystem(route: Route) -> None:
                # Home ("/home/e2e", and the bare home listing) shows "Desktop";
                # the typed "/home/e2e/does-not-exist" 404s exactly as the host
                # does for a missing path.
                path_part = route.request.url.split("?")[0]
                if path_part.endswith("/filesystem/home/e2e/does-not-exist"):
                    await route.fulfill(
                        status=404,
                        content_type="application/json",
                        body=json.dumps({"detail": "path does not exist"}),
                    )
                    return
                entries = [
                    {
                        "name": "Desktop",
                        "path": "/home/e2e/Desktop",
                        "type": "directory",
                        "bytes": None,
                        "modified_at": 0,
                    }
                ]
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"object": "list", "data": entries, "has_more": False}),
                )

            # Registered last so it wins over the broader **/v1/hosts glob.
            await page.route(_FILESYSTEM_RE, handle_filesystem)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "e2e"
            )

            # Open the browser; the valid home listing shows Desktop.
            await open_landing_workspace_picker(page)
            await expect(page.get_by_test_id("workspace-picker-entry-Desktop")).to_be_visible()

            # Type a nonexistent path and commit with Enter.
            path_input = page.get_by_test_id("workspace-picker-path-input")
            await path_input.fill("~/does-not-exist")
            await path_input.press("Enter")

            # The picker surfaces a doesn't-exist error (pre-fix it silently kept
            # showing the previous directory while retrying the 404)...
            error = page.get_by_test_id("workspace-picker-error")
            await expect(error).to_be_visible()
            await expect(error).to_contain_text("doesn't exist")
            # ...and the previous directory's rows are gone — no stale listing.
            await expect(page.get_by_test_id("workspace-picker-entry-Desktop")).to_have_count(0)
        finally:
            await browser.close()


def test_start_session_add_worktree(seeded_session: tuple[str, str]) -> None:
    """Naming a branch attaches a git worktree spec to the create call.

    Opening the worktree chip and entering a branch (plus a base branch)
    must (a) surface in the chip label and (b) reach ``POST /v1/sessions``
    as ``git: {branch_name, base_branch}``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_add_worktree(base_url, session_id))


async def _drive_add_worktree(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the worktree chip and name a branch + base branch.
            await page.get_by_test_id("new-chat-landing-branch-chip").click()
            await page.get_by_test_id("new-chat-landing-branch-input").fill("feature/login")
            # The base-branch input only appears once a branch name is set.
            await expect(page.get_by_test_id("new-chat-landing-base-branch-input")).to_be_visible()
            await page.get_by_test_id("new-chat-landing-base-branch-input").fill("main")

            # The chip label follows the branch name.
            await expect(page.get_by_test_id("new-chat-landing-branch-chip")).to_contain_text(
                "feature/login"
            )

            # Filling the message closes the popover, then send.
            await page.get_by_test_id("new-chat-landing-input").fill("implement login")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("git") == {"branch_name": "feature/login", "base_branch": "main"}, body
        finally:
            await browser.close()


def test_start_session_select_existing_worktree(seeded_session: tuple[str, str]) -> None:
    """Picking an existing worktree starts in its directory in git bind mode.

    The branch chip's input doubles as a combobox: focusing it lists the
    repo's existing worktrees (``GET /v1/hosts/{id}/worktrees``). Selecting
    one must (a) point the workspace at that worktree's directory and
    (b) send the ``git`` spec in bind mode on ``POST /v1/sessions`` —
    ``existing_worktree: true`` with the worktree's branch as
    ``branch_name`` — so no worktree is created but the sidebar shows the
    branch and the delete flow can offer to remove it.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_select_existing_worktree(base_url, session_id))


async def _drive_select_existing_worktree(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_worktrees(route: Route) -> None:
                # The main tree (is_main) plus one linked worktree. The picker
                # hides the main tree, so only "feature/x" is offered.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "object": "list",
                            "data": [
                                {
                                    "path": "/work/repo",
                                    "branch": "main",
                                    "is_main": True,
                                    "detached": False,
                                },
                                {
                                    "path": "/work/repo-worktrees/feature-x",
                                    "branch": "feature/x",
                                    "is_main": False,
                                    "detached": False,
                                },
                            ],
                        }
                    ),
                )

            # Registered after the common routes so it wins for its URL.
            await page.route(_WORKTREES_RE, handle_worktrees)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the worktree chip; focusing the branch combobox reveals the
            # repo's existing (linked) worktrees. The main tree is filtered out,
            # so only the one linked worktree is offered.
            await page.get_by_test_id("new-chat-landing-branch-chip").click()
            await page.get_by_test_id("new-chat-landing-branch-input").focus()
            option = page.get_by_test_id("new-chat-landing-worktree-option")
            await expect(option).to_have_count(1)
            await expect(option).to_contain_text("feature/x")
            await option.click()

            # The warning confirms the session will start in the existing
            # worktree (rather than creating a new one).
            await expect(
                page.get_by_test_id("new-chat-landing-existing-worktree-warning")
            ).to_be_visible()

            await page.get_by_test_id("new-chat-landing-input").fill("work in the worktree")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # Workspace is the worktree dir; the git spec is in bind mode
            # (existing_worktree) so no worktree is created, and the worktree's
            # branch rides along as branch_name so the sidebar shows it and the
            # delete flow can offer to remove it.
            assert body["workspace"] == "/work/repo-worktrees/feature-x", body
            assert body["git"]["existing_worktree"] is True, body
            assert body["git"]["branch_name"] == "feature/x", body
        finally:
            await browser.close()


# Session-bound agents the discovery scan returns. Both clone names below root
# to the built-in "claude-native-ui", so the picker must drop both; the fork of
# a fork (two nested suffixes) is the case a single-layer strip missed.
_SINGLE_FORK_NAME = "claude-native-ui (fork ag_aaa11111)"
_FORK_OF_FORK_NAME = "claude-native-ui (fork ag_aaa11111) (fork ag_bbb22222)"


def _fork_scan_body() -> str:
    """Stub body for the ``GET /v1/sessions?kind=any`` agent-discovery scan.

    Returns four session-bound agents that exercise every branch of the
    picker's shadow-dropping: the built-in's own row (dropped by id), a single
    fork and a fork-of-fork of the built-in (both dropped by rooted name), and
    one genuinely custom agent (must survive).
    """
    return json.dumps(
        {
            "object": "list",
            "data": [
                # Binds the built-in's own agent row — dropped by id.
                {
                    "id": "conv_native",
                    "agent_id": "ag_claude_e2e",
                    "agent_name": "claude-native-ui",
                },
                # Single fork of the built-in — dropped by name (one layer).
                {"id": "conv_f1", "agent_id": "ag_fork1", "agent_name": _SINGLE_FORK_NAME},
                # Fork of a fork — the regression: dropped only if EVERY clone
                # layer is stripped before the built-in-name check.
                {"id": "conv_ff", "agent_id": "ag_forkfork", "agent_name": _FORK_OF_FORK_NAME},
                # A genuinely custom agent — must SURVIVE and be offered.
                {"id": "conv_doc", "agent_id": "ag_doc", "agent_name": "doc-writer"},
            ],
            "has_more": False,
        }
    )


def test_start_session_picker_drops_fork_of_fork_shadows(
    seeded_session: tuple[str, str],
) -> None:
    """The landing picker hides fork-of-fork clones of a built-in agent.

    The picker (``useAvailableAgents``) merges the built-in list
    (``GET /v1/agents``) with session-scoped agents discovered by scanning the
    caller's sessions (``GET /v1/sessions?kind=any``), dropping any discovered
    agent whose clone name roots back to a built-in. A fork of a fork nests two
    clone suffixes — ``"claude-native-ui (fork …) (fork …)"`` — so a single-
    layer strip leaves ``"claude-native-ui (fork …)"``, which is not a built-in
    name, and the clone leaked into the picker as a SECOND "Claude Code" row.

    This drives that regression end to end against the rendered picker: only
    the real built-in Claude Code and a genuinely custom agent are offered;
    both the single-fork and the fork-of-fork clones are dropped.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_fork_of_fork_dedup(base_url, session_id))


async def _drive_fork_of_fork_dedup(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                # Sole built-in: claude-native-ui, display "Claude Code".
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_fork_scan_body()
                )

            async def handle_enrich(route: Route) -> None:
                # Only the surviving custom agent reaches the per-agent enrich
                # fetch — the dropped shadows never get here.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "id": "ag_doc",
                            "object": "agent",
                            "name": "doc-writer",
                            "description": "Documentation specialist",
                            "harness": "claude-sdk",
                            "skills": [],
                        }
                    ),
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            # kind=any returns the fork + custom session-bound agents; the bare
            # conversation-list GET still falls through to the real server.
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_scan)
            # Per-agent enrich fetch for whichever agent survives the dedup.
            await page.route(re.compile(r"/v1/sessions/[^/]+/agent$"), handle_enrich)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent picker dropdown.
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # The real built-in Claude Code is offered inline...
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            ).to_be_visible(timeout=30_000)
            # ...and BOTH fork clones of the built-in are dropped. Pre-fix the
            # fork-of-fork (ag_forkfork) rendered as a duplicate "Claude Code".
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_fork1")).to_have_count(0)
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_forkfork")).to_have_count(
                0
            )
            # Top level: the built-in Claude row + the "Custom agents" submenu
            # trigger — no duplicate "Claude Code" sneaks in via a leaked clone.
            await expect(page.locator("[data-harness-menu-row]")).to_have_count(1)
            # The genuinely custom agent survives, inside the Custom agents submenu.
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_doc")).to_be_visible()
        finally:
            await browser.close()


@pytest.mark.parametrize(
    ("native_name", "mode", "mode_label", "launch_args"),
    [
        ("cursor", "plan", "Plan", ["--mode", "plan"]),
        (
            "antigravity",
            "skip",
            "Skip permissions",
            ["--dangerously-skip-permissions"],
        ),
    ],
)
def test_start_session_native_permissions_without_empty_edit(
    seeded_session: tuple[str, str],
    native_name: str,
    mode: str,
    mode_label: str,
    launch_args: list[str],
) -> None:
    """Mode-only harnesses select directly and configure through the hand menu."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(
        _drive_native_permissions_without_edit(
            base_url, session_id, native_name, mode, mode_label, launch_args
        )
    )


async def _drive_native_permissions_without_edit(
    base_url: str,
    session_id: str,
    native_name: str,
    mode: str,
    mode_label: str,
    launch_args: list[str],
) -> None:
    agent_id = f"ag_{native_name}_e2e"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=json.dumps(
                    {
                        "data": [
                            {
                                "id": agent_id,
                                "name": f"{native_name}-native-ui",
                                "display_name": native_name.capitalize(),
                                "harness": f"{native_name}-native",
                                "skills": [],
                            }
                        ]
                    }
                ),
            )
            await page.route(
                re.compile(r"/v1/sessions\?.*kind=any"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                timeout=30_000
            )
            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(picker).to_have_attribute(
                "aria-label", re.compile(native_name.capitalize())
            )
            await picker.click()
            row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
            if await row.count() == 0:
                await page.get_by_test_id("new-chat-landing-harness-more").click()
            await expect(row).to_be_visible()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
            ).to_have_count(0)
            await row.click()
            await expect(picker).to_have_attribute("aria-expanded", "false")
            await expect(page.get_by_role("menu")).to_have_count(0)

            permission = page.get_by_test_id("new-chat-landing-permission-chip")
            await permission.click()
            await page.get_by_test_id(f"new-chat-landing-permission-option-{mode}").click()
            await expect(permission).to_contain_text(mode_label)
            await expect(page.get_by_test_id("new-chat-landing-config-modal")).to_have_count(0)
            await page.get_by_test_id("new-chat-landing-input").fill("inspect this repository")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == agent_id, body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("terminal_launch_args") == launch_args, body
        finally:
            await browser.close()


def test_start_session_agy_skip_permissions(seeded_session: tuple[str, str]) -> None:
    """Arming agy's DANGEROUS permission bypass rides along to the create.

    ``--dangerously-skip-permissions`` is agy's only pre-emptive permission
    control and is all-or-nothing: once armed, Omnigent cannot re-gate
    individual tools, because agy fires no pre-tool hook for it to intercept.
    The red banner is therefore the only guardrail between the user and an
    agent that edits any file and runs any command without asking — so this
    covers both that the warning appears while the option is selected and that
    the flag actually reaches ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--dangerously-skip-permissions"]``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_agy_skip_permissions(base_url, session_id))


async def _drive_agy_skip_permissions(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_antigravity_native_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed agy agent feeds
            # the picker.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # agy auto-selects; its permission toggle lives in the hand dropdown.
            skip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(skip).to_be_visible()

            banner = page.get_by_test_id("new-chat-landing-agy-skip-banner")
            # Nothing is bypassed until the user opts in, so the warning must
            # not be showing on open — otherwise it reads as noise and stops
            # carrying weight when it matters.
            await expect(banner).not_to_be_visible()

            # agy has exactly two states: its own prompt, or no prompt at all.
            await skip.click()
            for label in ("Ask every time", "Skip permissions"):
                await expect(page.get_by_role("menuitem", name=label, exact=True)).to_be_visible()
            await page.get_by_test_id("new-chat-landing-permission-option-skip").click()

            await expect(skip).to_contain_text("Skip permissions")
            await expect(banner).to_be_visible()
            await expect(banner).to_contain_text("Danger")

            await expect(
                page.get_by_test_id("new-chat-landing-permission-menu")
            ).not_to_be_visible()
            await skip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-default").click()
            await expect(banner).not_to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-permission-menu")
            ).not_to_be_visible()
            await skip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-skip").click()
            await expect(banner).to_be_visible()

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_antigravity_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("terminal_launch_args") == ["--dangerously-skip-permissions"], body
        finally:
            await browser.close()


def test_start_session_agy_default_sends_no_permission_flag(
    seeded_session: tuple[str, str],
) -> None:
    """Leaving agy's permission toggle alone launches it with no extra flags.

    The default must stay agy's own request-review prompt: a session that
    silently inherited the bypass would strip every confirmation without the
    user ever choosing it. Pins that the untouched toggle sends NO
    ``terminal_launch_args`` at all, not an empty-string or default-valued one.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_agy_default_permissions(base_url, session_id))


async def _drive_agy_default_permissions(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_antigravity_native_agents_body(),
            )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            skip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(skip).to_be_visible()
            await expect(skip).to_contain_text("Ask every time")
            await expect(page.get_by_test_id("new-chat-landing-agy-skip-banner")).to_have_count(0)

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_antigravity_e2e", body
            assert not body.get("terminal_launch_args"), body
        finally:
            await browser.close()
