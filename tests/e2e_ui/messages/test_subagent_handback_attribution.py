"""UI journey: a background subagent's hand-back must not render as a user message.

When a background subagent reports back to the parent conversation, Claude
Code queues the report as an ``isMeta`` prompt-mode ``queued_command``
attachment wrapped in ``<agent-message>``. That report is agent output, not
something the person typed, so the Omnigent transcript must not attribute
it to the user.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _send,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# Hand-back prefix some Claude Code versions add inside the <agent-message>
# wrapper; asserted alongside the sentinel to cover that flavor too.
_HANDBACK_MARKER = "[Subagent hand-back]"

# Launch -> background worker turn -> delayed parent turn -> hand-back turn.
_BACKGROUND_FLOW_TIMEOUT_MS = 240_000


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_subagent_handback_not_rendered_as_user_message(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A background Task's report never shows up as a user-sent message."""
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    # Longer than the worker token so a request carrying both (the parent
    # resends its full history) routes to the parent queue.
    parent_token = f"omni-parent-trigger-{nonce}"
    worker_token = f"wrk-{nonce}"
    handback_sentinel = f"omni-handback-{nonce}"
    parent_done = f"omni-parent-done-{nonce}"

    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "OK.")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "OK.")

    # Two warm-up turns: boot-time probes and the session-title generation
    # (which reuses the main model and would otherwise drain the scripted
    # queues) both fire during these throwaway exchanges.
    for warmup in ("Say OK.", "Say OK once more."):
        _send(page, warmup)
        expect(page.locator(_ASSISTANT, has_text="OK.").first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    _log.info("warm-up turns settled")

    # Any later title-regeneration request quotes the user's message, so it
    # would content-match the parent queue; this longer token outranks it.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Background research session"}] * 5,
        key="title-decoy",
        match="Write the title in the predominant language",
    )

    task_arguments = json.dumps(
        {
            "description": "Background research",
            "prompt": f"{worker_token} gather the findings and report back to main",
            "subagent_type": "general-purpose",
            "run_in_background": True,
            "name": "researcher",
        }
    )
    send_message_arguments = json.dumps(
        {
            "to": "main",
            "summary": "background research report",
            "message": f"{handback_sentinel} background research finished.",
        }
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "name": "Task",
                        "arguments": task_arguments,
                        "call_id": f"toolu_task_{nonce}",
                    }
                ]
            },
            # Server-side delay keeps the parent turn active while the worker
            # hands back, so the report is queued mid-turn.
            {"text": "Still working on the main task.", "delay": 25},
            {"text": f"Received the background report. {parent_done}"},
        ],
        key="parent",
        match=parent_token,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "name": "SendMessage",
                        "arguments": send_message_arguments,
                        "call_id": f"toolu_send_{nonce}",
                    }
                ]
            },
            {"text": "Report delivered."},
            {"text": "Report delivered."},
        ],
        key="worker",
        match=worker_token,
    )

    _send(page, f"{parent_token} please run the research task in a background subagent.")
    _log.info("trigger turn sent (worker=%s sentinel=%s)", worker_token, handback_sentinel)

    # The parent receives the queued hand-back and answers once more; waiting
    # on that reply settles the whole background flow on fixed builds too.
    expect(page.locator(_ASSISTANT, has_text=parent_done).first).to_be_visible(
        timeout=_BACKGROUND_FLOW_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    _log.info("background flow settled (parent_done rendered)")

    expect(page.locator(_USER, has_text=handback_sentinel)).to_have_count(0)
    expect(page.locator(_USER, has_text=_HANDBACK_MARKER)).to_have_count(0)

    user_texts = [
        _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
        if item.get("role") == "user"
    ]
    offending = [
        text for text in user_texts if handback_sentinel in text or _HANDBACK_MARKER in text
    ]
    assert not offending, f"subagent hand-back stored as user message(s): {offending}"
