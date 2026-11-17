"""E2E: ``/rewind`` in a claude-sdk session's composer.

A real Claude CLI (via the Agent SDK) answers from the mock LLM on the shared
runner. ``/rewind`` lists the session's prompts; choosing the second of three
moves the view to a new session holding only the first turn, with the chosen
prompt back in the composer, while the original keeps a notice linking to it.
The edited prompt is then answered with only the first turn as context.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui._claude_sdk_session import create_claude_sdk_session
from tests.e2e_ui.chat._claude_sdk_continuation import (
    COMPOSER,
    USER,
    ClaudeSdkRun,
    send_and_wait,
    session_mentions,
)
from tests.e2e_ui.chat._claude_sdk_continuation import (
    claude_sdk_run as claude_sdk_run,
)
from tests.e2e_ui.conftest import configure_mock_llm


@pytest.mark.timeout(600)
def test_claude_sdk_rewind_follows_the_truncated_session(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    claude_sdk_run: ClaudeSdkRun,
) -> None:
    """Rewind to the second prompt from the composer, then send it edited."""
    runner_id = claude_sdk_run.runner_id
    source_id, _agent_name = create_claude_sdk_session(
        live_server, runner_id, mock_llm_server_url, name_prefix="sdk-rewind"
    )
    claude_sdk_run.created.append(source_id)
    uid = uuid.uuid4().hex[:6]
    # Tokens of one length: the mock answers a prompt by its latest, and the
    # longer edited token wins over the kept first one.
    first, second, third = (f"turn{turn}-prompt-{uid}" for turn in (1, 2, 3))
    edited = f"edited-rewound-prompt-{uid}"
    # The CLI's side requests share the match, so queue enough replies.
    for token, reply in (
        (first, "ack one"),
        (second, "ack two"),
        (third, "ack three"),
        (edited, "ack edited"),
    ):
        configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 20, key=token, match=token)

    page.goto(f"{live_server}/c/{source_id}")
    send_and_wait(page, f"Say ack. {first}", "ack one")
    send_and_wait(page, f"Say ack. {second}", "ack two")
    send_and_wait(page, f"Say ack. {third}", "ack three")

    page.get_by_placeholder(COMPOSER).fill("/rew")
    page.get_by_test_id("slash-menu-item-rewind").click()
    rows = page.get_by_test_id("rewind-prompt")
    expect(rows).to_have_count(3)
    expect(rows.nth(1)).to_contain_text(second)
    rows.nth(1).click()

    expect(page).not_to_have_url(re.compile(f"/c/{source_id}$"), timeout=30_000)
    rewound_id = page.url.rsplit("/c/", 1)[1]
    claude_sdk_run.created.append(rewound_id)
    expect(page.get_by_placeholder(COMPOSER)).to_have_value(f"Say ack. {second}")
    expect(page.locator(USER).filter(has_text=first)).to_be_visible()
    expect(page.locator(USER).filter(has_text=second)).to_have_count(0)
    assert httpx.get(f"{live_server}/v1/sessions/{rewound_id}").json()["runner_id"] == runner_id
    assert session_mentions(live_server, source_id, f"/c/{rewound_id}")

    # The edited prompt is answered with the first turn as context, and
    # neither the discarded turn nor the one after it.
    send_and_wait(page, f"Say ack. {edited}", "ack edited")
    edited_requests = [
        body for body in map(json.dumps, get_mock_requests(mock_llm_server_url)) if edited in body
    ]
    assert edited_requests
    assert any(first in body and "ack one" in body for body in edited_requests)
    for dropped in (second, "ack two", third, "ack three"):
        assert not any(dropped in body for body in edited_requests), dropped
