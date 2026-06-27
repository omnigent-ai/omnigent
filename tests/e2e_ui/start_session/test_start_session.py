"""E2E: starting a new session from the home composer ("/").

The landing composer (``NewChatLandingScreen`` in
``ap-web/src/shell/NewChatDialog.tsx``) owns session creation end to end:
the textarea is the new session's first message and the footer chips —
host, working directory, git worktree — plus the unified agent/harness
picker supply every create parameter. The picker is a single dropdown
(``new-chat-landing-agent-select``); each agent's run-config knobs live in a
per-entry submenu (see :func:`_open_entry_config`). Hitting Send POSTs
``/v1/sessions`` and navigates to the new session; there is no modal.

These tests cover the three configuration affordances the user reaches
before sending:

1. **Permission mode** — Claude Code's ``--permission-mode`` choices, in
   the agent picker's per-entry config submenu. A non-default pick rides
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
    ``claude-native-ui`` — is what makes the composer render the harness picker
    (an **Agent Harness** radio group) instead of Claude Code's permission-mode
    pill. Polly is
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


async def _open_entry_config(page, agent_id: str) -> None:
    """Open the agent/harness picker and drill into one entry's config submenu.

    The redesigned composer replaces the old per-control pills/triggers (the
    run-mode pill, the model trigger, the harness trigger) with a single
    agent/harness dropdown (``new-chat-landing-agent-select``). Each agent is a
    row; a knobbed entry's run-config (model / effort / permission / approval /
    brain-harness override) lives in a per-entry **submenu**. A plain *click* on
    a knobbed row COMMITS that agent and closes the menu, so config flows hover
    the row and nudge it with ``ArrowRight`` to open the submenu without
    committing — the Playwright counterpart of the unit test's
    ``openAgentConfig`` helper.

    :param page: The Playwright page (the landing picker is already mounted).
    :param agent_id: The stubbed agent id whose submenu to open, e.g.
        ``"ag_claude_e2e"``.
    """
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    await row.hover()
    await row.press("ArrowRight")


def test_start_session_select_permission_mode(seeded_session: tuple[str, str]) -> None:
    """Picking a non-default permission mode rides along to the create call.

    Selecting "Accept edits" in the Claude Code entry's config submenu
    must (a) check that radio as immediate feedback and
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

            # Neutralize agent discovery so ONLY the stubbed Claude agent feeds
            # the picker. The landing picker merges `/v1/agents` with agents found
            # by scanning the caller's sessions (`/v1/sessions?kind=any`); on the
            # shared e2e_ui server a native agent another test left behind would
            # otherwise leak in and — ranking ahead — auto-select, so opening
            # Claude's submenu and picking a knob would SWITCH agent mid-flow
            # (remounting the submenu and detaching the next row). Registered
            # after _register_common_routes so it wins the kind=any scan.
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
            # Claude Code auto-selects (only built-in, ranked first); its config
            # lives in the picker's per-entry submenu (model / effort / permission
            # mode), opened without committing the row.
            await _open_entry_config(page, "ag_claude_e2e")
            # All six Claude permission modes render as radio rows in the submenu.
            for mode in ("default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-permission-{mode}")
                ).to_be_visible()
            accept_edits = page.get_by_test_id("new-chat-landing-permission-acceptEdits")
            await accept_edits.click()

            # The radio reflects the non-default pick immediately; the submenu
            # stays open after a permission pick, so close it (submenu, then root)
            # before typing into the composer.
            await expect(accept_edits).to_have_attribute("aria-checked", "true")
            await page.keyboard.press("Escape")
            await page.keyboard.press("Escape")

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


def test_start_session_select_model_and_effort(seeded_session: tuple[str, str]) -> None:
    """Picking a model + reasoning effort rides along to the create call.

    For the Claude-native agent the config submenu shows a model/effort
    picker that defaults to Claude Code's own "Sonnet / Medium". Selecting
    "Opus" and "High" must (a) check those radios as immediate feedback and
    (b) reach ``POST /v1/sessions`` as ``model_override: "opus"`` +
    ``reasoning_effort: "high"`` (the runner reads them as ``--model`` /
    ``--effort`` at terminal launch).
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
            # leaked native agent auto-selecting ahead of Claude would make the
            # model pick SWITCH agent, remounting the submenu and detaching the
            # effort row before it can be clicked. Registered after
            # _register_common_routes so it wins the kind=any scan.
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
            # Claude Code auto-selects; open its config submenu, which carries the
            # model + effort radio groups defaulting to Claude Code's effective
            # defaults (Sonnet / Medium).
            await _open_entry_config(page, "ag_claude_e2e")
            await expect(page.get_by_test_id("new-chat-landing-model-sonnet")).to_have_attribute(
                "aria-checked", "true"
            )
            await expect(page.get_by_test_id("new-chat-landing-effort-medium")).to_have_attribute(
                "aria-checked", "true"
            )

            # Pick model and effort in SEPARATE submenu visits. Picking a knob
            # COMMITS the agent, which collapses the submenu (its model / effort /
            # permission rows unmount) while the ROOT menu stays open — so a second
            # knob clicked in the same visit chases a row that has already detached
            # and flakes ("detached from the DOM, retrying" until the click times
            # out). Reopen the submenu between the two picks: the picks persist as
            # screen state, and each click then lands in a fresh, stable submenu.
            opus = page.get_by_test_id("new-chat-landing-model-opus")
            await opus.click()
            await expect(opus).to_have_attribute("aria-checked", "true")
            # The model pick collapsed the submenu; wait for it to fully unmount,
            # then reopen it. The root menu never closed (the radio's preventDefault
            # keeps it open), so reopen via the row — re-hover + ArrowRight — rather
            # than re-clicking the trigger (which would race the still-settling
            # dismiss and fail to reopen).
            row = page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            await expect(page.get_by_test_id("new-chat-landing-effort-high")).to_have_count(0)
            await row.hover()
            await row.press("ArrowRight")
            # The model pick persisted across the reopen.
            await expect(page.get_by_test_id("new-chat-landing-model-opus")).to_have_attribute(
                "aria-checked", "true"
            )
            high = page.get_by_test_id("new-chat-landing-effort-high")
            await high.click()
            await expect(high).to_have_attribute("aria-checked", "true")
            # Close the submenu then the root menu before typing the prompt.
            await page.keyboard.press("Escape")
            await page.keyboard.press("Escape")

            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_claude_e2e", body
            assert body.get("model_override") == "opus", body
            assert body.get("reasoning_effort") == "high", body
        finally:
            await browser.close()


def test_start_session_select_approval_mode(seeded_session: tuple[str, str]) -> None:
    """Picking a non-default approval preset rides along to the create call.

    Selecting "Full access" in the Codex entry's config submenu must reach
    ``POST /v1/sessions`` as
    ``terminal_launch_args: ["--sandbox", "danger-full-access",
    "--ask-for-approval", "never"]``. (Unlike the model/permission radios, an
    approval pick commits and closes the menu.)
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
            await expect(picker).to_contain_text("Codex")
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
            # Codex auto-selects (only built-in); its approval presets live in the
            # picker's per-entry config submenu.
            await _open_entry_config(page, "ag_codex_e2e")
            # All three Codex approval presets render as radio rows in the submenu.
            for mode in ("default", "full-access", "read-only"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-approval-{mode}")
                ).to_be_visible()
            # Picking an approval preset commits and closes the menu (unlike the
            # model/permission radios), leaving the composer ready for the prompt.
            await page.get_by_test_id("new-chat-landing-approval-full-access").click()

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


def test_start_session_bypass_sandbox(seeded_session: tuple[str, str]) -> None:
    """Arming the DANGEROUS Codex full-bypass toggle rides along to the create.

    The bypass switch in the Codex entry's config submenu is the first-class
    opt-in for Codex's ``--dangerously-bypass-approvals-and-sandbox`` stance. It
    is deliberately hard to arm: the Switch stays **disabled** until the user
    types the confirmation phrase *verbatim* (a click alone, or a near-miss
    phrase, never arms it), and once on, a persistent red banner shows under the
    composer — surviving the menu's close. When armed, the create
    ``POST /v1/sessions`` must carry the
    ``omnigent.codex_native.bypass_sandbox: "1"`` conversation label so the
    runner launches Codex with the bypass flag.
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
            # Codex auto-selects (only built-in); the bypass opt-in lives inside
            # its config submenu, below the approval presets.
            await _open_entry_config(page, "ag_codex_e2e")

            # Guardrail: the bypass Switch is DISABLED until the verbatim phrase
            # is typed — a click alone can never arm the dangerous mode.
            switch = page.get_by_test_id("new-chat-landing-bypass-sandbox-switch")
            await expect(switch).to_be_disabled()

            # A near-miss phrase (different case) keeps it disabled — the match
            # is verbatim, no case-folding or trimming.
            confirm = page.get_by_test_id("new-chat-landing-bypass-sandbox-confirm")
            await confirm.fill("Bypass Sandbox")
            await expect(switch).to_be_disabled()

            # The exact phrase arms the Switch; flip it on.
            await confirm.fill("bypass sandbox")
            await expect(switch).to_be_enabled()
            await switch.click()

            # Close the submenu then the root menu; the in-menu banner goes with
            # them, but the persistent red banner under the composer must remain —
            # proof the armed stance stays visible after the menu closes.
            await page.keyboard.press("Escape")
            await page.keyboard.press("Escape")
            await expect(
                page.get_by_test_id("new-chat-landing-bypass-sandbox-active-banner")
            ).to_be_visible()

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
        finally:
            await browser.close()


def test_start_session_select_harness(seeded_session: tuple[str, str]) -> None:
    """For a bundle agent (Polly/Debby), the composer offers an agent-harness pick.

    Unlike Claude Code — whose submenu shows permission/model knobs — Polly and
    Debby declare a brain harness, so their config submenu renders an "Agent
    Harness" radio group. Selecting a non-default harness ("Pi") must (a) show
    all four harness options and (b) reach ``POST /v1/sessions`` as
    ``harness_override: "pi"``.
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
            # override radios live in the picker's per-entry config submenu.
            await _open_entry_config(page, "ag_polly_e2e")
            # All four brain harnesses render as radio rows, in registry order.
            for harness in ("claude-sdk", "openai-agents", "codex", "pi"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-harness-{harness}")
                ).to_be_visible()
            # Picking a harness commits and closes the menu (the agent chip keeps
            # the bare agent label "Polly"); the override rides along on create.
            await page.get_by_test_id("new-chat-landing-harness-pi").click()

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
            await expect(agent_chip).to_contain_text("OpenCode")
            await expect(agent_chip).not_to_contain_text("native")

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
            }, body
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
            await expect(agent_chip).to_contain_text("Kimi")
            await expect(agent_chip).not_to_contain_text("native")

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
            }, body
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
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_kimi_native_e2e")
            ).to_be_visible(timeout=30_000)
            # ...and the SDK kimi row is dropped (hidden by NEW_SESSION_HIDDEN_AGENTS).
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_kimi_sdk_e2e")
            ).to_have_count(0)
            # Two menu items total: the one native Kimi + the "Create custom
            # agent" action — no second "Kimi" sneaks in via the SDK row.
            await expect(page.get_by_role("menuitem")).to_have_count(2)
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
            # Three options total: the built-in + the one custom agent +
            # the "Create custom agent" action — no duplicate "Claude Code"
            # sneaks in via a leaked clone.
            await expect(page.get_by_role("menuitem")).to_have_count(3)
        finally:
            await browser.close()
