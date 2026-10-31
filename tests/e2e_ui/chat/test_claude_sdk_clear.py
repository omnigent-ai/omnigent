"""E2E: ``/clear`` in a claude-sdk session's composer.

A real Claude CLI (via the Agent SDK) answers from the mock LLM on the shared
runner. ``/clear`` moves the view to a new, empty session on the same runner,
while the original keeps its turn and a notice linking to the new session.
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_sdk_terminated_cli_recovery import (
    _create_claude_sdk_session,
)
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
USER = '[data-testid="message-bubble"][data-role="user"]'
WORKING = '[data-testid="working-indicator"]'
COMPOSER = "Send a message…"


def send_and_wait(page: Page, text: str, reply: str) -> None:
    """Send *text* from the composer and wait for *reply* and an idle turn."""
    page.get_by_placeholder(COMPOSER).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=180_000)
    expect(page.locator(WORKING)).to_have_count(0, timeout=180_000)


def run_slash_command(page: Page, typed: str, command: str) -> None:
    """Type *typed* and pick *command* from the slash menu."""
    page.get_by_placeholder(COMPOSER).fill(typed)
    page.get_by_test_id(f"slash-menu-item-{command}").click()


def session_mentions(base_url: str, session_id: str, text: str) -> bool:
    """Whether one of *session_id*'s newest messages contains *text*."""
    items = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items", params={"order": "desc", "limit": 5}
    ).json()["data"]
    return any(text in part.get("text", "") for item in items for part in item.get("content", []))


@pytest.mark.timeout(600)
def test_claude_sdk_clear_follows_a_new_empty_session(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``/clear`` from the composer opens an empty session on the same runner."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    source_id, _agent_name = _create_claude_sdk_session(
        live_server, runner_id, mock_llm_server_url
    )
    token = f"clear-prompt-{uuid.uuid4().hex[:6]}"
    # The CLI's side requests share the match, so queue enough replies.
    configure_mock_llm(mock_llm_server_url, [{"text": "ack one"}] * 20, key=token, match=token)

    page.goto(f"{live_server}/c/{source_id}")
    send_and_wait(page, f"Say ack. {token}", "ack one")

    run_slash_command(page, "/cle", "clear")
    expect(page).not_to_have_url(re.compile(f"/c/{source_id}$"), timeout=30_000)
    cleared_id = page.url.rsplit("/c/", 1)[1]
    expect(page.get_by_placeholder(COMPOSER)).to_be_visible()
    expect(page.locator(USER)).to_have_count(0)
    assert httpx.get(f"{live_server}/v1/sessions/{cleared_id}").json()["runner_id"] == runner_id
    assert session_mentions(live_server, source_id, f"/c/{cleared_id}")
