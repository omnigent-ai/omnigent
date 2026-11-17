"""A Stop arriving between transcript polls must deliver its final report."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities import ConversationItem, MessageData, PagedList
from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.server.routes._sessions.orchestration import (
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.stores.conversation_store import ConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_text",
    ["DONE: tests passed.", "Final report. " * 500, ""],
    ids=["final", "long-final", "empty-final"],
)
async def test_stop_delivers_final_output_before_transcript_catches_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_text: str
) -> None:
    """Exercise the real transcript, hook and status-enrichment functions offline."""
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    stored: list[ConversationItem] = []
    deliveries: list[str | None] = []
    store = Mock(spec=ConversationStore)
    store.list_items.side_effect = lambda *args, **kwargs: PagedList(data=list(reversed(stored)))

    def append_message(role: str, source_id: str, text: str) -> None:
        with transcript.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": role,
                        "uuid": source_id,
                        "message": {"role": role, "content": [{"type": "text", "text": text}]},
                    }
                )
                + "\n"
            )

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = body["data"]
        if body["type"] == "external_conversation_item" and data["item_type"] == "message":
            stored.append(
                ConversationItem(
                    id=data["source_id"],
                    type="message",
                    status="completed",
                    response_id=data["response_id"],
                    created_at=len(stored) + 1,
                    data=MessageData(**data["item_data"]),
                )
            )
        elif body["type"] == "external_session_status" and data["status"] == "idle":
            enriched = await _enrich_terminal_status_with_subagent_output(
                data, "idle", "conv_child", store
            )
            deliveries.append(enriched.get("output"))
        return httpx.Response(200, json={})

    append_message("user", "user-1", "Implement the fix.")
    append_message("assistant", "progress-1", "Checking documentation before handing back.")
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-child",
            "transcript_path": str(transcript),
        },
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript, line_cursor=0, byte_offset=0
    )
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://ap"
    ) as client:

        async def forward_items() -> None:
            nonlocal state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_child",
                bridge_dir=bridge_dir,
                agent_name="developer",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

        await forward_items()
        # Both records arrive after this poll's transcript snapshot, before its hooks phase.
        append_message("assistant", "final-1", final_text)
        bridge.record_hook_event(
            bridge_dir,
            {
                "hook_event_name": "Stop",
                "session_id": "claude-child",
                "transcript_path": str(transcript),
                "last_assistant_message": final_text,
            },
        )
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir, start_at_end=False, session_id="conv_child"
        )

        async def forward_hooks() -> None:
            nonlocal hook_state
            hook_state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_child",
                bridge_dir=bridge_dir,
                state=hook_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id=state.current_response_id,
            )

        await forward_hooks()
        assert deliveries == [final_text]
        # The later transcript poll must not cause another completion delivery.
        await forward_items()
        await forward_hooks()
        assert deliveries == [final_text]
        if final_text:
            assert any(
                isinstance(item.data, MessageData)
                and any(block.get("text") == final_text for block in item.data.content)
                for item in stored
            )
