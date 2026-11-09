"""Browser-lane coverage for the Files tab before the workspace environment answers."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract

_LISTING = {
    "object": "list",
    "has_more": False,
    "data": [{"path": "README.md", "name": "README.md", "type": "file", "bytes": 10}],
}
_NO_CHANGES = {"object": "list", "has_more": False, "data": []}


@pytest.mark.parametrize("answer", ["loads", "fails"])
def test_files_tab_says_loading_until_the_workspace_answers(
    page: Page,
    chat_session_contract: ChatSessionContract,
    answer: str,
) -> None:
    """The Files tab says it's loading until the environment answers, then shows the
    files or the failure, never an empty workspace."""
    chat = chat_session_contract
    environment_url = (
        f"{chat.contract.base_url}/v1/sessions/{chat.session_id}/resources/environments/default"
    )
    held: list[Route] = []
    chat.contract.route(environment_url, lambda route: held.append(route))
    chat.contract.route(f"{environment_url}/filesystem*", lambda r: r.fulfill(json=_LISTING))
    chat.contract.route(f"{environment_url}/changes*", lambda r: r.fulfill(json=_NO_CHANGES))
    rail_state = [
        {"id": chat.session_id, "state": {"open": True, "widthPx": 420, "rightRailTab": "files"}}
    ]
    page.add_init_script(
        "localStorage.setItem('omnigent:session-workspace-state', "
        f"{json.dumps(json.dumps(rail_state))});"
    )
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(chat.url)

    rail = page.get_by_role("complementary", name="Workspace")
    status = rail.get_by_text(re.compile(r"^(Loading…|No files in workspace)$"))
    try:
        expect(status).to_have_text("Loading…", timeout=20_000)
    finally:
        # Answer the held request either way, so a failure doesn't leave it pending.
        for route in held:
            if answer == "loads":
                route.fulfill(json={"metadata": {"root": "/workspace"}})
            else:
                route.fulfill(status=500, json={"error": {"message": "environment unavailable"}})
    if answer == "loads":
        expect(rail.get_by_text("README.md")).to_be_visible(timeout=10_000)
    else:
        expect(rail.get_by_text("Failed to load")).to_be_visible(timeout=10_000)
    expect(rail.get_by_text("No files in workspace")).to_have_count(0)
