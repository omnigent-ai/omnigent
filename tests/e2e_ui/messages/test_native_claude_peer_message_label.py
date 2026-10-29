"""Claude peer wakes keep their source label through persisted chat history."""

import json
from pathlib import Path

from playwright.sync_api import Page, expect

from omnigent.entities import MessageData, NewConversationItem
from omnigent.harnesses.claude_native.bridge import read_transcript_items_from_offset
from tests.e2e_ui.conftest import seed_committed_items

from .test_message_render_parity import _ASSISTANT, _USER


def test_native_claude_peer_wake_label_survives_reload(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A peer input after a settled turn renders separately from scheduled wakes."""
    base_url, session_id = seeded_session
    transcript = tmp_path / "session.jsonl"
    prompt = "Summarize the current review."
    initial_reply = "The current review is complete."
    peer_payload = (
        '<teammate-message teammate_id="reviewer">Check the retry path.</teammate-message>'
    )
    peer_reply = "The other session asked me to check the retry path."
    transcript.write_text(
        "".join(
            json.dumps(entry) + "\n"
            for entry in (
                {
                    "type": "user",
                    "uuid": "human-prompt",
                    "message": {"role": "user", "content": prompt},
                },
                {
                    "type": "assistant",
                    "uuid": "human-reply",
                    "message": {"role": "assistant", "content": initial_reply},
                },
            )
        ),
        encoding="utf-8",
    )
    initial = read_transcript_items_from_offset(
        transcript, 0, start_line=0, agent_name="hello_world"
    )
    with transcript.open("a", encoding="utf-8") as stream:
        for entry in (
            {
                "type": "user",
                "uuid": "peer-prompt",
                "origin": {"kind": "peer"},
                "message": {"role": "user", "content": peer_payload},
            },
            {
                "type": "assistant",
                "uuid": "peer-reply",
                "message": {"role": "assistant", "content": peer_reply},
            },
        ):
            stream.write(json.dumps(entry) + "\n")
    peer = read_transcript_items_from_offset(
        transcript,
        initial.byte_offset,
        start_line=initial.line_cursor,
        agent_name="hello_world",
        current_response_id=initial.current_response_id,
        settled_response_id=initial.current_response_id,
    )
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=item.response_id,
                data=MessageData.model_validate(item.data),
            )
            for item in [*initial.items, *peer.items]
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")
    label = page.get_by_test_id("system-message")
    expect(label).to_have_text("System: Message from another session", timeout=30_000)
    expect(label).to_be_visible()
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER)).to_contain_text(prompt)
    expect(page.locator(_ASSISTANT)).to_have_count(2)
    expect(page.locator(_ASSISTANT).nth(0)).to_contain_text(initial_reply)
    expect(page.locator(_ASSISTANT).nth(1)).to_contain_text(peer_reply)
    expect(page.get_by_text(peer_payload, exact=False)).to_have_count(0)
    expect(page.get_by_text("scheduled prompt fired", exact=False)).to_have_count(0)

    page.reload()
    expect(label).to_have_text("System: Message from another session", timeout=30_000)
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_ASSISTANT)).to_have_count(2)
    expect(page.locator(_ASSISTANT).nth(1)).to_contain_text(peer_reply)
    expect(page.get_by_text(peer_payload, exact=False)).to_have_count(0)
    expect(page.get_by_text("scheduled prompt fired", exact=False)).to_have_count(0)
