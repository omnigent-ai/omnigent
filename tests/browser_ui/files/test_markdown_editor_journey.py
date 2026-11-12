"""Repeatable Markdown editing and loading journeys for recorded comparisons."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

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


def _comment(session: BrowserSession, start: int, end: int, anchor: str) -> dict:
    return {
        "id": "boundary-comment",
        "conversation_id": session.session_id,
        "path": _PATH,
        "body": "Boundary regression",
        "start_index": start,
        "end_index": end,
        "anchor_content": anchor,
        "status": "draft",
        "created_at": 1,
        "updated_at": 1,
        "created_by": None,
    }


def _highlights(editor: Locator) -> list[dict]:
    return editor.locator('[data-comment-id="boundary-comment"]').evaluate_all(
        """elements => elements.map(element => ({
            text: element.textContent,
            from: element.closest('.tiptap').editor.view.posAtDOM(element, 0),
        }))"""
    )


@pytest.mark.parametrize(
    "raw,selection,expected_raw,expected_highlight",
    [
        (
            "alpha\r\nbeta",
            [1, 7],
            [0, 7],
            [{"text": "alpha", "from": 1}, {"text": " ", "from": 6}],
        ),
        ("word  \nnext\n\nword\n\n[details](word)", [1, 6], [0, 4], [{"text": "word", "from": 1}]),
        (
            "[label](" + "x" * 59 + ") word **word**![alt](image.png)\n\n",
            [12, 17],
            [76, 80],
            [{"text": "word", "from": 12}],
        ),
    ],
    ids=["crlf-soft-break", "hard-break-hidden-url", "image-proportional-collision"],
)
def test_markdown_comment_boundary_save_reload(
    page: Page,
    seeded_session: BrowserSession,
    output_path: str,
    raw: str,
    selection: list[int],
    expected_raw: list[int],
    expected_highlight: list[dict],
) -> None:
    _mock_markdown_files(page, seeded_session, {_PATH: raw})
    seeded_session.contract.json(
        f"/v1/sessions/{seeded_session.session_id}/resources/environments/default/filesystem/src/image.png",
        {"error": "Synthetic missing image"},
        status=404,
    )

    def save(request: Request) -> dict:
        data = request.post_data_json
        saved = _comment(
            seeded_session, data["start_index"], data["end_index"], data["anchor_content"]
        )
        seeded_session.comments.append(saved)
        return saved

    seeded_session.contract.json(
        f"/v1/sessions/{seeded_session.session_id}/comments", save, method="POST"
    )
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    editor = viewer.locator(".tiptap")
    expect(editor).to_be_visible(timeout=30_000)
    editor.evaluate(
        """(element, [from, to]) => {
        const view = element.editor.view;
        view.focus();
        const start = view.domAtPos(from), end = view.domAtPos(to);
        const range = document.createRange();
        range.setStart(start.node, start.offset); range.setEnd(end.node, end.offset);
        const selection = window.getSelection();
        selection.removeAllRanges(); selection.addRange(range);
    }""",
        selection,
    )
    page.get_by_role("button", name="Add comment", exact=True).click()
    viewer.locator("textarea[placeholder='Add a comment…']").fill("Boundary regression")
    viewer.get_by_role("button", name="Add Comment", exact=True).click()
    expect(editor.locator('[data-comment-id="boundary-comment"]').first).to_be_visible()
    saved = seeded_session.comments[0]
    assert [saved["start_index"], saved["end_index"]] == expected_raw
    assert _highlights(editor) == expected_highlight
    page.reload()
    expect(editor.locator('[data-comment-id="boundary-comment"]').first).to_be_visible(
        timeout=30_000
    )
    assert _highlights(editor) == expected_highlight
    target = Path(output_path) / "comment-boundary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"saved": saved, "highlights": _highlights(editor)}, indent=2))


def test_markdown_existing_comment_after_autosave(
    page: Page,
    seeded_session: BrowserSession,
) -> None:
    raw = "word **word** word\n\n[Details](https://example.invalid/" + "x" * 100 + ")"
    files = {_PATH: raw}
    _mock_markdown_files(page, seeded_session, files)
    seeded_session.comments.append(_comment(seeded_session, 7, 11, "word"))
    writes = []

    def save(request: Request) -> dict:
        files[_PATH] = request.post_data_json["content"]
        writes.append(files[_PATH])
        return {"saved": True}

    seeded_session.contract.json(
        f"/v1/sessions/{seeded_session.session_id}/resources/environments/default/filesystem/{_PATH}",
        save,
        method="PUT",
    )
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    editor = viewer.locator(".tiptap")
    expect(editor).to_be_visible(timeout=30_000)
    assert _highlights(editor) == [{"text": "word", "from": 6}]
    editor.evaluate(
        """element => {
            element.editor.view.focus();
            element.editor.commands.setTextSelection(element.editor.state.doc.content.size - 1);
        }"""
    )
    page.keyboard.press("Enter")
    page.keyboard.type("tail " * 80)
    expect(viewer.get_by_role("button", name="All changes saved")).to_be_visible(timeout=15_000)
    assert writes and files[_PATH].startswith("word **word** word")
    assert _highlights(editor) == [{"text": "word", "from": 6}]
    page.reload()
    expect(editor.locator('[data-comment-id="boundary-comment"]')).to_be_visible(timeout=30_000)
    assert _highlights(editor) == [{"text": "word", "from": 6}]


@pytest.mark.parametrize(
    "updated,chunk_failures",
    [
        ("prefix say **word** now", 0),
        ("prefix say word now", 0),
        ("prefix say word now", 1),
    ],
    ids=["formatting-retained", "formatting-removed", "chunk-load-fallback"],
)
def test_markdown_source_comment_after_external_edit(
    page: Page,
    seeded_session: BrowserSession,
    updated: str,
    chunk_failures: int,
) -> None:
    original = "say **word** now"
    files = {_PATH: original}
    _mock_markdown_files(page, seeded_session, files)
    seeded_session.comments.append(_comment(seeded_session, 0, len(original), "say word now"))
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    editor = viewer.locator(".tiptap")
    expect(editor).to_be_visible(timeout=30_000)
    expect(editor.locator('[data-comment-id="boundary-comment"]').first).to_be_visible()
    files[_PATH] = updated
    attempts = []
    if chunk_failures:

        def load_chunk(route: Route) -> None:
            attempts.append(route.request.url)
            if len(attempts) <= chunk_failures:
                route.abort()
            else:
                route.continue_()

        page.route("**/markdownCommentRemap-*.js", load_chunk)
    page.reload()
    expect(editor).to_contain_text("prefix say word now", timeout=30_000)
    viewer.get_by_role("button", name=re.compile(r"^View mode")).click()
    page.get_by_role("menuitem", name="Source", exact=True).click()
    line = viewer.locator("[data-line='1']")
    expect(line).to_contain_text(updated)
    overlay = line.locator("span[aria-hidden][style*='ch']").first
    expect(overlay).to_have_attribute("style", re.compile(r"^left: calc\(7ch \+ 0\.75rem\);"))
    assert overlay.evaluate("element => element.style.width") == f"{len(updated) - 7}ch"
    if chunk_failures:
        assert len(attempts) == 1


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
    # Exercise the native menu from both pointer and keyboard focus before
    # verifying that the fence language survives autosave and reload.
    pickers = editor.get_by_role("combobox", name="Code block language")
    pickers.first.hover()
    pickers.first.select_option("typescript")
    picker = pickers.last
    picker.focus()
    picker.press("End")
    picker.press("Enter")
    expect(picker).to_have_value("yaml")
    expect(viewer.get_by_role("button", name="All changes saved")).to_be_visible(timeout=15_000)
    assert writes and "# caret stays in the code block" in writes[-1]
    assert "```yaml" in writes[-1]
    assert "```typescript" in writes[-1]

    page.reload()
    expect(editor.locator("pre").last).to_contain_text("journey edit")
    expect(pickers.first).to_have_value("typescript")
    expect(pickers.last).to_have_value("yaml")
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
    _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "editor"}
    )
    # Locator assertions collect an accessibility snapshot of the whole editor.
    # Wait on the terminal marker directly so that work cannot inflate timings.
    page.wait_for_function(
        """() => {
            const editor = document.querySelector('.tiptap');
            return editor?.lastElementChild?.textContent === 'Load journey complete.'
                && editor.checkVisibility();
        }""",
        timeout=180_000,
    )
    metrics = page.evaluate("""async () => {
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
        return {...window.markdownProbe, navigationToReadyMs: performance.now(),
            editors: document.querySelectorAll('.tiptap').length};
    }""")
    assert metrics["editors"] == 1
    metrics.update(
        readiness_probe="terminal-marker",
        fixture_sha256=hashlib.sha256(content.encode()).hexdigest(),
        bytes=len(content.encode()),
        browser=page.context.browser.version,
        viewport=page.viewport_size,
    )
    target = Path(output_path) / "markdown-load.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, indent=2) + "\n")
