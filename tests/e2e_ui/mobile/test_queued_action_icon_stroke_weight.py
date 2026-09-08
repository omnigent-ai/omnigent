"""E2E: queued-row action icons match the composer's icon stroke weight on mobile.

Drives the real SPA at an iPhone-class viewport (390x844, touch, mobile UA),
queues a follow-up while the agent is busy, and measures the *rendered* stroke
thickness (CSS px) of the queued row's steer / edit / delete icons against the
composer's own attach control sitting right below the strip.

All of these icons are lucide glyphs drawn on a 24-unit viewBox with a unit
``stroke-width`` of 2. Rendering one set in a larger box while keeping the same
unit stroke scales its lines up physically (rendered px = unit stroke x
rendered size / viewBox), so the enlarged mobile queued-row icons read visibly
heavier than the composer controls beside them. The queued-row actions must
render with (approximately) the same absolute stroke thickness as the
composer's own icons.

The ``/events`` route is fulfilled by the test itself and no ``session.status``
event ever follows, so the session's local status stays busy and the follow-up
is held in the client-side queue -- the same no-LLM pattern as
``test_queued_row_tap_targets.py``. The queued strip and its per-row actions
then render deterministically, with no dependence on model output.
"""

from __future__ import annotations

import json
import os

from playwright.sync_api import Browser, Route, expect

# iPhone-12-class portrait viewport -- comfortably below the Tailwind ``md``
# breakpoint (768px) so every ``md:``/``max-md:`` rule resolves to its mobile
# branch.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# How much heavier (relative) a queued-row icon's rendered stroke may be than
# the composer baseline before it reads as a different visual weight. The
# unscaled-lucide failure mode is ~25% heavier (20px vs 16px box at the same
# unit stroke), well past this.
_TOLERANCE = 0.10

_MSG1 = "sentinel-stroke-msg1 holds the turn open"
_MSG2 = "sentinel-stroke-msg2 queued follow-up row"

# Accessible names of the queued row's steer / edit / delete actions -- the
# icons the strip shows alongside each queued message.
_ACTION_LABELS = (
    "Send queued message now",
    "Edit queued message",
    "Remove queued message",
)

# Effective on-screen stroke thickness of an SVG icon, in CSS px: the computed
# unit stroke of its drawn shapes scaled by rendered-size / viewBox. Reads the
# first shape child (falling back to the root) so both root-level overrides
# and inherited strokes are captured.
_STROKE_PROBE = """
(svg) => {
  const rect = svg.getBoundingClientRect();
  const vb =
    svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width
      ? svg.viewBox.baseVal.width
      : rect.width;
  const shape =
    svg.querySelector("path, line, polyline, circle, rect, ellipse, polygon") ?? svg;
  const unitStroke = parseFloat(getComputedStyle(shape).strokeWidth);
  return {
    renderedPx: rect.width,
    viewBox: vb,
    unitStroke: unitStroke,
    strokePx: unitStroke * (rect.width / vb),
  };
}
"""


def test_queued_action_icons_match_composer_stroke_weight(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Queued-row steer/edit/delete icons keep the composer's stroke weight.

    Failure mode this catches: on the mobile layout the strip's action icons
    render in a larger box with an unscaled unit stroke, so their lines are
    physically thicker (~1.67px) than the attach/mic controls (~1.33px) right
    below them, reading as visibly heavier, inconsistent iconography.
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

        # Wait for the send to register as a busy turn before queueing the
        # follow-up, so msg2 deterministically lands in the client-side queue.
        expect(composer).to_have_attribute(
            "placeholder", "Send a follow-up (queued) — Esc to stop", timeout=15_000
        )

        # msg2 -> typed while busy -> held in the client-side queue and shown
        # in the docked strip above the composer.
        composer.fill(_MSG2)
        send.click()
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_be_visible(timeout=15_000)
        expect(strip).to_contain_text(_MSG2)

        # Baseline: the composer's attach icon, rendered right below the strip
        # (the mic uses the identical size/style but may not mount in
        # browsers without speech APIs, so attach is the stable reference).
        attach_icon = page.get_by_role("button", name="Attach files").locator("svg")
        expect(attach_icon).to_be_visible()
        baseline = attach_icon.evaluate(_STROKE_PROBE)
        assert baseline["strokePx"] > 0, f"attach icon has no measurable stroke: {baseline}"

        measures: dict[str, dict[str, float]] = {}
        for label in _ACTION_LABELS:
            icon = page.get_by_role("button", name=label).locator("svg")
            expect(icon).to_be_visible()
            measures[label] = icon.evaluate(_STROKE_PROBE)

        # Linger briefly so the queued row -- the state under test -- is
        # plainly visible in journey recordings before assertions run.
        page.wait_for_timeout(1_500)

        limit = baseline["strokePx"] * (1 + _TOLERANCE)
        heavier = {
            label: round(m["strokePx"], 3)
            for label, m in measures.items()
            if m["strokePx"] > limit
        }
        assert not heavier, (
            "queued-row action icons render visibly heavier strokes than the "
            f"composer's controls beside them: {heavier} px vs attach baseline "
            f"{baseline['strokePx']:.3f}px (allowed <= {limit:.3f}px); "
            f"full measurements: baseline={baseline}, actions={measures}"
        )
    finally:
        context.close()
