"""Full-stack regression for terminal clipboard consent's native helper path."""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_INITIAL_CLIPBOARD = "clipboard before terminal copy"
_CLIPBOARD_STUB = """
window.__terminalClipboard = {
  text: "clipboard before terminal copy",
  writes: [],
};
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: {
    writeText: async (text) => {
      window.__terminalClipboard.text = text;
      window.__terminalClipboard.writes.push(text);
    },
    readText: async () => window.__terminalClipboard.text,
  },
});
const originalExecCommand = document.execCommand.bind(document);
document.execCommand = (command, ...args) => {
  if (command.toLowerCase() === "copy") return false;
  return originalExecCommand(command, ...args);
};
"""


def _expect_clipboard(page: Page, expected: str) -> None:
    page.wait_for_function(
        "expected => window.__terminalClipboard.text === expected",
        arg=expected,
    )


def test_native_clipboard_helper_obeys_live_settings_in_existing_terminal(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Exercise the real shell → native helper → tmux → browser consent path."""
    base_url, session_id = terminal_session
    page.add_init_script(_CLIPBOARD_STUB)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    terminal = rail.get_by_test_id("terminal-view").last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    native_bin = tmp_path / "native-bin"
    native_bin.mkdir()
    bypass_marker = tmp_path / "bypassed-consent"
    native_copy = native_bin / "pbcopy"
    native_copy.write_text(f"#!/bin/sh\ncat > {shlex.quote(str(bypass_marker))}\n")
    native_copy.chmod(0o700)

    def run_in_terminal(command: str, marker: str) -> None:
        done = tmp_path / marker
        textarea.focus()
        page.keyboard.insert_text(f"{command}; printf done > {shlex.quote(str(done))}")
        page.keyboard.press("Enter")
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert done.exists(), "real terminal command did not finish"
        assert not bypass_marker.exists(), "native clipboard command bypassed consent"

    fallback = shlex.quote(str(native_bin))
    run_in_terminal(
        'case "${PATH%%:*}" in */clipboard-bin) '
        f'export PATH="${{PATH%%:*}}":{fallback}:"${{PATH#*:}}";; '
        f'*) export PATH={fallback}:"$PATH";; esac',
        "path-ready",
    )

    def native_copy_text(text: str, marker: str) -> None:
        run_in_terminal(f"printf %s {shlex.quote(text)} | pbcopy", marker)

    native_copy_text("native copy waiting for consent λ", "ask-done")
    expect(consent).to_be_visible()
    _expect_clipboard(page, _INITIAL_CLIPBOARD)
    consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(page, "native copy waiting for consent λ")
    native_copy_text("native copy already allowed", "allow-done")
    _expect_clipboard(page, "native copy already allowed")

    settings = page.context.new_page()
    try:
        settings.goto(f"{base_url}/settings/general")
        preference = settings.get_by_test_id("terminal-clipboard-preference-select")
        for option in ("Block copying", "Ask before copying", "Allow copying"):
            settings.bring_to_front()
            preference.click()
            settings.get_by_role("option", name=option, exact=True).click()
            expect(preference).to_contain_text(option)
            page.bring_to_front()
            native_copy_text(f"native copy with {option}", f"{option.split()[0]}-done")
            if option == "Allow copying":
                _expect_clipboard(page, f"native copy with {option}")
                expect(consent).to_have_count(0)
            else:
                if option == "Ask before copying":
                    expect(consent).to_be_visible()
                else:
                    expect(
                        page.get_by_text("Copying from this terminal is blocked.")
                    ).to_be_visible()
                    expect(consent).to_have_count(0)
                _expect_clipboard(page, "native copy already allowed")
    finally:
        settings.close()
    expect(terminal).to_have_attribute("data-state", "connected")
