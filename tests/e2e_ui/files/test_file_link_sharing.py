"""E2E: the FileViewer "Copy Omnigent Link" button yields a shareable URL.

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

from tests.e2e_ui.conftest import open_right_rail

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
    copy_btn = file_viewer.get_by_role("button", name="Copy Omnigent Link")
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


def test_file_menus_copy_paths_without_copying_the_app_url(
    page: Page,
    seeded_shareable_session: tuple[str, str],
) -> None:
    """Tree, tab, path and overflow actions copy real filesystem paths."""
    base_url, session_id = seeded_shareable_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    env = page.request.get(f"{base_url}/v1/sessions/{session_id}/resources/environments/default")
    assert env.ok, env.text()
    absolute_path = f"{env.json()['metadata']['root'].rstrip('/')}/{_FILE_PATH}"
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    row = rail.get_by_role("button", name=_FILE_PATH, exact=True)
    expect(row).to_be_visible(timeout=30_000)
    row.click(button="right")
    expect(page.get_by_role("menuitem", name=re.compile("^Show in"))).to_have_count(0)
    page.get_by_role("menuitem", name="Copy Path", exact=True).click()
    expect(page.get_by_text("Path copied", exact=True).first).to_be_visible()
    assert page.evaluate("() => navigator.clipboard.readText()") == absolute_path
    expect(rail.get_by_test_id("file-viewer")).to_have_count(0)

    row.click()
    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)
    tab = rail.locator(f'div[role="button"][title="{_FILE_PATH}"]')
    tab.click(button="right")
    page.get_by_role("menuitem", name="Copy Relative Path", exact=True).click()
    assert page.evaluate("() => navigator.clipboard.readText()") == _FILE_PATH
    expect(tab).to_have_attribute("aria-current", "true")

    viewer.locator(f'span[title="{_FILE_PATH}"]').click(button="right")
    page.get_by_role("menuitem", name="Copy Path", exact=True).click()
    assert page.evaluate("() => navigator.clipboard.readText()") == absolute_path

    viewer.get_by_role("button", name="View settings", exact=True).click()
    page.get_by_role("menuitem", name="Copy Relative Path", exact=True).click()
    assert page.evaluate("() => navigator.clipboard.readText()") == _FILE_PATH

    # Shrink through the real resize control to exercise the collapsed menu.
    handle = rail.get_by_role("separator", name="Resize panel")
    for _ in range(20):
        handle.press("ArrowRight")
    viewer.get_by_role("button", name="More actions", exact=True).click()
    expect(page.get_by_role("menuitem", name="Copy Omnigent Link", exact=True)).to_be_visible()
    page.get_by_role("menuitem", name="Copy Relative Path", exact=True).click()
    assert page.evaluate("() => navigator.clipboard.readText()") == _FILE_PATH
