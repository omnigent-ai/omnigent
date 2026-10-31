"""E2E: ``/rewind`` in a claude-sdk session's composer.

A real Claude CLI (via the Agent SDK) answers from the mock LLM on the shared
runner. ``/rewind`` lists the session's prompts; choosing the second one moves
the view to a new session holding only the first turn, with the chosen prompt
back in the composer, while the original keeps a notice linking to it.
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_sdk_clear import (
    COMPOSER,
    USER,
    run_slash_command,
    send_and_wait,
    session_mentions,
)
from tests.e2e_ui.chat.test_claude_sdk_terminated_cli_recovery import (
    _create_claude_sdk_session,
)
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm


@pytest.mark.timeout(600)
def test_claude_sdk_rewind_follows_the_truncated_session(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Rewind to the second prompt from the composer."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    source_id, _agent_name = _create_claude_sdk_session(
        live_server, runner_id, mock_llm_server_url
    )
    uid = uuid.uuid4().hex[:6]
    first, second = f"first-prompt-{uid}", f"second-prompt-{uid}"
    # The CLI's side requests share the match, so queue enough replies.
    for token, reply in ((first, "ack one"), (second, "ack two")):
        configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 20, key=token, match=token)

    page.goto(f"{live_server}/c/{source_id}")
    send_and_wait(page, f"Say ack. {first}", "ack one")
    send_and_wait(page, f"Say ack. {second}", "ack two")

    run_slash_command(page, "/rew", "rewind")
    rows = page.get_by_test_id("rewind-prompt")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text(second)
    rows.first.click()

    expect(page).not_to_have_url(re.compile(f"/c/{source_id}$"), timeout=30_000)
    rewound_id = page.url.rsplit("/c/", 1)[1]
    expect(page.get_by_placeholder(COMPOSER)).to_have_value(f"Say ack. {second}")
    expect(page.locator(USER).filter(has_text=first)).to_be_visible()
    expect(page.locator(USER).filter(has_text=second)).to_have_count(0)
    assert httpx.get(f"{live_server}/v1/sessions/{rewound_id}").json()["runner_id"] == runner_id
    assert session_mentions(live_server, source_id, f"/c/{rewound_id}")
