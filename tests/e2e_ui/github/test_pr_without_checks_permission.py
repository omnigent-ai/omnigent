"""The Pull Requests panel resolves a PR whose checks the gh token cannot read.

GitHub refuses ``statusCheckRollup`` to a fine-grained personal access token
(``GraphQL: Resource not accessible by personal access token``) while every other
PR field, ``gh auth status``, ``gh repo view`` and the REST ``commits/{sha}/pulls``
lookup succeed. This test owns its server and runner so a PATH-shimmed ``gh``
can stand in for such a token; git, the runner and the SPA run normally. The PR
must still render, with the Checks area explaining why its check runs are missing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

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
PR_TITLE = "Teach the panel about token limits"
BRANCH = "local-topic"
CHECKS_REFUSED = (
    "GraphQL: Resource not accessible by personal access token "
    "(repository.pullRequests.nodes.0.statusCheckRollup.nodes.0.commit.statusCheckRollup)"
)
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
    "body": "Resolves even when checks are unreadable.",
    "comments": [],
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "name": "unit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/example/project/actions/runs/1",
        }
    ],
}


@pytest.fixture
def pr_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str, Path]]:
    binary = tmp_path / "bin"
    # Stands in for gh signed in with a fine-grained PAT: only a ``pr view`` that
    # asks for statusCheckRollup fails, exactly as the real CLI does for such a token.
    write_gh_shim(binary, repo=REPO, pr=PR, checks_error=CHECKS_REFUSED)
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


def _checks_note(rail: Locator) -> Locator:
    return rail.get_by_text(re.compile(r"Checks can.t be read with this GitHub token"))


def test_branch_pr_resolves_when_checks_are_forbidden(
    request: pytest.FixtureRequest, pr_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, session_id, binary = pr_session
    page: Page = request.getfixturevalue("page")
    rail = open_pull_requests_tab(page, base_url, session_id)
    no_pr = rail.get_by_text(re.compile(rf"No open PR for\s*{BRANCH}"))
    row = rail.get_by_role("list", name="Pull requests").get_by_role(
        "button", name=re.compile(r"^example\/project #42")
    )
    expect(row.or_(no_pr)).to_be_visible(timeout=30_000)
    if row.count():
        row.click()
    page.screenshot(path=str(tmp_path / "branch-pr.png"), animations="disabled")
    calls = [json.loads(line) for line in (binary / "gh-calls.log").read_text().splitlines()]
    assert any(c["args"][:2] == ["pr", "view"] and c["rc"] == 1 for c in calls), calls
    expect(no_pr).to_have_count(0)
    expect(_pr_header(rail)).to_be_visible()
    expect(_pr_header(rail)).to_have_attribute("href", PR_URL)
    expect(_checks_note(rail)).to_be_visible()


def test_linked_pr_resolves_when_checks_are_forbidden(
    request: pytest.FixtureRequest, pr_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, session_id, binary = pr_session
    page: Page = request.getfixturevalue("page")
    rail = open_pull_requests_tab(page, base_url, session_id)
    no_pr = rail.get_by_text(re.compile(rf"No open PR for\s*{BRANCH}"))
    row = rail.get_by_role("list", name="Pull requests").get_by_role(
        "button", name=re.compile(r"^example\/project #42")
    )
    expect(row.or_(no_pr)).to_be_visible(timeout=30_000)
    rail.get_by_role("button", name="Link a PR", exact=True).click()
    rail.get_by_label("Pull request URL").fill(PR_URL)
    rail.get_by_role("button", name="Link", exact=True).click()
    expect(rail.get_by_label("Pull request URL")).to_have_count(0)
    row.click()
    unreachable = rail.get_by_text(re.compile(r"Can.t reach the upstream repo"))
    expect(_pr_header(rail).or_(unreachable)).to_be_visible(timeout=30_000)
    page.screenshot(path=str(tmp_path / "linked-pr.png"), animations="disabled")
    calls = [json.loads(line) for line in (binary / "gh-calls.log").read_text().splitlines()]
    assert any(c["args"][:3] == ["pr", "view", "42"] and c["rc"] == 1 for c in calls), calls
    expect(unreachable).to_have_count(0)
    expect(rail.get_by_role("combobox", name=re.compile(r"GitHub account"))).to_have_count(0)
    expect(_pr_header(rail)).to_be_visible()
    expect(_checks_note(rail)).to_be_visible()
