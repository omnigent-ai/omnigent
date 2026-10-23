"""Built-SPA proof for structured questions in full chat and the side-chat tab."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    _ChatSseStream,
    install_chat_session_routes,
    list_payload,
    message_item,
)


@pytest.fixture(params=[False, True], ids=["full-chat", "side-chat"])
def question_chat(
    page: Page, chat_session_contract: ChatSessionContract, request: pytest.FixtureRequest
) -> Iterator[tuple[ChatSessionContract, ChatSessionContract]]:
    parent = chat_session_contract
    if not request.param:
        yield parent, parent
        return
    stream = _ChatSseStream()
    source = ChatSessionContract(page, parent.contract, stream, session_id="question-side-chat")
    install_chat_session_routes(source)
    source.update_session(
        labels={"omnigent.side_chat": "1", "omnigent.side_chat.source_id": parent.session_id}
    )
    parent.set_items(
        [
            message_item(
                "parent-text", "assistant", "Independent parent chat.", response_id="parent-turn"
            )
        ]
    )
    parent.contract.json(f"/v1/sessions/{parent.session_id}/policies", list_payload([]))
    parent.contract.json("/v1/policy-registry", list_payload([]))
    parent.contract.json(f"/v1/sessions/{parent.session_id}/owner", {"owner": None})
    state = {
        "id": parent.session_id,
        "state": {
            "open": True,
            "rightRailTab": "sidechat",
            "openSideChats": [source.session_id],
            "selectedSideChatId": source.session_id,
        },
    }
    page.add_init_script(
        "localStorage.setItem('omnigent:session-workspace-state', "
        f"JSON.stringify([{json.dumps(state)}]));"
        f"localStorage.setItem('omnigent.sideChatInherited:{source.session_id}', '[]');"
    )
    try:
        yield source, parent
    finally:
        stream.close()


def _open_question_chat(page: Page, parent: ChatSessionContract, side_chat: bool) -> None:
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.goto(parent.url)
    if side_chat:
        toggle = page.get_by_role("button", name=re.compile("right panel", re.I))
        if toggle.get_attribute("aria-label") == "Expand right panel":
            toggle.click()
        page.get_by_role("tab", name="Side chat 1", exact=True).click()


@pytest.mark.parametrize("harness", ["codex", "acp:cline"])
def test_async_questions_survive_reload_and_reply_to_the_source_chat(
    page: Page,
    question_chat: tuple[ChatSessionContract, ChatSessionContract],
    tmp_path: Path,
    harness: str,
) -> None:
    page.add_init_script("Object.defineProperty(window.crypto, 'subtle', { value: undefined })")
    source, parent = question_chat
    source.harness = harness
    side_chat = source is not parent
    prompt = message_item(
        "question-source",
        "assistant",
        "Original structured question text.",
        response_id="question-turn",
    )
    prompt["model"] = source.agent_id
    prompt["async_user_input"] = {
        "call_id": "question-call",
        "reply_id": "a" * 32,
        "questions": [
            {"title": "Choose a route", "options": ["First (Recommended)", "Second"]},
            {"title": "Describe your answer", "options": None},
        ],
    }
    source.set_items([prompt] if side_chat else [])
    _open_question_chat(page, parent, side_chat)
    source.wait_for_stream()
    if not side_chat:
        source.set_items([prompt])
        source.emit({"event": "response.output_item.done", "data": {"item": prompt}})
    root = page.locator(".side-chat-backdrop") if side_chat else page
    card = root.get_by_test_id("async-question-card")
    expect(card).to_be_visible()
    expect(root.get_by_text("Original structured question text.", exact=True)).to_be_visible()
    expect(card.get_by_label("First (Recommended)")).to_be_checked()
    assert not source.event_posts
    page.screenshot(path=str(tmp_path / "question-controls.png"))
    card.get_by_label("Second").click()
    card.get_by_role("button", name="Next").click()
    card.get_by_placeholder("Type something").fill("My custom answer")
    composer = (
        root.get_by_test_id("side-chat-input")
        if side_chat
        else root.get_by_label("Message the agent")
    )
    composer.fill("Preserve this composer draft")
    page.reload()
    expect(card.get_by_placeholder("Type something")).to_have_value("My custom answer")
    if side_chat:
        composer.fill("Preserve this composer draft")
    else:
        expect(composer).to_have_value("Preserve this composer draft")
    assert not source.event_posts
    held: list[Route] = []
    posted: list[dict[str, Any]] = []

    def hold_reply(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        held.append(route)
        posted.append(route.request.post_data_json)

    source.contract.route(
        re.compile(
            rf"^{re.escape(source.contract.base_url)}/v1/sessions/{source.session_id}/events$"
        ),
        hold_reply,
    )
    card.get_by_role("button", name="Submit").evaluate(
        "button => { button.click(); button.click(); }"
    )
    expect(card.get_by_role("status")).to_contain_text("Sending")
    if side_chat:
        page.get_by_label("Message the agent").fill("Independent parent draft")
    expect(card.get_by_role("button", name="Retry delivery")).to_be_disabled()
    assert len(posted) == 1
    body = posted[0]["data"]
    assert body["async_question_reply"] == {
        "source_item_id": prompt["id"],
        "answers": {"question-call:0": "Second", "question-call:1": "My custom answer"},
        "cancelled": False,
    }
    assert len(body["stable_id"]) == 32
    held[0].fulfill(json={"queued": True, "pending_id": "pending-question"})
    expect(card.get_by_role("status")).to_contain_text("Reply queued")
    expect(composer).to_have_value("Preserve this composer draft")
    answer = message_item(
        body["stable_id"], "user", body["content"][0]["text"], response_id="answer-turn"
    )
    answer["async_question_reply"] = body["async_question_reply"]
    answer["user_authored"] = True
    source.set_items([answer, prompt])
    page.reload()
    expect(card.get_by_role("status")).to_have_text("Answer submitted")
    expect(card.get_by_role("button", name="Submit")).to_have_count(0)
    if not side_chat:
        expect(composer).to_have_value("Preserve this composer draft")
    if side_chat:
        expect(page.get_by_label("Message the agent")).to_have_value("Independent parent draft")
        assert not parent.event_posts
    assert len(posted) == 1
    page.screenshot(path=str(tmp_path / "question-answer-restored.png"))


def test_native_claude_questions_reply_through_the_source_hook(
    page: Page,
    question_chat: tuple[ChatSessionContract, ChatSessionContract],
    tmp_path: Path,
) -> None:
    source, parent = question_chat
    source.harness = "claude-native"
    source.set_items(
        [
            message_item(
                "waiting", "assistant", "Waiting for input.", response_id="native-question-turn"
            )
        ]
    )
    elicitation_id = "elicit_claude_browser_questions"
    resolve = f"/v1/sessions/{source.session_id}/elicitations/{elicitation_id}/resolve"

    def resolved(_request: Request) -> dict[str, object]:
        source.update_session(pending_elicitations=[])
        return {}

    source.contract.json(resolve, resolved, method="POST")
    side_chat = source is not parent
    _open_question_chat(page, parent, side_chat)
    source.wait_for_stream()
    event = {
        "event": "response.elicitation_request",
        "data": {
            "elicitation_id": elicitation_id,
            "response_id": "native-question-turn",
            "params": {
                "mode": "form",
                "message": "Claude has two questions",
                "phase": "permission_request",
                "policy_name": "claude_native_permission",
                "content_preview": "",
                "requestedSchema": {},
                "ask_user_question": {
                    "questions": [
                        {
                            "question": "Which colour?",
                            "options": [{"label": "Blue"}, {"label": "Green"}],
                            "multiSelect": False,
                        },
                        {
                            "question": "Which test mode?",
                            "options": [{"label": "Fast"}, {"label": "Thorough"}],
                            "multiSelect": False,
                        },
                    ]
                },
            },
        },
    }
    source.update_session(pending_elicitations=[{"type": event["event"], **event["data"]}])
    source.emit(event)
    root = page.locator(".side-chat-backdrop") if side_chat else page
    card = root.get_by_test_id("approval-card")
    expect(card.get_by_test_id("ask-user-question-form")).to_be_visible()
    composer = (
        root.get_by_test_id("side-chat-input")
        if side_chat
        else root.get_by_label("Message the agent")
    )
    composer.fill("Keep this independent draft")
    card.get_by_label("Blue", exact=True).check()
    card.get_by_role("button", name="Next").click()
    card.get_by_placeholder("Type something").fill("My own test mode")
    if side_chat:
        page.get_by_label("Message the agent").fill("Independent parent draft")
    page.screenshot(path=str(tmp_path / "native-claude-questions.png"))
    with page.expect_request(
        lambda r: r.method == "POST" and r.url.endswith(resolve)
    ) as submitted:
        card.get_by_role("button", name="Submit").click()
    body = submitted.value.post_data_json
    assert body["action"] == "accept"
    assert body["content"] == {"Which colour?": "Blue", "Which test mode?": "My own test mode"}
    expect(composer).to_have_value("Keep this independent draft")
    expect(card.get_by_role("button", name="Submit")).to_have_count(0)
    assert not source.event_posts
    if side_chat:
        expect(page.get_by_label("Message the agent")).to_have_value("Independent parent draft")
        assert not parent.event_posts
