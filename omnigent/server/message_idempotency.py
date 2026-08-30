"""Durable idempotency for client-submitted session messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import ConversationStore, MessageEventReceipt

_MAX_IN_FLIGHT = 10_000
_OWNER_LEASE_SECONDS = 300
_CROSS_REPLICA_JOIN_SECONDS = 2.0
_CROSS_REPLICA_POLL_SECONDS = 0.05


@dataclass
class _InFlight:
    fingerprint: str
    task: asyncio.Task[dict[str, bool | str]]


_in_flight: dict[tuple[int, str, str], _InFlight] = {}
_lock = threading.Lock()


def event_fingerprint(payload: Mapping[str, Any], created_by: str | None) -> str:
    """Return a canonical digest used to detect id reuse with different input."""
    material = json.dumps(
        {"created_by": created_by, "event": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(material).hexdigest()


async def run_once(
    conversation_store: ConversationStore,
    session_id: str,
    client_event_id: str,
    fingerprint: str,
    operation: Callable[[], Coroutine[Any, Any, dict[str, bool | str]]],
) -> dict[str, bool | str]:
    """Claim one logical message durably and replay its terminal outcome."""
    key = (id(conversation_store), session_id, client_event_id)
    joined_existing = False
    with _lock:
        entry = _in_flight.get(key)
        if entry is None:
            if len(_in_flight) >= _MAX_IN_FLIGHT:
                raise OmnigentError(
                    "Too many message submissions are currently in progress",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            task = asyncio.create_task(
                _run_durable(
                    conversation_store,
                    session_id,
                    client_event_id,
                    fingerprint,
                    operation,
                )
            )
            entry = _InFlight(fingerprint=fingerprint, task=task)
            _in_flight[key] = entry
            task.add_done_callback(lambda completed: _remove_in_flight(key, completed))
        elif entry.fingerprint != fingerprint:
            _raise_identity_conflict()
        else:
            joined_existing = True

    outcome = await asyncio.shield(entry.task)
    return _as_replay(outcome) if joined_existing else outcome


async def _run_durable(
    conversation_store: ConversationStore,
    session_id: str,
    client_event_id: str,
    fingerprint: str,
    operation: Callable[[], Coroutine[Any, Any, dict[str, bool | str]]],
) -> dict[str, bool | str]:
    owner_id = uuid.uuid4().hex
    claimed, receipt = await asyncio.to_thread(
        conversation_store.claim_message_event,
        session_id,
        client_event_id,
        fingerprint,
        owner_id=owner_id,
        lease_expires_at=int(time.time()) + _OWNER_LEASE_SECONDS,
    )
    if not claimed:
        return await _join_or_replay(
            conversation_store,
            session_id,
            client_event_id,
            fingerprint,
            receipt,
            operation,
        )

    try:
        outcome = await operation()
    except asyncio.CancelledError:
        # Process shutdown or task cancellation can race an external dispatch.
        # Leave the durable receipt pending so no replica guesses that retrying
        # is safe.
        raise
    except Exception as exc:
        if _is_definite_pre_dispatch_failure(exc):
            await asyncio.to_thread(
                conversation_store.abandon_message_event,
                session_id,
                client_event_id,
                fingerprint,
            )
        else:
            await asyncio.to_thread(
                conversation_store.complete_message_event,
                session_id,
                client_event_id,
                fingerprint,
                status="failed" if _is_definitive_failure(exc) else "uncertain",
                outcome=None,
            )
        raise
    await asyncio.to_thread(
        conversation_store.complete_message_event,
        session_id,
        client_event_id,
        fingerprint,
        status="completed",
        outcome=outcome,
    )
    return outcome


async def _join_or_replay(
    conversation_store: ConversationStore,
    session_id: str,
    client_event_id: str,
    fingerprint: str,
    receipt: MessageEventReceipt,
    operation: Callable[[], Coroutine[Any, Any, dict[str, bool | str]]],
) -> dict[str, bool | str]:
    """Join a live cross-replica owner briefly, then fail closed."""
    if receipt.fingerprint != fingerprint:
        _raise_identity_conflict()
    deadline = time.monotonic() + _CROSS_REPLICA_JOIN_SECONDS
    current = receipt
    while (
        current.status == "pending"
        and current.lease_expires_at > int(time.time())
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(_CROSS_REPLICA_POLL_SECONDS)
        refreshed = await asyncio.to_thread(
            conversation_store.get_message_event,
            session_id,
            client_event_id,
        )
        if refreshed is None:
            # The owner proved it failed before dispatch and released the
            # claim. This caller may safely compete for a fresh claim.
            return await _run_durable(
                conversation_store,
                session_id,
                client_event_id,
                fingerprint,
                operation,
            )
        current = refreshed
        if current.fingerprint != fingerprint:
            _raise_identity_conflict()
    return _replay_receipt(current, fingerprint)


def _replay_receipt(
    receipt: MessageEventReceipt,
    fingerprint: str,
) -> dict[str, bool | str]:
    if receipt.fingerprint != fingerprint:
        _raise_identity_conflict()
    if receipt.status == "completed" and receipt.outcome is not None:
        return _as_replay(receipt.outcome)
    if receipt.status == "failed":
        raise OmnigentError(
            "The original message submission failed; submit again as a new message",
            code=ErrorCode.MESSAGE_EVENT_FAILED,
        )
    if receipt.status == "uncertain":
        raise OmnigentError(
            "The original message may have been delivered before its outcome "
            "became uncertain. It was not sent again to avoid a duplicate.",
            code=ErrorCode.MESSAGE_EVENT_UNCERTAIN,
        )
    if receipt.lease_expires_at > int(time.time()):
        raise OmnigentError(
            "The original message submission is still active. Retry with the same "
            "client_event_id to check its outcome.",
            code=ErrorCode.MESSAGE_EVENT_PENDING,
        )
    raise OmnigentError(
        "The original message may have been delivered before its owner stopped. "
        "It was not sent again to avoid a duplicate; keep this client_event_id "
        "while checking the conversation.",
        code=ErrorCode.MESSAGE_EVENT_UNCERTAIN,
    )


def _is_definite_pre_dispatch_failure(exc: Exception) -> bool:
    if not isinstance(exc, OmnigentError):
        return False
    if exc.code == ErrorCode.WRONG_REPLICA:
        return True
    return (
        exc.code == ErrorCode.RUNNER_UNAVAILABLE
        and str(exc) == "No runner bound for session"
    )


def _is_definitive_failure(exc: Exception) -> bool:
    """Return whether the server proved the operation ended without acceptance."""
    return isinstance(exc, OmnigentError) and 400 <= exc.http_status < 500


def _as_replay(outcome: dict[str, bool | str]) -> dict[str, bool | str]:
    return {**outcome, "idempotency_replayed": True}


def _raise_identity_conflict() -> None:
    raise OmnigentError(
        "client_event_id was already used for a different message",
        code=ErrorCode.MESSAGE_EVENT_IDENTITY_CONFLICT,
    )


def _remove_in_flight(
    key: tuple[int, str, str],
    task: asyncio.Task[dict[str, bool | str]],
) -> None:
    with _lock:
        entry = _in_flight.get(key)
        if entry is not None and entry.task is task:
            _in_flight.pop(key, None)
    if not task.cancelled():
        task.exception()


def reset_for_tests() -> None:
    """Clear process-local single-flight tasks between isolated tests."""
    with _lock:
        _in_flight.clear()
