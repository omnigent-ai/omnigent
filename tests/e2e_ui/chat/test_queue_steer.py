"""E2E: steering a queued message sends it NOW, mid-turn.

Guards the steer affordance of the client-side queue:

    A first message is sent (and acked), but no ``session.status`` event
    ever follows, so the session's local status stays "streaming"
    (busy). A follow-up typed into the composer is then held in the
    client-side queue — shown in the docked strip, NOT POSTed (the idle
    auto-flush never fires because idle never comes). Clicking the row's
    "Steer" button POSTs it immediately — the only thing that could send
    it here, since the session never went idle.

Why async Playwright (not the sync ``page`` fixture): the route handler
inspects and fulfills every ``/events`` POST to record which messages the
SPA sent and when, across interleaved UI actions (send, queue, steer). It
is a sync test driving the async flow in a fresh thread (see
:func:`_run_in_fresh_loop`) because the suite's many sync
pytest-playwright tests leave the main-thread loop in a state where
pytest-asyncio can't start one.

The route handler fulfills every ``/events`` POST itself, so no real turn
runs and the test needs no working LLM — it asserts purely on when (and
whether) the SPA POSTs the steered message.
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
_MSG1 = "sentinel-steer-msg1-2b8d first message, holds the turn open"
_MSG2 = "sentinel-steer-msg2-6f4a queued then steered"

_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception is captured and re-raised on the calling
    thread so the test fails normally.

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


def test_steer_sends_queued_message_while_busy(
    seeded_session: tuple[str, str],
) -> None:
    """Steering a queued message POSTs it immediately, mid-turn.

    Failure mode this catches: steer does nothing (message stays queued),
    or the message is only sent after the turn ends (indistinguishable
    from auto-flush) — either means the steer path is broken.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_steer(base_url, session_id))


async def _drive_steer(base_url: str, session_id: str) -> None:
    """Async body of the steer test. See the test docstring.

    :param base_url: Spawned server base URL.
    :param session_id: The seeded, runner-bound session.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint. Each is
            # acked immediately; no session.status event ever follows, so the
            # session's local status stays "streaming" (busy) after msg1 —
            # which is what makes the follow-up queue instead of send.
            event_posts: list[tuple[str, str]] = []

            async def handle_events(route: Route) -> None:
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                sid = match.group(1)
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                event_posts.append((sid, text))
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            await page.route("**/v1/sessions/*/events", handle_events)

            await page.goto(f"{base_url}/c/{session_id}")
            composer = page.get_by_label("Message the agent")
            await page.get_by_placeholder(_COMPOSER_PLACEHOLDER).wait_for(
                state="visible", timeout=15_000
            )
            send_button = page.get_by_role("button", name="Send", exact=True)

            # msg1 → POST + acked; the send flips local status to streaming and
            # no idle event arrives, so the session stays busy.
            await composer.fill(_MSG1)
            await send_button.click()
            await _wait_until(lambda: any(text == _MSG1 for _, text in event_posts))

            # msg2 → typed while busy → held in the client-side queue, shown in
            # the docked strip, NOT POSTed (auto-flush only fires on idle, which
            # never comes here).
            await composer.fill(_MSG2)
            await send_button.click()
            await page.get_by_test_id("composer-queued-strip").wait_for(
                state="visible", timeout=15_000
            )
            assert all(text != _MSG2 for _, text in event_posts), (
                f"msg2 was POSTed before steer (should be held client-side): {event_posts}"
            )

            # Steer msg2 → it must POST now, even though the session never went
            # idle. This is what distinguishes steer from the idle auto-flush:
            # the only reason msg2 could POST here is the explicit steer.
            await page.get_by_role("button", name="Send queued message now").click()
            await _wait_until(lambda: any(text == _MSG2 for _, text in event_posts))

            # The steered message left the queue (strip empties).
            await page.get_by_test_id("composer-queued-strip").wait_for(
                state="hidden", timeout=15_000
            )
        finally:
            await browser.close()
