from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions import orchestration
from tests.server.integration.test_sessions_external_item_idempotency import (
    _create_session,
    _post_item,
)


@pytest.mark.asyncio
async def test_identical_antigravity_prompts_consume_pending_ids_once_in_order(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _create_session(
        client, "agy-identical-prompts", labels={"omnigent.wrapper": "antigravity-native-ui"}
    )
    consumed: list[str | None] = []
    publish = orchestration._publish_external_conversation_item

    def capture(*args: Any, **kwargs: Any) -> None:
        consumed.append(kwargs.get("cleared_pending_id"))
        publish(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_publish_external_conversation_item", capture)
    content = [{"type": "input_text", "text": "Continue"}]
    first_id = pending_inputs.record(
        session_id, content, created_by="author@example.com", stable_id="a" * 32
    )
    unmatched_id = pending_inputs.record(
        session_id, [{"type": "input_text", "text": "different prompt"}], stable_id="c" * 32
    )
    second_id = pending_inputs.record(
        session_id, content, created_by="author@example.com", stable_id="b" * 32
    )
    first = await _post_item(client, session_id, text="Continue", source_id="agy:user:1")
    remaining = pending_inputs.snapshot_for(session_id)
    assert [entry["pending_id"] for entry in remaining] == [unmatched_id, second_id]
    assert consumed == [first_id]

    duplicate = await _post_item(client, session_id, text="Continue", source_id="agy:user:1")
    assert duplicate["item_id"] == first["item_id"]
    assert pending_inputs.snapshot_for(session_id) == remaining
    assert consumed == [first_id]

    second = await _post_item(client, session_id, text="Continue", source_id="agy:user:2")
    duplicate_second = await _post_item(
        client, session_id, text="Continue", source_id="agy:user:2"
    )
    assert second["item_id"] != first["item_id"]
    assert duplicate_second["item_id"] == second["item_id"]
    assert consumed == [first_id, second_id]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        unmatched_id
    ]
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert [item["created_by"] for item in items] == ["author@example.com"] * 2
    assert [item["content"] for item in items] == [content, content]
