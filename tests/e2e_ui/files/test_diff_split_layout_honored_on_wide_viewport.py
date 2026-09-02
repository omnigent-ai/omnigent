"""E2E: the diff view must not fall back to unified when the window has room.

A user who prefers the side-by-side (split) diff layout opens a changed
file's diff in a wide (1920px) window and must not get the inline (unified)
rendering with the split/unified toggle hidden — that would leave no way to
switch back even though the chat column has over a thousand spare pixels.

Mechanism guarded against (verified live on the buggy build):

- The workspace rail opens at ``useResizableInlinePanel``'s default width,
  which is clamped to at most 600px regardless of viewport size.
- Monaco's ``renderSideBySideInlineBreakpoint`` (900px, left at its default in
  ``MonacoDiffViewer``) silently collapses ``renderSideBySide: true`` into the
  inline view below 900px of editor width.
- ``FileViewer.splitToggleAvailable`` hides the split/unified toggle below the
  same 900px (``MONACO_SPLIT_BREAKPOINT``), so the fallback is also unrecoverable
  from the toolbar.
- Unless the rail claims ``SPLIT_DIFF_MIN_WIDTH`` (920, ``codeViewerHelpers.ts``)
  while a diff is showing, the fallback fires despite the room being available.

The test drives the real user journey in the SPA: a split-diff preference is
persisted (the user chose split earlier), the browser window is wide, the user
opens the workspace rail's Changes tab and clicks the changed file. It then
asserts the diff actually renders side-by-side and the layout toggle is
offered. Both assertions fail on the buggy build (unified rendering, toggle
hidden) and pass once the diff view claims enough rail width (the
``SPLIT_DIFF_MIN_WIDTH`` contract) or split is otherwise honored at this
viewport.

Workspace data (changed-files list, before/after diff content, file content)
is stubbed with ``page.route`` — the suite's established pattern for the
changed-files surface (see ``test_changed_files_git_status_error.py`` and the
GitHub-tab suite) — because the shared e2e runner has no git-backed session
workspace. The bug under test is purely client-side layout; everything the
regression guards (rail sizing, FileViewer toolbar, Monaco diff options) runs
for real.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_PATH = "split_layout_target.py"
_BEFORE = "\n".join(f"line_{i} = {i}" for i in range(1, 30)) + "\n"
_AFTER = _BEFORE.replace("line_5 = 5", "line_5 = 500").replace("line_20 = 20", "line_20 = 2000")

# A comfortably wide desktop window: the report's premise is "enough room".
_VIEWPORT = {"width": 1920, "height": 1080}


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Pin the wide viewport at context creation so recordings match it too."""
    return {**browser_context_args, "viewport": _VIEWPORT}


def _stub_changed_file(page: Page, session_id: str) -> None:
    """Serve one modified file through the changed-files/diff/content endpoints.

    :param page: Playwright page (routes registered before navigation).
    :param session_id: The seeded session id the URLs are scoped to.
    """

    def _changes(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "object": "session.environment.filesystem.entry",
                            "path": _FILE_PATH,
                            "name": _FILE_PATH,
                            "status": "modified",
                            "bytes": len(_AFTER),
                            "modified_at": 1_700_000_000,
                            "lines_added": 2,
                            "lines_removed": 2,
                        }
                    ],
                    "has_more": False,
                }
            ),
        )

    def _diff(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "session.environment.filesystem.file_diff",
                    "path": _FILE_PATH,
                    "before": _BEFORE,
                    "after": _AFTER,
                }
            ),
        )

    def _file(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "session.environment.filesystem.file_content",
                    "path": _FILE_PATH,
                    "content": _AFTER,
                    "encoding": "utf-8",
                    "truncated": False,
                    "content_type": "text/x-python",
                }
            ),
        )

    sid = re.escape(session_id)
    path = re.escape(_FILE_PATH)
    page.route(
        re.compile(rf"/v1/sessions/{sid}/resources/environments/[^/]+/changes(\?|$)"),
        _changes,
    )
    page.route(
        re.compile(rf"/v1/sessions/{sid}/resources/environments/[^/]+/diff/{path}"),
        _diff,
    )
    page.route(
        re.compile(rf"/v1/sessions/{sid}/resources/environments/[^/]+/filesystem/{path}"),
        _file,
    )


def test_split_diff_layout_survives_default_rail_width_on_wide_viewport(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A split-preferring user's diff renders side-by-side in a 1920px window.

    On the buggy build the diff silently collapses to the inline (unified)
    rendering and the split/unified toggle is hidden, because the workspace
    rail never claims the width a split diff needs even though the viewport
    has ample room.
    """
    base_url, session_id = seeded_session

    # The user chose the split layout earlier; it is a persisted, app-global
    # preference (localStorage), exactly what a returning user carries.
    page.add_init_script(
        """
        localStorage.setItem(
          "omnigent:file-view-preferences",
          JSON.stringify({
            diffActive: true,
            diffLayout: "split",
            previewableViewMode: "editor",
            hideWhitespace: false,
            wrapLines: false,
          }),
        );
        """
    )
    _stub_changed_file(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The user opens the Changes tab and clicks the modified file.
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")
    file_button = rail.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # The diff view opens (diffActive preference + the file being in the
    # changed list select the diff surface).
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    diff_editor = file_viewer.locator(".monaco-diff-editor").first
    expect(diff_editor).to_be_visible(timeout=60_000)
    expect(file_viewer.locator(".monaco-diff-editor .view-lines").first).to_be_visible(
        timeout=30_000
    )

    # THE BUG: with the split preference saved and >1300px of unused viewport,
    # the diff must render side-by-side. Monaco toggles the `side-by-side`
    # class on the diff root exactly when the split rendering is live, so this
    # is the user-visible layout, not an implementation detail. On the buggy
    # build the rail's 600px default keeps the editor under Monaco's 900px
    # inline breakpoint and this stays unified.
    expect(diff_editor).to_have_class(
        re.compile(r"(^|\s)side-by-side(\s|$)"),
        timeout=10_000,
    )

    # And the user must be able to switch layouts: the split/unified toggle
    # (labelled with the layout it would switch TO) is hidden below the same
    # 900px breakpoint on the buggy build.
    layout_toggle = file_viewer.get_by_role(
        "button", name=re.compile("^(Split|Unified) view$")
    ).first
    expect(layout_toggle).to_be_visible(timeout=10_000)
