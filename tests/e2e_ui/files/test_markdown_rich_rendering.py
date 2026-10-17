"""E2E: rich markdown rendering and source toggle in the FileViewer.

Counterpart to ``test_markdown_editor_comments.py`` (comment flow): this
covers the *rendering* half — a GFM fixture must render as styled HTML in the
rich editor, the source toggle must flip to raw markdown, and a code fence
typed into the live editor must keep the caret. Seeded via the filesystem PUT
endpoint (no agent run).
"""

from __future__ import annotations

import contextlib
import re
import textwrap
from collections.abc import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_MARKDOWN_FILE_PATH = "design_rich.md"
_WRAPPED_PARAGRAPH = textwrap.fill(
    "This design note describes how the workspace file viewer renders Markdown "
    "documents in its three modes. The rendered preview reflows every paragraph to "
    "the full width of the viewer, while the editing surface must keep the text "
    "selectable so reviewers can anchor comments to exact phrases in the file. Both "
    "surfaces read the same source bytes, so any difference in how far a line of "
    "prose extends across the panel comes from the way each surface lays the text "
    "out rather than from the document itself.",
    width=80,
)

# A GitHub-flavored markdown fixture covering the rich-rendering construct
# matrix: headings, unordered + ordered lists, a fenced code block, a
# table, a link, inline code/bold, and a blockquote.
_MARKDOWN_CONTENT = f"""\
# Rich Design Document

A short intro paragraph with **bold text**, _italics_, and `inline_code`.

{_WRAPPED_PARAGRAPH}

## Goals

- First bullet point
- Second bullet point
- Third bullet point

## Steps

1. Step one
2. Step two
3. Step three

## Example

```python
def add(a: int, b: int) -> int:
    return a + b
```

## Comparison Table

| Option | Latency | Notes        |
| ------ | ------- | ------------ |
| Alpha  | Low     | Preferred    |
| Beta   | High    | Fallback     |

## Reference

See the [project homepage](https://example.databricks.com/docs) for details.

> A blockquote with a caution about scope.
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_rich_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the rich markdown file and yield (base_url, session_id, path).

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _MARKDOWN_FILE_PATH)
    finally:
        # Session deletion leaves workspace files behind.
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(file_url, timeout=10.0).raise_for_status()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.browser_context_args(viewport={"width": 1600, "height": 900})
def test_markdown_renders_rich_constructs_and_source_toggle(
    page: Page,
    seeded_rich_markdown_session: tuple[str, str, str],
) -> None:
    """Rich markdown constructs render as HTML; source toggle shows raw text."""
    base_url, session_id, _file_path = seeded_rich_markdown_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)

    file_button = page.get_by_role(
        "button", name=re.compile(rf"^{re.escape(_MARKDOWN_FILE_PATH)}\b")
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (the mobile
    # push-panel, md:hidden, and the desktop rail one). Match the visible
    # one directly rather than by DOM order — order is not guaranteed.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    # The open file is identified by its tab (the desktop viewer header no
    # longer repeats a top-level filename — it's redundant with the tab).
    # exact=True targets the close button, not the tab div whose accessible
    # name also contains "Close <name>".
    expect(
        page.get_by_role("button", name=f"Close {_MARKDOWN_FILE_PATH}", exact=True).first
    ).to_be_visible()

    # Edit mode renders each construct as its semantic HTML element.
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor.locator("h1")).to_contain_text("Rich Design Document")
    expect(editor.locator("h2").filter(has_text="Goals")).to_be_visible()
    expect(editor.locator("ul li").filter(has_text="First bullet point")).to_be_visible()
    expect(editor.locator("ol li").filter(has_text="Step one")).to_be_visible()
    expect(editor.locator("pre")).to_contain_text("def add(a: int, b: int)")
    expect(editor.locator("table")).to_be_visible()
    expect(editor.locator("th").filter(has_text="Latency")).to_be_visible()
    expect(editor.locator("td").filter(has_text="Preferred")).to_be_visible()
    link = editor.locator("a").filter(has_text="project homepage")
    expect(link).to_have_attribute("href", "https://example.databricks.com/docs")
    expect(editor.locator("blockquote")).to_contain_text("caution about scope")
    # Parsed, not dumped verbatim: the heading text is "Goals", never "## Goals".
    expect(editor.locator("h2").filter(has_text="Goals")).not_to_contain_text("##")

    page.get_by_role("button", name="Full screen", exact=True).click()
    for mode, root in (("Preview", ".markdown-preview"), ("Edit", ".ProseMirror")):
        switch_markdown_view_mode(page, file_viewer, mode)
        paragraph = file_viewer.locator(f"{root} p").filter(has_text="This design note")
        expect(paragraph).to_be_visible()
        layout = paragraph.evaluate("""paragraph => {
            const range = document.createRange();
            range.selectNodeContents(paragraph);
            const box = paragraph.getBoundingClientRect();
            return {
                width: box.width,
                fill: (range.getBoundingClientRect().right - box.left) / box.width,
            };
        }""")
        # Eighty-column source lines cannot fill a paragraph this wide.
        assert layout["width"] > 900, layout
        assert layout["fill"] > 0.9, f"{mode} paragraph does not fill the viewer: {layout}"

    # Copy across a reflowed source newline without replacing it with a space.
    lines = _WRAPPED_PARAGRAPH.splitlines()
    last_word, first_word = lines[0].rsplit(" ", 1)[1], lines[1].split(" ", 1)[0]
    editor.locator("h1").click()
    editor.locator("span.md-soft-break").first.evaluate(
        """(span, [before, after]) => {
            window.getSelection().setBaseAndExtent(
                span.previousSibling, span.previousSibling.textContent.length - before,
                span.nextSibling, after,
            );
            window.__copiedText = null;
            document.addEventListener("copy", event => {
                window.__copiedText = event.clipboardData.getData("text/plain");
            }, {once: true});
        }""",
        [len(last_word), len(first_word)],
    )
    expect(
        page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    ).to_be_visible()
    page.keyboard.press("ControlOrMeta+c")
    page.wait_for_function("() => window.__copiedText !== null")
    assert page.evaluate("window.__copiedText") == f"{last_word}\n{first_word}"

    # Source view: raw markdown becomes visible, no contenteditable editor.
    switch_markdown_view_mode(page, file_viewer, "Source")
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)
    expect(file_viewer.get_by_text("## Goals", exact=False)).to_be_visible(timeout=10_000)
    expect(file_viewer.get_by_text("```python", exact=False)).to_be_visible()
    expect(file_viewer.get_by_text("| Option | Latency |", exact=False)).to_be_visible()

    # Switch back to the rich editor.
    switch_markdown_view_mode(page, file_viewer, "Edit")
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor.locator("h1")).to_contain_text("Rich Design Document")


def _let_editor_follow_caret(page: Page) -> None:
    """Yield a frame so ProseMirror adopts a native caret move.

    Clicks and Home/End move the DOM caret directly; ProseMirror reads it on the
    asynchronous ``selectionchange`` event, so a key sent in the same turn would
    still apply at the editor's previous selection.
    """
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve)))")


def _file_saved_and_reread(file_path: str, containing: str) -> Callable[[Response], bool]:
    """Match the GET that re-reads ``file_path`` after the auto-save PUT of an edit.

    The write invalidates the file query, and a clean editor adopts whatever
    that refetch returns, resetting the caret; edits must not straddle it. The
    PUT must carry ``containing`` so an earlier, partial save does not satisfy it.
    """
    saw_put = False

    def match(response: Response) -> bool:
        nonlocal saw_put
        if not response.url.split("?")[0].endswith(f"/filesystem/{file_path}"):
            return False
        if (
            response.ok
            and response.request.method == "PUT"
            and containing in (response.request.post_data or "")
        ):
            saw_put = True
        return saw_put and response.ok and response.request.method == "GET"

    return match


@pytest.mark.browser_context_args(viewport={"width": 1600, "height": 900})
def test_code_fence_typed_into_the_editor_keeps_the_caret_inside(
    page: Page,
    seeded_rich_markdown_session: tuple[str, str, str],
) -> None:
    """Fences keep the caret: one mounted unfocused on load, one created while typing."""
    base_url, session_id, file_path = seeded_rich_markdown_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)
    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor.locator("h1")).to_contain_text("Rich Design Document", timeout=10_000)
    expect(editor.locator("pre")).to_have_count(1)
    saved = file_viewer.get_by_role("button", name="All changes saved")

    # The seeded fence mounted while the editor was unfocused; edits land in it.
    seeded_fence = editor.locator("pre").first
    seeded_fence.click()
    page.keyboard.press("End")
    _let_editor_follow_caret(page)
    with page.expect_response(_file_saved_and_reread(file_path, "# edited"), timeout=20_000):
        page.keyboard.type("  # edited")
        expect(seeded_fence).to_contain_text("# edited")
    expect(saved).to_be_visible(timeout=15_000)

    # A new paragraph after the heading becomes a code block through the
    # ``` input rule, so its node view mounts while the editor has focus.
    page.keyboard.press("Control+Home")
    _let_editor_follow_caret(page)
    page.keyboard.press("End")
    _let_editor_follow_caret(page)
    page.keyboard.press("Enter")
    with page.expect_response(_file_saved_and_reread(file_path, "fenced text"), timeout=20_000):
        page.keyboard.type("``` ")
        expect(editor.locator("pre")).to_have_count(2)
        page.keyboard.type("fenced text")
        # Read the caret right after typing, before any auto-save refetch.
        caret_in_fence = editor.evaluate(
            """() => {
            const node = document.getSelection()?.anchorNode;
            const element = node?.nodeType === Node.TEXT_NODE ? node.parentElement : node;
            return !!element?.closest("pre");
        }"""
        )
        fence = editor.locator("pre").filter(has_text="fenced text")
        expect(fence).to_be_visible()
        expect(editor.locator("p").filter(has_text="fenced text")).to_have_count(0)
    assert caret_in_fence, "the caret left the code block after its node view mounted"
    expect(saved).to_be_visible(timeout=15_000)
