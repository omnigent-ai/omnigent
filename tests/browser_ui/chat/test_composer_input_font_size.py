"""Composer inputs follow the interface font preference across app surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat._geometry_helpers import open_landing, open_live
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    list_payload,
    message_item,
)

_TOUCH = pytest.mark.browser_context_args(has_touch=True)
# Minimal iOS shell bridge; the app marks itself native when this object exists.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""
_CONTEXT_SKILL = {"name": "context", "description": "Review context"}


def _metrics(element: Locator) -> dict[str, Any]:
    return element.evaluate("""element => {
        const style = getComputedStyle(element);
        const px = property => parseFloat(style[property]) || 0;
        const bounds = element.getBoundingClientRect();
        return {
            fontSize: px('fontSize'), lineHeight: px('lineHeight'),
            fontFamily: style.fontFamily, fontWeight: style.fontWeight,
            letterSpacing: style.letterSpacing, whiteSpace: style.whiteSpace,
            overflowWrap: style.overflowWrap, wordBreak: style.wordBreak,
            contentX: bounds.left + element.clientLeft + px('paddingLeft'),
            contentY: bounds.top + element.clientTop + px('paddingTop'),
            contentWidth: element.clientWidth - px('paddingLeft') - px('paddingRight'),
            contentHeight: element.scrollHeight - px('paddingTop') - px('paddingBottom'),
            scrollHeight: element.scrollHeight, clientHeight: element.clientHeight,
            horizontalOverflow: element.scrollWidth - element.clientWidth,
        };
    }""")


def _reply_to(page: Page, text: Locator) -> None:
    text.evaluate("""element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.dispatchEvent(new Event("selectionchange"));
    }""")
    page.get_by_role("button", name="Reply", exact=False).click()


def _exercise_input(page: Page, element: Locator, *, mobile: bool) -> None:
    expect(element).to_be_visible(timeout=30_000)
    # The landing's interactive skill pills can cover the input's center.
    activate = element.tap if mobile else element.click
    activate(position={"x": 8, "y": 8})
    expect(element).to_be_focused()
    element.fill("First line")
    element.press("Enter" if mobile else "Shift+Enter")
    page.keyboard.insert_text("Second line")
    expect(element).to_have_value("First line\nSecond line")
    element.blur()
    activate(position={"x": 8, "y": 8})
    expect(element).to_be_focused()
    expect(element).to_have_value("First line\nSecond line")


@pytest.mark.parametrize(
    ("width", "font_size", "native"),
    [
        pytest.param(390, 11, False, marks=_TOUCH, id="mobile-small"),
        pytest.param(390, 13, False, marks=_TOUCH, id="mobile-default"),
        pytest.param(390, 18, False, marks=_TOUCH, id="mobile-large"),
        pytest.param(1440, 11, False, id="desktop-small"),
        pytest.param(1440, 13, False, id="desktop-default"),
        pytest.param(1440, 18, False, id="desktop-large"),
        pytest.param(390, 13, True, marks=_TOUCH, id="ios-default"),
        pytest.param(390, 18, True, marks=_TOUCH, id="ios-large"),
    ],
)
def test_composer_input_font_setting_and_alignment(
    page: Page,
    chat_session_contract: ChatSessionContract,
    tmp_path: Path,
    width: int,
    font_size: int,
    native: bool,
) -> None:
    """Landing, command backdrops and interleaved replies share the chosen typography."""
    chat = chat_session_contract
    mobile = width < 768
    viewport = {"width": width, "height": 844}
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    if native:
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    quotes = ["First point to discuss.", "Second point to discuss."]
    chat.set_items(
        [
            message_item("assistant-points", "assistant", "\n\n".join(quotes), response_id="pts"),
            message_item("user-points", "user", "Explain both points.", response_id="pts"),
        ]
    )
    chat.set_skills([_CONTEXT_SKILL])

    # A bundled skill exposes the landing input's text-ui placeholder overlay.
    chat.contract.json(
        "/v1/agents",
        {
            "data": [
                {
                    "id": "ag_font_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Typography test agent",
                    "harness": "claude-sdk",
                    "skills": [_CONTEXT_SKILL],
                }
            ]
        },
    )
    chat.contract.json(OWN_AGENTS, {"data": []})
    open_landing(page, chat, viewport)
    landing = page.get_by_test_id("new-chat-landing-input")
    hint = page.get_by_text("Describe a task, or try a skill", exact=True)
    expect(hint).to_be_visible(timeout=30_000)
    observed = {"landing-hint": _metrics(hint)}
    skill_metrics = _metrics(page.get_by_test_id("skill-pill-context"))
    page.screenshot(path=tmp_path / "landing-placeholder.png", animations="disabled")
    _exercise_input(page, landing, mobile=mobile)
    observed.update(
        {"landing": _metrics(landing), "landing-area": _metrics(landing.locator(".."))}
    )
    page.screenshot(path=tmp_path / "landing-composer.png", animations="disabled")
    # The session page reads the contract's bound agent again.
    chat.contract.json("/v1/agents", lambda _request: list_payload([chat._agent()]))

    open_live(page, chat, viewport)
    if native:
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
    main = page.get_by_role("textbox", name="Message the agent", exact=True)
    _exercise_input(page, main, mobile=mobile)
    observed.update({"session": _metrics(main), "session-area": _metrics(main.locator(".."))})
    page.screenshot(path=tmp_path / "session-composer.png", animations="disabled")

    # Both mirrors must exceed the height cap so scrollHeight measures text wrapping.
    draft = "/context " + "review this multiline draft " * 100 + "\n" + "unbroken" * 80
    main.fill(draft)
    expect(main).to_have_attribute("data-slash-command", "true")
    overlay = page.get_by_test_id("composer-highlight-overlay")
    expect(overlay).to_be_visible()
    assert overlay.evaluate("element => element.textContent") == draft
    observed.update({"slash-input": _metrics(main), "slash-overlay": _metrics(overlay)})
    page.screenshot(path=tmp_path / "slash-command-overlay.png", animations="disabled")

    # A nonempty introduction mounts the first editable block before its quote.
    main.fill("My introduction.")
    for index, quote in enumerate(quotes, start=1):
        paragraph = page.locator('[data-role="assistant"]').get_by_text(quote, exact=True)
        expect(paragraph).to_be_visible()
        _reply_to(page, paragraph)
        reply = page.get_by_role("textbox", name=f"Reply text before quote {index}", exact=True)
        expect(reply).to_have_value("My introduction." if index == 1 else "My first answer.")
        observed[f"reply-{index}"] = _metrics(reply)
        main.fill("My first answer." if index == 1 else "My second answer.")
    expect(page.get_by_test_id("composer-reply-blocks").locator("textarea")).to_have_count(2)
    expect(overlay).to_have_count(0)
    observed["reply-tail"] = _metrics(main)
    quote_metrics = _metrics(
        page.get_by_test_id("composer-reply-quote").first.locator("blockquote")
    )
    page.screenshot(path=tmp_path / "interleaved-replies.png", animations="disabled")

    # Preserve every surface's baseline evidence before reporting typography failures.
    print(f"Composer typography ({width}px, preference={font_size}, native={native}): {observed}")
    expected_size = font_size
    for name, measured in observed.items():
        assert measured["fontSize"] == pytest.approx(expected_size, abs=0.01), (name, measured)
        assert measured["lineHeight"] == pytest.approx(expected_size * 1.6, abs=0.02), (
            name,
            measured,
        )
    assert quote_metrics["fontSize"] == pytest.approx(expected_size * 0.9, abs=0.01)
    assert skill_metrics["fontSize"] == pytest.approx(expected_size, abs=0.01)

    textarea_metrics, overlay_metrics = observed["slash-input"], observed["slash-overlay"]
    for property_name in (
        "fontFamily",
        "fontWeight",
        "letterSpacing",
        "whiteSpace",
        "overflowWrap",
        "wordBreak",
    ):
        assert textarea_metrics[property_name] == overlay_metrics[property_name], property_name
    for measured in (textarea_metrics, overlay_metrics):
        assert measured["scrollHeight"] > measured["clientHeight"], measured
        assert measured["horizontalOverflow"] <= 1, measured
    for property_name in ("contentX", "contentY", "contentWidth", "contentHeight"):
        assert textarea_metrics[property_name] == pytest.approx(
            overlay_metrics[property_name], abs=2
        ), (property_name, textarea_metrics, overlay_metrics)

    viewport_meta = page.locator('meta[name="viewport"]').get_attribute("content") or ""
    assert "user-scalable=no" not in viewport_meta.replace(" ", "").lower()
    assert "maximum-scale" not in viewport_meta.lower()
