"""E2E: growing the composer must leave the transcript above it untouched.

The composer auto-grows by collapsing its textarea to ``height: auto`` and
reading the content height back (``hooks/useAutoGrowTextarea.ts``). That
collapse lasts one layout pass, during which the composer is one row tall and
the transcript's scroll viewport is correspondingly taller — so the browser
clamps the transcript's ``scrollTop`` against the taller viewport's smaller
maximum. The clamp survives the composer springing back, which shunted the
whole transcript down a line on every re-measure: Shift+Enter, and then every
keystroke once the composer was multi-line.

A second, subtler source of movement is the composer's height itself: while it
was a plain flex sibling, every extra row stole height from the transcript's
scroll viewport. The messages could be held still through that, but the native
scrollbar (drawn from ``clientHeight``/``scrollHeight``) and the turn rail
(centered on the same box) still jittered. The composer now floats its extra
rows over the transcript instead, so that box never changes.

Layout regressions like these are invisible below the browser — jsdom has no
layout, so ``scrollHeight``/``clientHeight`` are 0 there and neither the clamp
nor the viewport arithmetic happens at all. The assertions pin what has to hold
at once: the messages, the scroll geometry, and the rail ticks all stay exactly
where they were, and the transcript stays stuck to the bottom so a streaming
reply still follows.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

_TEXT_SECTION = '[data-testid="assistant-text-section"]'

# Geometry of the transcript and the composer, read together so a measurement
# can't straddle a layout change.
#
# ``distanceFromBottom`` is 0-or-1 while the transcript is stuck to the bottom
# (use-stick-to-bottom parks it one pixel short of the maximum) and grows once
# a clamp has knocked it loose. ``viewport`` is the scroll geometry the native
# scrollbar is drawn from — any change there moves the thumb, which reads as
# jitter next to a composer that's only supposed to be growing. ``railTicks``
# is the left-edge turn minimap, centered on the same box.
_PROBE = """() => {
    const ta = document.querySelector('textarea[aria-label="Message the agent"]');
    const scroller = ta.closest('form').parentElement.querySelector('[role="log"] > div');
    const rail = document.querySelector('.turn-rail-fade');
    return {
        messageTops: [...document.querySelectorAll('[data-testid="assistant-text-section"]')]
            .map((section) => Math.round(section.getBoundingClientRect().top)),
        composerHeight: Math.round(ta.getBoundingClientRect().height),
        distanceFromBottom: Math.round(
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop),
        viewport: [scroller.clientHeight, scroller.scrollHeight, Math.round(scroller.scrollTop)],
        railTicks: rail
            ? [...rail.querySelectorAll('button')]
                  .map((tick) => Math.round(tick.getBoundingClientRect().top))
            : null,
    };
}"""


def _settled_geometry(page: Page, timeout_s: float = 15.0) -> dict:
    """Read :data:`_PROBE` once two consecutive reads agree.

    The layout settles through several passes — the composer's measurement, the
    trailing spacer's ResizeObserver, then stick-to-bottom — and how long that
    takes varies with CI load. Polling for a stable read beats sleeping a fixed
    guess: it can't return mid-settle, and it doesn't pay a fixed cost once the
    layout is already quiet.

    :param page: Playwright page on the chat surface.
    :param timeout_s: How long to keep polling before giving up.
    :returns: The probe reading, once stable.
    :raises AssertionError: If the layout never stops changing.
    """
    previous = page.evaluate(_PROBE)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
        current = page.evaluate(_PROBE)
        if current == previous:
            return current
        previous = current
    raise AssertionError(f"layout never settled; last reading: {previous}")


def test_composer_growth_does_not_shift_transcript(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Newlines, typing, and deletions all leave the transcript where it was."""
    base_url, session_id = seeded_session
    # Enough turns to overflow the viewport: the transcript has to be
    # scrollable for a scrollTop clamp to have anywhere to land, and each turn
    # is one tick on the rail.
    for i in range(6):
        seed_committed_turn(
            session_id,
            prompt=f"Question {i}?",
            reply=f"Paragraph {i}. " + ("filler sentence for height. " * 12),
            response_id=f"resp_{i}",
        )

    page.goto(f"{base_url}/c/{session_id}")

    sections = page.locator(_TEXT_SECTION)
    expect(sections).to_have_count(6, timeout=30_000)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    # The baseline is the resting layout: the initial scroll-to-bottom and the
    # trailing spacer's measurement have both landed.
    baseline = _settled_geometry(page)
    assert baseline["distanceFromBottom"] <= 1, baseline
    assert baseline["railTicks"], baseline

    def assert_transcript_idle(state: dict, label: str) -> None:
        """Everything but the composer itself is byte-identical to baseline."""
        for key in ("messageTops", "viewport", "railTicks"):
            assert state[key] == baseline[key], (label, key, baseline[key], state[key])
        assert state["distanceFromBottom"] <= 1, (label, state)

    # Grow: three Shift+Enter newlines, one line taller each.
    for _ in range(3):
        composer.press("Shift+Enter")
    grown = _settled_geometry(page)
    assert grown["composerHeight"] > baseline["composerHeight"], grown
    assert_transcript_idle(grown, "after newlines")

    # Type into the now-multi-line composer: the height doesn't change, but the
    # hook re-measures — and used to clamp the transcript on every keystroke.
    composer.type("hello", delay=30)
    assert_transcript_idle(_settled_geometry(page), "after typing")

    # Shrink back: deleting the newlines returns the composer to one row and
    # still leaves the transcript untouched.
    for _ in range(len("hello") + 3):
        composer.press("Backspace")
    shrunk = _settled_geometry(page)
    assert shrunk["composerHeight"] == baseline["composerHeight"], (baseline, shrunk)
    assert_transcript_idle(shrunk, "after deleting")
