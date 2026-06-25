"""E2E: auth-aware Codex availability in the New Chat landing screen.

The landing composer (``NewChatLandingScreen`` in
``ap-web/src/shell/NewChatDialog.tsx``) warns — but does not block — when the
selected agent's harness is not ready on the selected host. For Codex the
readiness signal is structured: the host's ``host.hello`` readiness map flows
through ``host_store`` and ``GET /v1/hosts`` as a per-harness
``configured_harnesses`` value of ``"needs-auth"``, ``"binary-missing"``, or
available (absent / ``true``). This PR makes the picker render that distinction:

* the **needs-auth message** under the composer
  (``new-chat-landing-harness-warning``):
  ``"<agent> needs Codex authentication on <host> — run codex login on that
  machine."`` — shown for any selected Codex agent (native or brain harness).
* the **needs-auth badge** (``new-chat-landing-harness-warning-codex``,
  text ``"needs auth"``) on the Codex row inside a bundle agent's Advanced
  "Agent Harness" menu.

Why the ``page.route`` stubbing (mirrors
``start_session/test_start_session.py``): the e2e harness's runner tunnels
directly into the server and registers no *host*, so ``GET /v1/hosts`` has
nothing real to return and there is no seam to inject a per-harness readiness
reason server-side. Faking ``/v1/hosts`` (with ``configured_harnesses``) and
``/v1/agents`` is the established way these tests drive the landing picker; it
is also exactly the wire shape the host readiness map produces, so the stub
exercises the real availability/reason → picker rendering path.

The async-in-a-fresh-thread shape is inherited from
``start_session/test_start_session.py`` for the reason documented there: once a
pytest-playwright *sync* test has run in the session, pytest-asyncio can't start
a loop on the main thread, so each async body runs in its own thread via
:func:`asyncio.run`.
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
_HOST_NAME = "e2e-host"


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


def _hosts_body(configured_harnesses: dict[str, Any] | None) -> str:
    """Stub body for ``GET /v1/hosts``: one online host the composer picks.

    :param configured_harnesses: The host's per-harness readiness map, mirroring
        what the ``host.hello`` readiness map produces (e.g.
        ``{"codex-native": "needs-auth"}``). ``None`` omits the field, modelling
        an older host that never reported readiness ("unknown" — never warns).
    """
    host: dict[str, Any] = {
        "host_id": _HOST_ID,
        "name": _HOST_NAME,
        "owner": "e2e",
        "status": "online",
    }
    if configured_harnesses is not None:
        host["configured_harnesses"] = configured_harnesses
    return json.dumps({"hosts": [host]})


def _codex_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Codex agent.

    ``codex-native-ui`` + ``harness: "codex-native"`` is what the frontend treats
    as a Codex harness (``isCodexHarness``), so a host readiness reason of
    ``"needs-auth"`` for that harness drives the under-composer warning message.
    Sole agent, so it auto-selects and no explicit pick is needed.
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


def _polly_codex_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: a bundle agent on the codex brain harness.

    Polly is a multi-agent bundle (not a native terminal wrapper), so its
    Advanced menu renders the **Agent Harness** radio group with a row per brain
    harness (including ``codex``). That row carries the readiness *badge* when the
    host reports the harness unavailable — the second selector under test. Sole
    agent, so it auto-selects.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_polly_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Multi-agent coding",
                    "harness": "codex",
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(
    page,
    *,
    agents_body: str,
    configured_harnesses: dict[str, Any] | None,
) -> None:
    """Register the host/agent stubs and neutralize agent discovery.

    :param page: The Playwright page to install routes on.
    :param agents_body: Body for the ``GET /v1/agents`` stub.
    :param configured_harnesses: Readiness map for the stubbed host's
        ``configured_harnesses`` (see :func:`_hosts_body`).
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_hosts_body(configured_harnesses),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=agents_body)

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed agent feeds the picker.
        # On the shared e2e_ui server, sessions other tests left behind would
        # otherwise leak in and — ranking ahead — auto-select, swapping the
        # selected harness out from under the warning assertion.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    # Registered after the broad globs so it wins the kind=any discovery scan;
    # the bare conversation-list GET still falls through to the real server.
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_codex_needs_auth_warns_and_clears_when_available(
    seeded_session: tuple[str, str],
) -> None:
    """A needs-auth Codex host warns to run ``codex login``; an available host doesn't.

    Drives the auth-aware availability the PR adds end to end against the
    rendered landing screen:

    1. **needs-auth** — the stubbed host reports ``configured_harnesses:
       {"codex-native": "needs-auth"}`` for the selected Codex agent. The picker
       shows the under-composer warning naming the host and telling the user to
       ``run codex login`` on that machine.
    2. **available** — when the same host omits the reason (Codex ready), the
       warning is absent. Proves the warning is reason-driven, not always-on.
    """
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    _run_in_fresh_loop(_drive_codex_needs_auth(base_url))


async def _drive_codex_needs_auth(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # needs-auth: the host reports the Codex harness as needing auth.
            await _register_routes(
                page,
                agents_body=_codex_native_agents_body(),
                configured_harnesses={"codex-native": "needs-auth"},
            )
            # Seed a recent working directory so the composer auto-fills (it
            # never has to touch the host-less file browser).
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

            # Codex auto-selects (sole agent) on the needs-auth host, so the
            # under-composer warning renders, names the host, and tells the user
            # to run `codex login` (rendered as a <code> element inside the
            # message, so we match the surrounding copy).
            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(warning).to_be_visible(timeout=30_000)
            await expect(warning).to_contain_text("needs Codex authentication")
            await expect(warning).to_contain_text(_HOST_NAME)
            await expect(warning).to_contain_text("codex login")
        finally:
            await browser.close()

    # available: the same host with Codex ready — no reason, no warning.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(
                page,
                agents_body=_codex_native_agents_body(),
                # Codex available (None / absent reason) — the picker must not warn.
                configured_harnesses={"codex-native": True},
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
            # The input is up and Codex has auto-selected; an available host
            # leaves no warning. count()==0 (not "not visible") because the
            # element is conditionally rendered, never just hidden.
            await expect(page.get_by_test_id("new-chat-landing-harness-warning")).to_have_count(0)
        finally:
            await browser.close()


def test_codex_needs_auth_badge_in_harness_menu(
    seeded_session: tuple[str, str],
) -> None:
    """A bundle agent's Advanced "Agent Harness" menu badges the Codex row "needs auth".

    For a brain-harness bundle agent (Polly), the Advanced menu lists each brain
    harness as a radio row. When the selected host reports the ``codex`` harness
    as ``needs-auth``, that row carries the warning badge
    (``new-chat-landing-harness-warning-codex``) reading "needs auth" — the
    per-row counterpart to the under-composer message.
    """
    base_url, session_id = seeded_session
    del session_id
    _run_in_fresh_loop(_drive_codex_badge(base_url))


async def _drive_codex_badge(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(
                page,
                agents_body=_polly_codex_agents_body(),
                configured_harnesses={"codex": "needs-auth"},
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

            # Polly auto-selects (sole agent), so the Advanced chip opens the
            # brain-harness radio group.
            await page.get_by_test_id("new-chat-landing-advanced-chip").click()
            badge = page.get_by_test_id("new-chat-landing-harness-warning-codex")
            await expect(badge).to_be_visible(timeout=30_000)
            await expect(badge).to_contain_text("needs auth")
        finally:
            await browser.close()
