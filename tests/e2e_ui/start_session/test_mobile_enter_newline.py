"""E2E: Enter key inserts a newline (not submit) in the new-session composer on mobile.

On touch-primary devices the on-screen keyboard has no Shift key, so pressing
Enter must insert a newline and submission must remain an explicit tap on the
Send button.  The in-session ``Composer`` already guards this with an
``!isMobile`` check (``ChatPage.tsx``).  The new-session landing screen
(``NewChatDialog.tsx``) was missing that guard, so Enter triggered
``handleCreate()`` — submitting the session — instead of inserting a newline.

The observable failure (reproduced on the running build at the time of filing,
commit d2556dff):

  1. Open the new-session screen (``/``) in a mobile-viewport browser context
     (``is_mobile: True``, ``has_touch: True``, viewport width < 768 px).
  2. Pick a non-terminal agent (polly / any YAML agent — the composer is
     mounted; terminal agents launch into xterm.js immediately so the web
     composer is never shown).
  3. Type text in the ``new-chat-landing-input`` textarea and press Enter.
  4. Actual:   ``POST /v1/sessions`` fires — the session is submitted.
     Expected: a newline is inserted; the textarea still contains the draft.

This test asserts the expected behavior: Enter does NOT fire a create POST, and
the textarea still contains the draft (with a newline appended).  A passing test
means the bug is fixed.  On the unfixed code it fails because a create POST
arrives instead.

Pattern mirrors ``test_start_session.py`` — async bodies in fresh threads to
avoid the pytest-playwright/asyncio loop incompatibility, with the same
``page.route`` stubs for ``/v1/hosts``, ``/v1/agents``, and
``POST /v1/sessions``.  The test uses a ``new_context`` (not bare ``new_page``)
with the iPhone 13 device descriptor so the SPA's ``(pointer: coarse)`` /
``(max-width: 767.98px)`` guards resolve correctly; video is recorded via the
context so a ``--video on`` pass captures the failing state.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# ---------------------------------------------------------------------------
# Constants copied from test_start_session.py (same server/stub shape)
# ---------------------------------------------------------------------------

_HOST_ID = "host_e2e"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright sync tests in the same process;
    once one has run, pytest-asyncio can't start a loop on the main thread.
    Exceptions (including assertion failures) are re-raised on the caller.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if "error" in captured:
        raise captured["error"]


def _agents_body_polly() -> str:
    """Stub ``GET /v1/agents`` that returns *only* the polly agent.

    Polly is a YAML/orchestrator agent (harness: ``claude-sdk``) — it always
    uses the web composer, so the textarea is mounted and Enter semantics are
    observable.  This is exactly the agent the bug report used.
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
                }
            ]
        }
    )


def _hosts_body() -> str:
    """Stub ``GET /v1/hosts``: one online host the picker auto-selects.

    Mirrors the shape ``test_start_session.py`` uses: ``hosts`` (not ``data``),
    ``host_id`` (not ``id``), ``status: "online"`` so the SPA's
    ``h.status === "online"`` guard picks it up for auto-selection.
    """
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


# ---------------------------------------------------------------------------
# The reproduction drive
# ---------------------------------------------------------------------------


def test_mobile_landing_enter_inserts_newline_not_submit(
    seeded_session: tuple[str, str],
) -> None:
    """Enter in the new-session textarea on a touch device inserts a newline.

    On the unfixed code Enter fires ``handleCreate()`` (a ``POST /v1/sessions``)
    instead of inserting a newline.  The test:

    * opens a mobile-viewport browser (iPhone 13 descriptor — ``is_mobile``,
      ``has_touch``, viewport 390×664, UA matches iOS Safari);
    * stubs the agent catalog to return only ``polly`` (a YAML/web-composer
      agent), making it the auto-selected agent;
    * types a line of text and presses ``Enter``;
    * asserts the create POST was NOT fired (draft preserved, no navigation);
    * asserts the textarea still contains the original text (newline appended or
      at minimum the original text is intact — a submit clears the composer and
      navigates away, which is the failure mode).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_mobile_enter_newline(base_url, session_id))


async def _drive_mobile_enter_newline(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        # iPhone 13 device descriptor: is_mobile=True, has_touch=True,
        # viewport 390×664 (below Tailwind's md breakpoint at 768px).
        # ``(pointer: coarse)`` and ``(max-width: 767.98px)`` both fire, so the
        # SPA's mobile guard — when present — correctly suppresses Enter-to-send.
        # Use Chromium (available in CI); the SPA's media-query guards are
        # browser-agnostic. Strip ``default_browser_type`` — it's metadata for
        # Playwright's device registry, not a valid context argument.
        iphone = pw.devices["iPhone 13"]
        ctx_kwargs = {k: v for k, v in iphone.items() if k != "default_browser_type"}
        recordings_dir = Path(__file__).resolve().parents[3] / "recordings" / "omni-3766"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            **ctx_kwargs,
            record_video_dir=str(recordings_dir),
        )
        try:
            page = await ctx.new_page()

            # Track whether a create POST fires — on the buggy code it does.
            create_fired = False

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_agents_body_polly(),
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                nonlocal create_fired
                if route.request.method == "POST":
                    # Record the submit and let the navigation happen so the
                    # test doesn't hang — but the assertion below will fail.
                    create_fired = True
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # Suppress the agent-discovery scan (kind=any) so only the stubbed
            # polly agent feeds the picker — same guard as test_start_session.py.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent workspace so the host chip auto-fills and the Send
            # button can become enabled.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            input_el = page.get_by_test_id("new-chat-landing-input")
            await input_el.wait_for(state="visible", timeout=30_000)

            # Type a line of text — the draft before the (wrongly) submitted session.
            draft_text = "first line of my prompt"
            await input_el.fill(draft_text)

            # Wait for the Send button to become enabled — this is the moment
            # canSubmit transitions to true (host selected, agent selected,
            # message non-empty, workspace valid).  On the unfixed code, pressing
            # Enter right now calls handleCreate() which POSTs the session because
            # the guard is missing the !isMobile check.
            submit_btn = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit_btn).to_be_enabled(timeout=15_000)

            # Press Enter — on a touch device this should insert a newline;
            # on the unfixed code it submits the session.
            await input_el.press("Enter")

            # Give any navigation a moment to start (it would navigate away within
            # ~500 ms on the unfixed code once the route handler returns).
            await page.wait_for_timeout(800)

            # --- Assertions -------------------------------------------------------

            # 1. The create POST must NOT have fired.
            assert not create_fired, (
                "Enter triggered a session create (POST /v1/sessions) on a "
                "touch-primary device — it should have inserted a newline instead. "
                "The new-session composer is missing the !isMobile guard that "
                "ChatPage.tsx's Composer already has."
            )

            # 2. The textarea must still be present (not navigated away).
            await expect(input_el).to_be_visible(timeout=3_000)

            # 3. The textarea must still contain the original text (the draft is
            #    preserved; a newline may or may not have been appended, but the
            #    text must not have been cleared by a submit).
            value: str = await input_el.input_value()
            assert draft_text in value, (
                f"Textarea lost the draft text after Enter — got {value!r}. "
                f"Expected it to still contain {draft_text!r}."
            )
        finally:
            await ctx.close()
            await browser.close()
