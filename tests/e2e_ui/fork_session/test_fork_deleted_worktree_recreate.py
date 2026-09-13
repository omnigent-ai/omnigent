"""Browser e2e: forking a session whose worktree directory was deleted.

When a session's git worktree has been deleted from disk (manual cleanup,
``git worktree remove``, or an archive-triggered cleanup), forking that
session with the original worktree name pre-filled in the dialog fails with
a "directory doesn't exist" error instead of creating a fresh worktree at
that path.

Root cause: ``ForkSessionDialog.handleFork`` pre-flights the *effective
workspace* with ``checkHostDirectory`` before creating anything.  When the
pre-filled repo + source-branch are left untouched, ``usingSourceWorktree``
is ``true`` and ``effectiveWorkspace`` is set to the **source's worktree
path** (``/work/repo-worktrees/fix-1``), not the repo.  If that directory
has since been deleted the pre-flight 404s, ``handleFork`` surfaces an
error and returns early — no fork, no runner launch, no navigation.

The workaround: change the branch name in Advanced settings.  That flips
``usingSourceWorktree`` to ``false``, which takes the create-new-worktree
path that does work.  Keeping the original name should also work.

Expected fix: when ``usingSourceWorktree`` and the directory pre-flight
returns a "not found" error, fall back to the worktree-create submission
path (pass git options to ``launchRunner`` with the original branch name)
rather than surfacing the error and aborting.

Test shape
----------
No real ``omnigent host`` is needed — the host-side wire is stubbed at the
network layer (same pattern as
``test_clone_worktree_source_prefills_repo_and_validates_directory``):

- ``GET /v1/hosts`` → one online host.
- ``GET /v1/sessions/{id}`` → patched with host + worktree geometry.
- ``GET /v1/hosts/{id}/filesystem/**`` → 404 for the *worktree directory*
  (simulating a deleted worktree), 200 for the repo path and everything
  else (simulating an intact repo).
- ``POST /v1/hosts/{id}/runners`` → records the launch body.
- ``POST /v1/sessions/{id}/fork`` → passes through to the real server.

The test submits the dialog **without touching the pre-filled fields**
(same branch as the source) and asserts:

1. No error toast is shown in the dialog.
2. The page navigates to a NEW session (not back to the source).
3. A runner launch is fired — and it carries ``git`` options so the host
   creates the worktree at the original path + branch.  Without the fix
   the test stops at assertion 1 (error toast visible) or at assertion 2
   (still on the source URL).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

# Unique marker so other tests' transcripts can't satisfy this test's
# content assertions.
_WT_DELETED_MARKER = "tangerine-deleted-wt-marker"

# Fake host + worktree geometry: the source session appears bound to this
# host inside a server-created worktree (``<repo>-worktrees/<branch>``).
_HOST_ID = "host_e2e_wt_deleted"
_REPO = "/work/repo"
_WT_DIR = "/work/repo-worktrees/fix-1"
_WT_BRANCH = "fix-1"


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_fork_deleted_worktree_same_name_succeeds(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Fork with the original branch pre-filled must succeed when the worktree is gone.

    The source session is bound to a git worktree whose directory no longer
    exists on disk.  Submitting the fork dialog **without changing the
    pre-filled branch** must create the fork and launch the runner with git
    options to recreate the worktree — not surface an error and abort.

    Failure modes this catches:

    - Dialog shows "The working directory … doesn't exist" and the fork is
      never created (the original regression): ``handleFork`` aborts on
      the pre-flight failure even though it could recover by recreating the
      missing worktree.
    - Fork is created but the runner launch omits git options: the host
      would try to ``checkout`` a branch into a non-existent directory and
      fail silently, leaving an unbound clone.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL;
        used to script the seed turn so no real credentials are needed.
    """
    base_url, session_id = seeded_session

    runner_bodies: list[dict[str, Any]] = []
    fork_calls: list[str] = []

    # ── Network stubs ──────────────────────────────────────────────────────

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "e2e-wt-deleted-host",
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {},
                        }
                    ]
                }
            ),
        )

    def handle_session_detail(route: Route) -> None:
        # Patch the real session so it reads as a coding session bound to
        # the fake host inside a worktree.  Non-GET traffic (e.g. PATCH)
        # passes through untouched so the fork call itself is real.
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = _HOST_ID
        body["workspace"] = _WT_DIR
        body["git_branch"] = _WT_BRANCH
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_filesystem(route: Route) -> None:
        # Pre-flight for the worktree directory (the deleted one): 404 to
        # simulate it missing from disk.  The repo path itself and every
        # other path (used by the autocomplete on the workspace input) is
        # listable so the form can proceed.
        url = route.request.url
        # URL-decoded path segment after /filesystem/
        import urllib.parse

        decoded_url = urllib.parse.unquote(url)
        if _WT_DIR in decoded_url:
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"detail": "no such directory"}),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"object": "list", "data": [], "has_more": False}),
            )

    def handle_runners(route: Route) -> None:
        runner_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runner_id": "runner_e2e_wt_deleted", "status": "launching"}),
        )

    def handle_fork(route: Route) -> None:
        fork_calls.append(route.request.url)
        route.continue_()

    page.route("**/v1/hosts", handle_hosts)
    # Regex so the slim snapshot variant (``?include_items=false&…``) is also
    # patched — otherwise the props feeding the dialog see the unpatched
    # session and take the non-coding path.
    page.route(
        re.compile(rf".*/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session_detail,
    )
    page.route(f"**/v1/hosts/{_HOST_ID}/filesystem/**", handle_filesystem)
    page.route(f"**/v1/hosts/{_HOST_ID}/runners", handle_runners)
    page.route("**/v1/sessions/*/fork", handle_fork)

    # ── Seed one turn so the dialog has an assistant bubble to anchor on ──

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="clone-wt-deleted-seed",
        match=_WT_DELETED_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_WT_DELETED_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    # ── Open the fork dialog ───────────────────────────────────────────────

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # A coding source (workspace present + online host) shows "Clone & start".
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start")

    # Verify the prefill: original repo as the directory, source branch as
    # the worktree — confirming usingSourceWorktree will be true on submit.
    page.get_by_test_id("fork-session-advanced-toggle").click()
    expect(page.get_by_test_id("workspace-path-input")).to_have_value(_REPO)
    expect(page.get_by_test_id("fork-session-branch-input")).to_have_value(_WT_BRANCH)

    # ── Submit WITHOUT changing anything ─────────────────────────────────
    # The branch field still matches the source branch → usingSourceWorktree
    # is true → effectiveWorkspace is the (deleted) worktree directory.
    # The pre-flight for that path will 404.  Without the fix the dialog
    # surfaces an error here; with the fix it falls back to the create path.

    submit.click()

    # ── Assert 1: no error toast ──────────────────────────────────────────
    # The bug manifests as an inline error in the dialog.  A visible error
    # element at this point means handleFork aborted on the pre-flight.
    error_locator = page.get_by_test_id("fork-session-error")
    # Give the dialog a short beat to show an error if it's going to.
    # We use not_to_be_visible rather than to_have_count(0): the element
    # may already be in the DOM but hidden; we just need it not displayed.
    expect(error_locator).not_to_be_visible(timeout=5_000)

    # ── Assert 2: navigation to a new session ────────────────────────────
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # ── Assert 3: runner launched with git options ────────────────────────
    # The fix must take the create-new-worktree path (not just skip the
    # pre-flight): the runner POST must carry git options so the host
    # actually recreates the worktree at the original path + branch.
    deadline = time.monotonic() + 30.0
    while not runner_bodies and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(runner_bodies) == 1, "expected exactly one background runner launch"
    launch = runner_bodies[0]
    assert launch["session_id"] == fork_id, launch
    # Recreating launches from the REPO path — the host derives the worktree
    # directory from the branch and creates it via ``git worktree add``.
    assert launch["workspace"] == _REPO, (
        f"runner must launch from the repo so the host recreates the worktree: {launch}"
    )
    # git options must be present so the host creates the worktree rather
    # than trying to bind to a (still missing) directory.
    assert "git" in launch, (
        f"runner launch must carry git options to recreate the deleted worktree: {launch}"
    )
    git_opts = launch["git"]
    assert git_opts.get("branch_name") == _WT_BRANCH, (
        f"git options must name the original branch to recreate: {git_opts}"
    )
    # The branch already exists — the host must check it OUT, not re-create
    # it (``-b`` on an existing branch fails and would strand the clone).
    assert git_opts.get("existing_branch") is True, (
        f"git options must flag the branch as pre-existing: {git_opts}"
    )
