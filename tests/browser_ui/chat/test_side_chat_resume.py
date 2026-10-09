"""Browser-contract proof for side-chat sending and Resume flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload


def test_runnerless_side_chat_sends_from_its_direct_url(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Chat-only side-chat ancestry does not require a coding workspace."""
    chat = chat_session_contract
    source_id = "browser-side-parent"
    chat.update_session(
        host_id=None,
        runner_id=None,
        workspace=None,
        runner_online=True,
        host_online=False,
        labels={"omnigent.side_chat": "1", "omnigent.side_chat.source_id": source_id},
    )
    chat.set_health(runner_online=True, host_online=False)
    source = {**chat._session(), "id": source_id, "labels": {}}
    chat.contract.json(f"/v1/sessions/{source_id}", source)

    page.goto(chat.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    prompt = "Continue this side chat without a workspace."
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()

    expect(composer).to_have_value("")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert len(chat.event_posts) == 1
    event = chat.event_posts[0]["body"]
    assert event["type"] == "message"
    assert event["data"]["content"] == [{"type": "input_text", "text": prompt}]
    assert chat.session_patches == []


def test_side_chat_resume_conflict_refetches_metadata_and_seals_pane(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
) -> None:
    """A failed side-chat resume becomes read-only without reloading the app."""
    chat = chat_session_contract
    artifacts = Path(output_path)
    child_id = "browser-side-child"
    child_path = f"/v1/sessions/{child_id}"
    child_gets: list[dict[str, Any]] = []
    retry_posts: list[dict[str, Any]] = []
    stop_posts: list[dict[str, Any]] = []
    child_closed = False

    chat.update_session(harness="openai-agents")
    chat.set_items(
        [
            {
                "id": "parent-history",
                "response_id": "parent-response",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "input_text", "text": "Parent remains usable."}],
            }
        ]
    )
    child_session = dict(chat._session())
    child_session.update(
        {
            "id": child_id,
            "agent_id": chat.agent_id,
            "agent_name": chat.agent_id,
            "harness": "openai-agents",
            "status": "failed",
            "labels": {},
        }
    )
    child_items = [
        {
            "id": "child-history",
            "response_id": "child-response",
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [
                {"type": "input_text", "text": "Side-chat history that must remain visible."}
            ],
        },
        {
            "id": "child-error",
            "response_id": "child-response",
            "type": "error",
            "status": "completed",
            "source": "execution",
            "code": "required_terminal_exited",
            "message": "The side-chat runner exited before it could resume.",
        },
    ]

    contract = chat.contract
    base = re.escape(chat.base_url)
    child_session_pattern = re.compile(rf"^{base}{re.escape(child_path)}(?:\?.*)?$")

    def get_child_session(route: Route) -> None:
        if route.request.method != "GET":
            route.fallback()
            return
        snapshot = dict(child_session)
        snapshot["labels"] = {"omnigent.closed": "true"} if child_closed else {}
        child_gets.append({"labels": dict(snapshot["labels"])})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(snapshot))

    contract.route(child_session_pattern, get_child_session)
    contract.json(f"{child_path}/items", lambda _request: list_payload(child_items))
    contract.json(f"{child_path}/agent", lambda _request: chat._agent())
    contract.json(f"{child_path}/child_sessions", list_payload([]))
    contract.json(f"{child_path}/resources/terminals", list_payload([]))
    contract.response(f"{child_path}/read-state", method="PUT")
    contract.sse(f"{child_path}/stream")
    contract.json(f"/v1/sessions/{chat.session_id}/policies", list_payload([]))
    contract.json("/v1/policy-registry", list_payload([]))
    contract.json(f"/v1/sessions/{chat.session_id}/owner", {"owner": None})

    def retry_child(route: Route) -> None:
        nonlocal child_closed
        if route.request.method != "POST":
            route.fallback()
            return
        body = json.loads(route.request.post_data or "{}")
        if body == {"type": "stop_session", "data": {}}:
            stop_posts.append(body)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"queued": False}),
            )
            return
        retry_posts.append(body)
        assert body == {"type": "retry_session", "data": {}}
        child_closed = True
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {"error": {"code": "conflict", "message": "This side chat has ended."}}
            ),
        )

    contract.route(re.compile(rf"^{base}{re.escape(child_path)}/events$"), retry_child)

    page.goto(chat.url)
    page.evaluate(
        """state => {
          localStorage.setItem("omnigent:session-workspace-state", JSON.stringify([state]));
          localStorage.setItem("omnigent.sideChatInherited:browser-side-child", "[]");
        }""",
        {
            "id": "browser-chat-session",
            "state": {
                "open": True,
                "rightRailTab": "sidechat",
                "openSideChats": ["browser-side-child"],
                "selectedSideChatId": "browser-side-child",
            },
        },
    )
    page.reload()
    workspace_state = page.evaluate("localStorage.getItem('omnigent:session-workspace-state')")
    assert workspace_state is not None and '"browser-side-child"' in workspace_state
    panel_toggle = page.get_by_role("button", name=re.compile("right panel", re.IGNORECASE))
    if panel_toggle.get_attribute("aria-label") == "Expand right panel":
        panel_toggle.click()
    expect(page.get_by_role("tab", name="Side chat 1")).to_be_visible(timeout=10_000)
    page.get_by_role("tab", name="Side chat 1").click()
    expect(page.get_by_text("Side-chat history that must remain visible")).to_be_visible(
        timeout=20_000
    )
    resume = page.get_by_role("button", name="Resume session", exact=True)
    expect(resume).to_be_visible(timeout=10_000)
    expect(page.get_by_text("Parent remains usable.")).to_be_visible()
    page.screenshot(path=str(artifacts / "side-chat-before.png"), full_page=True)
    metadata_gets_before = len(child_gets)

    resume.click()

    expect(page.get_by_text("This side chat has ended and can’t be continued.")).to_be_visible(
        timeout=10_000
    )
    expect(resume).to_have_count(0)
    expect(page.get_by_text("Side-chat history that must remain visible")).to_be_visible()
    expect(page.get_by_text("Parent remains usable.")).to_be_visible()
    parent_composer = page.get_by_placeholder("Send a message…")
    expect(parent_composer).to_be_enabled()
    parent_composer.fill("Parent composer remains editable.")
    expect(parent_composer).to_have_value("Parent composer remains editable.")
    parent_composer.fill("")
    assert retry_posts == [{"type": "retry_session", "data": {}}]
    assert len(child_gets) > metadata_gets_before
    assert child_gets[-1]["labels"] == {"omnigent.closed": "true"}
    page.screenshot(path=str(artifacts / "side-chat-after.png"), full_page=True)
