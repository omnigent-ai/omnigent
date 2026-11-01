"""Cancelled and skipped checks are reported as their own outcomes.

A ``statusCheckRollup`` CheckRun that ended CANCELLED or SKIPPED never ran to
completion, so the Summary view must not count it as failed or passed: each
outcome gets its own grey pill whose hover list names the jobs. The runner's
classification is the boundary under test, so this test owns its server and
runner and puts a ``gh`` shim first on their PATH; git, the runner and the SPA
run normally.
"""

from __future__ import annotations

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

REPO = "acme/app"
PR_NUMBER = 4242
PR_TITLE = "Report check outcomes"
BRANCH = "report-check-outcomes"
PR = {
    "number": PR_NUMBER,
    "url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
    "title": PR_TITLE,
    "state": "OPEN",
    "isDraft": False,
    "author": {"login": "contributor"},
    "baseRefName": "main",
    "headRefName": BRANCH,
    "headRefOid": "a" * 40,
    "baseRefOid": "b" * 40,
    "body": "Every CI outcome should be visible at a glance.",
    "comments": [],
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "name": name,
            "status": "COMPLETED",
            "conclusion": conclusion,
            "detailsUrl": f"https://github.com/{REPO}/actions/runs/{index}",
        }
        for index, (name, conclusion) in enumerate(
            [
                ("unit", "SUCCESS"),
                ("lint", "SUCCESS"),
                ("e2e", "FAILURE"),
                ("deploy", "CANCELLED"),
                ("docs", "SKIPPED"),
            ],
            start=1,
        )
    ],
}
PILL = re.compile(r"^(\d+)\s+(passed|pending|failed|cancelled|skipped)$")


@pytest.fixture
def pr_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str]]:
    binary = tmp_path / "bin"
    write_gh_shim(binary, repo=REPO, pr=PR)
    workspace = tmp_path / "checkout"
    make_checkout(workspace, branch=BRANCH, remote=f"https://github.com/{REPO}.git")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = stack_env(binary, runtime, mock_llm_server_url)
    with server_runner(runtime, base_env=env, workspace=workspace) as stack:
        stack.start_runner()
        yield stack.base_url, create_session(stack.base_url, stack.runner_id, workspace)


def _hover_jobs(page: Page, pill: Locator) -> list[str]:
    """Hover ``pill``, return the job names its card lists, then close the card."""
    pill.hover()
    card = page.locator('[data-slot="hover-card-content"][data-state="open"]')
    expect(card).to_be_visible()
    jobs = card.get_by_role("listitem").all_inner_texts()
    page.mouse.move(0, 0)
    expect(card).to_have_count(0)
    return jobs


def test_cancelled_and_skipped_checks_get_their_own_pills(
    request: pytest.FixtureRequest, pr_session: tuple[str, str]
) -> None:
    base_url, session_id = pr_session
    page: Page = request.getfixturevalue("page")
    rail = open_pull_requests_tab(page, base_url, session_id)
    linked_prs = rail.get_by_role("list", name="Pull requests")
    expect(linked_prs.get_by_role("button", name="2 passed")).to_be_visible()
    expect(linked_prs.get_by_role("button", name=re.compile("cancelled|skipped"))).to_have_count(0)
    linked_prs.get_by_role("button", name=re.compile(r"^acme\/app #4242")).click()
    expect(rail.get_by_role("link", name=f"{PR_TITLE} #{PR_NUMBER}")).to_be_visible(timeout=30_000)
    checks = rail.get_by_role("heading", name="Checks").locator("..")
    pills = checks.get_by_role("button", name=PILL)
    expect(pills.first).to_be_visible()

    outcomes: dict[str, tuple[int, list[str]]] = {}
    for pill in pills.all():
        text = pill.inner_text().strip()
        match = PILL.match(text)
        assert match, text
        outcomes[match[2]] = (int(match[1]), _hover_jobs(page, pill))
    assert outcomes == {
        "passed": (2, ["unit", "lint"]),
        "failed": (1, ["e2e"]),
        "cancelled": (1, ["deploy"]),
        "skipped": (1, ["docs"]),
    }
