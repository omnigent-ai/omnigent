"""Repeatable Markdown editing and loading journeys for recorded comparisons."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
    _open_viewer,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture
pytestmark = pytest.mark.browser_context_args(
    viewport={"width": 1600, "height": 1000},
    record_video_size={"width": 1600, "height": 1000},
    device_scale_factor=1,
    color_scheme="light",
    locale="en-US",
    timezone_id="UTC",
)

_CONTENT = Path(__file__).with_name("markdown_editor_journey.md").read_text()
_PATH = "src/markdown-editor-journey.md"


def test_markdown_editor_review_journey(page: Page, seeded_session: BrowserSession) -> None:
    """Read, edit, save/reload, search, switch modes/layouts, then draft a review."""
    files = {_PATH: _CONTENT}
    _mock_markdown_files(page, seeded_session, files)
    _, session_id = seeded_session
    writes: list[str] = []

    def save(request: Request) -> dict[str, bool]:
        files[_PATH] = request.post_data_json["content"]
        writes.append(files[_PATH])
        return {"saved": True}

    seeded_session.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{_PATH}",
        save,
        method="PUT",
    )
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    editor = viewer.locator(".tiptap")
    expect(
        editor.get_by_role("heading", name="Markdown editor journey", exact=True)
    ).to_be_visible()
    expect(editor.locator("table")).to_contain_text("Preserve edits")
    expect(editor.locator("pre")).to_have_text('print("original example")')
    expect(editor.locator("ol li")).to_have_count(2)

    editor.get_by_text("Journey end.", exact=True).click(click_count=3)
    page.wait_for_function("window.getSelection()?.toString().trim() === 'Journey end.'")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    page.keyboard.type("```python")
    page.keyboard.press("Enter")
    page.keyboard.type('print("journey edit")')
    page.keyboard.press("Enter")
    page.keyboard.type("# caret stays in the code block")
    expect(editor.locator("pre").last).to_have_text(
        'print("journey edit")\n# caret stays in the code block'
    )
    assert editor.locator("pre").last.evaluate(
        "el => el.contains(document.getSelection().anchorNode)"
    )
    expect(viewer.get_by_role("button", name="All changes saved")).to_be_visible(timeout=15_000)
    assert writes and "# caret stays in the code block" in writes[-1]

    page.reload()
    expect(editor.locator("pre").last).to_contain_text("journey edit")
    editor.locator("pre").last.click()
    page.keyboard.press("ControlOrMeta+f")
    search = viewer.get_by_placeholder(re.compile(r"^Find"))
    expect(search).to_be_focused()
    search.fill("journey edit")
    expect(viewer.get_by_text("1 / 1", exact=True)).to_be_visible()
    viewer.get_by_role("button", name="Close search", exact=True).click()

    for mode in ("Source", "Preview", "Edit"):
        viewer.get_by_role("button", name=re.compile(r"^View mode")).click()
        page.get_by_role("menuitem", name=mode, exact=True).click()
        if mode == "Source":
            expect(viewer.locator("[data-line]").filter(has_text="journey edit")).to_be_visible()
        else:
            selector = ".markdown-preview" if mode == "Preview" else ".tiptap"
            expect(viewer.locator(selector)).to_contain_text("journey edit", timeout=30_000)

    for width in (390, 1600):
        page.set_viewport_size({"width": width, "height": 1000})
        expect(editor).to_be_visible()
        expect(editor.locator("pre").last).to_contain_text("journey edit")

    # Select the review anchor directly, without racing focus reconciliation
    # against a burst of cursor-navigation keys.
    editor.get_by_text(
        "Review this document before sharing the implementation plan.", exact=True
    ).dblclick(position={"x": 10, "y": 10})
    page.wait_for_function("window.getSelection()?.toString() === 'Review'")
    page.get_by_role("button", name="Add comment", exact=True).click()
    expect(viewer.get_by_text("Selection: Review", exact=True)).to_be_visible()
    draft = viewer.get_by_placeholder("Add a comment", exact=False)
    draft.fill("Please review this example before sharing.")
    expect(draft).to_have_value("Please review this example before sharing.")
    expect(draft).to_be_focused()


@pytest.mark.parametrize("kib", [64, 256])
def test_markdown_editor_load_journey(
    page: Page, seeded_session: BrowserSession, kib: int, output_path: str
) -> None:
    """Record real elapsed load time without imposing a machine-specific limit."""
    content = _CONTENT * (kib * 1024 // len(_CONTENT.encode())) + "\nLoad journey complete.\n"
    _mock_markdown_files(page, seeded_session, {_PATH: content})
    page.add_init_script("""
        window.markdownProbe = {selectionReads: 0, longestTaskMs: 0};
        new PerformanceObserver(list => {
            for (const task of list.getEntries()) {
                window.markdownProbe.longestTaskMs = Math.max(
                    window.markdownProbe.longestTaskMs, task.duration);
            }
        }).observe({type: 'longtask', buffered: true});
        const getSelection = document.getSelection.bind(document);
        document.getSelection = (...args) => {
            window.markdownProbe.selectionReads++;
            return getSelection(...args);
        };
    """)
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    expect(viewer.locator(".tiptap")).to_contain_text("Load journey complete.", timeout=180_000)
    metrics = page.evaluate("""async () => {
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
        return {...window.markdownProbe, navigationToReadyMs: performance.now(),
            editors: document.querySelectorAll('.tiptap').length};
    }""")
    metrics.update(
        fixture_sha256=hashlib.sha256(content.encode()).hexdigest(),
        bytes=len(content.encode()),
        browser=page.context.browser.version,
        viewport=page.viewport_size,
    )
    target = Path(output_path) / "markdown-load.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, indent=2) + "\n")
