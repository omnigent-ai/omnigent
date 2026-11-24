"""A shared session's editor reads its pull request but can't change it through the owner's login.

The session's host runs ``gh`` as the owner, so the server lets only the owner change the
session's PRs. This test owns its server and runner, with a PATH-shimmed ``gh`` serving the
branch's PR. The owner sees the panel's change controls. An editor, granted the strongest
level short of ownership, sees the same PR with a note instead, and the server refuses the
editor's comment before ``gh`` is ever run for it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Locator, expect

from tests._helpers.server_runner import server_runner
from tests.e2e_ui.github._gh_shim import (
    create_session,
    make_checkout,
    open_pull_requests_tab,
    stack_env,
    write_gh_shim,
)

REPO = "example/project"
PR_URL = "https://github.com/example/project/pull/42"
PR_TITLE = "Share the session with a reviewer"
BRANCH = "local-topic"
PR = {
    "number": 42,
    "url": PR_URL,
    "title": PR_TITLE,
    "state": "OPEN",
    "isDraft": False,
    "author": {"login": "contributor"},
    "baseRefName": "main",
    "headRefName": BRANCH,
    "headRefOid": "a" * 40,
    "baseRefOid": "b" * 40,
    "body": "Only the owner changes this PR through the session.",
    "comments": [],
    "statusCheckRollup": [],
}
_LEVEL_EDIT = 2
_OWNER_ONLY_NOTE = re.compile(r"Only the session.s owner can change its pull requests here")


@pytest.fixture
def pr_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str, Path]]:
    binary = tmp_path / "bin"
    write_gh_shim(binary, repo=REPO, pr=PR)
    workspace = tmp_path / "checkout"
    make_checkout(workspace, branch=BRANCH, remote=f"https://github.com/{REPO}.git")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = stack_env(binary, runtime, mock_llm_server_url)
    with server_runner(runtime, base_env=env, workspace=workspace) as stack:
        stack.start_runner()
        session_id = create_session(stack.base_url, stack.runner_id, workspace)
        yield stack.base_url, session_id, binary


def _pr_header(rail: Locator) -> Locator:
    return rail.get_by_role("link", name=f"{PR_TITLE} #42")


def _gh_calls(binary: Path) -> list[list[str]]:
    return [
        json.loads(line)["args"] for line in (binary / "gh-calls.log").read_text().splitlines()
    ]


def test_an_editor_reads_the_pr_but_only_the_owner_changes_it(
    browser: Browser, pr_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, session_id, binary = pr_session
    editor = f"editor-{uuid.uuid4().hex[:6]}@ui.test"

    # The owner (the server's default identity) gets the change controls.
    owner_context = browser.new_context()
    try:
        owner_rail = open_pull_requests_tab(owner_context.new_page(), base_url, session_id)
        expect(_pr_header(owner_rail)).to_be_visible(timeout=30_000)
        expect(owner_rail.get_by_role("button", name="Link a PR")).to_be_visible()
        expect(owner_rail.get_by_text(_OWNER_ONLY_NOTE)).to_have_count(0)
    finally:
        owner_context.close()

    granted = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": editor, "level": _LEVEL_EDIT},
        timeout=30,
    )
    assert granted.status_code in (200, 201, 204), granted.text

    editor_context = browser.new_context(extra_http_headers={"X-Forwarded-Email": editor})
    try:
        page = editor_context.new_page()
        rail = open_pull_requests_tab(page, base_url, session_id)
        # The PR itself still renders, so the missing controls below aren't a failed load.
        expect(_pr_header(rail)).to_be_visible(timeout=30_000)
        expect(rail.get_by_text(_OWNER_ONLY_NOTE)).to_be_visible()
        expect(rail.get_by_role("button", name="Link a PR")).to_have_count(0)
        expect(rail.get_by_role("button", name="Unlink PR")).to_have_count(0)
        expect(rail.get_by_role("button", name="Merge")).to_have_count(0)
        page.screenshot(path=str(tmp_path / "editor-pr.png"), animations="disabled")

        refused = page.request.post(
            f"{base_url}/v1/sessions/{session_id}/resources/github/comments",
            data={"pr_url": PR_URL, "body": "Posted as the owner?"},
        )
        assert refused.status == 403, refused.text()
        assert "Only the session's owner can change its pull requests" in refused.text()
        # The panel keeps polling `gh pr view`; the comment endpoint itself is never called.
        assert not any(
            any(arg.endswith("/issues/42/comments") for arg in call) for call in _gh_calls(binary)
        )
    finally:
        editor_context.close()
