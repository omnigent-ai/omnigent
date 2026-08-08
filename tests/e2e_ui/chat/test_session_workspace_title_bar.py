"""E2E: the session header names the repo and branch the session works in.

The header's title bar reads the default environment's ``metadata.git``, which
the runner derives from the workspace's ``.git`` state. This drives the real
SPA against the real runner and asserts the two agree: whatever repo and ref
the API reports must be what the header renders.

The runner's workspace is a git checkout locally but a bare temp directory in
CI, so the fixture initializes a repo at the reported root when there isn't
one (and removes it afterwards). Without that the test would skip in CI —
covering nothing exactly where coverage is wanted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_SEEDED_BRANCH = "e2e/title-bar"


def _read_git_metadata(base_url: str, session_id: str) -> dict[str, object] | None:
    """Return the session environment's ``metadata.git`` block, or None."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default",
        timeout=10.0,
    )
    resp.raise_for_status()
    metadata = resp.json().get("metadata", {})
    git = metadata.get("git")
    return git if isinstance(git, dict) else None


def _read_root(base_url: str, session_id: str) -> str | None:
    """Return the session environment's workspace root path, or None."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default",
        timeout=10.0,
    )
    resp.raise_for_status()
    root = resp.json().get("metadata", {}).get("root")
    return root if isinstance(root, str) else None


@pytest.fixture
def workspace_git(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, dict[str, object]]]:
    """Yield the session's git identity, seeding a repo at the root if needed.

    :param seeded_session: ``(base_url, session_id)`` from the shared fixture.
    :returns: ``(base_url, session_id, git)`` where ``git`` is the
        environment's ``metadata.git`` block.
    """
    base_url, session_id = seeded_session
    git = _read_git_metadata(base_url, session_id)
    if git is not None:
        # Already a checkout (local dev runs the runner in this repo) — assert
        # against the live state rather than nesting a repo inside it.
        yield (base_url, session_id, git)
        return

    root = _read_root(base_url, session_id)
    assert root, "environment must report metadata.root"
    Path(root).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", _SEEDED_BRANCH],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    try:
        seeded = _read_git_metadata(base_url, session_id)
        assert seeded is not None, (
            "runner reported no metadata.git for a freshly initialized repo at "
            f"{root!r} — the environment resource is not reading .git state"
        )
        yield (base_url, session_id, seeded)
    finally:
        shutil.rmtree(Path(root) / ".git", ignore_errors=True)


def test_header_names_the_repo_and_ref_the_session_works_in(
    page: Page,
    workspace_git: tuple[str, str, dict[str, object]],
) -> None:
    """The title bar renders the repo and ref the runner reports for the workspace."""
    base_url, session_id, git = workspace_git
    repo = str(git["repo"])
    ref = git["ref"]
    page.goto(f"{base_url}/c/{session_id}")

    identity = page.get_by_test_id("workspace-identity")
    expect(identity).to_be_visible(timeout=30_000)
    # The repo name is the part a user scans for when several sessions are
    # open; a miss here means the runner's git block never reached the header.
    expect(identity).to_contain_text(repo)
    assert ref is not None, "a checked-out repo must report a ref"
    expect(identity).to_contain_text(str(ref))
    if git.get("worktree"):
        # The only on-screen difference between two sessions on the same repo.
        expect(identity).to_contain_text("worktree")
