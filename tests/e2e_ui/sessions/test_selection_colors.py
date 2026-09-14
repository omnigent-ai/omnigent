"""Browser text selection follows Appearance accents with readable opaque colors."""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, expect


def _selection_colors(text: Locator) -> dict[str, list[int] | str]:
    """Select real UI text and resolve its painted selection colors in the browser."""
    return text.evaluate(
        """element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            const style = getComputedStyle(element, '::selection');
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 1;
            const context = canvas.getContext('2d');
            const rgba = color => {
                context.clearRect(0, 0, 1, 1);
                context.fillStyle = color;
                context.fillRect(0, 0, 1, 1);
                return Array.from(context.getImageData(0, 0, 1, 1).data);
            };
            const probe = document.createElement('span');
            element.appendChild(probe);
            const surface = token => {
                probe.style.color = `var(--${token})`;
                return rgba(getComputedStyle(probe).color);
            };
            const result = {
                background: rgba(style.backgroundColor),
                foreground: rgba(style.color),
                page: surface('background'),
                card: surface('card-solid'),
                selectedText: selection.toString(),
            };
            probe.remove();
            return result;
        }"""
    )


def _contrast(first: list[int], second: list[int]) -> float:
    def luminance(color: list[int]) -> float:
        channels = [channel / 255 for channel in color[:3]]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return sum(
            channel * weight
            for channel, weight in zip(linear, [0.2126, 0.7152, 0.0722], strict=True)
        )

    values = sorted([luminance(first), luminance(second)])
    return (values[1] + 0.05) / (values[0] + 0.05)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_selection_follows_live_custom_accent_with_readable_colors(
    page: Page, live_server: str, mode: str
) -> None:
    """The real selection pseudo-element stays visible after two live accent edits."""
    page.goto(f"{live_server}/settings/appearance")
    palette = page.get_by_test_id("color-theme-select")
    expect(palette).to_be_visible(timeout=30_000)
    page.get_by_test_id(f"theme-{mode}").click()
    palette.click()
    page.get_by_role("option", name="GitHub", exact=True).click()
    text = page.get_by_text("Choose how Omnigent looks on this device.", exact=True)
    expect(text).to_be_visible()
    previous = _selection_colors(text)["background"]

    for accent in ["#a855f7", "#f97316"]:
        page.get_by_test_id("custom-theme-accent-trigger").click()
        page.get_by_test_id("custom-theme-accent-input").fill(accent)
        page.keyboard.press("Escape")
        expect(palette).to_contain_text("Custom")
        expect(page.get_by_test_id("custom-theme-accent-trigger")).to_contain_text(accent.upper())
        colors = _selection_colors(text)
        assert colors["selectedText"] == "Choose how Omnigent looks on this device."
        background = colors["background"]
        foreground = colors["foreground"]
        assert isinstance(background, list) and isinstance(foreground, list)
        assert background != previous, "selection must follow the changed accent without a reload"
        assert background[3] == 255 and foreground[3] == 255, "selection colors must be opaque"
        assert _contrast(background, foreground) >= 4.5, "selected glyphs must remain readable"
        for name in ["page", "card"]:
            surface = colors[name]
            assert isinstance(surface, list)
            assert _contrast(background, surface) >= 3, f"selection disappears against {name}"
        previous = background
