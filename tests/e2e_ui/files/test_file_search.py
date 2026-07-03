"""E2E: the Files-panel search box filters the All-files tree.

In the All (folder-tree) scope the search field runs a server-side recursive
``/search`` call and renders the matches as a flat list (``useWorkspaceFileSearch``
→ ``FolderTree`` search mode). Three files with distinguishing name fragments are
seeded so a query can isolate a subset and exclude the rest.

Note on scope: the *Changed* scope's search filters the changed-files list, which
in this e2e harness can only be populated by a git workspace or the agent's
``sys_os_write`` tool — neither is reachable deterministically here (the seeded
temp workspace is non-git, and the openai-agents harness writes outside the
web-visible ``default`` environment). The Changed-list filter is covered by the
``FlatFileList`` component test instead. This e2e pins the server-backed All search.
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

# Two share the "alpha" fragment; one is "beta" — a query is unambiguous.
_ALPHA_ONE = "alpha_one.py"
_ALPHA_TWO = "alpha_two.py"
_BETA = "beta_three.txt"
_ALL_FILES = (_ALPHA_ONE, _ALPHA_TWO, _BETA)


def _put_file(base_url: str, session_id: str, path: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": f"contents of {path}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def all_search_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Three files PUT to the workspace so they populate the All tree listing."""
    base_url, session_id = seeded_session
    for path in _ALL_FILES:
        _put_file(base_url, session_id, path)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _row(rail: Locator, name: str) -> Locator:
    return rail.get_by_role("button", name=re.compile(re.escape(name))).filter(has_text=name)


def test_search_filters_all_files(
    page: Page,
    all_search_session: tuple[str, str],
) -> None:
    """Typing in the All search box runs the server search and lists matches."""
    base_url, session_id = all_search_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    rail = page.get_by_role("complementary", name="Workspace")
    search = rail.get_by_role("searchbox", name="Search all files")
    expect(search).to_be_visible(timeout=30_000)

    # Server-side recursive search (debounced ~300ms): only beta matches.
    search.fill("beta_three")
    expect(_row(rail, _BETA)).to_be_visible(timeout=15_000)
    expect(_row(rail, _ALPHA_ONE)).to_have_count(0)
    expect(_row(rail, _ALPHA_TWO)).to_have_count(0)

    # Re-querying for the shared fragment surfaces both alpha files.
    search.fill("alpha_")
    expect(_row(rail, _ALPHA_ONE)).to_be_visible(timeout=15_000)
    expect(_row(rail, _ALPHA_TWO)).to_be_visible()
    expect(_row(rail, _BETA)).to_have_count(0)
