from __future__ import annotations

import asyncio
import weakref

from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_erasure_store import SqlAlchemyMemoryErasureStore

_stores: weakref.WeakKeyDictionary[
    ConversationStore,
    SqlAlchemyMemoryErasureStore,
] = weakref.WeakKeyDictionary()


def configure(
    conversation_store: ConversationStore,
    erasure_store: SqlAlchemyMemoryErasureStore | None,
) -> None:
    if erasure_store is None:
        _stores.pop(conversation_store, None)
    else:
        _stores[conversation_store] = erasure_store


async def subject_is_disabled(
    conversation_store: ConversationStore,
    *,
    workspace_id: int,
    account_subject: str,
) -> bool:
    store = _stores.get(conversation_store)
    if store is None:
        return False
    return await asyncio.to_thread(
        store.subject_is_disabled,
        workspace_id,
        account_subject,
    )
