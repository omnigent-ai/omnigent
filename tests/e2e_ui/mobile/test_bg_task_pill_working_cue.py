"""Mobile: the background-task pill must carry the working/idle state.

Once a background shell is alive, the ``background-task-pill`` above the
composer is the only always-on-screen status surface a phone has: the
end-of-thread "Working…" shimmer scrolls out of the viewport the moment the
user scrolls up to re-read earlier messages (or the keyboard shortens the
screen). The pill therefore has to say whether the agent's turn is running —
visually, and through its accessible name for screen readers.

Regression shape this guards: the pill renders byte-identical DOM and an
identical ``aria-label`` ("N background tasks still running") whether the
session is ``running`` or ``idle``, so once the shimmer leaves the viewport
the screen carries no working/idle cue at all.

Like ``test_working_indicator_background_tasks``, status edges drive the real
Sessions events route (the same path the claude-native forwarder posts to),
so the tests are deterministic — no live LLM turn.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

_WORKING = '[data-testid="working-indicator"]'
_PILL = '[data-testid="background-task-pill"]'
_AGENT_NAME = "hello_world"

# Phone-sized viewport (the reported surface): short enough that a handful of
# messages overflow the thread, so the end-of-thread shimmer can leave the
# viewport when the user scrolls up.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Mirror of WORKING_MESSAGES in web/src/pages/ChatPage.tsx — the shimmer's
# rotating labels. Which one shows depends on the wall-clock bucket, so the
# sanity wait accepts any of them. Keep in sync if that pool changes.
_WORKING_LABELS = (
    "Working…",
    "Cooking…",
    "Crunching…",
    "Tinkering…",
    "Pondering…",
    "Brewing…",
)
_WORKING_LABEL_RE = re.compile("|".join(re.escape(label) for label in _WORKING_LABELS))

# Scroll the conversation to the top. The tallest scrollable descendant of the
# role="log" region is the StickToBottom viewport (same detection as
# test_jump_to_top.py).
_SCROLL_TO_TOP = """
() => {
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) {
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  });
  (best || log).scrollTop = 0;
}
"""

# The pill's full rendered markup — a working state must change SOMETHING here
# (a spinner, a shimmer, a label) for the state to be visible on screen.
_PILL_HTML = "() => document.querySelector('" + _PILL + "').outerHTML"

# The pill's screen-reader presentation: accessible name plus visible text.
_PILL_ACCESS = (
    "() => { const el = document.querySelector('"
    + _PILL
    + "'); return (el.getAttribute('aria-label') || '') + '\\n' + el.innerText; }"
)


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    background_task_count: int | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"idle"``.
    :param background_task_count: Background shells still running as of this
        status edge. ``None`` omits the field (the sticky tally is left
        untouched); an explicit ``0`` is the authoritative Stop-hook clear.
    :returns: None.
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


def _seed_assistant_messages(base_url: str, session_id: str, count: int) -> None:
    """Seed deterministic assistant bubbles so the thread overflows a phone.

    Uses the ``external_assistant_message`` events route (no LLM turn), the
    same seeding ``test_chat_code_block_wrap`` uses.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param count: Number of assistant bubbles to append.
    :returns: None.
    """
    for i in range(count):
        text = (
            f"Earlier reply {i + 1}.\n\n"
            "A few lines of prior conversation so the thread grows taller "
            "than a phone screen and the end-of-thread area can scroll out "
            "of view."
        )
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {"agent": _AGENT_NAME, "text": text},
            },
            timeout=10.0,
        )
        resp.raise_for_status()


def test_pill_shows_working_cue_when_shimmer_scrolls_away(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """While the agent works, the always-visible pill must not look idle.

    Journey: open a session on a phone that has one background shell running
    (the pill shows "1 background task"), a new turn starts (the shimmer
    lights at the end of the thread — the turn is genuinely active), the user
    starts a follow-up draft and scrolls up to re-read earlier messages. The
    shimmer leaves the viewport; the pill — the only status surface still on
    screen — must render differently than it does when the session sits idle.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)

    # A prior turn ended with a background shell still running, and the
    # thread already holds enough messages to overflow a phone screen.
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    _seed_assistant_messages(base_url, session_id, count=12)

    page.goto(f"{base_url}/c/{session_id}")
    pill = page.locator(_PILL)
    working = page.locator(_WORKING)
    expect(pill).to_contain_text("1 background task", timeout=15_000)
    expect(working).to_have_count(0)

    # The idle-with-background-tasks presentation of the pill.
    idle_html = page.evaluate(_PILL_HTML)

    # A new turn starts (the `running` edge a composer send produces): the
    # end-of-thread shimmer lights, so the turn is genuinely active.
    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)

    # The user starts a follow-up draft (with a draft present the send button
    # keeps its plain arrow — no Interrupt morph to lean on) and scrolls up to
    # re-read earlier messages, pushing the end-of-thread shimmer out of the
    # phone viewport. The pill, above the composer and outside the scroll
    # container, stays on screen.
    page.get_by_label("Message the agent").fill("follow-up draft")
    page.evaluate(_SCROLL_TO_TOP)
    expect(working).not_to_be_in_viewport()
    expect(pill).to_be_in_viewport()

    # The pill is now the only status surface on screen, so its rendering
    # must distinguish "agent working" from "sitting idle".
    try:
        page.wait_for_function(
            "(idleHtml) => document.querySelector('" + _PILL + "').outerHTML !== idleHtml",
            arg=idle_html,
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        working_html = page.evaluate(_PILL_HTML)
        pytest.fail(
            "The background-task pill renders identically while the agent is "
            "working and while it sits idle — with the shimmer scrolled out "
            "of the viewport, a phone shows no working/idle cue at all.\n"
            f"idle pill:    {idle_html}\n"
            f"working pill: {working_html}"
        )


def test_pill_accessible_name_distinguishes_working_from_idle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The pill's screen-reader presentation must reflect the working state.

    The pill announces "N background tasks still running" via ``aria-label``
    whether the agent is mid-turn or idle, so a screen-reader user is in the
    same position as the sighted one: no way to tell the two states apart
    from the one persistent status surface.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)

    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    pill = page.locator(_PILL)
    expect(pill).to_contain_text("1 background task", timeout=15_000)
    idle_access = page.evaluate(_PILL_ACCESS)

    # A new turn starts; the shimmer confirms the client resolved the session
    # as working, so the pill's unchanged announcement below is the defect,
    # not a status-plumbing gap.
    _publish_status(base_url, session_id, "running")
    expect(page.locator(_WORKING)).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)

    try:
        page.wait_for_function(
            "(idleAccess) => { const el = document.querySelector('"
            + _PILL
            + "'); return ((el.getAttribute('aria-label') || '') + '\\n' + el.innerText)"
            " !== idleAccess; }",
            arg=idle_access,
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        working_access = page.evaluate(_PILL_ACCESS)
        pytest.fail(
            "The pill exposes the same accessible text while the agent is "
            "working and while it is idle — a screen reader cannot tell the "
            "two states apart.\n"
            f"idle:    {idle_access!r}\n"
            f"working: {working_access!r}"
        )
