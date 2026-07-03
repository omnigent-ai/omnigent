"""E2E: the All-mode "files to include / exclude" glob filters narrow search.

In the All (folder-tree) scope the search bar has a filters toggle that
reveals VSCode-style "files to include" / "files to exclude" glob inputs
(see ``SearchFilterInput`` in ``FilesPanel.tsx``). Per
``useWorkspaceFileSearch`` the globs only *narrow an active text query* —
they don't search on their own — so the test always types a query first,
then applies a glob and asserts the results shrink accordingly.

Files sharing a "report" fragment but split across ``.py`` / ``.txt``
extensions are seeded so an include/exclude glob isolates one extension.
Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PY_A = "report_alpha.py"
_PY_B = "report_beta.py"
_TXT_C = "report_gamma.txt"
_ALL_FILES = (_PY_A, _PY_B, _TXT_C)


def _seed_file(base_url: str, session_id: str, path: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": f"contents of {path}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_filter_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path in _ALL_FILES:
        _seed_file(base_url, session_id, path)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _row(rail: Locator, name: str) -> Locator:
    return rail.get_by_role("button", name=re.compile(re.escape(name))).filter(has_text=name)


def test_all_mode_include_exclude_filters_narrow_search(
    page: Page,
    seeded_filter_session: tuple[str, str],
) -> None:
    """An include glob then an exclude glob narrow the All-mode search results."""
    base_url, session_id = seeded_filter_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    rail = page.get_by_role("complementary", name="Workspace")
    search = rail.get_by_role("searchbox", name="Search all files")
    expect(search).to_be_visible(timeout=30_000)

    # Unfiltered query matches all three "report*" files.
    search.fill("report")
    for path in _ALL_FILES:
        expect(_row(rail, path)).to_be_visible(timeout=15_000)

    # Reveal the glob filter inputs.
    rail.get_by_role("button", name="Show search filters").click()
    include = rail.get_by_role("textbox", name="files to include")
    exclude = rail.get_by_role("textbox", name="files to exclude")
    expect(include).to_be_visible()
    expect(exclude).to_be_visible()

    # Include only *.py → the .txt file drops out (globs are debounced ~300ms).
    include.fill("*.py")
    expect(_row(rail, _PY_A)).to_be_visible(timeout=15_000)
    expect(_row(rail, _PY_B)).to_be_visible()
    expect(_row(rail, _TXT_C)).to_have_count(0)

    # Clear include, switch to excluding *.py → now only the .txt remains.
    include.fill("")
    exclude.fill("*.py")
    expect(_row(rail, _TXT_C)).to_be_visible(timeout=15_000)
    expect(_row(rail, _PY_A)).to_have_count(0)
    expect(_row(rail, _PY_B)).to_have_count(0)
