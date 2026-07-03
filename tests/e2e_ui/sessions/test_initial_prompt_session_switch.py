"""E2E: a new chat's initial prompt never leaks into another session.

Guards the initial-prompt cross-session leak (sibling of the queued-
message regression in ``test_cross_session_routing``, but a different
code path):

    The user opens the home composer ("New session"), types an initial
    prompt for a brand-new session A, and creates it. The prompt
    auto-sends to A (its runner is online), but the held prompt state is
    NOT cleared while the user stays on A. The user then clicks an
    already-running session B in the sidebar. The held prompt MUST stay
    bound to A — it must never be POSTed into B (which would deliver a
    duplicate of A's prompt to B).

Root cause it catches (an effect-ordering race in ``ChatPage``): the
consume effect (``setInitialPrompt(...)``) and the auto-send effect both
key off ``urlConvId``. The prompt auto-sends to A once A's runner is
online, but the held prompt state is NOT cleared while the user stays on
A. So on the A→B switch the auto-send effect re-runs in the same commit
with the STALE prompt (consumed for A) while ``urlConvId`` is already B
and B's runner reads online. ``send()`` then pins the live store id —
already B — and a DUPLICATE of A's prompt floods B. (The same race also
leaks when A's runner is still offline at switch time; this test drives
the runner-online variant because it needs no health stubbing.) The fix
binds the prompt to the conversation it was consumed for and gates the
auto-send on a match (``shouldSendInitialPrompt``'s ``promptConversationId``).

The composer needs a host + agent catalog the headless harness can't
produce (its runner is directly tunneled, no host daemon), and the
create POST would really launch a runner. So the host list, the agent
catalog, and the create POST are stubbed via ``page.route`` — the REAL
composer still performs the REAL ``setPendingInitialPrompt`` + ``navigate``
handoff into a REAL, pre-seeded session A. ``/events`` is intercepted
(like the sibling test) so no real turn runs and the assertion is purely
on where each POST is addressed. The async-in-a-fresh-thread shape and
the sidebar (client-side) navigation are inherited from
``test_cross_session_routing`` for the same reasons documented there.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright

# Unique sentinels so each POST body is unambiguously identifiable.
_PROMPT = "sentinel-initprompt-7b3e initial prompt bound to session A"
_FOLLOWUP = "sentinel-followup-2d9a live send into session B"

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


def test_initial_prompt_stays_bound_to_origin_session_after_switch(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A held initial prompt for A must not leak into B after switching.

    Failure mode this catches: the prompt typed for the new session A is
    POSTed to session B (the now-active session the user switched to)
    because the auto-send effect fired with a stale prompt during the
    A→B switch commit.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_initial_prompt_switch(base_url, session_a, session_b))


async def _drive_initial_prompt_switch(base_url: str, session_a: str, session_b: str) -> None:
    """Async body of the initial-prompt leak test. See the module docstring.

    :param base_url: Spawned server base URL.
    :param session_a: The pre-seeded session the composer "creates"; the
        initial prompt is composed for and correctly sent to it.
    :param session_b: The already-running session the user switches to,
        into which the held prompt must never leak.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint.
            event_posts: list[tuple[str, str]] = []

            async def handle_events(route: Route) -> None:
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                event_posts.append((match.group(1), text))
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_hosts(route: Route) -> None:
                # One online host so the composer can pick a host
                # (the directly-tunneled harness registers no host).
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
                # The app's own agent list uses GET /v1/sessions, so this
                # route only feeds the composer's auto-selected first agent.
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
                # Fake ONLY the composer's create POST, returning the
                # pre-seeded session A's id so the real handoff
                # (setPendingInitialPrompt + navigate) targets a real
                # session. Everything else (the GET conversation list,
                # per-session reads) goes to the real server.
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

            # Start in the already-running session B (full reload is fine
            # here — the race only matters once we navigate client-side).
            await page.goto(f"{base_url}/c/{session_b}")

            # Seed a recent working directory for the stubbed host so the home
            # composer auto-fills the working-directory chip. The file browser
            # has its own tests; this test only needs a valid workspace so the
            # Send button enables, and seeding avoids depending on the picker's
            # (stubbed, host-less) filesystem listing. Keyed by the host id the
            # composer auto-selects from the stubbed /v1/hosts.
            await page.evaluate(
                """() => localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({ host_e2e: ["/tmp"] }),
                )"""
            )

            # "New session" now routes to the home composer (the modal is
            # retired). Compose the new session A there: the host + first agent
            # auto-select and the working directory auto-fills from the seeded
            # recent, so only the prompt needs typing.
            await page.get_by_test_id("new-chat-button").click()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=15_000
            )
            # The composer textarea doubles as the new session's initial prompt.
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            # Playwright auto-waits for Send to be actionable (enabled) before
            # clicking — it enables only once a message + host + agent + a valid
            # workspace are all set, so this also confirms the form is complete.
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create handoff navigates to A, whose runner is online, so
            # the prompt correctly auto-sends to A. Wait for that POST: it
            # confirms the real consume + auto-send path ran (and leaves the
            # held prompt state uncleared, which is what the switch re-fires).
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))
            await _wait_until(lambda: any(text == _PROMPT for _, text in event_posts))

            # Switch to the running session B via the sidebar link — a
            # client-side navigation that preserves the JS module state.
            await page.locator(f'a[href="/c/{session_b}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}"))

            # Drive a real follow-up send into B. It MUST land in B, and it
            # acts as a barrier: once it is observed the A→B switch commit
            # (where the leak would fire) has fully completed, so any leak
            # of the held prompt into B is already recorded by now.
            composer = page.get_by_label("Message the agent")
            await composer.fill(_FOLLOWUP)
            await page.get_by_role("button", name="Send", exact=True).click()
            await _wait_until(lambda: any(text == _FOLLOWUP for _, text in event_posts))

            followup_targets = [sid for sid, text in event_posts if text == _FOLLOWUP]
            assert followup_targets == [session_b], (
                f"the live follow-up must post to B ({session_b}); targets were {followup_targets}"
            )
            prompt_targets = [sid for sid, text in event_posts if text == _PROMPT]
            # Sanity: the prompt did reach its origin A — proves the real
            # consume + auto-send path ran (so a clean run isn't a no-op).
            assert session_a in prompt_targets, (
                f"the initial prompt never reached its origin session A ({session_a}); "
                f"targets were {prompt_targets} — the test did not exercise auto-send"
            )
            # The core assertion: the prompt composed for A must never have
            # been POSTed into B, the session the user switched to.
            assert session_b not in prompt_targets, (
                f"the initial prompt composed for session A ({session_a}) leaked into "
                f"session B ({session_b}): POST targets were {prompt_targets}. A target "
                f"of B is the cross-session initial-prompt leak."
            )
        finally:
            await browser.close()
