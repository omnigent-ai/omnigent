"""E2E: the Files panel "Sort" control reorders the All (folder-tree) view.

The Files panel exposes a Sort dropdown (Filename / Last edited / Size / Type)
that orders both the Changed list and the All tree (see ``SortSelector`` and
``compareTreeNodes`` in ``FilesPanel.tsx`` / ``FolderTree.tsx``). This test
seeds three files whose alphabetical order is the reverse of their size order,
opens the All scope, and asserts that switching the sort criterion visibly
reorders the rows.

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

# Names and sizes chosen so alphabetical order (aaa < mmm < zzz) is the REVERSE
# of size order (zzz biggest, aaa smallest) — so a sort change is unambiguous.
_SMALL = "aaa.txt"
_MID = "mmm.txt"
_BIG = "zzz.txt"
_CONTENT = {_SMALL: "a\n", _MID: "m" * 200, _BIG: "z" * 4000}
_ALL_FILES = (_SMALL, _MID, _BIG)


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_sort_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    for path, content in _CONTENT.items():
        _seed_file(base_url, session_id, path, content)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _row(rail: Locator, name: str) -> Locator:
    return rail.get_by_role("button", name=re.compile(re.escape(name))).filter(has_text=name)


def _row_y(rail: Locator, name: str) -> float:
    box = _row(rail, name).bounding_box()
    assert box is not None, f"row {name!r} has no bounding box"
    return box["y"]


def _select_sort(page: Page, rail: Locator, label: str) -> None:
    rail.get_by_role("button", name=re.compile(r"^Sort:")).click()
    page.get_by_role("menuitemradio", name=label).click()


def test_all_view_sort_reorders_files(
    page: Page,
    seeded_sort_session: tuple[str, str],
) -> None:
    """Switching the Sort criterion reorders the All-view file rows."""
    base_url, session_id = seeded_sort_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("searchbox", name="Search all files")).to_be_visible(timeout=30_000)
    for name in _ALL_FILES:
        expect(_row(rail, name)).to_be_visible(timeout=15_000)

    # Sort by Filename → alphabetical: aaa.txt above zzz.txt.
    _select_sort(page, rail, "Filename")
    expect(_row(rail, _SMALL)).to_be_visible()
    assert _row_y(rail, _SMALL) < _row_y(rail, _BIG)

    # Sort by Size → largest first: zzz.txt (biggest) now above aaa.txt.
    _select_sort(page, rail, "Size")
    expect(_row(rail, _BIG)).to_be_visible()
    assert _row_y(rail, _BIG) < _row_y(rail, _SMALL)
