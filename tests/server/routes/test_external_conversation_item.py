"""Tests for externally-forwarded conversation-item parsing."""

import asyncio
import threading
from typing import Any

import pytest

from omnigent.entities import ConversationItem, MessageData, NewConversationItem
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.routes.sessions import _parse_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_native_message_stream_id_survives_snapshot_serialization() -> None:
    """The completed item durably identifies the preview stream it replaces."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                "response_id": "turn_1",
                "message_id": "codex:thread_1:turn_1:agentMessage:item_1",
            },
        )
    )

    assert isinstance(parsed.data, MessageData)
    assert parsed.data.stream_message_id == "codex:thread_1:turn_1:agentMessage:item_1"
    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["stream_message_id"] == (
        "codex:thread_1:turn_1:agentMessage:item_1"
    )


class _RecordingConversationStore(SqlAlchemyConversationStore):
    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self.append_threads: list[int] = []

    def append(
        self, conversation_id: str, items: list[NewConversationItem]
    ) -> list[ConversationItem]:
        self.append_threads.append(threading.get_ident())
        return super().append(conversation_id, items)


class _LeasedConversationStore(_RecordingConversationStore):
    hook_calls = 0

    async def run_in_thread_with_background_lease(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        # Stand in for a deployment's resource-lifetime hook; persistence is real.
        assert method_name == "append"
        self.hook_calls += 1
        return await asyncio.to_thread(getattr(self, method_name), *args, **kwargs)


def _external_message() -> SessionEventInput:
    return SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "native-transcript-record",
            "item_type": "message",
            "item_data": {
                "role": "assistant",
                "agent": "worker",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    )


@pytest.mark.parametrize("leased", [False, True])
async def test_external_append_uses_optional_store_hook_and_preserves_retries(
    db_uri: str, leased: bool
) -> None:
    store = (_LeasedConversationStore if leased else _RecordingConversationStore)(db_uri)
    conv = store.create_conversation(title="Existing title")
    body = _external_message()

    first = await _persist_external_conversation_item(conv.id, conv, body, store, enabled=False)
    retry = await _persist_external_conversation_item(conv.id, conv, body, store, enabled=False)

    assert first == retry
    assert [item.id for item in store.list_items(conv.id).data] == [first]
    assert len(store.append_threads) == 2
    assert all(worker != threading.get_ident() for worker in store.append_threads)
    if isinstance(store, _LeasedConversationStore):
        assert store.hook_calls == 2


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_external_append_does_not_fall_back_when_store_hook_fails(
    db_uri: str, failure: type[BaseException]
) -> None:
    class UnavailableLeaseStore(_RecordingConversationStore):
        async def run_in_thread_with_background_lease(
            self, method_name: str, *args: Any, **kwargs: Any
        ) -> Any:
            raise failure("lease unavailable")

    store = UnavailableLeaseStore(db_uri)
    conv = store.create_conversation(title="Existing title")

    with pytest.raises(failure, match="lease unavailable"):
        await _persist_external_conversation_item(
            conv.id, conv, _external_message(), store, enabled=False
        )

    assert not store.append_threads
    assert store.list_items(conv.id).data == []
