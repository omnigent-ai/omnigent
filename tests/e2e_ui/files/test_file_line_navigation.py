"""Browser regressions for chat citations into collapsed Monaco diff context.

Only the file API responses are fixtures; chat links, view selection, diff
calculation, collapsed regions, and scrolling run through the real UI.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Page, expect

_FILE_PATH = "src/citation_target.py"
_BEFORE_LINES = [f"# original source line {line}" for line in range(1, 501)]
_AFTER_LINES = [f"# inserted line {line}" for line in range(1, 6)] + _BEFORE_LINES
_AFTER_LINES[350] = "# changed current line 351"
_BEFORE = "\n".join(_BEFORE_LINES)
_AFTER = "\n".join(_AFTER_LINES)

_CENTERED_LINE = """text => {
  for (const line of document.querySelectorAll(
    '[data-testid="file-viewer"] .modified .view-line'
  )) {
    if (line.textContent.replace(/\u00a0/g, ' ') !== text) continue;
    const rect = line.getBoundingClientRect();
    const editor = line.closest('.monaco-editor').getBoundingClientRect();
    if (!rect.height || !editor.height) continue;
    if (Math.abs((rect.top + rect.bottom - editor.top - editor.bottom) / 2) < 25) return true;
  }
  return false;
}"""


@pytest.mark.parametrize("layout", ["split", "unified"])
def test_chat_line_link_expands_and_centers_diff_context(
    page: Page,
    seeded_session: tuple[str, str],
    layout: str,
) -> None:
    """Click hidden current-file lines without changing the selected diff view."""
    base_url, session_id = seeded_session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        f"{environment_url}/changes",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {
                        "path": _FILE_PATH,
                        "name": _FILE_PATH,
                        "status": "modified",
                        "bytes": len(_AFTER),
                        "modified_at": 1,
                    }
                ],
            }
        ),
    )
    page.route(
        f"{environment_url}/filesystem/{_FILE_PATH}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": _FILE_PATH,
                "content": _AFTER,
                "encoding": "utf-8",
                "content_type": "text/plain",
                "bytes": len(_AFTER),
            }
        ),
    )
    page.route(
        f"{environment_url}/diff/{_FILE_PATH}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_diff",
                "path": _FILE_PATH,
                "before": _BEFORE,
                "after": _AFTER,
            }
        ),
    )
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "hello_world",
                "text": f"[Line 100]({_FILE_PATH}:100) and [Line 200]({_FILE_PATH}:200)",
            },
        },
        timeout=10,
    )
    response.raise_for_status()
    # Keep the rail wide enough for Monaco's actual side-by-side layout.
    page.set_viewport_size({"width": 3200, "height": 1000})
    preferences = json.dumps({"diffActive": True, "diffLayout": layout})
    page.add_init_script(
        f"localStorage.setItem('omnigent:file-view-preferences', {json.dumps(preferences)});"
    )
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}&diff=1")

    viewer = page.locator('[data-testid="file-viewer"]:visible')
    diff = viewer.locator(".monaco-diff-editor")
    expect(diff).to_be_visible(timeout=30_000)
    modified = diff.locator(".modified .view-lines")
    expect(diff.get_by_text("339 hidden lines", exact=True)).to_be_visible(timeout=20_000)
    expect(modified.get_by_text(_AFTER_LINES[350], exact=True)).to_be_visible()
    expect(modified.get_by_text(_AFTER_LINES[99], exact=True)).to_have_count(0)
    separator = page.get_by_role("separator", name="Resize panel", exact=True)
    box = separator.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(1600, box["y"] + box["height"] / 2)
    page.mouse.up()
    if layout == "split":
        expect(diff).to_have_class(re.compile(r"\bside-by-side\b"))
    else:
        expect(diff).not_to_have_class(re.compile(r"\bside-by-side\b"))

    for line in (100, 200, 100):
        page.get_by_role("button", name=f"Line {line}", exact=True).click()
        # The five inserted lines ensure original/current line numbers differ.
        expect(modified.get_by_text(_AFTER_LINES[line - 1], exact=True)).to_be_visible()
        page.wait_for_function(_CENTERED_LINE, arg=_AFTER_LINES[line - 1], timeout=10_000)
        expect(diff).to_be_visible()
        expect(page).to_have_url(re.compile(r"[?&]diff=1(?:&|$)"))
