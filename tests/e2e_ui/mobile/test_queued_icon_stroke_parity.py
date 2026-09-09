"""E2E: queued-row action icons match surrounding composer icon geometry.

Drives the real SPA at an iPhone-class viewport (390x844, touch, mobile), queues
a follow-up while the agent is busy, and compares the queued row's action icons
against the composer's attach icon. The actions keep 44px touch targets while
their visible glyph size and default Lucide stroke match the 16px composer
controls beside them.

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

_SIZE_TOLERANCE_PX = 0.5
_STROKE_TOLERANCE = 0.05

_MSG1 = "sentinel-geometry-msg1 holds the turn open"
_MSG2 = "sentinel-geometry-msg2 queued follow-up row"

# Accessible names of the queued row's interactive actions.
_QUEUED_ACTION_LABELS = (
    "Reorder queued message",
    "Send queued message now",
    "Edit queued message",
    "Remove queued message",
)

_ICON_GEOMETRY_JS = """
(svg) => {
  const rect = svg.getBoundingClientRect();
  const shape = svg.querySelector("path, line, polyline, circle, rect, ellipse, polygon") ?? svg;
  return {
    width: rect.width,
    height: rect.height,
    strokeWidth: parseFloat(getComputedStyle(shape).strokeWidth),
  };
}
"""


def _measure_icon(page: Page, label: str) -> dict[str, float]:
    """Measure the visible icon inside the button named ``label``."""
    button = page.get_by_role("button", name=label)
    expect(button).to_be_visible()
    svg = button.locator("svg").first
    expect(svg).to_be_visible()
    measured: dict[str, float] = svg.evaluate(_ICON_GEOMETRY_JS)
    assert measured["strokeWidth"] > 0, f"{label!r} icon has no measurable stroke"
    return measured


def test_queued_row_action_icon_geometry_matches_composer(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Queued-row glyphs match the surrounding composer control geometry.

    Failure mode this catches: enlarging the visible queued-row glyphs along
    with their touch targets makes the strip dominate the composer visually.
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
            body=json.dumps({"queued": True, "item_id": "ci_icon_geometry"}),
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

        queued = {label: _measure_icon(page, label) for label in _QUEUED_ACTION_LABELS}
        reference = _measure_icon(page, "Attach files")

        # Linger briefly so the queued row -- the state under test -- is
        # plainly visible in journey recordings before assertions run.
        page.wait_for_timeout(1_500)

        mismatches: list[str] = []
        for label, measured in queued.items():
            if abs(measured["width"] - reference["width"]) > _SIZE_TOLERANCE_PX:
                mismatches.append(
                    f"{label!r} is {measured['width']:.2f}px wide vs "
                    f"the attach icon at {reference['width']:.2f}px"
                )
            if abs(measured["height"] - reference["height"]) > _SIZE_TOLERANCE_PX:
                mismatches.append(
                    f"{label!r} is {measured['height']:.2f}px tall vs "
                    f"the attach icon at {reference['height']:.2f}px"
                )
            if abs(measured["strokeWidth"] - reference["strokeWidth"]) > _STROKE_TOLERANCE:
                mismatches.append(
                    f"{label!r} uses stroke-width {measured['strokeWidth']:.2f} vs "
                    f"the attach icon at {reference['strokeWidth']:.2f}"
                )
        assert not mismatches, (
            "queued-row action icons do not match the composer controls on mobile:\n  "
            + "\n  ".join(mismatches)
        )
    finally:
        context.close()
