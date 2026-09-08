"""E2E: queued-row action icons match surrounding composer icon stroke weight.

Drives the real SPA at an iPhone-class viewport (390x844, touch, mobile), queues
a follow-up while the agent is busy, and compares the *effective* stroke weight
of the queued row's steer / edit / delete icons against the composer's own
control icons (attach, plus mic when a dictation backend renders it). Lucide
icons draw a fixed 2-unit stroke in a 24-unit viewBox, so the stroke a user
sees scales with rendered size: effective px = stroke-width x (rendered px /
viewBox units). On mobile the queued-row icons grow (``max-md:size-5`` = 20px)
while the composer control icons stay 16px, so without stroke compensation the
steer / edit / delete icons render visibly heavier strokes than the controls
around them.

The ``/events`` route is fulfilled by the test itself and no ``session.status``
event ever follows, so the session's local status stays busy and the follow-up
is held in the client-side queue -- the same no-LLM pattern as
``test_queued_row_tap_targets.py``. The queued strip and its per-row actions
then render deterministically, with no dependence on model output.
"""

from __future__ import annotations

import json
import os

from playwright.sync_api import Browser, Page, Route, expect

# iPhone-12-class portrait viewport -- comfortably below the Tailwind ``md``
# breakpoint (768px) so every ``md:`` rule resolves to its mobile branch.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Allowed relative spread between a queued-row action icon's effective stroke
# and the surrounding composer icons'. 10% absorbs subpixel/zoom rounding while
# still failing a visible mismatch (20px vs 16px at stroke 2 is ~25% heavier).
_MAX_STROKE_RATIO = 1.10

_MSG1 = "sentinel-stroke-msg1 holds the turn open"
_MSG2 = "sentinel-stroke-msg2 queued follow-up row"

# Accessible names of the queued row's steer / edit / delete actions.
_QUEUED_ACTION_LABELS = (
    "Send queued message now",
    "Edit queued message",
    "Remove queued message",
)

# Composer controls surrounding the queued strip. Attach always renders; the
# mic only renders when a dictation backend is available, so it is measured
# opportunistically.
_COMPOSER_REFERENCE_LABELS = ("Attach files",)
_COMPOSER_OPTIONAL_LABELS = ("Voice dictation",)

# Effective on-screen stroke weight of an SVG icon: the computed stroke-width
# (in viewBox units) scaled by how much the viewBox is magnified on screen.
_EFFECTIVE_STROKE_JS = """
(svg) => {
  const rect = svg.getBoundingClientRect();
  const vb = (svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width)
    ? svg.viewBox.baseVal.width
    : rect.width;
  const strokeWidth = parseFloat(getComputedStyle(svg).strokeWidth);
  return {
    rendered: rect.width,
    viewBox: vb,
    strokeWidth: strokeWidth,
    effective: strokeWidth * (rect.width / vb),
  };
}
"""


def _measure_icon(page: Page, label: str) -> dict[str, float]:
    """Measure the stroke geometry of the icon inside the button named ``label``."""
    button = page.get_by_role("button", name=label)
    expect(button).to_be_visible()
    svg = button.locator("svg").first
    expect(svg).to_be_visible()
    measured: dict[str, float] = svg.evaluate(_EFFECTIVE_STROKE_JS)
    assert measured["effective"] > 0, f"{label!r} icon has no measurable stroke"
    return measured


def test_queued_row_action_icon_stroke_matches_composer(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Steer / edit / delete icons keep the surrounding controls' stroke weight.

    Failure mode this catches: on a phone-sized viewport the queued row's
    action icons render with visibly heavier strokes (larger effective px
    stroke-width) than the composer controls immediately around them, making
    the strip look inconsistent with the rest of the composer.
    """
    base_url, session_id = seeded_session
    context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    page = context.new_page()

    def ack_event(route: Route) -> None:
        # Ack every send; never emit a session.status event, so the SPA's
        # local status stays busy after msg1 and msg2 queues client-side.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_icon_stroke"}),
        )

    page.route("**/v1/sessions/*/events", ack_event)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        # Prove the mobile layout branch is actually in effect.
        assert page.evaluate("matchMedia('(max-width: 767.98px)').matches")

        send = page.get_by_role("button", name="Send", exact=True)

        # msg1 -> POST + acked; the send flips local status to streaming and
        # no idle event ever arrives, so the session stays busy.
        composer.fill(_MSG1)
        send.click()

        # msg2 -> typed while busy -> held in the client-side queue and shown
        # in the docked strip above the composer.
        composer.fill(_MSG2)
        send.click()
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_be_visible(timeout=15_000)
        expect(strip).to_contain_text(_MSG2)

        # Measure the queued row's action icons and the composer's own icons.
        queued = {label: _measure_icon(page, label) for label in _QUEUED_ACTION_LABELS}
        references = {label: _measure_icon(page, label) for label in _COMPOSER_REFERENCE_LABELS}
        for label in _COMPOSER_OPTIONAL_LABELS:
            optional = page.get_by_role("button", name=label)
            if optional.count() > 0 and optional.is_visible():
                references[label] = _measure_icon(page, label)

        # Linger briefly so the queued row -- the state under test -- is
        # plainly visible in journey recordings before assertions run.
        page.wait_for_timeout(1_500)

        # Every queued-row action icon must draw the same effective stroke
        # weight as each surrounding composer icon, within tolerance --
        # neither visibly heavier nor visibly lighter.
        mismatches: list[str] = []
        for q_label, q in queued.items():
            for r_label, r in references.items():
                ratio = max(q["effective"], r["effective"]) / min(q["effective"], r["effective"])
                if ratio > _MAX_STROKE_RATIO:
                    mismatches.append(
                        f"{q_label!r} draws a {q['effective']:.2f}px stroke "
                        f"({q['rendered']:.0f}px icon, stroke-width "
                        f"{q['strokeWidth']:.2f}/{q['viewBox']:.0f} viewBox) vs "
                        f"{r_label!r} at {r['effective']:.2f}px "
                        f"({r['rendered']:.0f}px icon) -- {ratio:.2f}x apart"
                    )
        assert not mismatches, (
            "queued-row action icons render inconsistent stroke weights vs the "
            "surrounding composer controls on mobile:\n  " + "\n  ".join(mismatches)
        )
    finally:
        context.close()
