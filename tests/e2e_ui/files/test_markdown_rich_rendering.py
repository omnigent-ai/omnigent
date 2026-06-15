"""E2E: rich markdown rendering and source toggle in the FileViewer.

Counterpart to ``test_markdown_editor_comments.py`` (comment flow): this
covers the *rendering* half — a GFM fixture must render as styled HTML in the
rich editor, and the source toggle must flip to raw markdown. Seeded via the
filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_MARKDOWN_FILE_PATH = "design_rich.md"

# A GitHub-flavored markdown fixture covering the rich-rendering construct
# matrix: headings, unordered + ordered lists, a fenced code block, a
# table, a link, inline code/bold, and a blockquote.
_MARKDOWN_CONTENT = """\
# Rich Design Document

A short intro paragraph with **bold text**, _italics_, and `inline_code`.

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
    yield (base_url, session_id, _MARKDOWN_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_markdown_renders_rich_constructs_and_source_toggle(
    page: Page,
    seeded_rich_markdown_session: tuple[str, str, str],
) -> None:
    """Rich markdown constructs render as HTML; source toggle shows raw text."""
    base_url, session_id, _file_path = seeded_rich_markdown_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

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

    # Markdown defaults to the rich-text editor; each construct renders as its
    # semantic HTML element.
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

    # Source toggle: raw markdown becomes visible, no contenteditable editor.
    file_viewer.get_by_role("button", name="Source view").click()
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)
    expect(file_viewer.get_by_text("## Goals", exact=False)).to_be_visible(timeout=10_000)
    expect(file_viewer.get_by_text("```python", exact=False)).to_be_visible()
    expect(file_viewer.get_by_text("| Option | Latency |", exact=False)).to_be_visible()

    # Toggle back to the rich editor.
    file_viewer.get_by_role("button", name="Rich text editor").click()
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor.locator("h1")).to_contain_text("Rich Design Document")
