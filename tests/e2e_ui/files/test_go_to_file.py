"""E2E: Go to File opens a workspace file by part of its name.

Files are seeded through the filesystem PUT, so the server-backed ``/search``
that Go to File calls finds them. The journey opens the dialog from its ⌘K row,
picks files by keyboard, and checks each one lands in the workspace's file viewer.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

# The hello_world agent writes seeded files under the server's cwd, the repo root
# (this file is ``<repo>/tests/e2e_ui/files/...``).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The name starts with the fragment, so it ranks first.
_TARGET = "src/planner_routes.py"
# Shares the fragment but ranks lower: it starts a word inside the name, not the name.
_OTHER = "docs/notes_planner_old.md"


def _put_file(base_url: str, session_id: str, path: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": f"contents of {path}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def go_to_file_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path in (_TARGET, _OTHER):
        _put_file(base_url, session_id, path)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _open_go_to_file(page: Page) -> None:
    """Open Go to File from its ⌘K row, with focus in its search field."""
    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_focused()
    palette_input.fill("go to file")
    page.get_by_role("option", name=re.compile(r"Go to File…")).click()
    expect(page.get_by_role("dialog", name="Go to File")).to_be_visible()
    expect(page.get_by_test_id("go-to-file-input")).to_be_focused()


def _expect_file_open(page: Page, path: str) -> None:
    folder, _, name = path.rpartition("/")
    expect(page).to_have_url(
        re.compile(rf"[?&]file={re.escape(folder)}(?:%2F|/){re.escape(name)}(?:&|$)"),
        timeout=15_000,
    )
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        f"contents of {path}", timeout=15_000
    )


def test_go_to_file_opens_files_from_the_palette(
    page: Page,
    go_to_file_session: tuple[str, str],
) -> None:
    base_url, session_id = go_to_file_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)
    # The composer takes focus once the session settles; wait for that, so it
    # can't pull focus out of the dialog mid-journey.
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_focused(timeout=30_000)

    _open_go_to_file(page)
    dialog = page.get_by_role("dialog", name="Go to File")
    page.get_by_test_id("go-to-file-input").fill("planner")
    rows = dialog.get_by_role("option")
    expect(rows).to_have_count(2, timeout=15_000)
    # The name that starts with the text ranks first, and is selected.
    expect(rows.first).to_contain_text("planner_routes.py")
    expect(rows.first).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    _expect_file_open(page, _TARGET)

    # Reopened, it starts empty; the arrow keys pick the lower-ranked file.
    _open_go_to_file(page)
    expect(page.get_by_test_id("go-to-file-input")).to_have_value("")
    page.get_by_test_id("go-to-file-input").fill("planner")
    expect(rows).to_have_count(2, timeout=15_000)
    page.keyboard.press("ArrowDown")
    expect(rows.nth(1)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    _expect_file_open(page, _OTHER)
