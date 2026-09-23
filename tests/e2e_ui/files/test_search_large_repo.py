"""Files-panel All-scope search on a repository larger than the scan budget.

The server-side directory walk has a fixed entry budget, so on a large repo it
can stop before reaching a file that sorts late in walk order. Seeds a git
workspace with >50k entries where the target file sorts after the budget is
spent, then drives the real search box and asserts the tracked file is found
and the panel does not report a definitive ``No files match``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

# >50k entries under an alphabetically-first directory exhaust the scan budget
# before the walk reaches the needle, which sorts last under ``zzz_target``.
_FILLER_COUNT = 55_000
_NEEDLE = "needle_past_scan_budget.py"
_NEEDLE_PATH = f"zzz_target/{_NEEDLE}"

_SEED_SCRIPT = f"""
set -e
mkdir -p aaa_filler zzz_target
seq -w 0 {_FILLER_COUNT - 1} | while read n; do : > "aaa_filler/f$n.txt"; done
echo "needle" > {_NEEDLE_PATH}
git init -q
git add -A
git -c user.email=e2e@example.com -c user.name=e2e commit -q -m seed
git ls-files --error-unmatch {_NEEDLE_PATH} >/dev/null 2>&1 && echo TRACKED=yes || echo TRACKED=no
"""


def _shell(base_url: str, session_id: str, command: str, timeout: int = 600) -> dict:
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": command, "timeout": timeout},
        timeout=timeout + 30,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.fixture
def large_repo_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a >50k-entry git workspace with a tracked file that sorts last."""
    base_url, session_id = seeded_session
    result = _shell(base_url, session_id, _SEED_SCRIPT)
    if result["exit_code"] != 0 or "TRACKED=yes" not in result.get("stdout", ""):
        pytest.skip(f"could not seed git workspace in runner env: {result!r}")
    yield (base_url, session_id)


def _row(rail: Locator, name: str) -> Locator:
    return rail.get_by_role("button", name=re.compile(re.escape(name))).filter(has_text=name)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_search_finds_tracked_file_in_large_repo(
    page: Page,
    large_repo_session: tuple[str, str],
) -> None:
    """Searching for a tracked file past the scan budget must still find it.

    Fails on the buggy build: the truncated walk returns no matches and the
    panel shows a definitive ``No files match``. Passes once the search reaches
    tracked files regardless of repository size (and no longer presents an
    aborted scan as a no-match).
    """
    base_url, session_id = large_repo_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    rail = page.get_by_role("complementary", name="Workspace")
    search = rail.get_by_role("searchbox", name="Search all files")
    expect(search).to_be_visible(timeout=30_000)
    # Wait for the initial All-tree listing so the panel has settled out of its
    # mount-time re-render (scope restore + first fetch) that would reset the box.
    folder_row = rail.get_by_role("button", name="zzz_target/", exact=True)
    expect(folder_row).to_be_visible(timeout=30_000)

    search.fill(_NEEDLE.removesuffix(".py"))
    expect(search).to_have_value(_NEEDLE.removesuffix(".py"))

    needle_row = _row(rail, _NEEDLE)
    no_match = rail.get_by_text(re.compile(r"No files match"))
    # Let the debounced (~300ms) server search settle to one of the two states.
    expect(needle_row.or_(no_match)).to_be_visible(timeout=30_000)

    # The bug reports a definitive no-match for an existing tracked file. The
    # tracked file must be found regardless of repository size, and the panel
    # must not present the aborted scan as a definitive no-match.
    expect(no_match).to_have_count(0)
    expect(needle_row).to_be_visible(timeout=30_000)
