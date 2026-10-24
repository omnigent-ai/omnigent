"""Durable, serial dispatch receipts for independently hosted child sessions."""

from __future__ import annotations

import hashlib
import json
import re

from omnigent.entities import ErrorData, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.remote_child_protocol import (
    REMOTE_CHILD_NAMESPACE,
    REMOTE_CHILD_STATE,
    SUBAGENT_DELIVERED_ID_LABEL_KEY,
    TERMINAL,
    remote_child_state,
)
from omnigent.stores.conversation_store import ConversationStore


def claim_remote_dispatch(store: ConversationStore, session_id: str, dispatch_id: str) -> None:
    """Reserve one turn before forwarding; ambiguous delivery stays reserved."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", dispatch_id):
        raise OmnigentError("invalid remote dispatch identity", code=ErrorCode.INVALID_INPUT)
    conv = store.get_conversation(session_id)
    if conv is None or not conv.host_id or not conv.parent_conversation_id:
        raise OmnigentError(
            "remote dispatch requires a host-bound child", code=ErrorCode.INVALID_INPUT
        )
    if conv.archived or conv.labels.get("omnigent.closed") == "true":
        raise OmnigentError("remote child is closed", code=ErrorCode.CONFLICT)
    previous = remote_child_state(conv.labels)
    if REMOTE_CHILD_STATE in conv.labels and previous is None:
        raise OmnigentError("remote child receipt is invalid", code=ErrorCode.CONFLICT)
    if previous is not None and previous.get("status") not in TERMINAL:
        raise OmnigentError(
            "remote child already has an unsettled dispatch", code=ErrorCode.CONFLICT
        )
    if (
        previous is not None
        and conv.labels.get(SUBAGENT_DELIVERED_ID_LABEL_KEY) != previous["dispatch_id"]
    ):
        raise OmnigentError(
            "collect the previous remote result before continuing", code=ErrorCode.CONFLICT
        )
    dispatch_key = f"{REMOTE_CHILD_NAMESPACE}dispatch.{dispatch_id}"
    state = {"dispatch_id": dispatch_id, "status": "pending", "response_id": None}
    if not store.compare_and_set_labels(
        session_id,
        {REMOTE_CHILD_STATE: conv.labels.get(REMOTE_CHILD_STATE), dispatch_key: None},
        {REMOTE_CHILD_STATE: json.dumps(state), dispatch_key: "1"},
    ):
        raise OmnigentError("remote child dispatch raced another request", code=ErrorCode.CONFLICT)


def record_remote_turn(
    store: ConversationStore,
    session_id: str,
    *,
    response_id: str | None,
    status: str,
    allow_empty: bool = False,
) -> bool:
    """Bind a fresh response at start and settle only that exact execution."""
    if not response_id:
        return status not in TERMINAL
    original_response_id = response_id
    response_id = hashlib.sha256(response_id.encode()).hexdigest()
    conv = store.get_conversation(session_id)
    if conv is None or not conv.host_id or not conv.parent_conversation_id:
        return True
    state = remote_child_state(conv.labels)
    if state is None:
        return True
    response_key = f"{REMOTE_CHILD_NAMESPACE}response.{response_id}"
    current = conv.labels.get(REMOTE_CHILD_STATE)
    if status == "running":
        if state.get("status") != "pending" or response_key in conv.labels:
            return True
        state.update(status="running", response_id=response_id)
        return store.compare_and_set_labels(
            session_id,
            {REMOTE_CHILD_STATE: current, response_key: None},
            {REMOTE_CHILD_STATE: json.dumps(state), response_key: state["dispatch_id"]},
        )
    if status in TERMINAL:
        if state.get("status") != "running" or state.get("response_id") != response_id:
            return True
        item_id = None
        after = None
        while True:
            page = store.list_items(session_id, order="desc", after=after)
            for item in page.data:
                if item.response_id == original_response_id and (
                    (isinstance(item.data, MessageData) and item.data.role == "assistant")
                    or (status == "failed" and isinstance(item.data, ErrorData))
                ):
                    item_id = item.id
                    break
            if item_id or not page.has_more or not page.data:
                break
            after = page.data[-1].id
        if item_id is None and status == "completed" and not allow_empty:
            return False
        state.update(status=status)
        state["result_item_id"] = item_id
        return store.compare_and_set_labels(
            session_id, {REMOTE_CHILD_STATE: current}, {REMOTE_CHILD_STATE: json.dumps(state)}
        )
    return True


def fail_remote_dispatch(
    store: ConversationStore, session_id: str, dispatch_id: str, response_id: str
) -> bool:
    """Settle an authoritative pre-execution failure, not uncertain delivery."""
    conv = store.get_conversation(session_id)
    state = remote_child_state(conv.labels) if conv else None
    if state is None or state["dispatch_id"] != dispatch_id or state["status"] != "pending":
        return False
    return record_remote_turn(
        store, session_id, response_id=response_id, status="running"
    ) and record_remote_turn(store, session_id, response_id=response_id, status="failed")


def confirm_remote_stop(store: ConversationStore, session_id: str) -> None:
    """Settle the active dispatch only after runner/host stop is confirmed."""
    conv = store.get_conversation(session_id)
    if conv is None:
        return
    state = remote_child_state(conv.labels)
    if state is None or state.get("status") in TERMINAL:
        return
    state["status"] = "cancelled"
    store.compare_and_set_labels(
        session_id,
        {REMOTE_CHILD_STATE: conv.labels.get(REMOTE_CHILD_STATE)},
        {REMOTE_CHILD_STATE: json.dumps(state)},
    )
