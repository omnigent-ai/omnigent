"""UI journey: sub-agent wake notices ride in as system-role messages.

Sub-agent completion wake notices ("[System: sub-agent ... finished — N
results waiting in inbox. Call sys_read_inbox to collect.]") used to be
POSTed and persisted as ``role="user"`` messages. A safety-tuned model sees
a *user* turn claiming to be a system instruction, flags it as prompt
injection, and refuses to drain its inbox — stranding the orchestration.
They are now delivered end to end with ``role="system"``.

Two browser-driven checks:

- The full journey: the user dispatches the ``researcher`` sub-agent from
  the composer, the child completes, and the auto-wake continuation
  surfaces the relayed result in the parent chat with NO further user
  input. The persisted wake notice must carry ``role="system"`` (on a
  pre-fix build it persists as ``role="user"``), and the transcript must
  not show the wake as a raw chat bubble (the Agents rail owns that
  status).
- The transcript render contract for the new role: a committed
  ``role="system"`` ``[System: ...]`` marker message must hydrate into the
  muted system-marker indicator, not vanish (a user-only role gate in the
  items->blocks pipeline silently dropped system-role items) and not
  render as a normal chat bubble.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, seed_committed_items

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

# One relay = dispatch turn + sub-agent turn + auto-wake continuation,
# three serial LLM calls, so the nonce assertion gets a generous budget.
_RELAY_TIMEOUT_MS = 240_000


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wake_notice_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    """Return the persisted sub-agent wake-notice message items.

    :param base_url: Spawned server base URL.
    :param session_id: The parent session whose items to read.
    :returns: Every message item whose text is a sub-agent wake notice,
        each flattened to ``{"role": ..., "text": ...}``.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=15.0)
    resp.raise_for_status()
    notices: list[dict[str, object]] = []
    for item in resp.json()["data"]:
        if item.get("type") != "message":
            continue
        data = item.get("data") or {}
        role = item.get("role") or data.get("role")
        content = item.get("content") or data.get("content") or []
        text = " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
        )
        if "[System: sub-agent" in text and "waiting in inbox" in text:
            notices.append({"role": role, "text": text})
    return notices


@pytest.mark.timeout(600)
def test_subagent_wake_notice_is_system_role_and_still_wakes_the_parent(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The wake notice persists as role=system and still drives the auto-wake.

    Journey (all in the browser): ask the agent to dispatch its
    ``researcher`` sub-agent → the dispatch ack renders → the child
    finishes → the inbox auto-wake continuation relays the child's nonce
    into the parent chat with NO further user input. Then the persisted
    wake notice must carry ``role="system"`` — on a pre-fix build the
    runner POSTs it as ``role="user"``, the exact shape a safety-tuned
    model refuses as prompt injection.
    """
    base_url, session_id = seeded_session
    suffix = uuid.uuid4().hex[:10]
    parent_token = f"wake-role-parent-{suffix}"
    child_token = f"wake-role-child-{suffix}"
    nonce = f"marker-{uuid.uuid4().hex[:10]}"

    # Parent queue (routed by the token in the user's prompt): dispatch the
    # researcher, ack, then — on the auto-wake continuation — relay the
    # nonce. The third response can only fire when the wake notice starts a
    # continuation turn, so the nonce rendering proves the wake delivered.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_researcher",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": "wake-role",
                                "args": f"Fetch the marker. Routing marker: {child_token}",
                            }
                        ),
                    }
                ]
            },
            {"text": "Researcher dispatched; waiting for its result."},
            {"text": f"The researcher reports: {nonce}."},
        ],
        key=parent_token,
        match=parent_token,
    )
    # Child queue (routed by the token in the dispatch args).
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {nonce}"}],
        key=child_token,
        match=child_token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    _send(
        page,
        "Dispatch the researcher sub-agent and report back exactly what it "
        f"says. Routing marker: {parent_token}",
    )

    # The auto-wake continuation surfaces the child's result with no
    # further user input — the wake notice both persisted and started a
    # turn (system-role tails must keep starting turns like user input).
    expect(page.locator(_ASSISTANT, has_text=nonce).first).to_be_visible(timeout=_RELAY_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # The persisted wake notice is a system-role message. On a pre-fix
    # build this is role="user" — the injection-looking shape the model
    # refuses — and the assertion below fails.
    notices = _wake_notice_items(base_url, session_id)
    assert notices, "no sub-agent wake notice was persisted in the parent session"
    for notice in notices:
        assert notice["role"] == "system", (
            f"sub-agent wake notice persisted with role={notice['role']!r}, "
            f"expected 'system': {notice['text']!r}"
        )

    # The wake is model-facing control traffic: the transcript must not
    # show it as a raw chat bubble (the Agents rail owns that status).
    expect(page.locator(_USER_BUBBLE, has_text="[System: sub-agent")).to_have_count(0)
    expect(page.locator(_ASSISTANT, has_text="[System: sub-agent")).to_have_count(0)


def test_committed_system_role_marker_renders_as_muted_indicator(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A committed role=system ``[System: ...]`` marker hydrates as a marker.

    System-role messages are new to the transcript pipeline; a user-only
    role gate in the items→blocks conversion silently dropped them, and a
    naive fix could render them as normal user bubbles. Seed a settled
    transcript whose middle item is a system-role task marker and assert
    the SPA renders the muted indicator — present, classified, and not a
    chat bubble.
    """
    from omnigent.entities import MessageData, NewConversationItem

    base_url, session_id = seeded_session
    marker_task = f"tsk_{uuid.uuid4().hex[:8]}"

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_sysrole_1",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "run the task"}]
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_sysrole_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Task started."}],
                    agent="hello_world",
                ),
            ),
            # The framework notice under test: system role, marker text.
            NewConversationItem(
                type="message",
                response_id="resp_sysrole_2",
                data=MessageData(
                    role="system",
                    content=[
                        {
                            "type": "input_text",
                            "text": f"[System: task {marker_task} (tool) completed]",
                        }
                    ],
                ),
            ),
            # Assistant tail so hydration never sees a pending turn-starter.
            NewConversationItem(
                type="message",
                response_id="resp_sysrole_2",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Task finished cleanly."}],
                    agent="hello_world",
                ),
            ),
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")

    # The surrounding conversation hydrated…
    expect(page.locator(_ASSISTANT, has_text="Task finished cleanly.").first).to_be_visible(
        timeout=30_000
    )
    # …and the system-role marker rendered as the muted indicator: not
    # dropped (the pre-fix pipeline skipped non-user/assistant roles) and
    # not a normal chat bubble.
    marker = page.locator('[data-testid="system-message"]')
    expect(marker.first).to_be_visible()
    expect(marker.first).to_have_attribute("data-system-kind", "task_completed")
    expect(marker.first).to_contain_text(re.compile(rf"Tool {marker_task} completed"))
    expect(page.locator(_USER_BUBBLE, has_text=f"[System: task {marker_task}")).to_have_count(0)
