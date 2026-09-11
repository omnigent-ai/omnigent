"""E2E: selecting chat text in dark mode paints a visible highlight.

Guarded regression: when the app-wide ``::selection`` rule
(``web/src/index.css``) borrows the sidebar's active-row pair, dark palettes
paint the selection with a low-alpha row tint — the default dark palette's
``rgba(240, 1, 150, 0.15)`` composited over the near-black page (``#0e1013``)
renders ~``rgb(48, 14, 39)``, a WCAG contrast ratio of ~1.11:1 against the
unselected background, i.e. an invisible selection highlight; only the glyphs
faintly tint pink. Dark mode must keep its own perceptible selection pair.

The test drives the real user journey — app in dark mode, open a session,
select a message's text — and measures what the user actually sees: it
screenshots the paragraph before and after selecting it, takes the median
color of the paragraph's background pixels in each, and requires the rendered
selection wash to reach a minimum WCAG contrast ratio against the unselected
background. On the buggy build the measured ratio is ~1.1 (fails); the
pre-regression dark style (solid brand accent) measures ~4.7 (passes), so any
visibly-highlighted fix clears the 1.5 floor comfortably.

No LLM turn is involved: the assistant message is seeded via the
``external_assistant_message`` session event (same pattern as
``test_chat_long_text_wrap.py``).
"""

from __future__ import annotations

import io
from statistics import median

import httpx
from PIL import Image
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_MESSAGE_TEXT = (
    "Selection visibility check: when a reader highlights this sentence in "
    "dark mode, the app must paint a clearly visible selection wash behind "
    "the glyphs so the selected range can be seen at a glance."
)

# Minimum WCAG contrast ratio between the rendered selection wash and the
# unselected background. Buggy dark palette measures ~1.1; the last known-good
# dark selection style (solid brand accent on #0e1013) measures ~4.7.
_MIN_SELECTION_CONTRAST = 1.5

# Per-channel tolerance for classifying a pixel as "background" (vs. glyph /
# antialiasing) around the paragraph's median color.
_BG_PIXEL_TOLERANCE = 16


def _linear(channel: int) -> float:
    """One sRGB channel (0-255) to linear light, per the WCAG formula."""
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _pixels(png_bytes: bytes) -> list[tuple[int, int, int]]:
    with Image.open(io.BytesIO(png_bytes)) as img:
        raw = img.convert("RGB").tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def _median_color(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    return (
        round(median(p[0] for p in pixels)),
        round(median(p[1] for p in pixels)),
        round(median(p[2] for p in pixels)),
    )


def test_dark_mode_text_selection_paints_visible_highlight(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Triple-click-selecting a dark-mode message shows a perceptible wash."""
    base_url, session_id = seeded_session

    # Seed a static assistant message so there is real transcript prose to
    # select, with no turn running (keeps every frame deterministic).
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MESSAGE_TEXT},
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()

    # A dark-mode user: the OS scheme is dark and the app's default "system"
    # theme resolves to it (next-themes toggles the `dark` class on <html>).
    page.emulate_media(color_scheme="dark")
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator(_ASSISTANT_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)
    expect(bubble).to_contain_text("Selection visibility check")
    assert page.evaluate("() => document.documentElement.classList.contains('dark')"), (
        "expected the app to render in dark mode (dark class on <html>)"
    )

    paragraph = bubble.locator("p").filter(has_text="Selection visibility check").first
    expect(paragraph).to_be_visible()
    # Let fonts/layout settle so the before/after screenshots differ only by
    # the selection painting.
    page.wait_for_timeout(500)

    before = _pixels(paragraph.screenshot())

    # The user gesture: triple-click selects the whole paragraph, then park
    # the pointer away from the text so no hover styling pollutes the frame.
    paragraph.click(click_count=3)
    page.mouse.move(0, 0)
    selected_len = page.evaluate("() => window.getSelection().toString().length")
    assert selected_len >= 20, f"expected the paragraph to be selected, got {selected_len} chars"
    # Hold the selected state briefly so it is visible in recordings.
    page.wait_for_timeout(1_200)

    after = _pixels(paragraph.screenshot())
    assert len(after) == len(before), "before/after screenshots must cover the same box"

    # Background pixels = those near the paragraph's median color (the wash
    # area between glyphs); glyph/antialiased pixels are excluded so glyph
    # recoloring alone cannot masquerade as a visible highlight.
    bg_color = _median_color(before)
    mask = [
        i
        for i, p in enumerate(before)
        if all(abs(p[c] - bg_color[c]) <= _BG_PIXEL_TOLERANCE for c in range(3))
    ]
    assert len(mask) >= 0.3 * len(before), (
        f"background-pixel mask too small ({len(mask)}/{len(before)}); "
        f"median color {bg_color} did not isolate the paragraph background"
    )
    assert _relative_luminance(bg_color) < 0.2, (
        f"expected a dark unselected background, got {bg_color} — dark mode did not take effect"
    )

    wash_color = _median_color([after[i] for i in mask])
    contrast = _contrast_ratio(wash_color, bg_color)

    selection_style = paragraph.evaluate(
        "el => { const s = getComputedStyle(el, '::selection');"
        " return { background: s.backgroundColor, color: s.color }; }"
    )
    assert contrast >= _MIN_SELECTION_CONTRAST, (
        "dark-mode text selection is not visibly highlighted: the rendered "
        f"selection wash {wash_color} has contrast {contrast:.2f}:1 against the "
        f"unselected background {bg_color} (needs >= {_MIN_SELECTION_CONTRAST}:1). "
        f"Computed ::selection style: {selection_style}"
    )
