"""Composer menus over a long transcript must not let its text ghost through in dark mode.

Built-in dark palettes make ``bg-popover`` translucent, so the ``/`` and ``@`` menus need a
backdrop blur; light mode (opaque popover) is the control. The leak is the share of panel
pixels that change when the transcript text behind the panel is hidden.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import (
    DESKTOP,
    open_live,
    park_at_bottom,
    seed_long_transcript,
    tag_scroller,
)
from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_MENUS = {
    "slash": ("/", "slash-command-menu", "[data-testid^='slash-menu-item-']"),
    "mention": ("@", "file-mention-menu", "[data-testid^='file-mention-item-']"),
}
# Transcript glyphs leaking through an 80 %-opaque panel differ from the panel fill by
# ~40 grey levels and cover ~5 % of the panel; a blurred or opaque panel leaves ~0 %.
_LEAK_LEVELS = 8
_MAX_LEAK_FRACTION = 0.005


def _stub_workspace_files(chat: ChatSessionContract, names: list[str]) -> None:
    environment = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(environment, {"metadata": {"root": "/workspace"}})
    chat.contract.json(f"{environment}/changes", list_payload([]))
    chat.contract.json(
        f"{environment}/filesystem",
        {
            "available": True,
            "data": [
                {"path": name, "name": name, "type": "file", "bytes": 10, "modified_at": None}
                for name in names
            ],
            "has_more": False,
        },
    )


def _choose_mode(page: Page, base_url: str, mode: str) -> None:
    page.goto(f"{base_url}/settings/appearance")
    group = page.get_by_role("radiogroup", name="Mode", exact=True)
    expect(group).to_be_visible(timeout=30_000)
    card = group.get_by_role("radio", name=mode)
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _open_chat_at_bottom(page: Page, chat: ChatSessionContract) -> None:
    open_live(page, chat, DESKTOP)
    tag_scroller(page)
    park_at_bottom(page)


def _open_menu_panel(page: Page, trigger: str, panel_test_id: str, rows_selector: str) -> Locator:
    """Type *trigger* into the composer and return its menu panel once the rows have loaded."""
    # The "@"-mention detector tracks the caret, so drive a real keystroke rather than
    # setting the value in one shot; the slash menu opens the same way. Clear first because
    # re-navigating between the light and dark passes restores the composer's saved draft.
    composer = page.get_by_label("Message the agent")
    composer.click()
    composer.fill("")
    composer.press_sequentially(trigger)
    expect(page.locator(rows_selector).first).to_be_visible()
    panel = page.get_by_test_id(panel_test_id)
    expect(panel.get_by_text("Loading")).to_have_count(0)
    return panel


def _set_transcript_text_visibility(page: Page, visibility: str) -> int:
    return page.evaluate(
        "visibility => {"
        " const nodes = document.querySelectorAll('[data-testid=\"message-bubble\"] p');"
        " nodes.forEach(el => { el.style.visibility = visibility; });"
        " return nodes.length; }",
        visibility,
    )


def _set_panel_backdrop_filter(panel: Locator, value: str | None) -> None:
    panel.evaluate(
        "(el, value) => {"
        " for (const prop of ['backdrop-filter', '-webkit-backdrop-filter']) {"
        "  if (value === null) el.style.removeProperty(prop);"
        "  else el.style.setProperty(prop, value);"
        " } }",
        value,
    )


def _background_alpha(color: str) -> float:
    """Alpha channel of a computed ``rgb()``/``rgba()`` color; opaque colors return 1.0."""
    inside = color[color.index("(") + 1 : color.index(")")]
    channels = [part.strip() for part in inside.split(",")]
    return float(channels[3]) if len(channels) == 4 else 1.0


def _leak_fraction(reference: bytes, actual: bytes, box: tuple[int, int, int, int]) -> float:
    from PIL import Image, ImageChops

    before = Image.open(io.BytesIO(reference)).convert("L").crop(box)
    after = Image.open(io.BytesIO(actual)).convert("L").crop(box)
    histogram = ImageChops.difference(before, after).histogram()
    total = sum(histogram)
    assert total > 0, f"empty diff region for box {box}"
    return sum(count for level, count in enumerate(histogram) if level > _LEAK_LEVELS) / total


def _measure_transcript_leak(page: Page, panel: Locator, label: str) -> dict[str, Any]:
    """Share of *panel* pixels that differ between hidden and shown transcript text."""
    style = panel.evaluate(
        "el => { const s = getComputedStyle(el); return {backgroundColor: s.backgroundColor,"
        " backdropFilter: s.backdropFilter || s.webkitBackdropFilter}; }"
    )
    box = panel.bounding_box()
    last_reply = page.locator(_ASSISTANT).last.bounding_box()
    assert box is not None and last_reply is not None
    overlaps_reply = (
        box["y"] < last_reply["y"] + last_reply["height"]
        and last_reply["y"] < box["y"] + box["height"]
    )
    hidden = _set_transcript_text_visibility(page, "hidden")
    assert hidden > 0, "no transcript paragraphs found to hide"
    reference = page.screenshot(animations="disabled")
    _set_transcript_text_visibility(page, "")
    actual = page.screenshot(animations="disabled")
    region = (
        int(box["x"]) + 2,
        int(box["y"]) + 2,
        int(box["x"] + box["width"]) - 2,
        int(box["y"] + box["height"]) - 2,
    )
    return {
        "label": label,
        "dark_mode": page.evaluate("() => document.documentElement.classList.contains('dark')"),
        "panel_style": style,
        "panel_box": box,
        "last_reply_box": last_reply,
        "menu_overlaps_last_reply": overlaps_reply,
        "leak_fraction": _leak_fraction(reference, actual, region),
    }


@pytest.mark.parametrize("menu", ["slash", "mention"])
def test_composer_menu_hides_the_transcript_in_dark_mode(
    page: Page, chat_session_contract: ChatSessionContract, menu: str
) -> None:
    """Light mode is the opaque control; dark mode must blur the reply behind the open menu."""
    chat = chat_session_contract
    # Native coding-agent harnesses enable "@" mentions; the slash menu is harness-agnostic.
    chat.harness = "claude-native"
    chat.set_skills([{"name": "review-pr", "description": "Review a pull request"}])
    _stub_workspace_files(chat, ["README.md", "main.py", "config.yaml"])
    seed_long_transcript(chat, turns=2, paragraphs=40)
    trigger, panel_test_id, rows_selector = _MENUS[menu]
    page.emulate_media(color_scheme="light")

    _open_chat_at_bottom(page, chat)
    panel = _open_menu_panel(page, trigger, panel_test_id, rows_selector)
    light = _measure_transcript_leak(page, panel, f"light-{menu}")
    assert light["menu_overlaps_last_reply"], light
    assert light["leak_fraction"] <= _MAX_LEAK_FRACTION, f"light-mode control leaks: {light}"
    # The light popover stays clear because it is opaque and unblurred: a stray light-mode blur
    # would make this pass for the wrong reason, so assert the dark-only variant is absent here.
    assert light["panel_style"]["backdropFilter"] in ("none", ""), (
        f"light-mode popover unexpectedly has a backdrop filter: {light}"
    )

    _choose_mode(page, chat.base_url, "Dark")
    _open_chat_at_bottom(page, chat)
    panel = _open_menu_panel(page, trigger, panel_test_id, rows_selector)
    dark = _measure_transcript_leak(page, panel, f"dark-{menu}")
    assert dark["dark_mode"] and dark["menu_overlaps_last_reply"], dark
    assert dark["leak_fraction"] <= _MAX_LEAK_FRACTION, (
        f"transcript text shows through the {menu} menu: {dark}"
    )

    # Negative control: the same panel without its blur must expose the ghosting, proving
    # the measurement can detect transcript text behind the menu. This needs a translucent
    # dark popover, so check that first — an opaque theme would make the control moot.
    assert _background_alpha(dark["panel_style"]["backgroundColor"]) < 1, (
        f"dark popover is opaque, so the blur negative control is moot: {dark}"
    )
    _set_panel_backdrop_filter(panel, "none")
    unblurred = _measure_transcript_leak(page, panel, f"dark-{menu}-unblurred")
    _set_panel_backdrop_filter(panel, None)
    assert unblurred["leak_fraction"] > _MAX_LEAK_FRACTION, (
        f"the leak measurement cannot detect ghosting: {unblurred}"
    )
