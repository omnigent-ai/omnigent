"""E2E: a message queued in one session never leaks into another.

Guards the cross-session message-routing regression under the client-side
queue model:

    Session B is busy (its first message's POST is held open, so B stays
    "streaming"), so a follow-up typed into B is held in B's client-side
    queue — NOT POSTed. While it waits there, the user switches to a
    different, idle session A. The queued message MUST stay bound to B: it
    must never be POSTed to A (the now-active session).

The queue is a per-conversation client-side buffer: ``maybeFlushQueuedHead``
only flushes the head whose ``conversationId`` matches the bound session, so a
message composed in B cannot be addressed to A. This test pins that no-leak
guarantee. (The positive path — a queued head flushing to its own session on
idle, in FIFO order — is covered by the ``chatStore`` unit tests.)

Why async Playwright (not the sync ``page`` fixture): the test inspects the
body of every ``/events`` POST via a route handler and asserts on which
session each was addressed to, across interleaved UI actions (send, switch
sessions, switch back). The route handler fulfills every POST itself, so no
real turn runs and the test needs no working LLM. It is a sync test driving
the async flow in a fresh thread (see :func:`_run_in_fresh_loop`) because the
suite's many sync pytest-playwright tests leave the main-thread loop in a
state where pytest-asyncio can't start one. Session switches are driven via
the in-app sidebar link (client-side navigation), NOT ``page.goto`` — a full
reload would reset the JS module state (the client-side queue lives in the
store) and dissolve the scenario under test.
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


def test_queued_message_stays_bound_to_origin_session(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A follow-up queued in B reaches B, never the active session A.

    Failure mode this catches: the queued ``_MSG2`` POST is addressed to
    session A (the now-active session) instead of session B (where it was
    composed) — a message leaking into the wrong, unrelated session.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_cross_session_routing(base_url, session_a, session_b))


async def _drive_cross_session_routing(base_url: str, session_a: str, session_b: str) -> None:
    """Async body of the cross-session routing test. See the test docstring.

    :param base_url: Spawned server base URL.
    :param session_a: The idle session the user switches to.
    :param session_b: The session both messages are composed in.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint.
            event_posts: list[tuple[str, str]] = []
            # Held so B's first POST stays in flight → the local send lifecycle
            # keeps B "streaming", so the follow-up queues instead of sending.
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
                # Hold ONLY B's first message open, so B stays busy (streaming)
                # while the follow-up is typed and queued.
                if session_id == session_b and not first_b_post_held:
                    first_b_post_held = True
                    await release_first.wait()
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            await page.route("**/v1/sessions/*/events", handle_events)

            # Start in session B.
            await page.goto(f"{base_url}/c/{session_b}")
            # Locate the textarea by its stable aria-label, not the
            # placeholder — the placeholder changes once a turn starts
            # streaming ("Send a follow-up (queued)…").
            composer = page.get_by_label("Message the agent")
            await page.get_by_placeholder(_COMPOSER_PLACEHOLDER).wait_for(
                state="visible", timeout=15_000
            )
            send_button = page.get_by_role("button", name="Send", exact=True)

            # msg1 → POST to B, held open by the route handler → B stays busy.
            await composer.fill(_MSG1)
            await send_button.click()
            await _wait_until(lambda: first_b_post_held)

            # msg2 → typed while B is busy → held in B's client-side queue,
            # shown in the docked strip, NOT POSTed.
            await composer.fill(_MSG2)
            await send_button.click()
            await page.get_by_test_id("composer-queued-strip").wait_for(
                state="visible", timeout=15_000
            )
            assert all(text != _MSG2 for _, text in event_posts), (
                f"msg2 was POSTed while queued (should be held client-side): {event_posts}"
            )

            # Switch to the idle session A via the sidebar link — a client-side
            # navigation that preserves the store (a full reload would drop the
            # queue). msg2 must NOT flush into A.
            await page.locator(f'a[href="/c/{session_a}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))
            # Release B's first POST so the send lifecycle can settle; the queued
            # msg2 is bound to B, so switching to A must not flush it there.
            release_first.set()
            # Give any errant flush a chance to fire before asserting the
            # negative (the queue head is bound to B, so nothing should POST).
            await asyncio.sleep(1.0)
            assert all(text != _MSG2 for _, text in event_posts), (
                f"msg2 leaked out of B while A was active: {event_posts}"
            )
            assert all(sid != session_a for sid, _ in event_posts), (
                f"a message leaked into the active session A: {event_posts}"
            )
        finally:
            await browser.close()
