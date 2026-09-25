"""Exercise the desktop's injected picker in Chromium with real modal focus rules.

The fixtures and picker are shared with the packaged Electron E2E. Only the
main-process console bridge is observed here; layout, focus and input are real.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NONCE = "dialog-focus-e2e"
_BUILD_ASSETS = """
const { buildDesignModeScript } = require('./web/electron/src/designModeScript');
const { NATIVE_DIALOG_PAGE } = require('./web/electron/e2e/desktopDesignPromptHarness');
const { buildRadixFormFixture } = require('./web/electron/e2e/fixtures/designModalFixture');
buildRadixFormFixture().then(radix => {
  process.stdout.write(JSON.stringify({
    picker: buildDesignModeScript('dialog-focus-e2e'),
    native: NATIVE_DIALOG_PAGE,
    radix: radix.html,
    script: radix.script,
  }));
}).catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.fixture(scope="module")
def design_assets() -> dict[str, str]:
    """Bundle the maintained Radix fixture with the repository's web dependencies."""
    result = subprocess.run(
        ["node", "-e", _BUILD_ASSETS],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("kind", ["native", "radix"])
def test_floating_editor_owns_modal_typing(
    page: Page, design_assets: dict[str, str], kind: str
) -> None:
    """Typing and Escape stay in the popup, with ordinary form editing restored on exit."""
    page.set_viewport_size({"width": 700, "height": 700})
    page.route(
        "http://design-fixture.test/modal",
        lambda route: route.fulfill(content_type="text/html", body=design_assets[kind]),
    )
    page.route(
        "http://design-fixture.test/app.js",
        lambda route: route.fulfill(
            content_type="application/javascript", body=design_assets["script"]
        ),
    )
    messages: list[str] = []
    page.on("console", lambda message: messages.append(message.text))
    page.goto("http://design-fixture.test/modal")
    field = page.locator("#scenario-period")
    modal = page.locator("#capacity-dialog")
    popup = page.locator("#__omni-popup")
    editor = page.locator("#__omni-popup-input")
    expect(field).to_be_visible()
    field.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type("W41")
    expect(field).to_be_focused()
    page.evaluate(
        """() => {
          window.__nativeSubmits = 0;
          document.addEventListener('submit', () => window.__nativeSubmits++, true);
        }"""
    )
    page.evaluate(design_assets["picker"])

    field.click()
    expect(editor).to_be_focused()
    assert (
        editor.evaluate("el => el.closest('dialog[open], [role=dialog]').id") == "capacity-dialog"
    )
    page.keyboard.type("asdf")
    expect(editor).to_have_value("asdf")
    expect(field).to_have_value("W41")
    expect(modal).to_be_visible()
    assert page.locator("#__omni-design-layer").evaluate("el => el.matches(':popover-open')")
    for control in [
        editor,
        page.locator("#__omni-popup-send"),
        page.locator("#__omni-popup-close"),
    ]:
        assert control.evaluate(
            """el => {
              const r = el.getBoundingClientRect();
              return el.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
            }"""
        ), "Popup controls must remain clickable outside the modal's clipped bounds"

    page.keyboard.press("Shift+Enter")
    page.keyboard.press("Escape")
    expect(popup).to_be_hidden()
    expect(modal).to_be_visible()
    expect(field).to_have_value("W41")
    expect(field).to_be_focused()
    submit_prefix = f"__omni_{_NONCE}_element_prompt_submit__"
    assert not any(message.startswith(submit_prefix) for message in messages)

    field.click()
    expect(editor).to_be_focused()
    instruction = "Use a week picker for Period."
    page.keyboard.type(instruction)
    with page.expect_console_message(
        predicate=lambda message: message.text.startswith(submit_prefix)
    ):
        page.keyboard.down("Enter")
    expect(editor).to_be_disabled()
    expect(page.locator("#__omni-popup-close")).to_be_focused()
    # Holding Enter repeats on Close after submission transfers keyboard focus.
    page.keyboard.down("Enter")
    page.keyboard.up("Enter")
    expect(popup).to_be_visible()
    submissions = [
        json.loads(message.removeprefix(submit_prefix))
        for message in messages
        if message.startswith(submit_prefix)
    ]
    assert len(submissions) == 1
    assert submissions[0]["prompt"] == instruction
    assert submissions[0]["element"]["id"] == "#scenario-period"
    assert page.evaluate("() => window.__nativeSubmits") == 0
    expect(field).to_have_value("W41")
    # Escape must also be owned by the popup while a submission is pending.
    page.keyboard.press("Escape")
    expect(popup).to_be_hidden()
    expect(modal).to_be_visible()

    page.evaluate("() => window.__omniDisableDesignMode()")
    expect(page.locator("#__omni-design-layer")).to_have_count(0)
    field.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type("W42")
    expect(field).to_have_value("W42")
    if kind == "radix":
        expect(page.locator("#scenario-values")).to_have_text("Period: W42")
        page.keyboard.press("Enter")
        assert page.evaluate("() => window.__nativeSubmits") == 1
