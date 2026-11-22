"""Side-chat replies mark the parent session's sidebar row unread.

A side chat is its own session, hidden from the sidebar, so a reply that lands
after the user has moved to another session must surface on the parent's row,
also while the parent's own turn is still running. The fixture's openai-agents
runner answers both conversations through the mock LLM; the fork, dispatch,
status, and sidebar list/updates paths are the real backend.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.chat.test_side_chat_entrypoints import _items, _start_side_chat
from tests.e2e_ui.conftest import configure_mock_llm

_UNREAD_SIDE_CHAT = "Unread side chat reply"


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its link."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _running_badge(row: Locator) -> Locator:
    return row.locator('[data-testid="session-state-badge"][data-state="running"]')


def _model_received(mock_url: str, token: str) -> bool:
    """Whether the mock model has received (and, when gated, is holding) a turn with *token*."""
    requests = get_mock_requests(mock_url)
    return any(token in json.dumps(request.get("input", [])) for request in requests)


@pytest.mark.parametrize("parent_running", [False, True], ids=["parent-idle", "parent-running"])
def test_side_chat_reply_marks_parent_row_unread(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
    parent_running: bool,
) -> None:
    """A reply that lands after the user leaves flags the parent row until it is opened."""
    base_url, parent_id, other_id = seeded_session_pair
    main_token = f"main-unread-{parent_id}"
    side_token = f"side-unread-{parent_id}"
    main_gate = f"main-gate-{parent_id}"
    side_gate = f"side-gate-{parent_id}"
    side_reply = "The side chat answered while you were away."
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": side_reply, "block": True, "gate": side_gate}],
        key=side_token,
        match=side_token,
    )
    if parent_running:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "The main task is done.", "block": True, "gate": main_gate}],
            key=main_token,
            match=main_token,
        )
    side_chat_ids: list[str] = []
    try:
        page.goto(f"{base_url}/c/{parent_id}")
        # The placeholder changes while a turn runs; the label doesn't.
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible()
        if parent_running:
            composer.fill(f"{main_token}: keep working")
            page.get_by_role("button", name="Send", exact=True).click()
            poll_until(lambda: _model_received(mock_llm_server_url, main_token), interval_s=0.1)

        with page.expect_response(f"**/v1/sessions/{parent_id}/fork") as fork:
            _start_side_chat(page, "panel", f"{side_token}: answer this side question")
        assert fork.value.ok
        side_chat_id = fork.value.json()["id"]
        side_chat_ids.append(side_chat_id)
        poll_until(lambda: _model_received(mock_llm_server_url, side_token), interval_s=0.1)

        # Switch to another session while the side chat is still answering.
        page.locator(f'a[href="/c/{other_id}"]').click()
        expect(page).to_have_url(f"{base_url}/c/{other_id}")
        left_parent_at = time.time()
        parent_row = _row(page, parent_id)
        expect(parent_row).to_be_visible()
        marker = parent_row.get_by_role("img", name=_UNREAD_SIDE_CHAT)
        expect(marker).to_have_count(0)
        if parent_running:
            expect(_running_badge(parent_row)).to_be_visible()

        # Read state has one-second resolution; land the reply in a later second.
        while int(time.time()) <= int(left_parent_at):
            page.wait_for_timeout(50)
        released = httpx.post(
            f"{mock_llm_server_url}/gate/release", params={"gate": side_gate}, timeout=5.0
        )
        released.raise_for_status()
        assert released.json()["released"] is True
        poll_until(
            lambda: side_reply in json.dumps(_items(base_url, side_chat_id)), interval_s=0.1
        )

        expect(marker, "the parent row should flag the side chat's unread reply").to_be_visible()
        if parent_running:
            expect(_running_badge(parent_row)).to_be_visible()

        # Opening the parent reads the reply; the marker stays cleared after leaving.
        page.locator(f'a[href="/c/{parent_id}"]').click()
        expect(page).to_have_url(f"{base_url}/c/{parent_id}")
        expect(composer).to_be_visible()
        page.locator(f'a[href="/c/{other_id}"]').click()
        expect(page).to_have_url(f"{base_url}/c/{other_id}")
        expect(marker).to_have_count(0)
    finally:
        for gate in (side_gate, main_gate):
            httpx.post(
                f"{mock_llm_server_url}/gate/release", params={"gate": gate}, timeout=5.0
            ).raise_for_status()
        for child_id in side_chat_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
