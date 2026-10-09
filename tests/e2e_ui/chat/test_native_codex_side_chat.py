"""Real Codex app-server side-chat lifecycle with scripted local model replies."""

from __future__ import annotations

import json
import shutil

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

from .test_side_chat_entrypoints import _ASSISTANT, _items, _send_parent, _start_side_chat


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.skipif(
    not shutil.which("codex") or not shutil.which("tmux"), reason="codex and tmux are required"
)
def test_native_codex_side_chat_inherits_context_and_closes_independently(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Native forks inherit context, isolate follow-ups, and leave the parent usable."""
    base_url, session_id = native_codex_mock_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    parent_question = f"native-turn-1-{session_id}: remember our parent context"
    parent_reply = "The native parent context is ready."
    question = f"native-turn-2-{session_id}: answer a side question"
    followup = f"native-turn-3-{session_id}: answer a follow-up"
    resumed_question = f"native-turn-4-{session_id}: answer in the parent"
    reset_mock_llm(mock_llm_server_url)
    for prompt, reply in (
        (parent_question, parent_reply),
        (question, "First native side answer."),
        (followup, "Second native side answer."),
        (resumed_question, "The native parent still works."),
    ):
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": reply}],
            match=prompt.split(":")[0],
            required_tools=["exec_command"],
        )
    set_fallback_mock_llm(mock_llm_server_url, "gpt-4o", "")

    _send_parent(page, parent_question, parent_reply)
    parent_items = _items(base_url, session_id)
    _start_side_chat(page, "slash", question)
    pane = page.locator(".side-chat-backdrop")
    expect(pane.locator(_ASSISTANT).filter(has_text="First native side answer.")).to_be_visible(
        timeout=60_000
    )
    expect(pane.get_by_text(parent_reply, exact=True)).to_have_count(0)

    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0)
    response.raise_for_status()
    children = [
        child
        for child in response.json()["data"]
        if child.get("labels", {}).get("omnigent.codex_native.agent_nickname") == "Side chat"
    ]
    assert len(children) == 1
    child_id = children[0]["id"]
    parent_response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    parent_response.raise_for_status()
    child_response = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
    child_response.raise_for_status()
    parent, child = parent_response.json(), child_response.json()
    assert child["runner_id"] == parent["runner_id"]
    child_thread_id = child["labels"]["omnigent.codex_native.subagent_thread_id"]
    assert child_thread_id
    assert child_thread_id != parent["external_session_id"]

    page.get_by_test_id("side-chat-input").fill(followup)
    expect(page.get_by_test_id("side-chat-send")).to_be_enabled(timeout=30_000)
    page.get_by_test_id("side-chat-send").click()
    expect(pane.locator(_ASSISTANT).filter(has_text="Second native side answer.")).to_be_visible(
        timeout=60_000
    )
    expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
    assert _items(base_url, session_id) == parent_items
    child_text = str(_items(base_url, child_id))
    for text in (question, followup, "First native side answer.", "Second native side answer."):
        assert text in child_text

    model_inputs = [
        json.dumps(request.get("input", []))
        for request in get_mock_requests(mock_llm_server_url, key="gpt-4o")
    ]
    first_input = next(text for text in model_inputs if question in text and followup not in text)
    followup_input = next(text for text in model_inputs if followup in text)
    assert parent_question in first_input
    assert parent_reply in first_input
    assert question in followup_input
    assert "First native side answer." in followup_input

    with page.expect_response(f"**/v1/sessions/{child_id}/events") as stopped:
        page.get_by_role("button", name="Close Side chat 1", exact=True).click()
    assert stopped.value.ok
    expect(page.get_by_test_id("side-chat-input")).to_have_count(0)
    child_response = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
    child_response.raise_for_status()
    assert child_response.json()["labels"]["omnigent.closed"] == "true"
    _send_parent(page, resumed_question, "The native parent still works.")
    parent_text = str(_items(base_url, session_id))
    assert question not in parent_text
    assert followup not in parent_text
