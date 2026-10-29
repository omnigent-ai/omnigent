"""E2E: Markdown editor links open directly, with a keyboard-accessible popup.

Plain clicks open web links in a new page and workspace-relative links in the
FileViewer. Alt-click places the caret for editing instead. Alt+Enter moves
focus to the popup's Open action, and the popup follows its link on resize.
Seeded via the filesystem PUT endpoint (no agent run); the linked site is a
routed stub, so the test never reaches the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

_MARKDOWN_FILE_PATH = "editor_link_popup.md"
_LINKED_FILE_PATH = "editor_link_popup_plan.md"

_LINK_URL = "https://tracker.external.example/browse/ES-1"

# The web link sits in a table cell, as in the original report.
_MARKDOWN_CONTENT = f"""\
# Capacity requests

| Cheat sheet |
| ----------- |
| **Example ticket.** [ES-1]({_LINK_URL}), raising the quota |

Read [the plan]({_LINKED_FILE_PATH}) first.
"""

_LINKED_FILE_CONTENT = """\
# Rollout plan

Steps for the rollout.
"""


@pytest.fixture
def seeded_link_popup_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    filesystem_url = (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    )
    seeded = {
        _MARKDOWN_FILE_PATH: _MARKDOWN_CONTENT,
        _LINKED_FILE_PATH: _LINKED_FILE_CONTENT,
    }
    for path, content in seeded.items():
        resp = httpx.put(
            f"{filesystem_url}/{path}",
            json={"content": content, "encoding": "utf-8"},
            timeout=10.0,
        )
        resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seeds here.
        for path in seeded:
            httpx.delete(f"{filesystem_url}/{path}", timeout=10.0)


def _fulfill_external_page(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="text/html",
        body="<title>ES-1</title><h1>ES-1</h1>",
    )


def _left(locator: Locator) -> float:
    box = locator.bounding_box()
    assert box is not None
    return box["x"]


def _resize_handle_left_of(page: Page, panel: Locator) -> Locator:
    """The resize handle on ``panel``'s left edge; the panel that hosts the
    FileViewer owns it, so it can sit outside the viewer element."""
    panel_left = _left(panel)
    for handle in page.get_by_role("separator", name="Resize panel").all():
        box = handle.bounding_box()
        if box and box["height"] > 0 and abs(box["x"] - panel_left) < 8:
            return handle
    raise AssertionError("no resize handle on the FileViewer's left edge")


def _drag(page: Page, handle: Locator, dx: float) -> None:
    box = handle.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y, steps=10)
    page.mouse.up()


def test_editor_link_popup_opens_web_link_in_new_page(
    page: Page,
    seeded_link_popup_session: tuple[str, str],
) -> None:
    """Alt-click keeps the link editable; the popup can still open a new page."""
    base_url, session_id = seeded_link_popup_session
    page.context.route("https://tracker.external.example/**", _fulfill_external_page)
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    link = editor.get_by_role("link", name="ES-1")
    expect(link).to_be_visible()

    link.click(modifiers=["Alt"])
    popup = page.locator("[data-link-bubble]")
    expect(popup).to_be_visible()
    expect(popup).to_contain_text(_LINK_URL)
    open_link = popup.get_by_role("link", name="Open")
    expect(open_link).to_have_attribute("href", _LINK_URL)

    # Pressing the popup keeps the editor focused, so the caret, and the
    # popup, stay in the link.
    popup.get_by_text(_LINK_URL).click()
    expect(editor).to_be_focused()
    expect(popup).to_be_visible()

    # Narrowing the FileViewer from its resize handle reflows the link without
    # a window resize; the popup follows it: below the link, shifted left only
    # as far as the viewport's right edge requires.
    link_left = _left(link)
    _drag(page, _resize_handle_left_of(page, file_viewer), 150)
    expect(editor).to_be_focused()
    assert _left(link) > link_left + 100
    page.wait_for_function(
        """() => {
          const popup = document.querySelector("[data-link-bubble]");
          const link = [...document.querySelectorAll("[contenteditable='true'] a")]
            .find((a) => a.textContent === "ES-1");
          if (!popup || !link) return false;
          const p = popup.getBoundingClientRect();
          const l = link.getBoundingClientRect();
          const left = Math.min(l.left, window.innerWidth - 8 - p.width);
          return Math.abs(p.left - left) < 2 && Math.abs(p.top - (l.bottom + 4)) < 2;
        }""",
        timeout=5_000,
    )

    with page.context.expect_page(timeout=10_000) as new_page_info:
        open_link.click()
    new_page = new_page_info.value
    new_page.wait_for_load_state()
    expect(new_page).to_have_url(_LINK_URL)
    expect(new_page.get_by_role("heading", name="ES-1")).to_be_visible()

    expect(page).to_have_url(re.compile(rf"/c/{session_id}"))
    expect(editor).to_be_visible()


def test_editor_link_popup_opens_relative_link_from_keyboard(
    page: Page,
    seeded_link_popup_session: tuple[str, str],
) -> None:
    """Alt+Enter, then Enter, on a workspace-relative link shows that file."""
    base_url, session_id = seeded_link_popup_session
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)

    editor.get_by_role("link", name="the plan").click(modifiers=["Alt"])
    popup = page.locator("[data-link-bubble]")
    expect(popup).to_contain_text(_LINKED_FILE_PATH)
    page.keyboard.press("Alt+Enter")
    open_button = popup.get_by_role("button", name="Open")
    expect(open_button).to_be_focused()
    page.keyboard.press("Enter")

    expect(page).to_have_url(re.compile(rf"file={re.escape(_LINKED_FILE_PATH)}"))
    expect(file_viewer.get_by_text("Steps for the rollout.")).to_be_visible(timeout=10_000)


@pytest.mark.browser_context_args(has_touch=True)
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile-touch"])
def test_editor_links_open_with_one_click(
    page: Page,
    seeded_link_popup_session: tuple[str, str],
    mobile: bool,
) -> None:
    """One click/tap follows table and relative links without a popup action."""
    page.set_viewport_size({"width": 390 if mobile else 1600, "height": 900})
    base_url, session_id = seeded_link_popup_session
    page.context.route("https://tracker.external.example/**", _fulfill_external_page)
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    link = editor.get_by_role("link", name="ES-1")

    if not mobile:
        # Selecting a link's text must not follow it on mouseup.
        box = link.bounding_box()
        assert box is not None
        y = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + 1, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 1, y, steps=10)
        page.mouse.up()
        assert page.evaluate("window.getSelection().toString()") == "ES-1"
        assert len(page.context.pages) == 1

    with page.context.expect_page(timeout=10_000) as new_page_info:
        link.tap() if mobile else link.click()
    new_page = new_page_info.value
    expect(new_page).to_have_url(_LINK_URL)
    expect(new_page.get_by_role("heading", name="ES-1")).to_be_visible()
    new_page.close()
    expect(page).to_have_url(re.compile(rf"/c/{session_id}"))

    relative_link = editor.get_by_role("link", name="the plan")
    relative_link.tap() if mobile else relative_link.click()
    expect(page).to_have_url(re.compile(rf"file={re.escape(_LINKED_FILE_PATH)}"))
    expect(file_viewer.get_by_text("Steps for the rollout.")).to_be_visible(timeout=10_000)
    assert len(page.context.pages) == 1

    # Navigation and selection must not rewrite the markdown through autosave.
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
        f"/{_MARKDOWN_FILE_PATH}",
        timeout=10.0,
    )
    response.raise_for_status()
    assert response.json()["content"] == _MARKDOWN_CONTENT
