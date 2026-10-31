"""Claude peer wakes keep their source label through persisted chat history."""

import json
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import read_transcript_items_from_offset
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from tests.e2e_ui.conftest import seed_committed_turn

from .test_message_render_parity import _ASSISTANT, _USER


def test_native_claude_peer_wake_label_survives_reload(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    transcript = tmp_path / "session.jsonl"
    prompt = "Summarize the current review."
    initial_reply = "The current review is complete."
    peer_payload = '<teammate-message teammate_id="reviewer">Check retries.</teammate-message>'
    peer_reply = "The other session asked me to check the retry path."
    response_id = "resp_review"
    seed_committed_turn(session_id, prompt=prompt, reply=initial_reply, response_id=response_id)
    entries = [
        {"type": role, "uuid": f"peer-{role}", "message": {"role": role, "content": text}}
        for role, text in (("user", peer_payload), ("assistant", peer_reply))
    ]
    entries[0]["origin"] = {"kind": "peer"}
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    peer = read_transcript_items_from_offset(
        transcript,
        0,
        start_line=0,
        agent_name="hello_world",
        current_response_id=response_id,
        settled_response_id=response_id,
    )
    with httpx.Client(base_url=base_url, timeout=15) as client:
        for item in peer.items:
            client.post(
                f"/v1/sessions/{session_id}/events",
                json=_external_conversation_item_event(item),
            ).raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    for reload_history in (False, True):
        if reload_history:
            page.reload()
        label = page.get_by_test_id("system-message")
        expect(label).to_have_text("System: Message from another session", timeout=30_000)
        expect(label).to_be_visible()
        expect(page.locator(_USER)).to_have_count(1)
        expect(page.locator(_USER)).to_contain_text(prompt)
        expect(page.locator(_ASSISTANT)).to_have_count(2)
        expect(page.locator(_ASSISTANT)).to_contain_text([initial_reply, peer_reply])
        expect(page.get_by_text(peer_payload, exact=False)).to_have_count(0)
        expect(page.get_by_text("scheduled prompt fired", exact=False)).to_have_count(0)
