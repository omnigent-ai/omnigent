"""E2E (phone viewport): the bottom chrome must carry a working/idle cue once
background tasks are running.

On a phone the end-of-thread "Working…" shimmer is the only working cue, and it
leaves the viewport as soon as the user scrolls up to re-read earlier messages
(or the keyboard shortens the screen). Once a background shell outlives a turn,
the composer stack (workspace bar + composer card) is the one surface that
stays on screen — so it must render differently while the agent's turn is
running than while the session sits idle, both visually and in its
accessibility tree.

Journey (the reporter's, on a narrow viewport):

1. hold a short conversation until the thread overflows the phone viewport,
2. a background shell outlives the turn (the native Stop-hook status edge,
   published through the sessions events route the harness forwarder posts to,
   carrying a positive ``background_task_count``),
3. type a follow-up draft (with a draft the send arrow never morphs into the
   Interrupt square) and scroll up — the shimmer slot leaves the viewport,
4. a new turn starts (status ``running``, e.g. sent from another device),
5. the bottom chrome must still show which state the session is in,
6. the turn settles back to idle — the cue must clear again.

All three signatures are captured at the same draft, focus, and scroll state;
the only difference between them is the session status, so any signature
difference is a working/idle cue. The working signature must differ from BOTH
surrounding idle signatures, so an unrelated async repaint landing mid-journey
(a workspace-bar refresh) can never satisfy the assertion by accident. The
failure this guards: the bottom chrome renders pixel-identical with an
identical accessibility tree in all three states, leaving a phone user (and a
screen reader) no way to tell a working agent from an idle one.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from playwright.sync_api import Page, expect

_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
# Queued strip, trays, workspace bar, and composer card all render inside the
# composer form — the persistent bottom chrome of a session page.
_BOTTOM_CHROME = "form.chat-composer-form"

# iPhone 13-class portrait viewport (matches Playwright's "iPhone 13" device
# profile, so a recorder run with ``--device "iPhone 13"`` films pixel-exact).
_IPHONE_VIEWPORT = {"width": 390, "height": 664}

_DRAFT = "actually, please also update the changelog"

# Tallest scrollable descendant of the conversation log — the StickToBottom
# scroll container.
_FIND_SCROLLER = """
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) {
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  });
"""
_THREAD_OVERFLOW = (
    "() => {" + _FIND_SCROLLER + " return best ? best.scrollHeight - best.clientHeight : 0; }"
)
_SCROLL_THREAD_TO_TOP = "() => {" + _FIND_SCROLLER + " if (best) best.scrollTop = 0; }"


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    background_task_count: int | None = None,
) -> None:
    """Publish a status edge through the sessions events route.

    This is the same path the native harness's status forwarder posts to; an
    omitted count preserves the sticky background-task tally.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"``.
    :param background_task_count: Background shells still running as of this
        edge. ``None`` omits the field.
    """
    data: dict[str, object] = {"status": status}
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_assistant_message(base_url: str, session_id: str, text: str) -> None:
    """Append a deterministic assistant bubble through the events route.

    Used to top up the thread's height when the mock LLM's replies run too
    short for the conversation to overflow a phone viewport.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param text: Assistant message body.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "hello_world", "text": text},
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _send_turn(page: Page, text: str, turn: int) -> None:
    """Send *text* and wait for the *turn*-th round trip to fully render."""
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(turn, timeout=15_000)
    expect(page.locator(_ASSISTANT)).to_have_count(turn, timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)


@dataclass
class _BottomChromeSignature:
    """User-perceivable state of the persistent bottom chrome."""

    aria: str
    pixels: bytes


def _bottom_chrome_signature(page: Page) -> _BottomChromeSignature:
    """Capture the bottom chrome once it paints two identical frames in a row,
    so an in-flight repaint never lands in a signature."""
    chrome = page.locator(_BOTTOM_CHROME)
    expect(chrome).to_be_visible()
    shot = chrome.screenshot(animations="disabled", caret="hide")
    for _ in range(10):
        page.wait_for_timeout(400)
        again = chrome.screenshot(animations="disabled", caret="hide")
        if again == shot:
            break
        shot = again
    return _BottomChromeSignature(aria=chrome.aria_snapshot(), pixels=shot)


def _signatures_with_background_task(
    page: Page,
    seeded_session: tuple[str, str],
) -> tuple[_BottomChromeSignature, _BottomChromeSignature, _BottomChromeSignature]:
    """Drive the journey; return (idle, working, idle-again) signatures."""
    base_url, session_id = seeded_session
    page.set_viewport_size(_IPHONE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()

    # Enough short turns that scrolling up puts the thread's end off screen.
    overflow = 0
    for turn in range(1, 4):
        _send_turn(page, f"Say hello ({turn}).", turn)
        overflow = page.evaluate(_THREAD_OVERFLOW)
    # The mock's replies vary in height; top up with seeded earlier replies
    # until the thread genuinely overflows the viewport.
    seeded = 0
    while overflow <= 300 and seeded < 12:
        seeded += 1
        _seed_assistant_message(
            base_url,
            session_id,
            f"Earlier reply {seeded}.\n\nA few more lines of prior conversation so "
            "the thread grows taller than a phone screen and the end-of-thread "
            "area can scroll out of view.",
        )
        expect(page.get_by_text(f"Earlier reply {seeded}.")).to_be_visible(timeout=15_000)
        overflow = page.evaluate(_THREAD_OVERFLOW)
    assert overflow > 300, f"thread did not overflow the phone viewport (overflow={overflow})"

    # The edge sequence a claude-native turn ends with: `running` for the
    # turn's activity, then the Stop-hook turn-end edge carrying the tally of
    # shells that outlive it. The leading `running` also clears a sticky
    # `failed` the mock lane's turn can leave, which would otherwise swallow
    # the turn-end edge (failed is sticky against trailing idles server-side).
    _publish_status(base_url, session_id, "running")
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    # No UI hook to await: on the buggy build this edge renders nothing at
    # all. Give the SSE edges time to land before snapshotting.
    page.wait_for_timeout(1_500)

    composer.fill(_DRAFT)
    composer.blur()
    page.evaluate(_SCROLL_THREAD_TO_TOP)

    idle = _bottom_chrome_signature(page)

    _publish_status(base_url, session_id, "running")
    working_indicator = page.locator(_WORKING)
    expect(working_indicator).to_be_attached(timeout=15_000)
    # A fresh status edge can re-stick the log to its end; the user is
    # re-reading earlier messages, so scroll back up before the premise check.
    page.evaluate(_SCROLL_THREAD_TO_TOP)
    # The end-of-thread shimmer exists but is off screen, and with a draft the
    # send arrow never morphs into Interrupt — the bottom chrome is all the
    # status surface the screen has left.
    expect(working_indicator).not_to_be_in_viewport()
    expect(page.get_by_role("button", name="Interrupt")).to_have_count(0)

    working = _bottom_chrome_signature(page)

    _publish_status(base_url, session_id, "idle")
    expect(working_indicator).to_have_count(0, timeout=15_000)

    idle_again = _bottom_chrome_signature(page)
    return idle, working, idle_again


def test_phone_bottom_chrome_distinguishes_working_from_idle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    idle, working, idle_again = _signatures_with_background_task(page, seeded_session)
    assert working.pixels != idle.pixels and working.pixels != idle_again.pixels, (
        "With a background task running and the shimmer scrolled off screen, the "
        "bottom chrome (workspace bar + composer) renders pixel-identical while "
        "the agent's turn is running vs while the session sits idle — a phone "
        "user has no working/idle cue anywhere on screen."
    )


def test_phone_bottom_chrome_distinguishes_working_for_screen_readers(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    idle, working, idle_again = _signatures_with_background_task(page, seeded_session)
    assert working.aria != idle.aria and working.aria != idle_again.aria, (
        "With a background task running, the bottom chrome exposes an identical "
        "accessibility tree while the agent's turn is running vs while the "
        "session sits idle — a screen-reader user has no working/idle cue either."
    )
