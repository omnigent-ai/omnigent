"""Cached PR details survive real runner shutdown and a fresh server process.

Own environment: reuse the GraphQL creation fixture's isolated server/runner.
Only the model and GitHub responses are scripted; capture, storage and UI are real.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import create_engine, select

from omnigent.db.db_models import SqlConversationMetadata
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner
from tests.e2e_ui.conftest import configure_mock_llm, open_right_rail, set_fallback_mock_llm
from tests.e2e_ui.github.test_graphql_pr_display import (
    _QUERY,
    _TITLE,
    _URL,
    pr_session,  # noqa: F401
)


@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_created_pr_survives_runner_and_server_shutdown(
    page: Page,
    pr_session: tuple[str, str, str, Path, ServerRunner],  # noqa: F811
    mock_llm_server_url: str,
    mobile: bool,
    tmp_path: Path,
) -> None:
    base_url, session_id, model, binary, stack = pr_session
    command = shlex.join(
        [
            str(binary / "gh"),
            "api",
            "graphql",
            "-f",
            f"query={_QUERY}",
            "--jq",
            ".data.createPullRequest.pullRequest",
        ]
    )
    reply = "Created the pull request."
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "create_pr",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": reply},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Done.")
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Create a pull request."}],
            },
        },
        timeout=30,
    ).raise_for_status()

    def turn_finished() -> bool:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
        return reply in json.dumps(snapshot) and snapshot["status"] == "idle"

    poll_until(turn_finished, timeout_s=60, interval_s=0.1)

    database = create_engine(f"sqlite:///{stack.root / 'chat.db'}")

    def snapshot_saved() -> bool:
        # Read fixture storage directly so no PR-resource GET can backfill the cache.
        with database.connect() as connection:
            raw = connection.scalar(
                select(SqlConversationMetadata.session_state).where(
                    SqlConversationMetadata.id == session_id
                )
            )
        state = json.loads(raw or "{}").get("_omnigent_session_prs_v1", {})
        resources = state.get("snapshots", {}).get(_URL, {})
        return all(kind in resources for kind in ("info", "changes", "diff"))

    try:
        poll_until(snapshot_saved, timeout_s=30, interval_s=0.1)
    finally:
        database.dispose()
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    else:
        info = httpx.get(f"{base_url}/v1/info").json()
        info["features"] = {
            **info.get("features", {}),
            "canvas": True,
            "sidebar_grouping_ui": True,
        }
        page.route("**/v1/info", lambda route: route.fulfill(json=info))
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
    if not mobile:
        open_right_rail(page)
        page.get_by_role("tab", name="Pull Requests").click()
        expect(page.get_by_text("Created from the active session checkout.")).to_be_visible()
    page.screenshot(path=str(tmp_path / "online.png"), animations="disabled")

    assert stack.runner is not None
    stack.runner.kill()
    stack.runner.wait(timeout=10)
    stack.runner = None
    poll_until(
        lambda: (
            not httpx.get(f"{base_url}/v1/runners/{stack.runner_id}/status").json().get("online")
        ),
        timeout_s=30,
        interval_s=0.1,
    )
    # A new page load discards all previously fetched browser data.
    page.goto("about:blank")
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    expect(page.get_by_role("link", name=f"{_TITLE} #42")).to_have_attribute("href", _URL)
    expect(page.get_by_text("Created from the active session checkout.")).to_be_visible()
    expect(page.get_by_text("Last synced", exact=False).first).to_be_visible()
    page.screenshot(path=str(tmp_path / "offline-summary.png"), animations="disabled")
    tabs = page.get_by_role("tablist", name="Pull request")
    tabs.get_by_role("tab", name="Comments 1", exact=True).click()
    expect(page.get_by_text("Ready for review.")).to_be_visible()
    page.screenshot(path=str(tmp_path / "offline-comments.png"), animations="disabled")
    tabs.get_by_role("tab", name="Changes", exact=True).click()
    expect(page.get_by_text('print("Hello from the saved PR")', exact=False).first).to_be_visible()
    page.screenshot(path=str(tmp_path / "offline-diff.png"), animations="disabled")

    stack.restart_server()
    page.reload()
    page.get_by_test_id("composer-pr-link").click()
    tabs.get_by_role("tab", name="Summary", exact=True).click()
    expect(page.get_by_text("Created from the active session checkout.")).to_be_visible()
    page.screenshot(path=str(tmp_path / "server-restarted.png"), animations="disabled")
    for suffix in ("", "/changes", "/diff"):
        response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/github{suffix}")
        response.raise_for_status()
        assert response.json()["cached"] is True
        assert response.json()["cached_at"] > 0
    if not mobile:
        page.get_by_test_id("sidebar-conversation-list").get_by_text(
            "Create a pull request.", exact=True
        ).hover()
        expect(page.get_by_test_id("session-tooltip-pr")).to_contain_text("#42")
        page.screenshot(path=str(tmp_path / "sidebar.png"), animations="disabled")
        page.goto(f"{base_url}/canvas")
        canvas_link = page.get_by_test_id("session-card").get_by_role(
            "link", name="Open pull request #42"
        )
        expect(canvas_link).to_have_attribute("href", _URL)
        page.screenshot(path=str(tmp_path / "canvas.png"), animations="disabled")
