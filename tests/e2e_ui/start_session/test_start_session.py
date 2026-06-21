"""E2E: starting a new session from the home composer ("/").

The landing composer (``NewChatLandingScreen`` in
``ap-web/src/shell/NewChatDialog.tsx``) owns session creation end to end:
the textarea is the new session's first message and the footer chips —
host, working directory, git worktree — plus the agent picker and its
Advanced settings menu supply every create parameter. Hitting Send POSTs
``/v1/sessions`` and navigates to the new session; there is no modal.

These tests cover the three configuration affordances the user reaches
before sending:

1. **Permission mode** — Claude Code's ``--permission-mode`` choices, in
   the agent picker's Advanced settings menu. A non-default pick rides
   along as ``terminal_launch_args``.
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

from playwright.async_api import Route, async_playwright, expect

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
    gating the Codex approval-mode UI in the Advanced menu. Sole agent, so
    it auto-selects and no explicit pick is needed.
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


def _bundle_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the two harness-overridable bundle agents.

    Polly and Debby are multi-agent bundles, not native terminal wrappers, so
    their spec declares a brain harness (``harness: "claude-sdk"``) that lands
    them in ``BRAIN_HARNESS_LABELS``. That — and the fact that neither is named
    ``claude-native-ui`` — is what makes the Advanced menu render the **Agent
    Harness** radio group instead of Claude Code's permission modes. Polly is
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


def test_start_session_select_permission_mode(seeded_session: tuple[str, str]) -> None:
    """Picking a non-default permission mode rides along to the create call.

    Selecting "Accept edits" in the agent picker's Advanced settings menu
    must (a) surface in the agent chip label as immediate feedback and
    (b) reach ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--permission-mode", "acceptEdits"]``.
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
            # Claude Code auto-selects (only built-in, ranked first), so the
            # Advanced chip — gated on the Claude-native agent — is present.
            await page.get_by_test_id("new-chat-landing-advanced-chip").click()
            # All six Claude permission modes render as radio rows.
            for mode in ("default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-permission-{mode}")
                ).to_be_visible()
            await page.get_by_test_id("new-chat-landing-permission-acceptEdits").click()

            # The chip label reflects the non-default pick immediately.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Accept edits"
            )

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_claude_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("terminal_launch_args") == ["--permission-mode", "acceptEdits"], body
        finally:
            await browser.close()


def test_start_session_select_approval_mode(seeded_session: tuple[str, str]) -> None:
    """Picking a non-default approval preset rides along to the create call.

    Selecting "Full access" in the agent picker's Advanced settings menu
    must (a) surface in the agent chip label as immediate feedback and
    (b) reach ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--sandbox", "danger-full-access",
    "--ask-for-approval", "never"]``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_approval_mode(base_url, session_id))


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
            # Codex auto-selects (only built-in), so the Advanced chip —
            # gated on the Codex-native agent — is present.
            await page.get_by_test_id("new-chat-landing-advanced-chip").click()
            # All three Codex approval presets render as radio rows.
            for mode in ("default", "full-access", "read-only"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-approval-{mode}")
                ).to_be_visible()
            await page.get_by_test_id("new-chat-landing-approval-full-access").click()

            # The chip label reflects the non-default pick immediately.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Full access"
            )

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
        finally:
            await browser.close()


def test_start_session_select_harness(seeded_session: tuple[str, str]) -> None:
    """For a bundle agent (Polly/Debby), Advanced offers an agent-harness pick.

    Unlike Claude Code — whose Advanced menu shows permission modes — Polly and
    Debby declare a brain harness, so their Advanced menu renders an "Agent
    Harness" radio group. Selecting a non-default harness ("Pi") must (a) show
    all four harness options, (b) surface the pick in the agent chip label, and
    (c) reach ``POST /v1/sessions`` as ``harness_override: "pi"``.
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

            # Neutralize agent discovery so only the stubbed bundle agents
            # (Polly/Debby) feed the picker. The landing picker merges
            # `/v1/agents` with agents found by scanning the caller's sessions
            # (`/v1/sessions?kind=any`); on the shared e2e_ui server, a native
            # fork another test left behind sorts ahead of bundle agents and
            # auto-selects, so the Advanced chip would open permission modes
            # (or nothing) instead of Polly's harness group. Registered after
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
            # Polly auto-selects (ranked ahead of Debby), so the Advanced chip —
            # present because Polly declares a harness — opens the harness group.
            await page.get_by_test_id("new-chat-landing-advanced-chip").click()
            # All four brain harnesses render as radio rows, in registry order.
            for harness in ("claude-sdk", "openai-agents", "codex", "pi"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-harness-{harness}")
                ).to_be_visible()
            await page.get_by_test_id("new-chat-landing-harness-pi").click()

            # The chip label reflects the non-default harness immediately.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Polly (Pi)"
            )

            await page.get_by_test_id("new-chat-landing-input").fill("debate the design")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_polly_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            assert body.get("harness_override") == "pi", body
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
            await expect(agent_chip).to_contain_text("Pi")
            await expect(agent_chip).not_to_contain_text("native")

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
            }, body
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
            await expect(agent_chip).to_contain_text("Antigravity")
            await expect(agent_chip).not_to_contain_text("native")

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
            }, body
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
            await page.get_by_test_id("new-chat-landing-workspace-chip").click()
            await expect(page.get_by_test_id("workspace-picker")).to_be_visible()
            await page.get_by_test_id("workspace-picker-entry-projects").click()
            # The child listing confirms we navigated in.
            await expect(page.get_by_test_id("workspace-picker-entry-src")).to_be_visible()

            # Filling the message clicks outside the popover, closing it; the
            # chip now shows the navigated folder.
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
            await page.get_by_test_id("new-chat-landing-workspace-chip").click()
            await expect(page.get_by_test_id("workspace-picker")).to_be_visible()
            await page.get_by_test_id("workspace-picker-entry-projects").click()
            await expect(page.get_by_test_id("workspace-picker-entry-src")).to_be_visible()

            # Create a new folder under /home/e2e/projects.
            await page.get_by_test_id("workspace-picker-new-folder").click()
            await page.get_by_test_id("workspace-picker-new-folder-input").fill("new-app")
            await page.get_by_test_id("workspace-picker-new-folder-create").click()

            # The picker POSTs the joined path and drops into the new folder.
            await _wait_until(lambda: len(create_dir_bodies) == 1)
            assert create_dir_bodies[0]["path"] == "/home/e2e/projects/new-app", create_dir_bodies

            # Filling the message closes the popover; the chip now shows the
            # folder we just created.
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

            # The real built-in Claude Code is offered...
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            ).to_be_visible(timeout=30_000)
            # ...the genuinely custom agent survives...
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_doc")).to_be_visible()
            # ...and BOTH fork clones of the built-in are dropped. Pre-fix the
            # fork-of-fork (ag_forkfork) rendered as a duplicate "Claude Code".
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_fork1")).to_have_count(0)
            await expect(page.get_by_test_id("new-chat-landing-agent-ag_forkfork")).to_have_count(
                0
            )
            # Exactly two options total: the built-in + the one custom agent —
            # no duplicate "Claude Code" sneaks in via a leaked clone.
            await expect(page.get_by_role("menuitem")).to_have_count(2)
        finally:
            await browser.close()
