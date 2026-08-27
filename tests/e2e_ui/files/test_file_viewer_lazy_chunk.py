"""E2E: the FileViewer's JS is fetched on first file open, not on page load.

``FileViewer`` owns the app's only reachable path to the TipTap / ProseMirror
rich-text stack, so both mount sites (AppShell's mobile push panel and
WorkspacePanel's rail slot) render it through ``LazyFileViewer``. This test pins
the property that boundary exists for: booting a session must not download the
viewer, and clicking a file must still open it.

Without the boundary the viewer sits in the eagerly-loaded entry chunk — the
"not requested on load" assertion would then hold vacuously, so the test also
requires the chunk to arrive *after* the click.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Request, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_MARKDOWN_FILE_PATH = "lazy_notes.md"

_MARKDOWN_CONTENT = """\
# Lazy Notes

Opened on demand.
"""

# The viewer's own chunk, in either serving mode: a production build emits
# ``assets/FileViewer-<hash>.js``, the dev server serves
# ``/src/shell/FileViewer.tsx``. The leading slash keeps ``LazyFileViewer`` —
# statically imported, so part of the entry graph — from matching.
_FILE_VIEWER_CHUNK_RE = re.compile(r"/FileViewer[-.]")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_lazy_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed one markdown file and yield (base_url, session_id, path).

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


def test_file_viewer_chunk_loads_only_when_a_file_is_opened(
    page: Page,
    seeded_lazy_markdown_session: tuple[str, str, str],
) -> None:
    """The viewer's chunk is absent from page load and arrives on first open."""
    base_url, session_id, _file_path = seeded_lazy_markdown_session

    scripts: list[str] = []
    viewer_chunks: list[str] = []

    def _record(request: Request) -> None:
        if request.resource_type != "script":
            return
        scripts.append(request.url)
        if _FILE_VIEWER_CHUNK_RE.search(request.url):
            viewer_chunks.append(request.url)

    page.on("request", _record)

    page.goto(f"{base_url}/c/{session_id}?view=explore")

    # The seeded file rendering in the list means the shell and the rail have
    # both booted — anything eager has had its chance to request the chunk.
    file_button = page.get_by_role(
        "button", name=re.compile(rf"^{re.escape(_MARKDOWN_FILE_PATH)}\b")
    )
    expect(file_button).to_be_visible(timeout=30_000)

    # Guard against a vacuous pass: with no script request seen at all, the
    # assertion below would hold no matter where the viewer lives.
    assert scripts, "no script requests observed — the recorder never saw the page's JS"
    assert not viewer_chunks, (
        f"the FileViewer chunk was fetched before any file was opened: {viewer_chunks}"
    )

    file_button.click()

    # Two FileViewer instances mount with the same test id (AppShell's
    # md:hidden push panel and WorkspacePanel's rail slot); match the visible
    # one rather than relying on DOM order.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=30_000)
    expect(editor.locator("h1")).to_contain_text("Lazy Notes")

    # Resolving the lazy import is what fetched it: a statically imported
    # viewer would already be in the entry chunk and this would stay empty.
    assert viewer_chunks, (
        "opening a file did not fetch a separate FileViewer chunk — the viewer "
        "is back in the eagerly-loaded entry chunk"
    )
