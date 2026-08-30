from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import cast

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import message_idempotency
from omnigent.stores.conversation_store import ConversationStore, MessageEventReceipt

pytestmark = pytest.mark.asyncio


class _MemoryReceiptStore:
    def __init__(
        self,
        receipts: dict[tuple[str, str], MessageEventReceipt] | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self.receipts = receipts if receipts is not None else {}
        self.lock = lock if lock is not None else threading.Lock()

    def claim_message_event(
        self,
        conversation_id: str,
        client_event_id: str,
        fingerprint: str,
        *,
        owner_id: str,
        lease_expires_at: int,
    ) -> tuple[bool, MessageEventReceipt]:
        key = (conversation_id, client_event_id)
        with self.lock:
            existing = self.receipts.get(key)
            if existing is not None:
                return False, existing
            receipt = MessageEventReceipt(
                fingerprint,
                "pending",
                None,
                owner_id,
                lease_expires_at,
            )
            self.receipts[key] = receipt
            return True, receipt

    def get_message_event(
        self, conversation_id: str, client_event_id: str
    ) -> MessageEventReceipt | None:
        with self.lock:
            return self.receipts.get((conversation_id, client_event_id))

    def complete_message_event(
        self,
        conversation_id: str,
        client_event_id: str,
        fingerprint: str,
        *,
        status: str,
        outcome: dict[str, bool | str] | None,
    ) -> None:
        with self.lock:
            existing = self.receipts[(conversation_id, client_event_id)]
            self.receipts[(conversation_id, client_event_id)] = MessageEventReceipt(
                fingerprint,
                status,
                outcome,
                existing.owner_id,
                existing.lease_expires_at,
            )

    def abandon_message_event(
        self, conversation_id: str, client_event_id: str, fingerprint: str
    ) -> None:
        with self.lock:
            key = (conversation_id, client_event_id)
            existing = self.receipts.get(key)
            if (
                existing is not None
                and existing.fingerprint == fingerprint
                and existing.status == "pending"
            ):
                self.receipts.pop(key)


def receipt_store() -> ConversationStore:
    return cast(ConversationStore, _MemoryReceiptStore())


@pytest.fixture(autouse=True)
def reset_receipts() -> Iterator[None]:
    message_idempotency.reset_for_tests()
    yield
    message_idempotency.reset_for_tests()


async def test_concurrent_replays_share_one_operation() -> None:
    store = receipt_store()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"queued": True, "item_id": "accepted"}

    first = asyncio.create_task(
        message_idempotency.run_once(store, "session", "event", "fingerprint", operation)
    )
    await started.wait()
    replay = asyncio.create_task(
        message_idempotency.run_once(store, "session", "event", "fingerprint", operation)
    )
    release.set()

    expected = {"queued": True, "item_id": "accepted"}
    assert await asyncio.gather(first, replay) == [
        expected,
        {**expected, "idempotency_replayed": True},
    ]
    assert calls == 1


async def test_cross_replica_replay_joins_durable_owner() -> None:
    receipts: dict[tuple[str, str], MessageEventReceipt] = {}
    lock = threading.Lock()
    first_store = cast(ConversationStore, _MemoryReceiptStore(receipts, lock))
    second_store = cast(ConversationStore, _MemoryReceiptStore(receipts, lock))
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"queued": True, "item_id": "accepted"}

    first = asyncio.create_task(
        message_idempotency.run_once(
            first_store, "session", "event", "fingerprint", operation
        )
    )
    await started.wait()
    replay = asyncio.create_task(
        message_idempotency.run_once(
            second_store, "session", "event", "fingerprint", operation
        )
    )
    await asyncio.sleep(0)
    release.set()
    expected = {"queued": True, "item_id": "accepted"}
    assert await asyncio.gather(first, replay) == [
        expected,
        {**expected, "idempotency_replayed": True},
    ]
    assert calls == 1


async def test_distinct_event_ids_allow_identical_payloads() -> None:
    store = receipt_store()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        return {"queued": True}

    await message_idempotency.run_once(store, "session", "event-one", "same", operation)
    await message_idempotency.run_once(store, "session", "event-two", "same", operation)
    assert calls == 2


async def test_reusing_event_id_for_different_payload_is_rejected() -> None:
    store = receipt_store()

    async def operation() -> dict[str, bool | str]:
        return {"queued": True}

    await message_idempotency.run_once(store, "session", "event", "first", operation)
    with pytest.raises(OmnigentError) as exc_info:
        await message_idempotency.run_once(store, "session", "event", "second", operation)
    assert exc_info.value.code == ErrorCode.MESSAGE_EVENT_IDENTITY_CONFLICT


async def test_ambiguous_operation_becomes_uncertain_and_is_not_repeated() -> None:
    store = receipt_store()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("ambiguous transport failure")

    with pytest.raises(RuntimeError, match="ambiguous transport failure"):
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    with pytest.raises(OmnigentError) as replay_error:
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    assert calls == 1
    assert replay_error.value.code == ErrorCode.MESSAGE_EVENT_UNCERTAIN


async def test_terminal_client_error_is_durably_failed() -> None:
    store = receipt_store()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        raise OmnigentError("invalid message", code=ErrorCode.INVALID_INPUT)

    with pytest.raises(OmnigentError, match="invalid message"):
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    message_idempotency.reset_for_tests()
    with pytest.raises(OmnigentError, match="original message submission failed") as replay:
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    assert replay.value.code == ErrorCode.MESSAGE_EVENT_FAILED
    assert calls == 1


async def test_definite_pre_dispatch_failure_releases_claim_for_bounded_retry() -> None:
    store = receipt_store()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OmnigentError(
                "No runner bound for session", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        return {"queued": True}

    with pytest.raises(OmnigentError):
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    assert await message_idempotency.run_once(
        store, "session", "event", "same", operation
    ) == {"queued": True}
    assert calls == 2


async def test_wrong_replica_releases_claim_for_keyless_retry() -> None:
    store = receipt_store()
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OmnigentError("wrong replica", code=ErrorCode.WRONG_REPLICA)
        return {"queued": True}

    with pytest.raises(OmnigentError) as first:
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    assert first.value.code == ErrorCode.WRONG_REPLICA
    message_idempotency.reset_for_tests()
    assert await message_idempotency.run_once(
        store, "session", "event", "same", operation
    ) == {"queued": True}
    assert calls == 2


async def test_completed_outcome_survives_process_local_reset() -> None:
    store = receipt_store()

    async def first_operation() -> dict[str, bool | str]:
        return {"queued": True, "item_id": "durable"}

    expected = await message_idempotency.run_once(
        store, "session", "event", "same", first_operation
    )
    message_idempotency.reset_for_tests()

    async def must_not_repeat() -> dict[str, bool | str]:
        raise AssertionError("durable replay dispatched again")

    replay = await message_idempotency.run_once(
        store, "session", "event", "same", must_not_repeat
    )
    assert replay == {**expected, "idempotency_replayed": True}


async def test_orphaned_pending_claim_fails_visibly_without_dispatch() -> None:
    memory_store = _MemoryReceiptStore()
    memory_store.receipts[("session", "event")] = MessageEventReceipt(
        "same",
        "pending",
        None,
        "dead-owner",
        0,
    )
    store = cast(ConversationStore, memory_store)

    async def must_not_repeat() -> dict[str, bool | str]:
        raise AssertionError("uncertain delivery dispatched again")

    with pytest.raises(OmnigentError, match="may have been delivered") as exc_info:
        await message_idempotency.run_once(store, "session", "event", "same", must_not_repeat)
    assert exc_info.value.code == ErrorCode.MESSAGE_EVENT_UNCERTAIN


async def test_cancelled_owner_leaves_active_receipt_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = receipt_store()
    monkeypatch.setattr(message_idempotency, "_CROSS_REPLICA_JOIN_SECONDS", 0)

    async def cancelled_operation() -> dict[str, bool | str]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await message_idempotency.run_once(
            store,
            "session",
            "event",
            "same",
            cancelled_operation,
        )
    message_idempotency.reset_for_tests()

    async def must_not_repeat() -> dict[str, bool | str]:
        raise AssertionError("cancelled delivery was dispatched again")

    with pytest.raises(OmnigentError, match="still active") as exc_info:
        await message_idempotency.run_once(
            store,
            "session",
            "event",
            "same",
            must_not_repeat,
        )
    assert exc_info.value.code == ErrorCode.MESSAGE_EVENT_PENDING


async def test_receipt_completion_failure_never_reports_success() -> None:
    memory_store = _MemoryReceiptStore()

    def fail_complete(
        conversation_id: str,
        client_event_id: str,
        fingerprint: str,
        *,
        status: str,
        outcome: dict[str, bool | str] | None,
    ) -> None:
        raise OSError("receipt database unavailable")

    memory_store.complete_message_event = fail_complete  # type: ignore[method-assign]
    store = cast(ConversationStore, memory_store)
    calls = 0

    async def operation() -> dict[str, bool | str]:
        nonlocal calls
        calls += 1
        return {"queued": True}

    with pytest.raises(OSError, match="receipt database unavailable"):
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    message_idempotency.reset_for_tests()
    with pytest.raises(OmnigentError, match="still active") as replay:
        await message_idempotency.run_once(store, "session", "event", "same", operation)
    assert replay.value.code == ErrorCode.MESSAGE_EVENT_PENDING
    assert calls == 1


async def test_event_fingerprint_is_canonical_and_excludes_transport_identity() -> None:
    first = message_idempotency.event_fingerprint(
        {
            "type": "message",
            "data": {"role": "user", "content": [{"text": "héllo", "type": "input_text"}]},
        },
        "alice@example.com",
    )
    reordered = message_idempotency.event_fingerprint(
        {
            "data": {"content": [{"type": "input_text", "text": "héllo"}], "role": "user"},
            "type": "message",
        },
        "alice@example.com",
    )
    other_actor = message_idempotency.event_fingerprint(
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "héllo"}]},
        },
        "bob@example.com",
    )
    assert reordered == first
    assert other_actor != first
