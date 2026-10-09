"""E2E: macOS Ctrl+F must stay in the composer while a Markdown editor is open.

On macOS Ctrl+F is the emacs-style "move forward one character" binding in
text inputs; only Cmd+F is the find shortcut. The Markdown editor's
find-in-file listener is window-level, so with the editor open it can capture
Ctrl+F typed into the chat composer, open the find bar, and steal focus.
These tests pin the expected scoping on a macOS-emulated page:

- Ctrl+F with the composer focused leaves the find bar closed and focus in
  the composer.
- Cmd+F with the editor focused still opens the find bar and focuses it.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_PATH = "meeting_notes.md"

_FILE_CONTENT = """\
# Meeting Notes

Agenda item one covers the search bar behaviour.

Agenda item two covers keyboard shortcuts.
"""

# isMacPlatform() reads userAgentData.platform with navigator.platform as the
# fallback, so both are pinned (mirrors test_session_search.py).
_MACOS_INIT_SCRIPT = """
Object.defineProperty(navigator, "platform", { value: "MacIntel" });
Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
"""


@pytest.fixture
def seeded_markdown_session(seeded_session: tuple[str, str]) -> tuple[str, str]:
    """Seed a markdown file so the Files panel can open it in Editor mode."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return (base_url, session_id)


def _open_markdown_editor(page: Page, base_url: str, session_id: str) -> tuple[Locator, Locator]:
    """Open the seeded file from the Files panel; return (file_viewer, editor)."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    # Markdown opens in rich-text Editor mode by default; the contenteditable
    # surface confirms the mode whose find bar owns the window listener.
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor).to_contain_text("Meeting Notes")
    return file_viewer, editor


def test_ctrl_f_stays_in_composer_on_macos(
    page: Page, seeded_markdown_session: tuple[str, str]
) -> None:
    """Ctrl+F in the focused composer must not open the Markdown find bar."""
    base_url, session_id = seeded_markdown_session
    page.add_init_script(_MACOS_INIT_SCRIPT)
    _open_markdown_editor(page, base_url, session_id)

    composer = page.get_by_role("textbox", name="Message the agent")
    composer.click()
    composer.fill("move the caret with ctrl-f")
    # Caret before the end of the line, where Ctrl+F would move it forward.
    composer.press("Home")
    expect(composer).to_be_focused()

    page.keyboard.press("Control+f")
    # The find bar opens and steals focus a tick after keydown (its input is
    # focused via setTimeout), so settle before asserting nothing happened.
    page.wait_for_timeout(500)

    expect(page.get_by_placeholder("Find…")).to_have_count(0)
    expect(composer).to_be_focused()
    expect(composer).to_have_value("move the caret with ctrl-f")


def test_cmd_f_opens_find_bar_from_editor_on_macos(
    page: Page, seeded_markdown_session: tuple[str, str]
) -> None:
    """Cmd+F with focus in the Markdown editor still opens its find bar."""
    base_url, session_id = seeded_markdown_session
    page.add_init_script(_MACOS_INIT_SCRIPT)
    file_viewer, editor = _open_markdown_editor(page, base_url, session_id)

    editor.click()
    page.keyboard.press("Meta+f")

    find_input = file_viewer.get_by_placeholder("Find…")
    expect(find_input).to_be_visible()
    expect(find_input).to_be_focused()
