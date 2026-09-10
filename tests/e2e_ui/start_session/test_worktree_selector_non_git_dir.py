"""E2E: the composer's git worktree selector on a non-repository directory.

When the picked working directory is not a git repository, the new-chat
composer must hide the git worktree chip: offering it lets the user name
a branch, and the create then dies with ``worktree creation failed: not
a git repository: <path>`` from the host's git call — a dead-end state
the composer should never reach.

The composer already probes ``GET /v1/hosts/{id}/worktrees`` for the
picked directory, and that endpoint answers 400 for a non-git path (the
``useHostWorktrees`` hook maps it to "no worktrees here"), so the SPA
has everything it needs to hide the chip. A git repository always lists
its main work tree, so the same probe keeps the chip for real repos —
the second test pins that down so a fix can't over-hide.

Host-side wire is stubbed at the network layer (the e2e harness spawns
no ``omnigent host`` daemon — same pattern as
``fork_session/test_fork_deleted_worktree_recreate.py``): one online
host, and a worktrees answer per test mirroring the real endpoint's
contract. The recent-workspaces seed prefills the working directory so
the composer settles on the host + directory without browsing.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

# Stubbed host the composer auto-selects (keyed identically in the
# recent-workspaces localStorage seed).
_HOST_ID = "host_e2e_worktree_chip"
_HOST_NAME = "e2e-worktree-chip-host"

# A working directory that is NOT a git repository, and one that is.
_PLAIN_DIR = "/work/plain-dir"
_REPO_DIR = "/work/repo"

# The worktree-list endpoint the composer probes for the picked directory.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")


def _stub_hosts(page: Page) -> None:
    """Stub ``GET /v1/hosts`` with one online host the composer picks."""

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": _HOST_NAME,
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {},
                        }
                    ]
                }
            ),
        )

    page.route("**/v1/hosts", handle_hosts)


def _seed_recent_workspace(page: Page, path: str) -> None:
    """Prefill the composer's working directory from the recents seed."""
    page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: [{json.dumps(path)}] }})
        );"""
    )


def _open_landing_with_workspace(page: Page, base_url: str, dir_name: str) -> None:
    """Open the landing composer and wait for host + directory to settle.

    Waits for the composer, then for the worktrees probe of the picked
    directory to complete, so the chip assertion that follows judges the
    settled state rather than a loading flash.
    """
    with page.expect_response(_WORKTREES_RE, timeout=30_000):
        page.goto(f"{base_url}/")
        expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    # The picked host and directory are what gate the worktree chip, so
    # pin both down before judging the chip itself. The chip's sr-only
    # "Online" marker renders only when an online host is selected and
    # sandbox mode is off — the exact state the worktree chip depends on.
    expect(page.get_by_test_id("new-chat-landing-host-chip")).to_contain_text("Online")
    expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(dir_name)


def test_worktree_selector_hidden_for_non_git_directory(page: Page, live_server: str) -> None:
    """A non-git working directory must not offer the git worktree chip.

    The worktrees probe answers 400 ("not a git repository") exactly as
    the real endpoint does for a plain directory, so the composer knows
    the directory has no repo to branch. Offering the chip anyway is the
    bug: naming a branch there makes the create fail with ``worktree
    creation failed: not a git repository``.
    """

    def handle_worktrees(route: Route) -> None:
        # Real contract: a non-git path is a 400 with the git error detail.
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"detail": f"not a git repository: {_PLAIN_DIR}"}),
        )

    _stub_hosts(page)
    page.route(_WORKTREES_RE, handle_worktrees)
    _seed_recent_workspace(page, _PLAIN_DIR)

    _open_landing_with_workspace(page, live_server, "plain-dir")

    # The worktree chip must be gone for a directory with no git repo.
    expect(page.get_by_test_id("new-chat-landing-branch-chip")).to_have_count(0)


def test_worktree_selector_shown_for_git_repository(page: Page, live_server: str) -> None:
    """A git repository keeps the worktree chip (guards against over-hiding).

    The probe lists the repo's main work tree — the signal that the
    directory really is a repository — so the chip must stay, letting the
    user name a new worktree branch as before.
    """

    def handle_worktrees(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "path": _REPO_DIR,
                            "branch": "main",
                            "is_main": True,
                            "detached": False,
                        }
                    ],
                }
            ),
        )

    _stub_hosts(page)
    page.route(_WORKTREES_RE, handle_worktrees)
    _seed_recent_workspace(page, _REPO_DIR)

    _open_landing_with_workspace(page, live_server, "repo")

    chip = page.get_by_test_id("new-chat-landing-branch-chip")
    expect(chip).to_be_visible()
    chip.click()
    expect(page.get_by_test_id("new-chat-landing-branch-input")).to_be_visible()
