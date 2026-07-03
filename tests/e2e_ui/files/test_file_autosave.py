"""E2E: file edits auto-save without an explicit Save action.

The FileViewer has no Save button — both editor surfaces (TipTap for
markdown, Monaco for everything else) debounce edits and write them back
through ``PUT .../filesystem/{path}`` automatically (see
``useEditorAutoSave``). These tests drive a real edit in each surface and
prove the change reaches the server:

  1. A status indicator returns to "Saved" after the edit settles
     (the markdown toolbar pill / the Monaco toolbar chip).
  2. A fresh ``GET .../filesystem/{path}`` returns the edited content.

Both are seeded via the filesystem PUT endpoint (no agent run), so the
tests are deterministic.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

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


@pytest.fixture
def seeded_python(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _PY_PATH, _PY_CONTENT)
    try:
        yield (base_url, session_id)
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


def test_non_markdown_edit_autosaves(page: Page, seeded_python: tuple[str, str]) -> None:
    """Typing in the Monaco code editor auto-saves back to the server."""
    base_url, session_id = seeded_python
    page.goto(f"{base_url}/c/{session_id}?file={_PY_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # Non-markdown files render in Monaco (lazy-loaded). Wait for the editor.
    monaco = file_viewer.locator(".monaco-editor")
    expect(monaco).to_be_visible(timeout=20_000)
    expect(file_viewer.locator(".view-lines")).to_contain_text("greet", timeout=10_000)

    # Place the cursor at the start of the buffer and type a comment line. A
    # leading edit is unambiguous and easy to assert in the persisted file.
    sentinel = "autosaved_py_sentinel_9c2b"
    file_viewer.locator(".view-lines").click()
    page.keyboard.press("Control+Home")
    page.keyboard.type(f"# {sentinel}\n")

    # The FileViewer toolbar shows the Monaco auto-save status chip; it
    # settles back to "Saved" once the debounced write lands.
    expect(file_viewer.get_by_text("Saved", exact=True)).to_be_visible(timeout=15_000)

    persisted = _wait_for_persisted(base_url, session_id, _PY_PATH, sentinel)
    assert sentinel in persisted
