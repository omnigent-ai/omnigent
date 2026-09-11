"""E2E: ArrowUp-recall of a queued message must not leave the original queued.

Guards the "press up arrow to edit a queued message" journey:

    A first message is sent (and acked), but no ``session.status`` event
    ever follows, so the session's local status stays "streaming" (busy).
    A follow-up typed into the composer is then held in the client-side
    queue — shown in the docked strip, NOT POSTed. Pressing ArrowUp in
    the now-empty composer recalls that queued message's text for
    editing. After editing and re-sending, the queue must hold exactly
    ONE message — the edited one. If the original row survives the
    recall + re-send, the idle flush drains BOTH rows and the agent
    receives the message twice: once stale, once edited.

The failure mode this catches: ArrowUp recall fills the composer from
prompt history but never removes the recalled message from the queue
(only the strip's pencil button dequeues), so "editing" a queued message
duplicates it.

Why async Playwright (not the sync ``page`` fixture): the route handler
inspects and fulfills every ``/events`` POST to record which messages the
SPA sent and when, across interleaved UI actions (send, queue, recall,
re-send). It is a sync test driving the async flow in a fresh thread
(see :func:`_run_in_fresh_loop`) because the suite's many sync
pytest-playwright tests leave the main-thread loop in a state where
pytest-asyncio can't start one.

The route handler fulfills every ``/events`` POST itself, so no real
turn runs and the test needs no working LLM — the session never goes
idle, so nothing can drain the queue behind the assertions' back.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_COMPOSER_PLACEHOLDER = "Send a message…"
_MSG1 = "sentinel-uparrow-msg1-3e7c first message, holds the turn open"
_QUEUED = "sentinel-uparrow-queued-9a2d follow-up recalled for editing"
_EDITED = _QUEUED + " (edited-5c1b)"

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


def test_uparrow_edit_of_queued_message_dequeues_original(
    seeded_session: tuple[str, str],
) -> None:
    """ArrowUp-recall + re-send of a queued message leaves one queued row.

    Failure mode this catches: after recalling a queued message with
    ArrowUp, editing it, and re-sending, the strip holds BOTH the stale
    original and the edited copy — the original was never dequeued, so
    the idle flush would send the message twice.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_uparrow_edit(base_url, session_id))


async def _drive_uparrow_edit(base_url: str, session_id: str) -> None:
    """Async body of the ArrowUp-edit test. See the test docstring.

    :param base_url: Spawned server base URL.
    :param session_id: The seeded, runner-bound session.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            # Every (session_id, text) POSTed to a /events endpoint. Each is
            # acked immediately; no session.status event ever follows, so the
            # session's local status stays "streaming" (busy) after msg1 —
            # which is what makes the follow-ups queue instead of send.
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

            # The follow-up → typed while busy → held in the client-side queue,
            # shown in the docked strip, NOT POSTed.
            await composer.fill(_QUEUED)
            await send_button.click()
            strip = page.get_by_test_id("composer-queued-strip")
            await strip.wait_for(state="visible", timeout=15_000)
            await expect(strip.get_by_text(_QUEUED, exact=True)).to_be_visible()
            assert all(text != _QUEUED for _, text in event_posts), (
                f"queued follow-up was POSTed (should be held client-side): {event_posts}"
            )

            # ArrowUp in the empty composer recalls the queued message's text
            # for editing (the up-arrow edit affordance under test).
            await composer.click()
            await composer.press("ArrowUp")
            await expect(composer).to_have_value(_QUEUED, timeout=5_000)
            # Brief hold so the recalled-for-editing state is visible on film.
            await page.wait_for_timeout(500)

            # Edit the recalled text and re-send. The session is still busy, so
            # the edited message queues — and the ORIGINAL row must leave the
            # queue, or the idle flush would deliver the message twice.
            await composer.fill(_EDITED)
            await send_button.click()
            await expect(strip.get_by_text(_EDITED, exact=True)).to_be_visible(timeout=10_000)

            # THE BUG: the stale original stays queued alongside the edit.
            # Editing via ArrowUp must behave like the strip's pencil edit —
            # the recalled message leaves the queue.
            await expect(strip.get_by_text(_QUEUED, exact=True)).to_have_count(0, timeout=5_000)

            # Exactly one queued row remains — the edited one — and nothing
            # was POSTed behind the queue's back (the session never went idle).
            await expect(strip.get_by_text(_EDITED, exact=True)).to_have_count(1)
            assert all(text == _MSG1 for _, text in event_posts), (
                f"a queued message was POSTed without an idle flush: {event_posts}"
            )
        finally:
            # Close the context before the browser so a recorded video (when
            # the conftest injects record_video_dir) is fully flushed to disk
            # even on a failing run.
            await page.context.close()
            await browser.close()
