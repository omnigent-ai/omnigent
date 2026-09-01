from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Callable

from omnigent.entities import MessageData
from omnigent.memory.capture_models import MemoryCaptureTarget
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_capture_store import SqlAlchemyMemoryCaptureStore

_INTENT_TTL_SECONDS = 86_400
_stores: weakref.WeakKeyDictionary[
    ConversationStore,
    SqlAlchemyMemoryCaptureStore,
] = weakref.WeakKeyDictionary()
_wakers: weakref.WeakKeyDictionary[ConversationStore, Callable[[], None]] = (
    weakref.WeakKeyDictionary()
)


def configure(
    conversation_store: ConversationStore,
    capture_store: SqlAlchemyMemoryCaptureStore | None,
    wake: Callable[[], None] | None = None,
) -> None:
    if capture_store is None:
        _stores.pop(conversation_store, None)
        _wakers.pop(conversation_store, None)
    else:
        _stores[conversation_store] = capture_store
        if wake is None:
            _wakers.pop(conversation_store, None)
        else:
            _wakers[conversation_store] = wake


async def register_intent(
    conversation_store: ConversationStore,
    *,
    workspace_id: int,
    source_item_id: str,
    conversation_id: str,
    account_subject: str,
    targets: tuple[MemoryCaptureTarget, ...],
) -> dict[str, object] | None:
    store = _stores.get(conversation_store)
    if store is None or not targets:
        return None
    now = int(time.time())
    intent = await asyncio.to_thread(
        store.register_intent,
        workspace_id=workspace_id,
        source_item_id=source_item_id,
        conversation_id=conversation_id,
        account_subject=account_subject,
        targets=targets,
        now=now,
        expires_at=now + _INTENT_TTL_SECONDS,
    )
    return {
        "conversation_id": intent.conversation_id,
        "intent_id": intent.id,
        "source_item_id": intent.source_item_id,
        "workspace_id": intent.workspace_id,
    }


async def settle_intent(
    conversation_store: ConversationStore,
    correlation: object,
    *,
    response_id: str,
    completed: bool,
) -> bool:
    store = _stores.get(conversation_store)
    if store is None or not isinstance(correlation, dict):
        return False
    workspace_id = correlation.get("workspace_id")
    intent_id = correlation.get("intent_id")
    source_item_id = correlation.get("source_item_id")
    conversation_id = correlation.get("conversation_id")
    if (
        not isinstance(workspace_id, int)
        or isinstance(workspace_id, bool)
        or workspace_id < 0
        or not isinstance(intent_id, str)
        or not intent_id
        or not isinstance(source_item_id, str)
        or not source_item_id
        or not isinstance(conversation_id, str)
        or not conversation_id
    ):
        return False
    if completed:
        source_item, response_items = await asyncio.gather(
            asyncio.to_thread(
                conversation_store.get_item,
                conversation_id,
                source_item_id,
            ),
            asyncio.to_thread(
                conversation_store.list_items_by_response_id,
                conversation_id,
                response_id,
            ),
        )
        source_is_user = (
            source_item is not None
            and source_item.type == "message"
            and isinstance(source_item.data, MessageData)
            and source_item.data.role == "user"
        )
        response_has_assistant_text = any(
            item.type == "message"
            and isinstance(item.data, MessageData)
            and item.data.role == "assistant"
            and any(
                isinstance(block, dict)
                and block.get("type") == "output_text"
                and isinstance(block.get("text"), str)
                and bool(block["text"].strip())
                for block in item.data.content
            )
            for item in response_items
        )
        if not source_is_user or not response_has_assistant_text:
            completed = False
    if completed:
        jobs = await asyncio.to_thread(
            store.complete_intent,
            workspace_id=workspace_id,
            intent_id=intent_id,
            source_item_id=source_item_id,
            response_id=response_id,
            now=int(time.time()),
        )
        if jobs and (wake := _wakers.get(conversation_store)) is not None:
            wake()
        return bool(jobs)
    return await asyncio.to_thread(
        store.cancel_intent,
        workspace_id=workspace_id,
        intent_id=intent_id,
        source_item_id=source_item_id,
    )


def configured_store(
    conversation_store: ConversationStore,
) -> SqlAlchemyMemoryCaptureStore | None:
    return _stores.get(conversation_store)
