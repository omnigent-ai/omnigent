"""E2E: merging as another signed-in gh account from the Pull Requests tab.

An account that can read a PR may still be refused its merge, e.g. an
Enterprise Managed User on a public repository. The panel then shows the PR
normally, so the merge dialog offers its own "Merge as" picker. The runner
endpoints are stubbed; no real ``gh`` runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail, stub_session_pull_requests
from tests.e2e_ui.github.test_github_tab import _CHANGES, _INFO, _PR_DIFF

_PR_URL = "https://github.com/acme/app/pull/4242"
_REFUSAL = "The GitHub account on the host cannot merge this pull request."


def test_merge_dialog_switches_the_account_after_a_refusal(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    state: dict[str, Any] = {"account": "work", "merged_as": [], "preferences": []}

    def info() -> dict[str, Any]:
        return {
            **_INFO,
            "provider": "github",
            "tracking_available": True,
            "selected_pr_url": _PR_URL,
            "prs": [
                {
                    "url": _PR_URL,
                    "host": "github.com",
                    "provider": "github",
                    "repository": "acme/app",
                    "number": 4242,
                    "relationship": "created",
                }
            ],
            "pr": {
                **_INFO["pr"],
                "url": _PR_URL,
                "head_sha": "a" * 40,
                "merge_state_status": "CLEAN",
                "auto_merge": None,
                "checks": {"passing": 1, "failing": 0, "pending": 0, "total": 1, "runs": []},
            },
            "auth": {
                "authenticated": True,
                "hint": None,
                "cli": {"name": "gh", "available": True},
                "accounts": [
                    {"login": "work", "active": True, "state": "success", "host": "github.com"},
                    {
                        "login": "personal",
                        "active": False,
                        "state": "success",
                        "host": "github.com",
                    },
                ],
                "selected_account": state["account"],
            },
            "capabilities": {
                "account_switching": True,
                "base_remote_selection": True,
                "line_counts": True,
                "linked_pr_diff": True,
                "merging": True,
            },
        }

    def merge(route: Route) -> None:
        state["merged_as"].append(state["account"])
        if state["account"] == "work":
            route.fulfill(status=400, json={"detail": _REFUSAL})
        else:
            route.fulfill(json={"object": "session.github.pr_merge", "pr_url": _PR_URL})

    def set_preference(route: Route) -> None:
        body = json.loads(route.request.post_data or "{}")
        state["preferences"].append(body)
        state["account"] = body["account"]
        route.fulfill(json=info())

    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=info()))
    stub_session_pull_requests(page, lambda: info()["prs"], lambda _url: None)
    page.route(re.compile(r"/resources/github/changes"), lambda r: r.fulfill(json=_CHANGES))
    page.route(re.compile(r"/resources/github/diff(?:\?|$)"), lambda r: r.fulfill(json=_PR_DIFF))
    page.route(
        re.compile(r"/resources/github/merge-options"),
        lambda r: r.fulfill(
            json={
                "object": "session.github.merge_options",
                "methods": ["merge", "squash", "rebase"],
                "default": "squash",
            }
        ),
    )
    page.route(re.compile(r"/resources/github/merge(?:\?|$)"), merge)
    page.route(re.compile(r"/resources/github/preferences(?:\?|$)"), set_preference)

    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    rail = page.get_by_role("complementary", name="Workspace")
    open_right_rail(page)
    rail.get_by_role("tab", name="Pull Requests").click()
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    # The repository resolved, so the panel itself offers no account selector.
    expect(rail.get_by_role("combobox", name="GitHub account")).to_have_count(0)

    rail.get_by_role("button", name="Squash and merge").click()
    dialog = page.get_by_role("dialog")
    picker = dialog.get_by_role("combobox", name="Merge as")
    expect(picker).to_have_text("work (active)")
    confirm = dialog.get_by_role("button", name="Squash and merge")
    confirm.click()
    expect(dialog.get_by_role("alert")).to_have_text(_REFUSAL)
    page.screenshot(path=tmp_path / "merge-refused.png", animations="disabled")

    picker.click()
    page.get_by_role("option", name="personal").click()
    expect(picker).to_have_text("personal")
    expect(dialog.get_by_role("alert")).to_have_count(0)
    expect(confirm).to_be_enabled()
    assert state["preferences"] == [{"account": "personal", "pr_url": _PR_URL}]
    page.screenshot(path=tmp_path / "merge-as-personal.png", animations="disabled")

    confirm.click()
    expect(dialog).to_have_count(0)
    assert state["merged_as"] == ["work", "personal"]
