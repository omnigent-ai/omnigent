"""E2E: the Changed scope toggles between a flat list and a folder tree.

The Files panel's "Changed" scope renders changed files as a flat list by
default; a list/tree toggle in its toolbar switches to a collapsible folder
tree (``ChangedFileTree``), grouping the changed files under their directories
VS Code Source Control–style and starting fully expanded.

Driving real changed files needs an agent-run git workspace (the seeded PUT
files land in the web-visible ``default`` environment and don't show as
*changed*), so — like ``test_changed_files_git_status_error`` — this intercepts
the ``/changes`` endpoint and fulfils it with a fixed set of changed files
across nested folders. Everything else (environment availability, liveness)
stays real so the changed-files query actually fires and the panel renders.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# Changed files spanning two nested folders plus a root file, so the tree has
# something to group (``src/app/``) and the flat list has a directory to show.
_CHANGES = [
    {
        "path": "src/app/main.ts",
        "name": "main.ts",
        "status": "modified",
        "bytes": 120,
        "modified_at": 1_700_000_300,
        "lines_added": 3,
        "lines_removed": 1,
    },
    {
        "path": "src/app/util.ts",
        "name": "util.ts",
        "status": "created",
        "bytes": 64,
        "modified_at": 1_700_000_200,
        "lines_added": 8,
        "lines_removed": 0,
    },
    {
        "path": "README.md",
        "name": "README.md",
        "status": "modified",
        "bytes": 42,
        "modified_at": 1_700_000_100,
        "lines_added": 1,
        "lines_removed": 1,
    },
]


def _row(rail: Locator, text: str) -> Locator:
    """A file row is the button whose visible text contains *text*.

    Matches on ``has_text`` rather than the accessible name so that text
    containing ``/`` (e.g. the ``src/app`` directory chip) doesn't get turned
    into a Playwright role-name *regex* — the slashes break its selector parser.
    """
    return rail.get_by_role("button").filter(has_text=text)


def _folder(rail: Locator, name: str) -> Locator:
    """A tree folder header button, matched on its exact ``name/`` label.

    ``exact=True`` keeps it from colliding with a flat-list row whose full path
    merely *contains* the folder name (e.g. ``src/`` inside ``src/app/main.ts``).
    """
    return rail.get_by_role("button", name=f"{name}/", exact=True)


def test_changed_scope_toggles_between_list_and_tree(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The list/tree toggle groups the changed files into folders and back."""
    base_url, session_id = seeded_session

    def _serve_changes(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"object": "list", "data": _CHANGES, "has_more": False}),
        )

    # Intercept ONLY the changed-files endpoint, before navigation, so the very
    # first fetch returns our fixed set.
    page.route(
        re.compile(
            rf"/v1/sessions/{re.escape(session_id)}/resources/environments/[^/]+/changes(\?|$)"
        ),
        _serve_changes,
    )

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # Files is the default rail tab, and Changed the default scope — but a prior
    # session's remembered state could differ, so select both explicitly.
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    rail.get_by_role("radio", name="Changed").click()

    # List mode (the default): each row shows the file name plus its directory
    # (two separate spans since #4150), and there is no folder row yet.
    expect(_row(rail, "main.ts")).to_be_visible(timeout=30_000)
    expect(_row(rail, "src/app")).to_have_count(2)
    expect(_folder(rail, "src")).to_have_count(0)

    # Flip to the tree.
    rail.get_by_role("button", name="Switch to tree view").click()

    # Tree mode: changed files are grouped under their folders (fully expanded),
    # leaves show the filename only, and the toggle now offers the reverse.
    expect(_folder(rail, "src")).to_be_visible()
    expect(_folder(rail, "app")).to_be_visible()
    expect(_row(rail, "main.ts")).to_be_visible()
    # The folder is now a header row, so no leaf carries the directory text.
    expect(_row(rail, "src/app")).to_have_count(0)
    expect(rail.get_by_role("button", name="Switch to list view")).to_be_visible()

    # Collapsing a folder hides its descendants; re-expanding brings them back.
    _folder(rail, "app").click()
    expect(_row(rail, "main.ts")).to_have_count(0)
    _folder(rail, "app").click()
    expect(_row(rail, "main.ts")).to_be_visible()
