"""PR Summary reviewer and label chips keep their names and buttons in narrow rails."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import TOLERANCE, box
from tests.browser_ui.chat.session_contract import ChatSessionContract

_PR_URL = "https://example.com/acme/app/pull/4242"
_REVIEWERS = [
    {"login": "octo-reviewer", "kind": "user", "state": "CHANGES_REQUESTED"},
    {"login": "alice", "kind": "user", "state": "COMMENTED"},
    {"login": "bob", "kind": "user", "state": "APPROVED"},
    {"login": "platform", "kind": "team", "state": "PENDING"},
]
_STATE_LABELS = {
    "CHANGES_REQUESTED": "Changes requested",
    "COMMENTED": "Commented",
    "APPROVED": "Approved",
    "PENDING": "Pending",
}
_LABELS = ["bug", "needs-review-from-the-platform-team"]
_FITS = "el => el.clientWidth > 0 && el.scrollWidth <= el.clientWidth"


def _assert_inside(inner: Locator, outer: Locator) -> None:
    inner_box, outer_box = box(inner), box(outer)
    assert inner_box["x"] >= outer_box["x"] - TOLERANCE, (inner_box, outer_box)
    assert (
        inner_box["x"] + inner_box["width"] <= outer_box["x"] + outer_box["width"] + TOLERANCE
    ), (inner_box, outer_box)


@pytest.mark.parametrize("font_size", [14, 18], ids=["default-font", "large-font"])
@pytest.mark.parametrize("rail_width", [240, 600], ids=["narrow-rail", "wide-rail"])
def test_summary_chips_keep_names_and_actions_inside(
    page: Page,
    chat_session_contract: ChatSessionContract,
    rail_width: int,
    font_size: int,
) -> None:
    """Reviewer logins stay readable and each chip's button stays inside it; narrow
    rails stack Reviewers above Labels and truncate a long review state, keeping its
    full text in the tooltip, while a roomy rail still shows each full state."""
    chat = chat_session_contract
    api = f"/v1/sessions/{chat.session_id}"
    linked = {
        "url": _PR_URL,
        "host": "example.com",
        "repository": "acme/app",
        "number": 4242,
        "relationship": "created",
    }
    chat.contract.json(
        f"{api}/resources/github",
        {
            "object": "session.github.info",
            "available": True,
            "gh_available": True,
            "authenticated": True,
            "branch": "feature/summary",
            "base_ref": "main",
            "repo": {"name_with_owner": "acme/app"},
            "tracking_available": True,
            "selected_pr_url": _PR_URL,
            "prs": [linked],
            "capabilities": {"reviewer_editing": True, "label_editing": True},
            "pr": {
                "number": 4242,
                "title": "Keep reviewer chips readable",
                "state": "OPEN",
                "url": _PR_URL,
                "is_draft": False,
                "author": "octocat",
                "reviewers": _REVIEWERS,
                "labels": [
                    {"name": name, "color": "d73a4a", "description": None} for name in _LABELS
                ],
                "base_ref": "main",
                "head_ref": "feature/summary",
                "checks": {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []},
                "body": "Summary layout check.",
                "comments": [],
            },
        },
    )
    chat.contract.json(
        f"{api}/pull-requests", {"object": "list", "data": [linked], "synced": True}
    )
    chat.contract.json(f"{api}/resources/github/changes", {"data": []})
    chat.contract.json(f"{api}/resources/github/diff", {"patch": ""})
    environment = f"{api}/resources/environments/default"
    chat.contract.json(environment, {"metadata": {"root": "/workspace"}})
    chat.contract.json(f"{environment}/changes", {"object": "list", "has_more": False, "data": []})
    rail_state = [
        {
            "id": chat.session_id,
            "state": {"open": True, "widthPx": rail_width, "rightRailTab": "github"},
        }
    ]
    page.add_init_script(
        "localStorage.setItem('omnigent:session-workspace-state', "
        f"{json.dumps(json.dumps(rail_state))});"
        f"localStorage.setItem('omnigent:ui-font-size', '{font_size}');"
    )
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(chat.url)

    rail = page.get_by_role("complementary", name="Workspace")
    reviewers = rail.get_by_role("region", name="Reviewers")
    labels = rail.get_by_role("region", name="Labels")
    expect(reviewers.get_by_role("listitem")).to_have_count(len(_REVIEWERS), timeout=30_000)
    assert abs(box(rail)["width"] - rail_width) <= TOLERANCE

    reviewers_box, labels_box = box(reviewers), box(labels)
    if rail_width < 480:
        assert labels_box["y"] >= reviewers_box["y"] + reviewers_box["height"]
    else:
        assert labels_box["x"] >= reviewers_box["x"] + reviewers_box["width"]
        assert abs(labels_box["y"] - reviewers_box["y"]) <= TOLERANCE

    for reviewer in _REVIEWERS:
        login = reviewer["login"]
        chip = reviewers.locator("li").filter(has=page.get_by_text(login, exact=True))
        name = chip.get_by_text(login, exact=True)
        expect(name).to_be_visible()
        assert name.evaluate(_FITS), login
        state = _STATE_LABELS[reviewer["state"]]
        if rail_width < 480:
            expect(chip.get_by_title(state, exact=True)).to_contain_text(state)
        elif font_size == 14:
            assert chip.get_by_text(state, exact=True).evaluate(_FITS), state
        action = chip.get_by_role("button")
        expect(action).to_have_count(1)
        _assert_inside(action, chip)
        _assert_inside(chip, reviewers)
    if rail_width < 480:
        long_state = reviewers.get_by_text(_STATE_LABELS["CHANGES_REQUESTED"], exact=True)
        assert long_state.evaluate("el => el.scrollWidth > el.clientWidth")

    for label in _LABELS:
        chip = labels.get_by_role("listitem").filter(has=page.get_by_text(label, exact=True))
        remove = chip.get_by_role("button", name=f"Remove label {label}", exact=True)
        _assert_inside(remove, chip)
        _assert_inside(chip, labels)
