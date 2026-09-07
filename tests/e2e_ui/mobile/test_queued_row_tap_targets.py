"""E2E: queued-message action row is tappable on a phone-sized viewport.

Drives the real SPA at an iPhone-class viewport (390x844, touch, mobile UA),
queues a follow-up while the agent is busy, and measures the queued row's
interactive controls (drag handle, steer, edit, delete). Mobile tap targets
must be at least ~44x44 CSS px (Apple HIG / WCAG target-size guidance), and
adjacent actions must not be packed so closely that a finger tap is ambiguous.

The ``/events`` route is fulfilled by the test itself and no
``session.status`` event ever follows, so the session's local status stays
busy and the follow-up is held in the client-side queue -- the same no-LLM
pattern as ``test_queue_steer.py``. The queued strip and its per-row actions
then render deterministically, with no dependence on model output.
"""

from __future__ import annotations

import itertools
import json
import os

from playwright.sync_api import Browser, Route, expect

# iPhone-12-class portrait viewport -- comfortably below the Tailwind ``md``
# breakpoint (768px) so every ``md:`` rule resolves to its mobile branch.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Minimum mobile tap target (Apple HIG 44pt; WCAG's enhanced target size).
# Half a pixel of tolerance absorbs subpixel layout rounding.
_MIN_TAP_PX = 44.0
_EPSILON = 0.5

_MSG1 = "sentinel-tap-msg1 holds the turn open"
_MSG2 = "sentinel-tap-msg2 queued follow-up row"

# Accessible names of the queued row's interactive controls, left to right:
# drag handle, then the right-side steer / edit / delete cluster.
_ACTION_LABELS = (
    "Reorder queued message",
    "Send queued message now",
    "Edit queued message",
    "Remove queued message",
)


def test_queued_row_controls_meet_mobile_tap_target(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Every queued-row control offers a >=~44px tap target on mobile.

    Failure mode this catches: the drag handle and the steer/edit/delete
    buttons render icon-sized (~18x18px) hit areas packed ~24px apart on a
    phone viewport, making them difficult to activate accurately by touch.
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
            body=json.dumps({"queued": True, "item_id": "ci_tap_targets"}),
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

        # Exercise the second send after the background terminal mounts;
        # its scrollbar must not intercept the composer while hidden.
        warm_terminal = page.get_by_test_id("main-terminal-view")
        expect(warm_terminal).to_have_attribute("data-visible", "false", timeout=15_000)
        expect(warm_terminal.locator(".xterm")).to_be_attached(timeout=15_000)
        expect(warm_terminal).to_have_attribute("inert", "")

        # msg2 -> typed while busy -> held in the client-side queue and shown
        # in the docked strip above the composer.
        composer.fill(_MSG2)
        send.click()
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_be_visible(timeout=15_000)
        expect(strip).to_contain_text(_MSG2)

        boxes: dict[str, dict[str, float]] = {}
        for label in _ACTION_LABELS:
            control = page.get_by_role("button", name=label)
            expect(control).to_be_visible()
            box = control.bounding_box()
            assert box is not None, f"{label!r} has no bounding box"
            boxes[label] = box

        # Linger briefly so the queued row -- the state under test -- is
        # plainly visible in journey recordings before assertions run.
        page.wait_for_timeout(1_500)

        undersized = {
            label: (round(box["width"], 1), round(box["height"], 1))
            for label, box in boxes.items()
            if box["width"] < _MIN_TAP_PX - _EPSILON or box["height"] < _MIN_TAP_PX - _EPSILON
        }
        assert not undersized, (
            f"queued-row controls below the ~{_MIN_TAP_PX:.0f}x{_MIN_TAP_PX:.0f}px "
            f"mobile tap target (width, height): {undersized}"
        )

        # Adjacent right-side actions (steer -> edit -> delete) must not be
        # packed so closely that a finger tap is ambiguous: with >=44px-wide
        # targets that do not overlap, adjacent centers sit >=~44px apart.
        cluster = [boxes[label] for label in _ACTION_LABELS[1:]]
        for left, right in itertools.pairwise(cluster):
            left_cx = left["x"] + left["width"] / 2
            right_cx = right["x"] + right["width"] / 2
            spacing = right_cx - left_cx
            assert spacing >= _MIN_TAP_PX - _EPSILON, (
                "adjacent queued-row actions are packed too closely for "
                f"touch: centers only {spacing:.1f}px apart"
            )
    finally:
        context.close()
