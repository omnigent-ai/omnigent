"""E2E: the Pull Requests rail tab for an Azure DevOps workspace and an unsupported remote.

The tab reads ``/v1/sessions/{id}/resources/github*`` for every git provider.
Each test intercepts those endpoints with ``page.route`` and answers with canned
JSON, so the *frontend* is exercised without a real Azure DevOps organization,
``az`` CLI, or git checkout. Two behaviours are pinned:

1. An ``azure_devops`` payload renders the provider name, the ``org/project/repo``
   identity, and the PR title and ``!``-prefixed number.
2. A remote that no provider serves (``unsupported_remote``) renders the
   "No supported remote" empty state under a header that names no provider,
   with the ``gh auth login --hostname`` hint for a GitHub Enterprise host.

Both stay LLM-free and send no message.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.github.test_github_tab import _INFO, _stub_github

_PR_URL = "https://dev.azure.com/contoso/web/_git/app/pullrequest/7"

# The GitHub payload minus its legacy fields, so ``auth`` alone drives the panel.
_ADO_INFO = {
    **{key: value for key, value in _INFO.items() if key not in ("gh_available", "authenticated")},
    "branch": "feature/widget",
    "provider": "azure_devops",
    "auth": {
        "authenticated": True,
        "hint": None,
        "cli": {"name": "az", "available": True},
        "accounts": None,
        "selected_account": None,
    },
    "capabilities": {
        "account_switching": False,
        "base_remote_selection": False,
        "line_counts": False,
        "linked_pr_diff": False,
    },
    "repo": {"name_with_owner": "contoso/web/app"},
    "pr": {
        "number": 7,
        "title": "Add the widget",
        "state": "OPEN",
        "url": _PR_URL,
        "is_draft": False,
        "author": "dev",
        "base_ref": "main",
        "head_ref": "feature/widget",
        "checks": {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []},
        "body": "Adds the widget.",
        "comments": [],
    },
}

# What the host sends when the workspace remote belongs to no supported provider.
_UNSUPPORTED_REMOTE = {
    "object": "session.github.info",
    "available": False,
    "reason": "unsupported_remote",
    "remote_host": "gitlab.com",
    "provider": None,
}


def test_azure_devops_tab_shows_provider_repo_and_pull_request(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An Azure DevOps payload shows its name, repo, PR title, and ``!7``."""
    base_url, session_id = seeded_session
    # Stub the changes and diff endpoints first; the info route registered after
    # them takes precedence, so the panel receives the Azure DevOps payload.
    _stub_github(page)
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_ADO_INFO))
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()

    expect(rail.get_by_role("heading", name="Azure DevOps")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(re.compile(r"contoso/web/app"))).to_be_visible()
    expect(rail.get_by_text("Add the widget", exact=True)).to_be_visible()
    expect(rail.get_by_text("!7", exact=True)).to_be_visible()
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible()
    expect(rail.get_by_role("heading", name="GitHub")).to_have_count(0)
    expect(rail.get_by_role("combobox", name="GitHub account")).to_have_count(0)


def test_unsupported_remote_shows_empty_state_without_a_provider_name(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A remote no provider serves gets "No supported remote" and a neutral header."""
    base_url, session_id = seeded_session
    page.route(
        re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_UNSUPPORTED_REMOTE)
    )
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()

    expect(rail.get_by_text("No supported remote")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("gitlab.com", exact=True)).to_be_visible()
    # A GitHub Enterprise host the user never signed in to lands here too.
    expect(rail.get_by_text("gh auth login --hostname gitlab.com", exact=True)).to_be_visible()
    expect(rail.get_by_role("heading", name="Pull Requests")).to_be_visible()
    expect(rail.get_by_role("heading", name="GitHub")).to_have_count(0)
