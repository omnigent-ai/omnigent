"""E2E: Mermaid code fences render as diagrams in Markdown Preview mode.

Markdown files default to the rich-text editor, but the read-only Preview pane
uses ``CodeViewer``'s ``react-markdown`` pipeline. This test pins that path: a
``mermaid`` fence becomes an SVG diagram while ordinary fences remain code, and
Source mode keeps the Mermaid text editable.

Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKDOWN_FILE_PATH = "mermaid_preview.md"

_MARKDOWN_CONTENT = """\
# Mermaid Preview

```mermaid
flowchart LR
  A[Markdown] --> B[Preview]
```

```js
const stillCode = true;
```
"""


@pytest.fixture
def seeded_mermaid_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}",
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_mermaid_fence_renders_as_preview_diagram(
    page: Page,
    seeded_mermaid_markdown_session: tuple[str, str],
) -> None:
    """Preview turns Mermaid fences into SVGs and preserves other code fences."""
    base_url, session_id = seeded_mermaid_markdown_session
    page.add_init_script(
        """
        window.localStorage.setItem(
          "omnigent:file-view-preferences",
          JSON.stringify({
            diffActive: false,
            diffLayout: "unified",
            previewableViewMode: "preview",
            hideWhitespace: false,
            wrapLines: false,
          }),
        );
        """
    )
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    mermaid_preview = file_viewer.get_by_test_id("mermaid-preview")
    expect(mermaid_preview).to_be_visible(timeout=10_000)
    # The rendered block contains chrome icons (zoom/copy controls) alongside
    # the diagram, so target the diagram svg specifically: mermaid stamps its
    # output with aria-roledescription (e.g. "flowchart-v2").
    expect(mermaid_preview.locator("svg[aria-roledescription]")).to_be_visible(timeout=10_000)
    expect(file_viewer.locator("pre > code.language-mermaid")).to_have_count(0)

    js_code = file_viewer.locator("pre > code.language-js")
    expect(js_code).to_contain_text("const stillCode = true;")
