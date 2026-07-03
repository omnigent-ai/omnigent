"""E2E: a queued message is delivered to the session it was composed in.

Guards the cross-session message-routing regression:

    Session B's runner is slow to come up, so B's first message POST
    stays in flight. A second message typed into B queues behind it on
    the SPA's module-level send chain. While the chain is stalled the
    user switches to a different, already-running session A. The queued
    second message MUST still be POSTed to B — the session it was
    composed in — not to whatever session is active when the chain
    finally unblocks.

The bug routed the second message to A because the POST target was
re-resolved from the live ``conversationId`` only AFTER the chain
unblocked (``chatStore.ensureBoundSession``), by which point the user
had navigated to A. The fix pins the destination at submit time.

Why async Playwright (not the sync ``page`` fixture the other e2e_ui
tests use): the repro requires B's first ``POST /events`` to stay
outstanding WHILE the test performs more UI actions (type + send the
second message, then switch sessions). Holding a request open across
interleaved page actions needs a deferred ``route.continue_`` /
``route.fulfill``, which only the async API supports — a sync route
handler blocks the single greenlet and would deadlock. It is a sync
test that drives the async flow in a fresh thread (see
:func:`_run_in_fresh_loop`) rather than a pytest-asyncio test: the
suite's many sync pytest-playwright tests leave the main-thread loop in
a state where pytest-asyncio can't start one. The session switch is
driven via the in-app sidebar link (client-side navigation), NOT
``page.goto`` — a full reload would reset the JS module state (the send
chain) and dissolve the very race under test.

The route handler fulfills every ``/events`` POST itself, so no real
turn runs and the test needs no working LLM — it asserts purely on
where the SPA addressed each POST.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"
# Unique sentinels so each POST body is unambiguously identifiable.
_MSG1 = "sentinel-xsess-msg1-3a7f first message into B"
_MSG2 = "sentinel-xsess-msg2-9c2e second message into B"

_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    This file is a sync test that drives async Playwright. The e2e_ui suite
    runs many pytest-playwright **sync** tests in the same session; once one
    has run, pytest-asyncio can't start a loop on the main thread
    ("Runner.run() cannot be called from a running event loop"). Running the
    coroutine from a fresh thread via :func:`asyncio.run` sidesteps that
    entirely. Any exception — including assertion failures — is captured and
    re-raised on the calling thread so the test fails normally.

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


def test_queued_send_routes_to_origin_session_not_active_session(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Second message queued in B reaches B after switching to A.

    Failure mode this catches: the queued ``_MSG2`` POST is addressed to
    session A (the now-active session) instead of session B (where it was
    composed) — a message leaking into the wrong, unrelated session.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_cross_session_routing(base_url, session_a, session_b))


async def _drive_cross_session_routing(base_url: str, session_a: str, session_b: str) -> None:
    """Async body of the cross-session routing test. See the test docstring.

    :param base_url: Spawned server base URL.
    :param session_a: The running session the user switches to.
    :param session_b: The session both messages are composed in.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint.
            event_posts: list[tuple[str, str]] = []
            # Released to let B's first POST (msg1) finally complete; until
            # then it stays in flight and the send chain is stalled.
            release_first = asyncio.Event()
            first_b_post_held = False

            async def handle_events(route: Route) -> None:
                nonlocal first_b_post_held
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                session_id = match.group(1)
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                event_posts.append((session_id, text))
                # Hold ONLY B's first message open so a second send queues
                # behind it on the chain while we switch sessions.
                if session_id == session_b and not first_b_post_held:
                    first_b_post_held = True
                    await release_first.wait()
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            await page.route("**/v1/sessions/*/events", handle_events)

            # Start in session B (initial load may reload freely — the send
            # chain only matters once we begin sending).
            await page.goto(f"{base_url}/c/{session_b}")
            # Locate the textarea by its stable aria-label, not the
            # placeholder — the placeholder text changes once a turn starts
            # streaming ("Send a follow-up (queued)…"), which would break a
            # placeholder-pinned locator for the second send.
            composer = page.get_by_label("Message the agent")
            # The "Ask the agent anything…" placeholder only renders in the
            # idle/enabled state, so waiting on it confirms the chat surface
            # is ready to accept input (not "Waiting for agents…").
            await page.get_by_placeholder(_COMPOSER_PLACEHOLDER).wait_for(
                state="visible", timeout=15_000
            )
            send_button = page.get_by_role("button", name="Send", exact=True)

            # msg1 → POST to B, held open by the route handler above.
            await composer.fill(_MSG1)
            await send_button.click()
            await _wait_until(lambda: first_b_post_held)

            # msg2 → parked behind msg1 on the module-level send chain. The
            # composer keeps a working Send button while it holds a draft.
            await composer.fill(_MSG2)
            await send_button.click()

            # No msg2 POST has fired yet — it is blocked on the stalled chain.
            # (If it had, serialization is broken and the repro is invalid.)
            assert all(text != _MSG2 for _, text in event_posts), (
                f"msg2 POSTed before the chain unblocked: {event_posts}"
            )

            # Switch to the running session A via the sidebar link — a
            # client-side navigation that preserves the JS send chain (a full
            # page reload would reset it and dissolve the race).
            await page.locator(f'a[href="/c/{session_a}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

            # Release B's first POST → the chain unblocks and msg2 is sent.
            release_first.set()

            # msg2 must be delivered to B (origin), never to A (now active).
            await _wait_until(lambda: any(text == _MSG2 for _, text in event_posts))
            msg2_targets = [sid for sid, text in event_posts if text == _MSG2]
            assert msg2_targets == [session_b], (
                f"msg2 was composed in session B ({session_b}) and must be "
                f"delivered there, but POST targets were {msg2_targets}. A "
                f"target of {session_a} (session A) is the cross-session leak."
            )
            # And nothing leaked into the running session A at all.
            assert all(sid != session_a for sid, _ in event_posts), (
                f"a message leaked into the active session A: {event_posts}"
            )
        finally:
            await browser.close()
