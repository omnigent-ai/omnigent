"""E2E: GitHub task lists render as checkboxes in the markdown rich-text editor.

Counterpart to ``test_markdown_github_alerts.py``: this pins the GitHub-flavored
*task list* construct — list items opening with ``[ ]`` / ``[x]``. The
``TaskList`` / ``TaskItem`` nodes (from ``@tiptap/extension-list``, registered in
``MarkdownRichTextViewer.tsx``) turn those into a ``<ul data-type="taskList">``
whose items are drawn by a node-view as ``<li>`` carrying ``data-checked`` and an
``<input type="checkbox">`` — distinct from a plain bullet list, which stays an
ordinary ``<ul>`` with no checkboxes.

Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import switch_markdown_view_mode

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKDOWN_FILE_PATH = "tasks.md"

# An unchecked + a checked task item, then a plain bullet list. The plain list
# proves the checkbox treatment comes from the ``[ ]`` / ``[x]`` markers, not
# from bullet lists in general.
_MARKDOWN_CONTENT = """\
# Checklist

- [ ] Buy milk
- [x] Ship the PR

Groceries:

- apples
- oranges
"""


@pytest.fixture
def seeded_tasks_session(
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


def test_task_lists_render_as_checkboxes(
    page: Page,
    seeded_tasks_session: tuple[str, str],
) -> None:
    """``- [ ]`` / ``- [x]`` render as a checkbox list; plain bullets do not."""
    base_url, session_id = seeded_tasks_session
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # Markdown opens in the rich-text editor by default, where task lists
    # render as the dedicated TaskList node view.
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)

    # The task list renders as a single dedicated <ul data-type="taskList">.
    task_list = editor.locator('ul[data-type="taskList"]')
    expect(task_list).to_have_count(1)

    # Two items, each with a checkbox; order follows the source: Buy milk
    # (unchecked) then Ship the PR (checked).
    checkboxes = task_list.locator('input[type="checkbox"]')
    expect(checkboxes).to_have_count(2)
    expect(checkboxes.nth(0)).not_to_be_checked()
    expect(checkboxes.nth(1)).to_be_checked()
    expect(task_list).to_contain_text("Buy milk")
    expect(task_list).to_contain_text("Ship the PR")

    # The raw checkbox syntax is consumed — the rendered editor shows the
    # checkboxes, never the literal "[ ]" / "[x]" markers.
    expect(task_list).not_to_contain_text("[ ]")
    expect(task_list).not_to_contain_text("[x]")

    # The plain bullet list stays an ordinary list: no taskList typing and no
    # checkbox inside it.
    plain = editor.locator('ul:not([data-type="taskList"])').filter(has_text="apples")
    expect(plain).to_have_count(1)
    expect(plain.locator('input[type="checkbox"]')).to_have_count(0)

    # Source view: the raw markers are visible verbatim.
    switch_markdown_view_mode(page, file_viewer, "Source")
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)
    expect(file_viewer.get_by_text("- [x] Ship the PR", exact=False)).to_be_visible(timeout=10_000)
