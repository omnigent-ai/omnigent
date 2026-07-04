"""E2E: a git-status failure surfaces in the Files panel, not a blank list.

Regression guard for the "blank panel == clean tree" ambiguity. The changed-files
view (``/changes`` -> ``GitFilesystemRegistry.list_changed_files``) used to swallow
every ``git status`` failure to an empty list, which ``FlatFileList`` renders
identically to a genuinely clean tree ("No workspace changes yet"). A read that
*could not run* was therefore indistinguishable from "nothing changed", which is
exactly what made a real worktree report impossible to diagnose.

The fix makes ``/changes`` return ``500 {error:{code:"git_status_failed", message}}``
on failure, and the web hook (``useWorkspaceChangedFiles``) parses that body and
throws so the panel shows "Failed to load: <reason>" (in the destructive style)
instead of the misleading empty state.

The failure is injected by intercepting the ``/changes`` request and fulfilling it
with the 500 error shape the runner now returns — no real git breakage needed — so
the test deterministically exercises the hook's error parsing and the panel's error
branch. Everything else (environment availability, liveness) stays real so the
changed-files query actually fires.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# A distinctive, realistic git-status failure reason. Mirrors the shape
# ``GitStatusUnavailable.reason`` carries (argv + exit code + stderr) so the
# assertions prove the *server's* message reaches the UI verbatim rather than a
# bare status code.
_GIT_REASON = (
    "git status --porcelain --untracked-files=all exited 128: "
    "fatal: detected dubious ownership in repository at '/workspace'"
)


def test_git_status_failure_surfaces_in_files_panel(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed ``/changes`` shows "Failed to load: <reason>", never the empty state."""
    base_url, session_id = seeded_session

    def _fail_changes(route: Route) -> None:
        route.fulfill(
            status=500,
            headers={"content-type": "application/json"},
            body=json.dumps({"error": {"code": "git_status_failed", "message": _GIT_REASON}}),
        )

    # Intercept ONLY the changed-files endpoint. Registered before navigation so
    # the very first fetch fails; a plain 500 is not retried (it is not the
    # runner-offline 503), so the query settles to its error state immediately.
    page.route(
        re.compile(
            rf"/v1/sessions/{re.escape(session_id)}/resources/environments/[^/]+/changes(\?|$)"
        ),
        _fail_changes,
    )

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # Files is the default rail tab; click it explicitly so the assertion does
    # not depend on the remembered tab from a prior session. The Changed scope
    # (the flat changed-files list, where the error renders) is the default, but
    # select it explicitly for the same reason.
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    rail.get_by_role("radio", name="Changed").click()

    # The panel surfaces the server's reason verbatim, in the destructive style —
    # not a bare status code and not the misleading empty state.
    error_line = rail.get_by_text(re.compile(r"^Failed to load:"))
    expect(error_line).to_be_visible(timeout=30_000)
    expect(error_line).to_contain_text("exited 128")
    expect(error_line).to_contain_text("dubious ownership")

    # The whole point of the fix: a failed read is distinguishable from a clean
    # tree, so the empty-state copy must NOT be shown for a git-status failure.
    expect(rail.get_by_text("No workspace changes yet")).to_have_count(0)
