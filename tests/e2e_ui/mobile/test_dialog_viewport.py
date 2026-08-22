"""Phone-viewport dialog geometry: the panel fits the band the user can SEE.

A dialog is ``position: fixed``, so it anchors to the LAYOUT viewport — which
stays tall while the mobile URL bar or the soft keyboard cover its bottom.
``DialogContent`` (``web/src/components/ui/dialog.tsx``) therefore sizes and
centers against ``--omnigent-dialog-max-height`` / ``--omnigent-dialog-center``,
which ``index.css`` derives from the live ``visualViewport`` metrics
``useVisibleViewportHeight`` publishes as ``--omnigent-viewport-height`` /
``--omnigent-viewport-offset``.

The Vitest suites for that chain assert Tailwind class *strings* in jsdom, which
resolves no custom properties and does no layout: a typo'd var name or a sign
error in the ``calc()`` leaves every one of them green while the shipped dialog
computes ``max-height: none`` and hangs off the bottom of the screen. These
tests are the half only a real engine can prove:

  - the var chain RESOLVES to concrete pixels (``getComputedStyle`` gives a
    ``px`` cap, not ``none``), and that cap is smaller than the visible band;
  - the panel's box lands inside the band;
  - the footer button's box lands inside it too — and then actually takes a
    ``click()``. Playwright refuses to click a covered or off-screen element,
    so a landed click that fires the action is the reachability proof;
  - shrinking the published viewport (what the soft keyboard does) shrinks the
    panel with it, which is what tells a visible-viewport-derived value apart
    from a static ``85vh`` / ``top-1/4`` — with no browser chrome showing, the
    two resolve to the same pixels.

The reference band is read FROM THE PAGE (``window.visualViewport``) rather than
from the harness viewport, so every assertion is against exactly what the fix
measures.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, ViewportSize, expect

# iPhone-14-Pro-class portrait, and the same width shrunk to a height that
# forces long dialogs to overflow (roughly what an open soft keyboard leaves).
_PHONE: ViewportSize = {"width": 390, "height": 844}
_SHORT: ViewportSize = {"width": 390, "height": 500}

# The visible band in layout-viewport coordinates: what
# `useVisibleViewportHeight` publishes and `index.css` folds into
# `--omnigent-visible-*`. Read from the page, not from `_PHONE`, so the
# assertions are against what the fix itself measures.
_VISIBLE_BAND = "() => [window.visualViewport.height, window.visualViewport.offsetTop]"

# Stand in for the soft keyboard by shrinking the published height directly:
# the hook writes this same inline custom property from `visualViewport`'s
# resize, which headless Chromium offers no way to fire.
_PUBLISH_VIEWPORT_HEIGHT = (
    "(px) => document.documentElement.style.setProperty('--omnigent-viewport-height', px + 'px')"
)

# Slack for sub-pixel layout rounding when comparing boxes against the band.
_EPSILON = 1.0


def _px(value: str) -> float:
    """Parse a computed length, asserting it actually resolved to pixels.

    ``max-height: none`` / ``top: auto`` — what a broken var chain degrades to —
    fail here rather than silently comparing as 0.
    """
    assert value.endswith("px"), f"expected a resolved px length, got {value!r}"
    return float(value[:-2])


def _settled_px(element: Locator, prop: str) -> float:
    """Read *element*'s computed *prop* once it stops moving.

    The panel animates in (and transitions when the published viewport changes
    under it), so a single read can catch an interpolated value.
    """
    previous: float | None = None
    for _ in range(60):
        current = _px(element.evaluate(f"el => getComputedStyle(el).{prop}"))
        if previous is not None and abs(current - previous) < 0.5:
            return current
        previous = current
        element.page.wait_for_timeout(50)
    raise AssertionError(f"{prop} never settled (last read {previous})")


def _settled_box(element: Locator, label: str) -> dict[str, float]:
    """Return *element*'s bounding box once the open animation has finished."""
    previous: dict[str, float] | None = None
    for _ in range(60):
        current = element.bounding_box()
        assert current is not None, f"{label} has no box"
        if previous is not None and all(abs(current[k] - previous[k]) < 0.5 for k in current):
            return current
        previous = current
        element.page.wait_for_timeout(50)
    raise AssertionError(f"{label} never settled (last box {previous})")


def _assert_within_band(page: Page, element: Locator, label: str) -> None:
    """Assert *element*'s box lies inside the page's own visible viewport band."""
    height, offset_top = page.evaluate(_VISIBLE_BAND)
    box = _settled_box(element, label)
    assert box["y"] >= offset_top - _EPSILON, (
        f"{label} starts above the visible band: y={box['y']} < {offset_top}"
    )
    assert box["y"] + box["height"] <= offset_top + height + _EPSILON, (
        f"{label} extends below the visible band: "
        f"bottom={box['y'] + box['height']} > {offset_top + height}"
    )


def _assert_panel_capped_to_band(page: Page, panel: Locator) -> None:
    """Assert the panel's cap resolves to px, is under the band, and fits in it."""
    height, _offset_top = page.evaluate(_VISIBLE_BAND)
    cap = _settled_px(panel, "maxHeight")
    assert 0 < cap < height, f"cap {cap}px must be inside the {height}px visible band"
    # The centering origin must resolve too — `top: auto` would let a
    # `-translate-y-1/2` panel ride up out of the band.
    _settled_px(panel, "top")
    _assert_within_band(page, panel, "dialog panel")


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _open_delete_confirm(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the sidebar row kebab → Delete confirmation; return the dialog.

    The sidebar's confirm passes NO height class at all — one of the 20+ callers
    that relied on the shared component before it capped anything.
    ``?sidebar=open`` reveals the phone-width drawer (one-shot; the param strips
    itself), which is where the row kebab lives.
    """
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("delete-conversation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    return dialog


def _open_project_settings(page: Page, project: str) -> None:
    """Open the sidebar's folder kebab → "Project settings" for *project*."""
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    header.hover()
    page.get_by_role("button", name=f"Project actions for {project}").click()
    page.get_by_test_id("project-settings").click()
    # Save enables once the editor mounted and the config fetch settled.
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


@pytest.mark.parametrize("viewport", [_PHONE, _SHORT], ids=["390x844", "390x500"])
def test_confirm_dialog_fits_visible_band_and_delete_is_clickable(
    page: Page,
    seeded_session: tuple[str, str],
    viewport: ViewportSize,
) -> None:
    """A confirm dialog that passes NO height class stays inside the visible band.

    Before the shared ``DialogContent`` owned the cap, this dialog was centered
    on the layout viewport and uncapped, so on a phone its footer sat below the
    fold with nothing to scroll.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param viewport: Phone viewport under test (tall, and short enough to crowd).
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(viewport)
    dialog = _open_delete_confirm(page, base_url, session_id)

    _assert_panel_capped_to_band(page, dialog)

    # The footer lives OUTSIDE the scroll region, so Delete is on screen — and
    # the click LANDS, which is the part a class-string assertion cannot show
    # (Playwright refuses to click a covered or off-screen element).
    delete = dialog.get_by_role("button", name="Delete", exact=True)
    _assert_within_band(page, delete, "Delete button")
    delete.click()

    # It really was the Delete button: the dialog closes and the row goes with
    # it (delete is optimistic, so the row leaves on the next frame).
    expect(dialog).to_have_count(0)
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


@pytest.mark.parametrize("viewport", [_PHONE, _SHORT], ids=["390x844", "390x500"])
def test_long_form_dialog_fits_visible_band_and_save_is_clickable(
    page: Page,
    seeded_session: tuple[str, str],
    viewport: ViewportSize,
) -> None:
    """The long Project settings form scrolls its fields and keeps Save reachable.

    The tallest dialog in the shell: at 390x500 its fields cannot fit, so this
    exercises the overflow path — the body slot scrolls, the footer does not.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param viewport: Phone viewport under test.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.set_viewport_size(viewport)
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")

    _open_project_settings(page, project)
    dialog = page.get_by_role("dialog")
    _assert_panel_capped_to_band(page, dialog)

    # The scroller is the body slot, and it is the only thing that overflows —
    # the panel never does, which is what keeps the footer on screen.
    body = dialog.locator('[data-slot="dialog-body"]')
    expect(body).to_have_count(1)
    if viewport is _SHORT:
        assert body.evaluate("el => el.scrollHeight > el.clientHeight + 1"), (
            "the short viewport should force the dialog body to scroll"
        )

    save = page.get_by_test_id("project-settings-save")
    _assert_within_band(page, save, "Save button")
    save.click()
    expect(dialog).to_have_count(0)


def test_panel_tracks_the_published_visible_viewport(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Shrinking the published viewport (soft keyboard) shrinks the panel.

    This is what separates the fix from the ``85vh`` it replaced: with no
    browser chrome showing, a static cap and a visible-viewport-derived one
    resolve to the same pixels. Only re-publishing a smaller
    ``--omnigent-viewport-height`` — exactly what ``useVisibleViewportHeight``
    does when the keyboard opens — tells them apart.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_PHONE)
    dialog = _open_delete_confirm(page, base_url, session_id)

    full_cap = _settled_px(dialog, "maxHeight")
    full_top = _settled_px(dialog, "top")

    # The keyboard takes the bottom half of the screen.
    keyboard_band = 400
    page.evaluate(_PUBLISH_VIEWPORT_HEIGHT, keyboard_band)

    # `--omnigent-dialog-max-height` is the band less both safe insets (0 in a
    # plain browser) and a 1rem margin top and bottom; `--omnigent-dialog-center`
    # is the band's midpoint. Both must follow the published height.
    assert _settled_px(dialog, "maxHeight") == pytest.approx(keyboard_band - 32, abs=1)
    assert _settled_px(dialog, "top") == pytest.approx(keyboard_band / 2, abs=1)
    assert _settled_px(dialog, "maxHeight") < full_cap
    assert _settled_px(dialog, "top") < full_top

    # And the panel really is inside the shrunken band, not merely capped.
    box = _settled_box(dialog, "dialog panel")
    assert box["y"] >= -_EPSILON
    assert box["y"] + box["height"] <= keyboard_band + _EPSILON


def test_command_palette_override_is_visible_viewport_derived(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The palette's own ``top`` / cap come from the visible band, not ``25%``.

    ``CommandPalette`` deliberately overrides the shared centering (it sits high
    on the screen) and twMerge lets it win. That override must still be computed
    from the visible viewport: a plain ``top-1/4`` resolves against the taller
    layout viewport and drifts off screen with the URL bar or keyboard showing.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_PHONE)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    page.keyboard.press("Control+k")
    palette = page.get_by_role("dialog")
    expect(page.get_by_test_id("command-palette-input")).to_be_focused()
    _assert_panel_capped_to_band(page, palette)

    # Re-publish a keyboard-shrunk viewport: `top-1/4` (25% of the layout
    # viewport) and a static cap would not move, while the shipped
    # `top-[calc(--omnigent-visible-top + --omnigent-visible-height / 4)]` and
    # `max-h-[calc(--omnigent-visible-height * 0.75 - ... - 1rem)]` both do.
    keyboard_band = 400
    page.evaluate(_PUBLISH_VIEWPORT_HEIGHT, keyboard_band)
    assert _settled_px(palette, "top") == pytest.approx(keyboard_band / 4, abs=1)
    assert _settled_px(palette, "maxHeight") == pytest.approx(keyboard_band * 0.75 - 16, abs=1)
