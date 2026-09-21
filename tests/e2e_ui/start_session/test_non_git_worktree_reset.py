"""Switching to a non-Git workspace discards the composer's worktree request.

Host registration and session creation use the start-session route fixtures;
worktree responses come from the real host Git probe on temporary directories.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Route, async_playwright, expect

from omnigent.host.git_worktree import WorktreeError, list_worktrees
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _WORKTREES_RE,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)


@pytest.mark.parametrize("project_defaults", [False, True], ids=["standalone", "project"])
def test_non_git_workspace_clears_worktree_request(
    seeded_session: tuple[str, str], tmp_path: Path, project_defaults: bool
) -> None:
    """A non-Git folder disables worktrees and cannot inherit project Git defaults."""
    repo = tmp_path / "git-repo"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/repo.git"],
        check=True,
        capture_output=True,
    )
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    _run_in_fresh_loop(_drive_workspace_switch(*seeded_session, repo, plain, project_defaults))


async def _drive_workspace_switch(
    base_url: str, session_id: str, repo: Path, plain: Path, project_defaults: bool
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def probe_worktrees(route: Route) -> None:
                path = parse_qs(urlsplit(route.request.url).query)["path"][0]
                try:
                    worktrees = list_worktrees(repo_path=path)
                except WorktreeError as exc:
                    await route.fulfill(status=400, json={"detail": exc.message})
                else:
                    await route.fulfill(
                        json={"object": "list", "data": [asdict(tree) for tree in worktrees]}
                    )

            await page.route(_WORKTREES_RE, probe_worktrees)
            recent = json.dumps({_HOST_ID: [str(repo), str(plain)]})
            await page.add_init_script(
                f"localStorage.setItem('omnigent:recent-workspaces', JSON.stringify({recent}))"
            )

            if project_defaults:
                await page.route(
                    "**/v1/sessions/projects",
                    lambda route: route.fulfill(json=[{"id": "proj_git", "name": "GitProject"}]),
                )
                await page.route(
                    "**/v1/projects/proj_git",
                    lambda route: route.fulfill(
                        json={
                            "id": "proj_git",
                            "name": "GitProject",
                            "config": {
                                "host_id": _HOST_ID,
                                "workspace": str(repo),
                                "git": {"branch_name": "feature/project-default"},
                            },
                        }
                    ),
                )

            suffix = "?project=GitProject" if project_defaults else ""
            await page.goto(f"{base_url}/{suffix}")
            workspace_chip = page.get_by_test_id("new-chat-landing-workspace-chip")
            branch_chip = page.get_by_test_id("new-chat-landing-branch-chip")
            await expect(workspace_chip).to_contain_text("git-repo", timeout=30_000)
            await expect(branch_chip).to_be_enabled()
            await branch_chip.click()
            branch_input = page.get_by_test_id("new-chat-landing-branch-input")
            await branch_input.fill("feature/explicit-worktree")
            await expect(branch_chip).to_contain_text("feature/explicit-worktree")
            await page.keyboard.press("Escape")

            await workspace_chip.click()
            await page.get_by_role("button", name=str(plain), exact=True).click()
            await expect(workspace_chip).to_contain_text("plain-folder")
            await expect(branch_chip).to_have_count(0)
            await expect(branch_input).to_be_hidden()

            await page.get_by_test_id("new-chat-landing-input").fill("Work in the plain folder")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == str(plain), body
            if project_defaults:
                assert body["project_id"] == "proj_git", body
                # Explicit null prevents server-side project default filling.
                assert "git" in body and body["git"] is None, body
            else:
                assert "git" not in body, body
        finally:
            await browser.close()
