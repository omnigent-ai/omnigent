"""Composer inputs avoid Safari's small-text focus-zoom trigger without capping preferences.

Desktop WebKit verifies CSS and focus, not the real iOS software keyboard's zoom.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import seed_committed_turn
from tests.e2e_ui.mobile.test_ios_ipad_safe_layout import _IOS_SHELL_INIT_SCRIPT
from tests.e2e_ui.sessions.test_reply_quotes_session_switch import _reply_to

_TOUCH = pytest.mark.browser_context_args(has_touch=True)
_AGENT_SCAN = re.compile(r"/v1/sessions\?.*kind=any")


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


def _exercise_input(page: Page, element: Locator, *, mobile: bool) -> None:
    expect(element).to_be_visible(timeout=30_000)
    element.tap() if mobile else element.click()
    expect(element).to_be_focused()
    element.fill("First line")
    element.press("Enter" if mobile else "Shift+Enter")
    page.keyboard.insert_text("Second line")
    expect(element).to_have_value("First line\nSecond line")
    element.blur()
    element.tap() if mobile else element.click()
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
def test_composer_input_font_floor_and_alignment(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    width: int,
    font_size: int,
    native: bool,
) -> None:
    """Landing, command backdrops and interleaved replies share readable input typography."""
    base_url, session_id = seeded_session
    mobile = width < 768
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    if native:
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    quotes = ["First point to discuss.", "Second point to discuss."]
    seed_committed_turn(session_id, prompt="Explain both points.", reply="\n\n".join(quotes))

    # A bundled skill exposes the landing input's text-ui placeholder overlay.
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": "ag_font_e2e",
                        "name": "polly",
                        "display_name": "Polly",
                        "description": "Typography test agent",
                        "harness": "claude-sdk",
                        "skills": [{"name": "context", "description": "Review context"}],
                    }
                ]
            }
        ),
    )
    page.route(_AGENT_SCAN, lambda route: route.fulfill(json={"data": []}))
    page.goto(f"{base_url}/")
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
    page.unroute("**/v1/agents")
    page.unroute(_AGENT_SCAN)

    page.goto(f"{base_url}/c/{session_id}")
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
    ui_size = font_size * (14 / 13) if mobile else font_size
    expected_size = max(16, ui_size) if mobile else ui_size
    for name, measured in observed.items():
        assert measured["fontSize"] == pytest.approx(expected_size, abs=0.01), (name, measured)
        assert measured["lineHeight"] == pytest.approx(expected_size * 1.6, abs=0.02), (
            name,
            measured,
        )
    assert quote_metrics["fontSize"] == pytest.approx(ui_size * 0.9, abs=0.01)
    assert skill_metrics["fontSize"] == pytest.approx(ui_size, abs=0.01)

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

    viewport = page.locator('meta[name="viewport"]').get_attribute("content") or ""
    assert "user-scalable=no" not in viewport.replace(" ", "").lower()
    assert "maximum-scale" not in viewport.lower()
