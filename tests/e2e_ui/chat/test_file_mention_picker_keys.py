"""E2E: ``@`` file-picker keys — Tab moves focus, Enter attaches.

The in-session composer's ``@`` file-reference picker must follow the
reference-picker convention: Enter attaches the highlighted file or folder
(no Tab-attach, no Enter drill-in), and Tab / Shift+Tab move focus without
attaching. Both composers share ``useMentionBrowser``, so the in-session
journeys below guard the new-session launcher too.
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import Locator, Page, expect

_SEED_FILE = "readme-picker.md"
_SEED_DIR = "docsdir"

# The claude-native runner creates the session's OS environment on bind;
# seeding must wait for it (CI budget mirrors the render-parity suite).
_ENV_READY_TIMEOUT_S = 120.0


def _seed_workspace(base_url: str, session_id: str) -> None:
    """Write deterministic picker entries into the session workspace.

    The spawned CI runner serves each session an empty scratch workspace, so
    the entries the picker lists are seeded through the same filesystem API
    the files-panel tests use (parents are auto-created). Retries until the
    runner has created the session's OS environment.
    """
    deadline = time.monotonic() + _ENV_READY_TIMEOUT_S
    for path, content in ((_SEED_FILE, "# readme\n"), (f"{_SEED_DIR}/guide.md", "# guide\n")):
        while True:
            resp = httpx.put(
                f"{base_url}/v1/sessions/{session_id}"
                f"/resources/environments/default/filesystem/{path}",
                json={"content": content, "encoding": "utf-8"},
                timeout=10.0,
            )
            if resp.status_code < 400:
                break
            if time.monotonic() > deadline:
                resp.raise_for_status()
            time.sleep(2.0)


def _open_picker(
    page: Page, base_url: str, session_id: str, token: str, expected_top_row: str
) -> Locator:
    """Type *token* into the chat composer and wait for the picker's top row.

    :param page: The Playwright page.
    :param base_url: Spawned server base URL.
    :param session_id: The claude-native session to open.
    :param token: The mention token to type, e.g. ``"@read"``.
    :param expected_top_row: Text the highlighted top row must show.
    :returns: The composer textarea locator.
    """
    _seed_workspace(base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    # Native wrapper sessions default to the terminal view; the picker lives
    # in the chat composer.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    composer.press_sequentially(token, delay=50)
    top_row = page.get_by_test_id("file-mention-item-0")
    expect(top_row).to_be_visible(timeout=30_000)
    expect(top_row).to_contain_text(expected_top_row)
    expect(top_row).to_have_attribute("aria-selected", "true")
    return composer


def test_tab_moves_focus_without_attaching(
    page: Page, native_claude_mock_session: tuple[str, str]
) -> None:
    """Tab in the open picker moves focus; it must not attach the top row."""
    base_url, session_id = native_claude_mock_session
    composer = _open_picker(page, base_url, session_id, "@read", _SEED_FILE)

    composer.press("Tab")

    expect(composer).not_to_be_focused()
    expect(page.get_by_role("button", name=f"Remove {_SEED_FILE}")).to_have_count(0)
    expect(composer).to_have_value("@read")


def test_enter_attaches_highlighted_folder(
    page: Page, native_claude_mock_session: tuple[str, str]
) -> None:
    """Enter attaches the highlighted folder as a chip instead of drilling in."""
    base_url, session_id = native_claude_mock_session
    composer = _open_picker(page, base_url, session_id, "@docsd", f"{_SEED_DIR}/")

    composer.press("Enter")

    expect(page.get_by_role("button", name=f"Remove {_SEED_DIR}", exact=True)).to_be_visible(
        timeout=5_000
    )
    expect(composer).to_have_value("")
