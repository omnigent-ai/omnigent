"""E2E: mobile sidebar toggle reachability + chat-header tap-target sizes.

Regression guards for the mobile chat header's touch ergonomics. Drives the
real SPA at an iPhone-class viewport (390x844, touch, mobile UA) and checks
two symptoms as a user experiences them:

1. The chat-header "Open sidebar" toggle (top-left) must be tappable and must
   actually open the sidebar drawer -- and the drawer must close from a tap on
   the exposed right strip and reopen from the toggle again.
2. The chat header's icon buttons must meet the ~44px mobile tap-target
   minimum (Apple HIG / WCAG target-size guidance). A press anywhere inside
   the 44px minimum zone centred on a control must activate it; sub-44px hit
   boxes are how "need to tap a couple times" happens on a phone.

The near-miss press in the second test uses a precise pointer
(``page.mouse``), not ``page.touchscreen``: Chromium's touch emulation snaps
near-miss taps onto the nearest target from up to ~8px away (verified while
authoring this test), which would mask the undersized hit box. A precise
press has no such fuzz, so it deterministically probes the geometry that the
44px rule guarantees to imprecise fingers.

Same no-LLM pattern as the sibling mobile tests: the seeded session page
renders its header and drawer deterministically with no model output needed.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, BrowserContext, Page, expect

# iPhone-12/13-class portrait viewport -- comfortably below the Tailwind
# ``md`` breakpoint (768px) so every ``max-md:`` rule is in effect.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Minimum mobile tap target (Apple HIG 44pt; WCAG's enhanced target size).
# Half a pixel of tolerance absorbs subpixel layout rounding.
_MIN_TAP_PX = 44.0
_EPSILON = 0.5

# A visible touch dot for recorded runs, so the filmed journey shows *where*
# the user tapped (Playwright videos otherwise render touch input invisibly).
# pointer-events: none keeps it from ever intercepting the tap itself; gated
# on the recording env var so normal CI runs drive an unmodified page.
_TOUCH_DOT_SCRIPT = """
  window.addEventListener('pointerdown', (e) => {
    const dot = document.createElement('div');
    dot.style.cssText =
      'position:fixed;z-index:2147483647;pointer-events:none;' +
      'width:36px;height:36px;border-radius:50%;' +
      'background:rgba(255,80,80,0.45);border:2px solid rgba(255,80,80,0.9);' +
      `left:${e.clientX - 18}px;top:${e.clientY - 18}px;` +
      'transition:opacity 0.6s ease-out;';
    document.body.appendChild(dot);
    requestAnimationFrame(() => { dot.style.opacity = '0'; });
    setTimeout(() => dot.remove(), 700);
  }, true);
"""


def _new_mobile_page(browser: Browser) -> tuple[BrowserContext, Page]:
    """Open a phone-profile context (+ touch dot overlay when recording)."""
    context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        context.add_init_script(_TOUCH_DOT_SCRIPT)
    return context, context.new_page()


def _beat(page: Page) -> None:
    """Pause briefly between journey steps -- only while filming a clip."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(900)


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Load the session page and prove the mobile layout branch is active."""
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    assert page.evaluate("matchMedia('(max-width: 767.98px)').matches"), (
        "expected the mobile (max-md) layout branch to be in effect"
    )


def test_sidebar_opens_from_header_toggle_on_mobile(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The top-left header toggle opens the drawer; the strip tap closes it.

    Symptom guarded: "Sidebar button isn't clickable on mobile (top-left)."
    The toggle must offer a >=~44px tap target, be the element that actually
    receives a tap at its centre (nothing overlaying it), and drive the full
    drawer cycle: tap -> open, strip tap -> close, tap -> open again.
    """
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        _open_session(page, base_url, session_id)

        toggle = page.get_by_role("button", name="Open sidebar")
        expect(toggle).to_be_visible(timeout=10_000)

        # Tap-target size: the toggle itself must meet the 44px minimum.
        box = toggle.bounding_box()
        assert box is not None
        assert (
            box["width"] >= _MIN_TAP_PX - _EPSILON and box["height"] >= _MIN_TAP_PX - _EPSILON
        ), (
            f"'Open sidebar' toggle hit box is {box['width']:.1f}x"
            f"{box['height']:.1f}px, below the {_MIN_TAP_PX:.0f}px mobile minimum"
        )

        # Reachability: the toggle (not some overlay) receives a centre tap.
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        assert page.evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                const btn = el && el.closest('button');
                return !!(btn && btn.getAttribute('aria-label') === 'Open sidebar');
            }""",
            [cx, cy],
        ), "an overlay intercepts taps on the 'Open sidebar' toggle"

        drawer = page.locator('aside[aria-label="Conversations"]')
        expect(drawer).to_have_attribute("aria-hidden", "true")

        # 1st tap: the drawer must open (this is the reported failure point).
        _beat(page)
        toggle.tap()
        expect(drawer).to_have_attribute("aria-hidden", "false", timeout=5_000)
        expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible()

        # Tap the exposed right strip (the drawer stops 56px short of the
        # right edge; the scrim behind it is the documented way back).
        _beat(page)
        page.get_by_test_id("sidebar-scrim").tap(position={"x": 362, "y": 420})
        expect(drawer).to_have_attribute("aria-hidden", "true", timeout=5_000)

        # 2nd tap: reopening must work too ("need to tap a couple times").
        _beat(page)
        toggle.tap()
        expect(drawer).to_have_attribute("aria-hidden", "false", timeout=5_000)
        expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible()
        _beat(page)
    finally:
        context.close()


def test_header_kebab_meets_mobile_tap_target(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The header 'Conversation actions' kebab offers a >=~44px tap target.

    Symptom guarded: "Navbar buttons too small -- need to tap a couple
    times." The kebab's interactive hit area must cover the 44px minimum tap
    zone centred on it, so an imprecise finger press still activates it.
    Without a mobile size override (``max-md:size-11``) the kebab renders
    40x40 (Button size="icon" = ``size-10 md:size-8``), and a press landing
    in the outer ring of the minimum zone hits dead header space instead.
    """
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        _open_session(page, base_url, session_id)

        kebab = page.get_by_test_id("header-conversation-actions")
        expect(kebab).to_be_visible(timeout=10_000)
        box = kebab.bounding_box()
        assert box is not None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        menu = page.get_by_role("menu")

        # Journey step 1: a dead-centre tap works -- the control itself is
        # functional, the defect is purely its undersized hit area.
        _beat(page)
        page.touchscreen.tap(cx, cy)
        expect(menu).to_be_visible(timeout=5_000)
        _beat(page)
        page.keyboard.press("Escape")
        expect(menu).not_to_be_visible(timeout=5_000)

        # Journey step 2: press inside the 44px minimum zone, 1px outside a
        # 40px hit box (21px left of centre: |offset| > 40/2, < 44/2, with a
        # 1px margin each way). On a compliant >=44px control this activates
        # the menu; on the shrunken one it lands on dead header space -- the
        # mobile mis-tap users reported. Precise pointer, not touchscreen:
        # Chromium touch emulation would snap the near-miss onto the button
        # and mask the bug (see module docstring).
        ring_x, ring_y = cx - 21.0, cy
        _beat(page)
        page.mouse.click(ring_x, ring_y)
        try:
            expect(menu).to_be_visible(timeout=2_000)
        except AssertionError:
            raise AssertionError(
                f"Press at ({ring_x:.1f},{ring_y:.1f}) -- inside the "
                f"{_MIN_TAP_PX:.0f}px minimum tap zone centred on the "
                f"'Conversation actions' kebab -- did not open the menu: the "
                f"kebab's actual hit box is {box['width']:.1f}x"
                f"{box['height']:.1f}px, below the mobile tap-target minimum."
            ) from None
        page.keyboard.press("Escape")

        # Belt and braces: every icon-only button in the chat header meets
        # the minimum, so a future header control is held to the same bar.
        offenders = page.evaluate(
            """(min) => {
                const out = [];
                const header = document.querySelector('.chat-header');
                if (!header) return ['no .chat-header found'];
                for (const el of header.querySelectorAll('button')) {
                    if ((el.textContent || '').trim()) continue;  // icon-only
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;  // hidden
                    if (r.width < min || r.height < min) {
                        const name = el.getAttribute('aria-label')
                            || el.getAttribute('data-testid') || el.outerHTML.slice(0, 60);
                        out.push(`${name}: ${r.width.toFixed(1)}x${r.height.toFixed(1)}`);
                    }
                }
                return out;
            }""",
            _MIN_TAP_PX - _EPSILON,
        )
        assert not offenders, (
            "chat-header icon buttons below the "
            f"{_MIN_TAP_PX:.0f}px mobile tap-target minimum: {offenders}"
        )
    finally:
        context.close()
