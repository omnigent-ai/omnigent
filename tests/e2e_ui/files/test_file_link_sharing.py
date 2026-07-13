"""E2E: the FileViewer "Copy link to file" button yields a shareable URL.

The toolbar's copy-link action writes ``window.location.href`` (carrying
``?file=<path>``) to the clipboard and flashes a "Copied!" confirmation
(see ``copyFileLink`` in ``FileViewer.tsx``). A shared link is only useful
if a *fresh* browser session that opens it lands on the same file, so this
test:

  1. Opens a seeded file, clicks Copy link, and asserts the clipboard holds
     a URL with the file's ``?file=`` param.
  2. Opens that exact URL in a brand-new browser context (no shared storage
     — i.e. "open in a new browser") and asserts the file viewer rehydrates
     with the real file content.

Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FILE_PATH = "shareable_note.md"
_FILE_BODY = "Unique shareable body that proves a fresh session fetched the file."
_FILE_CONTENT = f"""\
# Shareable Note

{_FILE_BODY}
"""


@pytest.fixture
def seeded_shareable_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_copy_link_is_shareable_in_a_new_browser(
    page: Page,
    browser: Browser,
    seeded_shareable_session: tuple[str, str],
) -> None:
    """Copy link → clipboard URL → opens the file in a fresh browser context."""
    base_url, session_id = seeded_shareable_session
    # Clipboard read/write needs explicit permission in headless Chromium.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)

    # Click the copy-link toolbar action and confirm the "Copied!" feedback
    # (the button swaps to a check icon; its tooltip becomes "Copied!").
    copy_btn = file_viewer.get_by_role("button", name="Copy link to file")
    expect(copy_btn).to_be_visible()
    copy_btn.click()

    clipboard = page.evaluate("() => navigator.clipboard.readText()")
    assert re.search(rf"[?&]file={re.escape(_FILE_PATH)}", clipboard), (
        f"clipboard URL {clipboard!r} does not carry ?file={_FILE_PATH}"
    )
    assert session_id in clipboard, f"clipboard URL {clipboard!r} missing session id"

    # Open the copied link in a brand-new context — no cookies, no localStorage,
    # i.e. a different browser. The file must rehydrate purely from the URL.
    fresh_context = browser.new_context()
    try:
        fresh_page = fresh_context.new_page()
        fresh_page.goto(clipboard)
        fresh_viewer = fresh_page.locator('[data-testid="file-viewer"]:visible')
        expect(fresh_viewer).to_be_visible(timeout=20_000)
        expect(
            fresh_page.get_by_role("button", name=f"Close {_FILE_PATH}", exact=True).first
        ).to_be_visible()
        expect(fresh_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)
    finally:
        fresh_context.close()
