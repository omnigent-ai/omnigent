"""E2E: file edits auto-save without an explicit Save action.

The FileViewer has no Save button — both editor surfaces (TipTap for
small Markdown, Monaco for large Markdown and code) debounce edits and write them back
through ``PUT .../filesystem/{path}`` automatically (see
``useEditorAutoSave``). These tests drive a real edit in each surface and
prove the change reaches the server:

  1. A status indicator returns to "Saved" after the edit settles
     (the markdown toolbar pill / the Monaco toolbar chip).
  2. A fresh ``GET .../filesystem/{path}`` returns the edited content.

Both are seeded via the filesystem PUT endpoint (no agent run), so the
tests are deterministic. An open editor also follows saves made by another
process or session in place, so the user's next edit saves alongside them.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from playwright.sync_api import Page, expect

# Files land in ``<repo-root>/<session_id>/`` (the agent spec uses
# ``os_env.cwd: .``), so clean that per-session dir up in teardown.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


def _read_file(base_url: str, session_id: str, path: str) -> str:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["content"]


def _wait_for_persisted(
    base_url: str,
    session_id: str,
    path: str,
    needle: str,
    timeout_s: float = 15.0,
) -> str:
    """Poll the file-content endpoint until ``needle`` lands (auto-save)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = _read_file(base_url, session_id, path)
        if needle in last:
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"auto-save never persisted {needle!r} to {path}; last server content:\n{last}"
    )


_MARKDOWN_PATH = "autosave_doc.md"
_MARKDOWN_CONTENT = """\
# Auto Save Doc

A paragraph that will be edited in the rich-text editor.
"""

_PY_PATH = "autosave_module.py"
_PY_CONTENT = """\
def greet(name):
    return "hello " + name
"""


@pytest.fixture
def seeded_markdown(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _MARKDOWN_PATH, _MARKDOWN_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        _cleanup_session_workdir(session_id)


@pytest.fixture(params=[3, 3_000, 50_000], ids=["short", "past-agent-line-cap", "50k-lines"])
def seeded_python(
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, str]]:
    base_url, session_id = seeded_session
    first_padding_line = len(_PY_CONTENT.splitlines()) + 1
    content = _PY_CONTENT + "".join(
        f"line_{i} = {i}  # café\n" for i in range(first_padding_line, request.param)
    )
    content += "# file_end_marker\n"
    _seed_file(base_url, session_id, _PY_PATH, content)
    try:
        yield (base_url, session_id, content)
    finally:
        _cleanup_session_workdir(session_id)


def test_markdown_edit_autosaves(page: Page, seeded_markdown: tuple[str, str]) -> None:
    """Typing in the markdown rich-text editor auto-saves back to the server."""
    base_url, session_id = seeded_markdown
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor).to_contain_text("A paragraph that will be edited")

    # The save pill starts at "Saved" (clean). Type a unique sentinel at the
    # end of the document; the pill must transition through dirty back to
    # "Saved" and the edited markdown must reach the server.
    sentinel = "autosaved-md-sentinel-7f3a"
    editor.click()
    page.keyboard.press("Control+End")
    page.keyboard.type(f" {sentinel}")

    # The markdown editor toolbar's auto-save pill settles back to the saved
    # state. Its accessible name is the button's title ("All changes saved"),
    # not the short visible label.
    expect(file_viewer.get_by_role("button", name="All changes saved")).to_be_visible(
        timeout=15_000
    )

    persisted = _wait_for_persisted(base_url, session_id, _MARKDOWN_PATH, sentinel)
    assert sentinel in persisted

    # Undo after saving must persist the older document, then redo must save
    # the edit again against that new baseline.
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and response.url.endswith(f"/filesystem/{_MARKDOWN_PATH}")
        )
    ) as undo_save:
        page.keyboard.press("ControlOrMeta+z")
        expect(editor).not_to_contain_text(sentinel)
    assert undo_save.value.ok
    assert _read_file(base_url, session_id, _MARKDOWN_PATH).strip() == _MARKDOWN_CONTENT.strip()
    page.keyboard.press("ControlOrMeta+Shift+z")
    expect(editor).to_contain_text(sentinel)
    assert _wait_for_persisted(base_url, session_id, _MARKDOWN_PATH, sentinel) == persisted
    page.reload()
    expect(editor).to_contain_text(sentinel)


def test_open_markdown_follows_other_writers(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """An open editor shows other writers' saves in place, never over unsaved edits."""
    base_url, session_id, other_session_id = seeded_session_pair
    path = "shared_worklog.md"
    _seed_file(base_url, session_id, path, "# Worklog\n\nFirst entry.\n")
    try:
        listing = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem",
            timeout=10.0,
        )
        listing.raise_for_status()
        disk_path = Path(listing.json()["base"]) / path

        def replace_on_disk(content: str) -> None:
            staged = disk_path.with_name(f".{path}.tmp")
            staged.write_text(content)
            os.replace(staged, disk_path)

        page.goto(f"{base_url}/c/{session_id}?file={path}")
        viewer = page.locator('[data-testid="file-viewer"]:visible')
        editor = viewer.locator('[contenteditable="true"]')
        first_entry = editor.locator("p", has_text="First entry.")
        expect(first_entry).to_be_visible(timeout=10_000)
        page.evaluate("window.__sameDocument = true")
        # Park the caret at the end of the first entry, as a reader would.
        box = first_entry.bounding_box()
        assert box is not None
        first_entry.click(position={"x": box["width"] - 2, "y": box["height"] / 2})

        # Another process replaces the file on disk.
        replace_on_disk("# Worklog\n\nFirst entry.\n\nFrom another process.\n")
        expect(editor).to_contain_text("From another process.", timeout=10_000)

        # Another session saves the same file through its own filesystem API.
        saved = httpx.put(
            f"{base_url}/v1/sessions/{other_session_id}/resources/environments/default"
            f"/filesystem/{quote(str(disk_path).lstrip('/'))}?base=host",
            json={
                "content": "# Worklog\n\nFirst entry.\n\nFrom another process.\n\n"
                "From another session.\n",
                "encoding": "utf-8",
            },
            timeout=10.0,
        )
        saved.raise_for_status()
        expect(editor).to_contain_text("From another session.", timeout=10_000)
        assert page.evaluate("window.__sameDocument") is True

        page.keyboard.type(" Still here.")
        persisted = _wait_for_persisted(base_url, session_id, path, "Still here.")
        entries = ["First entry. Still here.", "From another process.", "From another session."]
        positions = [persisted.find(entry) for entry in entries]
        assert -1 not in positions and positions == sorted(positions), persisted

        # With saves failing, another writer's change must not replace unsaved
        # text: it raises the conflict, and Keep mine saves the user's version.
        put_only = f"**/filesystem/{path}"
        page.route(
            put_only,
            lambda route: route.abort() if route.request.method == "PUT" else route.continue_(),
        )
        page.keyboard.type(" Unsaved.")
        replace_on_disk(persisted + "\nFrom a later writer.\n")
        expect(viewer.get_by_text("modified externally", exact=False)).to_be_visible(
            timeout=10_000
        )
        expect(editor).to_contain_text("Still here. Unsaved.")
        expect(editor).not_to_contain_text("From a later writer.")
        page.unroute(put_only)
        viewer.get_by_role("button", name="Keep mine").click()
        kept = _wait_for_persisted(base_url, session_id, path, "Still here. Unsaved.")
        assert "From a later writer." not in kept
    finally:
        _cleanup_session_workdir(session_id)


def test_non_markdown_edit_autosaves(page: Page, seeded_python: tuple[str, str, str]) -> None:
    """The full file can be viewed and edited without losing its tail on save."""
    base_url, session_id, original_content = seeded_python
    page.goto(f"{base_url}/c/{session_id}?file={_PY_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # Non-markdown files render in Monaco (lazy-loaded). Wait for the editor.
    monaco = file_viewer.locator(".monaco-editor")
    expect(monaco).to_be_visible(timeout=20_000)
    expect(file_viewer.locator(".view-lines")).to_contain_text("greet", timeout=10_000)

    file_viewer.locator(".view-lines").click()
    page.keyboard.press("Control+End")
    expect(file_viewer.locator(".view-lines")).to_contain_text("file_end_marker")
    expect(
        file_viewer.get_by_text("This file is too large to load fully", exact=False)
    ).to_have_count(0)

    # Place the cursor at the start of the buffer and type a comment line. A
    # leading edit is unambiguous and easy to assert in the persisted file.
    sentinel = "autosaved_py_sentinel_9c2b"
    page.keyboard.press("Control+Home")
    page.keyboard.type(f"# {sentinel}\n")

    # The FileViewer toolbar shows the Monaco auto-save status chip; it
    # settles back to "Saved" once the debounced write lands.
    expect(file_viewer.get_by_text("Saved", exact=True)).to_be_visible(timeout=15_000)

    persisted = _wait_for_persisted(base_url, session_id, _PY_PATH, sentinel)
    assert persisted == f"# {sentinel}\n{original_content}"

    page.keyboard.press("Control+End")
    expect(file_viewer.locator(".view-lines")).to_contain_text("file_end_marker")


def test_large_markdown_source_autosaves(page: Page, seeded_session: tuple[str, str]) -> None:
    """Source fallback preserves the whole file and stays mounted across a smaller save."""
    base_url, session_id = seeded_session
    path = "large-autosave.md"
    line = "A paragraph in a large Markdown document.\n\n"
    original = "# Large document\n\n" + line * (1024 * 1024 // len(line) + 1)
    original += "Large document end.\n"
    _seed_file(base_url, session_id, path, original)
    try:
        page.goto(f"{base_url}/c/{session_id}?file={path}")
        viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(
            viewer.get_by_text("Large Markdown file opened as source", exact=False)
        ).to_be_visible()
        editor = viewer.locator(".monaco-editor")
        expect(editor).to_be_visible(timeout=30_000)
        expect(viewer.locator(".tiptap")).to_have_count(0)
        expect(viewer.get_by_role("button", name="View mode", exact=False)).to_have_count(0)
        viewer.locator(".view-lines").click()
        page.keyboard.press("ControlOrMeta+End")
        expect(viewer.locator(".view-lines")).to_contain_text("Large document end.")
        page.keyboard.press("ControlOrMeta+Home")
        sentinel = "# Saved large Markdown\n"
        page.keyboard.type(sentinel)
        assert _wait_for_persisted(base_url, session_id, path, sentinel) == sentinel + original
        page.reload()
        expect(viewer.locator(".view-lines")).to_contain_text("Saved large Markdown")
        viewer.locator(".view-lines").click()
        page.keyboard.press("ControlOrMeta+f")
        search = editor.get_by_role("textbox", name="Find", exact=True)
        expect(search).to_be_focused()
        search.fill("Large document end.")
        expect(editor.locator(".view-lines")).to_contain_text("Large document end.")
        page.keyboard.press("Escape")
        page.keyboard.press("ControlOrMeta+a")
        small = "# Smaller saved document\n"
        page.keyboard.type(small)
        assert _wait_for_persisted(base_url, session_id, path, small) == small
        expect(editor).to_be_visible()
        page.keyboard.type("Another edit after shrinking.")
        assert (
            _wait_for_persisted(base_url, session_id, path, "Another edit")
            == small + "Another edit after shrinking."
        )
        page.reload()
        expect(viewer.locator(".tiptap")).to_contain_text("Another edit after shrinking.")
    finally:
        _cleanup_session_workdir(session_id)
