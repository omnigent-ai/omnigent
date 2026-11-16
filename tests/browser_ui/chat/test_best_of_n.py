"""Best-of-N launch, grid comparison and keep, against explicit browser contracts."""

from __future__ import annotations

import json
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    chat_session_handle,
    list_payload,
    message_item,
)
from tests.browser_ui.conftest import BrowserContract

_GROUP = "omnigent.best_of_n.group"
_ATTEMPTS = "omnigent.best_of_n.attempts"
_AGENTS = [
    {
        "id": "ag_claude",
        "name": "claude-native-ui",
        "display_name": "Claude Code",
        "harness": "claude-native",
        "skills": [],
    },
    {
        "id": "ag_codex",
        "name": "codex-native-ui",
        "display_name": "Codex",
        "harness": "codex-native",
        "skills": [],
    },
]


def _screenshot(page: Page, artifacts: Path, name: str) -> None:
    page.screenshot(path=str(artifacts / f"{name}.png"))


def _record_patches(handle: ChatSessionContract, patches: list[dict[str, Any]]) -> None:
    """Merge label patches like the server does, so attempts keep their group label."""
    api = re.compile(rf"^{re.escape(handle.base_url)}/v1/sessions/{handle.session_id}(?:\?.*)?$")

    def patch(route: Route) -> None:
        if route.request.method != "PATCH":
            route.fallback()
            return
        body = json.loads(route.request.post_data or "{}")
        patches.append({"id": handle.session_id, **body})
        snapshot = handle.session_snapshot()
        if "labels" in body:
            handle.update_session(labels={**snapshot["labels"], **body["labels"]})
        if "archived" in body:
            handle.update_session(archived=body["archived"])
        route.fulfill(json=handle.session_snapshot())

    handle.contract.route(api, patch)


@pytest.mark.browser_context_args(device_scale_factor=2)
def test_best_of_n_starts_attempts_in_a_grid_and_keeps_one(
    page: Page, browser_contract: BrowserContract, output_path: str
) -> None:
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        source, *attempts = [
            stack.enter_context(
                contextmanager(chat_session_handle)(page, browser_contract, session_id=session_id)
            )
            for session_id in ("bon-source", "bon-1", "bon-2", "bon-3")
        ]
        source.update_session(
            title="Fix the flaky login test",
            workspace="/repo-worktrees/login",
            agent_id="ag_codex",
            family={"id": "bon-source", "representative_id": "bon-source", "pinned": False},
        )
        source.set_items(
            [
                message_item(
                    "q-1", "user", "Make the login test pass reliably", response_id="r-1"
                ),
                message_item("a-1", "assistant", "Working on it", response_id="r-1"),
            ]
        )
        created: list[dict[str, Any]] = []
        patches: list[dict[str, Any]] = []
        for attempt in attempts:
            attempt.set_items(
                [
                    message_item(
                        f"{attempt.session_id}-a",
                        "assistant",
                        "On it",
                        response_id=f"{attempt.session_id}-r",
                    )
                ]
            )
            _record_patches(attempt, patches)

        def sessions(route: Route) -> None:
            if route.request.method == "POST":
                body = json.loads(route.request.post_data or "{}")
                attempt = attempts[len(created)]
                created.append(body)
                attempt.update_session(
                    title=body["title"],
                    agent_id=body["agent_id"],
                    labels={_GROUP: body["labels"][_GROUP]},
                    git_branch=body["git"]["branch_name"],
                    family={
                        "id": "bon-source",
                        "representative_id": "bon-source",
                        "pinned": False,
                    },
                )
                route.fulfill(status=201, json=attempt.session_snapshot())
                return
            rows = [source, *attempts[: len(created)]]
            route.fulfill(
                json=list_payload(
                    [
                        row.session_snapshot()
                        for row in rows
                        if not row.session_snapshot().get("archived")
                    ]
                )
            )

        browser_contract.route(
            re.compile(rf"^{re.escape(browser_contract.base_url)}/v1/sessions(?:\?.*)?$"), sessions
        )
        browser_contract.json("/v1/agents", {"data": _AGENTS})
        browser_contract.json(OWN_AGENTS, {"data": []})
        browser_contract.json(
            "/v1/info",
            {
                "single_user": True,
                "sibling_sessions": True,
                "features": {"best_of_n": True, "grid_sessions_display_ui": True},
            },
        )
        browser_contract.json(
            "/health",
            {
                "sessions": {
                    s.session_id: {"host_online": True, "runner_online": True}
                    for s in (source, *attempts)
                }
            },
        )

        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(source.url)
        expect(page.get_by_text("Working on it", exact=True)).to_be_visible()

        row = page.locator('aside [data-sidebar-session-id="bon-source"]')
        row.click(button="right")
        page.get_by_role("menuitem", name="Best of N…").click()
        dialog = page.get_by_test_id("best-of-n-dialog")
        expect(dialog.get_by_test_id("best-of-n-prompt")).to_have_value(
            "Make the login test pass reliably"
        )
        expect(dialog.get_by_test_id("best-of-n-attempt-0")).to_have_text("Codex")
        expect(dialog.get_by_test_id("best-of-n-attempt-1")).to_have_text("Claude Code")
        dialog.get_by_test_id("best-of-n-add-attempt").click()
        expect(dialog.get_by_text("New worktrees of /repo", exact=True)).to_be_visible()
        _screenshot(page, artifacts, "1-best-of-n-dialog")
        dialog.get_by_test_id("best-of-n-start").click()

        expect(page).to_have_url(re.compile(r"/grid/[^/]+$"))
        expect(page.locator("[data-grid-session]")).to_have_count(3)
        for attempt in attempts:
            expect(page.locator(f'[data-grid-session="{attempt.session_id}"]')).to_be_visible()

        group = created[0]["labels"][_GROUP]
        assert [body["agent_id"] for body in created] == ["ag_codex", "ag_claude", "ag_claude"]
        for index, body in enumerate(created):
            assert body["sibling_source_id"] == "bon-source"
            assert body["host_id"] == source.host_id
            assert body["workspace"] == "/repo"
            assert body["git"]["branch_name"].startswith(f"best-of-n-{group}-{index + 1}-")
            assert body["labels"]["omnigent.ui"] == "terminal"
        assert len({body["git"]["branch_name"] for body in created}) == 3
        linked = {p["id"]: p["labels"][_ATTEMPTS] for p in patches if "labels" in p}
        assert linked == {a.session_id: "bon-1,bon-2,bon-3" for a in attempts}
        for attempt in attempts:
            sent = [post["body"]["data"]["content"] for post in attempt.event_posts]
            assert sent == [[{"type": "input_text", "text": "Make the login test pass reliably"}]]
        _screenshot(page, artifacts, "2-best-of-n-grid")

        kept = page.locator('[data-grid-session="bon-2"]')
        kept.get_by_role("button", name="Keep Attempt 2 · Claude Code").click()
        keep_dialog = page.get_by_test_id("keep-attempt-dialog")
        expect(keep_dialog.get_by_test_id("keep-attempt-others")).to_have_text(
            "Attempt 1 · CodexAttempt 3 · Claude Code"
        )
        _screenshot(page, artifacts, "3-keep-attempt")
        keep_dialog.get_by_test_id("keep-attempt-confirm").click()

        expect(page).to_have_url(re.compile(r"/c/bon-2$"))
        expect(
            page.get_by_text("Kept Attempt 2 · Claude Code. Archived 2 other attempts.")
        ).to_be_visible()
        archived = sorted(p["id"] for p in patches if p.get("archived") is True)
        assert archived == ["bon-1", "bon-3"]
        _screenshot(page, artifacts, "4-kept-attempt")
