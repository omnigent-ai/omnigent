"""E2E: pressing Esc to stop a running turn must KEEP the just-sent prompt.

User journey reproduced here:

1. open a session and type a prompt into the composer,
2. press Enter to send it -- the prompt renders immediately as a user
   bubble in the transcript (an optimistic ``pendingUserMessages`` entry)
   and the local status latches to ``streaming``,
3. press the ``Escape`` key to stop the running agent (the keyboard
   equivalent of the composer's Stop/Interrupt button),
4. observe the transcript: the just-sent prompt VANISHES.

The reported contract: pressing Esc to stop the agent should
leave the sent prompt in the chat session UI -- the same way clicking the
composer's "stop running" button does -- so the user never loses the
prompt they just sent.

Root-cause lead (for the fix step, not asserted here): the composer's
``Escape`` handler and the Stop button both call ``chatStore.stop()``,
which unconditionally does ``pendingUserMessages: []`` -- so any prompt
still optimistic (POSTed but not yet reconciled by
``session.input.consumed``) is dropped from the transcript. Escape makes
this trivially reproducible because ``status`` latches to ``streaming``
synchronously on send, so the interrupt fires while the bubble is still
pending.

How the pending state is held deterministically: a Playwright route
fulfills the ``type: "message"`` POST to ``/events`` locally with a
``queued`` ack (no ``session.input.consumed`` ever follows), so the
optimistic bubble stays pending -- exactly the transient state the real
send->interrupt race produces, but without racing a sub-second turn. The
same route fulfills the ``type: "interrupt"`` POST that ``stop()`` fires,
which is recorded to prove Esc actually triggered the interrupt.

Async Playwright (not the sync ``page`` fixture) so the e2e_ui suite's
autouse ``_record_video`` hook -- which patches the async ``Browser`` --
films the journey when ``OMNIGENT_E2E_RECORD_DIR`` is set. Driven in a
fresh thread/loop (:func:`_run_in_fresh_loop`) because the sync
pytest-playwright tests in this suite leave the main-thread loop unusable
for a new one (mirrors ``test_queue_steer`` / ``test_always_steer``).

On a build with the bug this FAILS at the final assertion: the prompt is
gone from the transcript after Esc. With the fix (``stop()`` preserving
the pending prompt) it PASSES::

    pytest tests/e2e_ui/chat/test_escape_stop_keeps_prompt.py
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Route, async_playwright, expect

_COMPOSER_PLACEHOLDER = "Send a message…"
_COMPOSER_LABEL = "Message the agent"
# Distinctive so the user bubble is unambiguous in the transcript.
_PROMPT = "sentinel prompt — keep me when Esc stops the agent"

_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception is captured and re-raised on the calling
    thread so the test fails normally.
    """
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 15.0) -> None:
    """Poll ``predicate`` on the event loop until true or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def test_escape_stop_keeps_the_just_sent_prompt(
    seeded_session: tuple[str, str],
) -> None:
    """Esc-to-stop must not delete the prompt the user just sent.

    Failure mode this catches: pressing Esc interrupts the turn but also
    wipes the optimistic user bubble, so the prompt the user typed and
    sent disappears from the chat with no undo.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_escape_stop(base_url, session_id))


async def _drive_escape_stop(base_url: str, session_id: str) -> None:
    """Async body of the Esc-stop journey. See the module docstring."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # No explicit record_video_dir: the suite's autouse `_record_video`
        # hook injects it from OMNIGENT_E2E_RECORD_DIR so the journey films.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Every /events POST this SPA sends, by type. The message POST is
            # fulfilled locally with a `queued` ack so no
            # `session.input.consumed` ever follows -- the optimistic bubble
            # stays pending (the real send->interrupt race window, held open
            # deterministically). The interrupt POST that stop() fires is
            # recorded to prove Esc triggered the interrupt, then acked too.
            posts: list[str] = []

            async def handle_events(route: Route) -> None:
                request = route.request
                if request.method != "POST" or urlparse(request.url).path != (
                    f"/v1/sessions/{session_id}/events"
                ):
                    await route.continue_()
                    return
                body = request.post_data_json
                posts.append(body.get("type", "") if isinstance(body, dict) else "")
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_escape_stop"}),
                )

            await page.route(f"**/v1/sessions/{session_id}/events", handle_events)

            await page.goto(f"{base_url}/c/{session_id}")
            composer = page.get_by_label(_COMPOSER_LABEL)
            await expect(composer).to_be_visible(timeout=30_000)
            await expect(page.get_by_placeholder(_COMPOSER_PLACEHOLDER)).to_be_visible(
                timeout=30_000
            )

            # 1-2. Type and send. The prompt renders immediately as a pending
            # user bubble and the local status latches to `streaming`.
            await composer.fill(_PROMPT)
            await page.get_by_role("button", name="Send", exact=True).click()

            prompt_bubble = page.locator(_USER_BUBBLE, has_text=_PROMPT)
            await expect(prompt_bubble).to_be_visible(timeout=15_000)
            await _wait_until(lambda: posts == ["message"])
            # Let the sent prompt sit on screen so the clip clearly shows it
            # before the interrupt.
            await page.wait_for_timeout(800)

            # 3. Press Esc to stop the running agent (keyboard equivalent of
            # the composer's Stop button). Focuses the composer textarea first.
            await composer.press("Escape")

            # Esc must actually trigger the interrupt (else a passing final
            # assertion would be meaningless): stop() fires a
            # `type: "interrupt"` POST to /events.
            await _wait_until(lambda: "interrupt" in posts)

            # Let the post-interrupt transcript settle (and film the outcome).
            await page.wait_for_timeout(1_500)

            # Contract: the just-sent prompt must SURVIVE the
            # Esc-stop, matching the Stop button's "keeps the prompt in the
            # chat" behavior. On the buggy build stop() has cleared
            # pendingUserMessages, so the bubble is gone and this fails; the
            # fix keeps it.
            await expect(prompt_bubble).to_be_visible(timeout=4_000)
        finally:
            # Finalize the video even when the drive fails, so a failed take
            # still yields footage.
            await context.close()
            await browser.close()
