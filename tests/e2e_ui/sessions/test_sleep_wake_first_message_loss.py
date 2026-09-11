"""E2E: computer sleep must not lose the in-flight first message.

Reproduces a user-visible defect: a session created from the landing composer
opens empty after a sleep/wake cycle even though the create itself succeeded
(e.g. Smart Routing scored and recorded the routed model at CREATE time, which
is durable server state — so the session "shows routing occurred" yet has no
transcript).

    1. Start a new session from the landing composer (the first-message
       handoff is identical for any agent).
    2. The computer sleeps before the first message is actually delivered. The
       in-flight ``POST /v1/sessions/{id}/events`` (the auto-send of the first
       message) is severed by the sleep and never reaches the server.
    3. On wake the SPA is forced to re-login, which is a HARD navigation
       (``redirectToLogin`` -> ``window.location.href`` in
       ``web/src/lib/identity.ts``). A hard navigation wipes the JS heap.

Root cause it catches: the landing composer's first message is stashed ONLY in
the module-level in-memory ``pendingInitialPrompts`` map
(``web/src/store/chatStore.ts``) and auto-sent by ``ChatPage`` after the
create-time ``navigate`` to ``/c/:id``. Without persistence, the hard
navigation of the forced re-login wipes both the pending prompt and the
in-flight send. With nothing persisted and no retry, the first message is
silently dropped: the session was created, yet its transcript stays empty.

The reproduction models the sleep/wake faithfully with two injected faults on
the SAME real handoff path the app runs:

- **sleep severs the send** — the auto-send ``POST .../events`` is ``abort``ed
  the first time it fires (the connection dies while the machine sleeps), so the
  first message never reaches the server.
- **wake forces a hard re-login** — a ``page.reload()`` models the forced-relogin
  hard navigation (``window.location.href``) that wipes the in-memory map.

After the reload the ``/events`` route is let through to the REAL server, so a
FIX that recovers the pending prompt (persist + re-dispatch, or otherwise) will
land the first message and it will appear in session A's transcript. The
assertions are on that user-visible outcome — after wake the prompt must be
POSTed to the server and rendered as a TRANSCRIPT MESSAGE BUBBLE. Asserting the
bubble (``data-testid="message-bubble"``) rather than any text on the page
matters: a restored composer *draft* also carries the text (in the composer
textarea), but a draft is not delivery — the defect is precisely that the
message never lands in the session.

The composer needs a host + agent catalog the headless harness can't produce and
the create POST would really launch a runner, so the host list, agent catalog,
and create POST are stubbed via ``page.route`` — the REAL composer still performs
the REAL ``setPendingInitialPrompt`` + ``navigate`` handoff into a REAL,
pre-seeded, runner-bound session A. The async-in-a-fresh-thread shape and the
stub helpers are inherited from ``test_initial_prompt_session_switch`` for the
same reasons documented there.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# A unique sentinel so the first-message POST body (and the rendered user
# bubble) are unambiguously identifiable.
_PROMPT = "sentinel first message that must survive sleep and wake"

_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")
# Bare create endpoint: ``/v1/sessions`` with an optional query, but NOT
# ``/v1/sessions/{id}/...`` — so the GET list and per-session reads pass
# through to the real server while only the POST create is faked.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured
    and re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises BaseException: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
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


def test_sleep_wake_does_not_lose_first_message(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A first message must survive computer sleep + forced-relogin wake.

    Failure mode this catches: the first message typed on the
    landing composer is stashed only in an in-memory map and auto-sent after
    navigation. If the send is interrupted (sleep) and the page then hard-
    navigates (forced re-login on wake), the map is wiped and the message is
    silently dropped — the session is created (routing recorded) but empty.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_sleep_wake(base_url, session_a, session_b))


async def _drive_sleep_wake(base_url: str, session_a: str, session_b: str) -> None:
    """Async body of the sleep/wake first-message-loss repro.

    :param base_url: Spawned server base URL.
    :param session_a: The pre-seeded, runner-bound session the composer
        "creates"; the initial prompt is composed for and auto-sent to it.
    :param session_b: An already-running session the user starts from (so the
        create handoff is a genuine client-side navigation into A).
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so the `finally` can close IT before the browser —
        # closing only the browser can drop an in-flight video recording
        # (OMNIGENT_E2E_RECORD_DIR) on the floor as a 0-byte file.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint, split by
            # whether it happened before or after the forced-relogin reload.
            events_before: list[tuple[str, str]] = []
            events_after: list[tuple[str, str]] = []
            # Flipped right before the reload so the /events handler switches
            # from "sleep severs the send" (abort) to "let the real send land".
            woke = {"v": False}

            async def handle_events(route: Route) -> None:
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                sid = match.group(1)
                if not woke["v"]:
                    # The machine sleeps mid-send: the in-flight auto-send POST
                    # is severed and never reaches the server.
                    events_before.append((sid, text))
                    await route.abort("connectionaborted")
                else:
                    # Awake again: let a recovered/re-dispatched send reach the
                    # REAL server so it persists and shows in the transcript.
                    events_after.append((sid, text))
                    await route.continue_()

            async def handle_hosts(route: Route) -> None:
                # One online host so the composer can pick a host (the
                # directly-tunneled harness registers no host).
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": "host_e2e",
                                    "name": "e2e-host",
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            async def handle_agents(route: Route) -> None:
                # The composer's available-agent catalog (GET /v1/agents).
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "data": [
                                {
                                    "id": "ag_e2e",
                                    "name": "hello_world",
                                    "display_name": "Hello World",
                                    "description": None,
                                    "harness": None,
                                }
                            ]
                        }
                    ),
                )

            async def handle_sessions(route: Route) -> None:
                # Fake ONLY the composer's create POST, returning the pre-seeded
                # session A's id so the real handoff (setPendingInitialPrompt +
                # navigate) targets a real, runner-bound session. Everything
                # else (the GET list, per-session reads) hits the real server.
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_a}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Start in an already-running session B, then open the landing
            # composer for the new session A (mirrors the app's real entry).
            await page.goto(f"{base_url}/c/{session_b}")

            # Seed a recent working directory for the stubbed host so the
            # composer auto-fills the working-directory chip and Send enables.
            await page.evaluate(
                """() => localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({ host_e2e: ["/tmp"] }),
                )"""
            )

            await page.get_by_test_id("new-chat-button").click()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=15_000
            )
            # The composer textarea doubles as the new session's initial prompt.
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            # Send enables only once message + host + agent + valid workspace are
            # all set; Playwright auto-waits for it to be actionable.
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create handoff navigates to A and auto-sends the first message.
            # Wait for that POST attempt — it confirms the REAL consume +
            # auto-send path ran (so a clean run isn't a no-op). The machine is
            # "asleep", so the handler aborts it: the message never lands.
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))
            await _wait_until(
                lambda: any(sid == session_a and text == _PROMPT for sid, text in events_before)
            )

            # Wake up: the forced re-login is a HARD navigation
            # (window.location.href), which wipes the in-memory pendingInitialPrompts
            # map. page.reload() models that heap-wiping hard navigation.
            woke["v"] = True
            await page.reload()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

            # Sanity: the auto-send path really ran before the interruption, so
            # the test genuinely exercised the first-message handoff.
            assert any(sid == session_a and text == _PROMPT for sid, text in events_before), (
                "the initial prompt never auto-sent to session A before the reload; "
                f"pre-wake /events posts were {events_before} — the test did not exercise "
                "the first-message handoff at all"
            )

            # Core assertions (user-visible, fix-agnostic): after waking from
            # sleep, the first message must NOT be lost — it must appear in
            # session A's TRANSCRIPT (a message bubble, not merely a restored
            # composer draft: the draft textarea also carries the text on some
            # builds, and a draft is not delivery). On the buggy build the
            # in-memory prompt is wiped by the hard re-login navigation with no
            # retry, so no bubble ever appears and this fails.
            await expect(
                page.get_by_test_id("message-bubble").filter(has_text=_PROMPT).first
            ).to_be_visible(timeout=20_000)
            # And the delivery must be real: the prompt reached the server
            # after wake (the pre-wake POST was severed by the sleep).
            await _wait_until(
                lambda: any(sid == session_a and text == _PROMPT for sid, text in events_after),
                timeout_s=15.0,
            )
        finally:
            await context.close()
            await browser.close()
