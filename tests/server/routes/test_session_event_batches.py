"""Tests for the batch coalescing path in POST /v1/sessions/{id}/events.

When a batch contains consecutive external_conversation_item entries (other
than user messages and slash_command items), the route must authorize once and
call conversation_store.append once per run instead of once per item.
"""

from __future__ import annotations

import threading
import uuid as _uuid_mod
from itertools import count
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import omnigent.server.routes.sessions.routes_events as routes_events_mod
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SessionEventInput

# ── helpers ───────────────────────────────────────────────────────────────────

_id_counter = count(1)


def _next_id() -> str:
    return f"item_{next(_id_counter)}"


def _make_conv(session_id: str = "conv_test", *, title: str = "Test conv") -> Conversation:
    # title is non-None so _seed_missing_title does not call update_conversation.
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        title=title,
    )


def _make_persisted(
    new_item: NewConversationItem,
    *,
    deduplicated: bool = False,
) -> ConversationItem:
    return ConversationItem(
        id=new_item.stable_id or _next_id(),
        type=new_item.type,
        status="completed",
        response_id=new_item.response_id,
        created_at=1000,
        data=new_item.data,
        deduplicated=deduplicated,
    )


# ── recording stores ──────────────────────────────────────────────────────────


class _RecordingStore:
    """Conversation-store stand-in that records get_conversation and append
    calls so tests can assert batch sizes and authorization counts."""

    def __init__(
        self,
        conv: Conversation,
        *,
        dedup_item_ids: set[str] | None = None,
    ) -> None:
        self._conv = conv
        self._dedup_ids: set[str] = dedup_item_ids or set()
        self._lock = threading.Lock()
        self.get_calls: list[str] = []
        self.append_calls: list[list[NewConversationItem]] = []

    def get_conversation(self, session_id: str) -> Conversation | None:
        with self._lock:
            self.get_calls.append(session_id)
        return self._conv

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        with self._lock:
            self.append_calls.append(list(items))
        return [
            _make_persisted(
                it,
                deduplicated=(it.stable_id in self._dedup_ids) if it.stable_id else False,
            )
            for it in items
        ]

    def update_conversation(self, session_id: str, **kw: Any) -> Conversation:
        return self._conv

    def get_session_connectivity(self, session_ids: list[str]) -> dict[str, Any]:
        return {}


class _RaisingAppendStore(_RecordingStore):
    """Recording store that raises RuntimeError on the Nth append call.

    Earlier calls succeed and are recorded; the failing call is not.
    """

    def __init__(self, conv: Conversation, *, raise_on_call: int) -> None:
        super().__init__(conv)
        self._raise_on = raise_on_call

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        with self._lock:
            next_call = len(self.append_calls) + 1
        if next_call == self._raise_on:
            raise RuntimeError("test-induced append failure")
        return super().append(session_id, items)


# ── client factory ────────────────────────────────────────────────────────────


def _make_client(store: _RecordingStore) -> TestClient:
    """FastAPI test client with auth and permissions disabled."""
    router = create_sessions_router(
        conversation_store=store,  # type: ignore[arg-type]
        agent_store=None,  # type: ignore[arg-type]
        runner_router=None,
        auth_provider=None,
        permission_store=None,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_oe(_req: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "http_status", 500),
            content={"error": str(exc)},
        )

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ── event body builders ───────────────────────────────────────────────────────


def _assistant_event(*, response_id: str = "resp_1", source_id: str | None = None) -> dict:
    d: dict[str, Any] = {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
            "agent": "claude-3.7",
        },
        "response_id": response_id,
    }
    if source_id is not None:
        d["source_id"] = source_id
    return {"type": "external_conversation_item", "data": d}


def _tool_call_event(call_id: str, *, response_id: str = "resp_1") -> dict:
    return {
        "type": "external_conversation_item",
        "data": {
            "item_type": "function_call",
            "item_data": {
                "name": "bash",
                "call_id": call_id,
                "arguments": "{}",
                "agent": "claude-3.7",
            },
            "response_id": response_id,
        },
    }


def _user_event(text: str = "hello", *, response_id: str = "resp_1") -> dict:
    return {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
            "response_id": response_id,
        },
    }


def _text_delta_event(delta: str = "hi") -> dict:
    """A non-external_conversation_item event type that _post_event_impl handles."""
    return {
        "type": "external_output_text_delta",
        "data": {"delta": delta, "response_id": "resp_delta"},
    }


def _slash_command_event(
    *,
    name: str = "my-plugin:my-skill",
    arguments: str = "ARG-123",
    response_id: str = "resp_slash",
) -> dict:
    """A Skill slash_command item (external_conversation_item with item_type=slash_command)."""
    return {
        "type": "external_conversation_item",
        "data": {
            "item_type": "slash_command",
            "item_data": {
                "agent": "claude-3.7",
                "kind": "skill",
                "name": name,
                "arguments": arguments,
            },
            "response_id": response_id,
        },
    }


def _assistant_event_with_created_by(created_by: str, *, source_id: str = "src_cb") -> dict:
    return {
        "type": "external_conversation_item",
        "created_by": created_by,
        "data": {
            "item_type": "message",
            "item_data": {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
                "agent": "claude-3.7",
            },
            "response_id": "resp_1",
            "source_id": source_id,
        },
    }


# ── tests: one append + one auth ─────────────────────────────────────────────


def test_100_assistant_tool_items_one_append_one_auth() -> None:
    """A 100-item batch of assistant/tool events produces exactly one
    conversation_store.append call and one get_conversation call."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [_assistant_event(response_id=f"r{i}", source_id=f"src_{i}") for i in range(50)] + [
        _tool_call_event(f"call_{i}", response_id=f"r{50 + i}") for i in range(50)
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202, resp.text
    acks = resp.json()
    assert isinstance(acks, list)
    assert len(acks) == 100

    assert len(store.get_calls) == 1, (
        f"Expected 1 get_conversation (one auth), got {len(store.get_calls)}"
    )
    assert len(store.append_calls) == 1, f"Expected 1 append call, got {len(store.append_calls)}"
    assert len(store.append_calls[0]) == 100


def test_ack_shape_matches_per_entry_contract() -> None:
    """Each ack has queued=False and an item_id, matching the per-entry shape."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [_assistant_event(source_id=f"src_{i}") for i in range(3)]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202
    acks = resp.json()
    for ack in acks:
        assert ack.get("queued") is False, ack
        assert "item_id" in ack, ack
        assert isinstance(ack["item_id"], str)


def test_input_order_preserved_in_append() -> None:
    """Items arrive at append in the same order they appear in the batch."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="src_a"),
        _assistant_event(source_id="src_b"),
        _assistant_event(source_id="src_c"),
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)
    assert resp.status_code == 202
    assert len(store.append_calls) == 1

    def _expected_stable(sid: str) -> str:
        return _uuid_mod.uuid5(
            _uuid_mod.NAMESPACE_URL,
            f"omnigent-external-item:conv_test:{sid}",
        ).hex

    appended = store.append_calls[0]
    assert appended[0].stable_id == _expected_stable("src_a")
    assert appended[1].stable_id == _expected_stable("src_b")
    assert appended[2].stable_id == _expected_stable("src_c")


# ── tests: user message and other non-coalescable events split the run ────────


def test_user_message_in_middle_splits_run() -> None:
    """A user-message interrupts a coalesced run: pre-items are flushed, the
    user message goes through the per-entry path, post-items form a new run."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = (
        [_assistant_event(source_id=f"pre_{i}") for i in range(5)]
        + [_user_event("hello")]
        + [_assistant_event(source_id=f"post_{i}") for i in range(5)]
    )
    resp = client.post("/sessions/conv_test/events", json=batch)
    assert resp.status_code == 202, resp.text

    acks = resp.json()
    assert len(acks) == 11

    assert len(store.append_calls) == 3, (
        f"Expected 3 append calls (pre + user + post), got {len(store.append_calls)}"
    )
    assert len(store.append_calls[0]) == 5, "pre-run should have 5 items"
    assert len(store.append_calls[1]) == 1, "user message should be its own append"
    assert len(store.append_calls[2]) == 5, "post-run should have 5 items"


def test_user_message_ack_is_queued_false() -> None:
    """A user message sent through the per-entry path returns queued=False + item_id."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    resp = client.post("/sessions/conv_test/events", json=[_user_event()])
    assert resp.status_code == 202, resp.text

    acks = resp.json()
    assert len(acks) == 1
    assert acks[0].get("queued") is False
    assert "item_id" in acks[0]


# ── tests: deduplication ──────────────────────────────────────────────────────


def test_deduplicated_items_not_republished() -> None:
    """Items the store returns as deduplicated must not trigger a publish call."""
    src = "dup_src"
    stable_id = _uuid_mod.uuid5(
        _uuid_mod.NAMESPACE_URL,
        f"omnigent-external-item:conv_test:{src}",
    ).hex

    store = _RecordingStore(_make_conv(), dedup_item_ids={stable_id})
    client = _make_client(store)

    published_ids: list[str] = []

    def _recording_publish(
        session_id: str,
        item: Any,
        *,
        message_id: str | None = None,
        cleared_pending_id: str | None = None,
    ) -> None:
        published_ids.append(item.id)

    with patch.object(
        routes_events_mod, "_publish_external_conversation_item", _recording_publish
    ):
        resp = client.post("/sessions/conv_test/events", json=[_assistant_event(source_id=src)])

    assert resp.status_code == 202
    assert published_ids == [], "Deduplicated item must not be re-published"

    acks = resp.json()
    assert len(acks) == 1
    assert "item_id" in acks[0]


def test_non_dedup_item_is_published() -> None:
    """A normal (non-deduplicated) item triggers exactly one publish call."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    published_ids: list[str] = []

    def _recording_publish(
        session_id: str,
        item: Any,
        *,
        message_id: str | None = None,
        cleared_pending_id: str | None = None,
    ) -> None:
        published_ids.append(item.id)

    with patch.object(
        routes_events_mod, "_publish_external_conversation_item", _recording_publish
    ):
        resp = client.post(
            "/sessions/conv_test/events",
            json=[_assistant_event(source_id="unique_src")],
        )

    assert resp.status_code == 202
    assert len(published_ids) == 1, "Expected exactly one publish call"


# ── tests: invalid source_id ──────────────────────────────────────────────────


def test_invalid_source_id_returns_error() -> None:
    """An empty source_id must produce an INVALID_INPUT error mentioning source_id."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    resp = client.post("/sessions/conv_test/events", json=[_assistant_event(source_id="")])

    assert resp.status_code in (400, 422), resp.text
    assert "source_id" in str(resp.json()), f"Error must mention source_id: {resp.json()}"


def test_invalid_source_id_after_valid_items_applies_earlier() -> None:
    """Non-atomic contract: valid items before an invalid source_id are applied."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="ok_1"),
        _assistant_event(source_id="ok_2"),
        _assistant_event(source_id=""),  # invalid
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code in (400, 422), resp.text
    assert len(store.append_calls) >= 1, "Earlier items must be applied before the error"
    total_applied = sum(len(c) for c in store.append_calls)
    assert total_applied == 2, f"Expected 2 items applied before error, got {total_applied}"


# ── tests: all-user-message batch falls through to per-entry path ─────────────


def test_all_user_message_batch_uses_per_entry_path() -> None:
    """A batch where every item is a user message (no coalescing candidates)
    uses the original per-entry path: one append per item."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [_user_event(f"msg {i}") for i in range(3)]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202, resp.text
    assert len(store.append_calls) == 3


# ── tests: elicitation driving ────────────────────────────────────────────────


def test_drive_elicitation_called_per_non_dedup_item_not_for_dedup() -> None:
    """_drive_terminal_resolved_elicitation fires for each non-deduplicated
    coalesced item and never for deduplicated ones."""
    src_a, src_b, src_c = "src_elicit_a", "src_elicit_b", "src_elicit_c"

    def _stable(sid: str) -> str:
        return _uuid_mod.uuid5(
            _uuid_mod.NAMESPACE_URL,
            f"omnigent-external-item:conv_test:{sid}",
        ).hex

    store = _RecordingStore(_make_conv(), dedup_item_ids={_stable(src_b)})
    client = _make_client(store)

    driven_ids: list[str] = []

    def _recording_drive(session_id: str, persisted: Any) -> None:
        driven_ids.append(persisted.id)

    batch = [
        _assistant_event(source_id=src_a),
        _assistant_event(source_id=src_b),  # dedup — must NOT drive
        _assistant_event(source_id=src_c),
    ]
    with patch.object(routes_events_mod, "_drive_terminal_resolved_elicitation", _recording_drive):
        resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202
    assert len(driven_ids) == 2, (
        f"Expected 2 elicitation drives (non-dedup items), got {len(driven_ids)}"
    )
    assert _stable(src_b) not in driven_ids, (
        "Deduplicated item must not trigger _drive_terminal_resolved_elicitation"
    )


# ── tests: mixed event types ──────────────────────────────────────────────────


def test_mixed_batch_with_non_eci_event_in_middle() -> None:
    """[assistant_item, external_output_text_delta, assistant_item]: the first
    run is flushed, the middle event goes through _post_event_impl (no item
    persisted), then a new coalesced run starts."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="pre_1"),
        _text_delta_event("delta_text"),
        _assistant_event(source_id="post_1"),
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)
    assert resp.status_code == 202, resp.text

    acks = resp.json()
    assert len(acks) == 3

    assert "item_id" in acks[0], f"acks[0] must have item_id: {acks[0]}"
    assert acks[0].get("queued") is False
    assert "item_id" not in acks[1], f"acks[1] must not have item_id: {acks[1]}"
    assert acks[1].get("queued") is False
    assert "item_id" in acks[2], f"acks[2] must have item_id: {acks[2]}"
    assert acks[2].get("queued") is False

    assert len(store.append_calls) == 2, (
        f"Expected 2 append calls (one per coalesced run), got {len(store.append_calls)}"
    )
    assert len(store.append_calls[0]) == 1
    assert len(store.append_calls[1]) == 1


# ── tests: append failure preserves earlier runs ──────────────────────────────


def test_append_error_mid_batch_leaves_earlier_runs_applied() -> None:
    """When append raises on the Nth call, the N-1 earlier flushes remain applied."""
    # Batch: [coalesced_1, user_message, coalesced_2]
    # call 1: flush coalesced_1 → succeeds
    # call 2: user_message via _post_event_impl → succeeds
    # call 3: final flush of coalesced_2 → RAISES
    store = _RaisingAppendStore(_make_conv(), raise_on_call=3)
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="c1"),
        _user_event("hello"),
        _assistant_event(source_id="c2"),
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code != 202, "Server error should propagate as non-202"
    assert len(store.append_calls) == 2, (
        f"Expected 2 applied calls before the error, got {len(store.append_calls)}"
    )
    total_applied = sum(len(c) for c in store.append_calls)
    assert total_applied == 2, f"Expected 2 items applied, got {total_applied}"


# ── tests: created_by authority ───────────────────────────────────────────────


def test_created_by_without_runner_authority_forbidden() -> None:
    """A coalescing candidate with created_by but no runner token must be
    rejected with 403, and no items should be appended."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [_assistant_event_with_created_by("runner-user@example.com")]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 403, (
        f"Expected FORBIDDEN when created_by set without runner authority. "
        f"Got {resp.status_code}: {resp.text}"
    )
    assert len(store.append_calls) == 0, (
        "No items must be applied when the first item raises FORBIDDEN"
    )


def test_created_by_forbidden_flushes_earlier_items() -> None:
    """Non-atomic contract: a valid item before the bad created_by is flushed
    before FORBIDDEN is raised."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="ok_before"),
        _assistant_event_with_created_by("runner-user@example.com", source_id="bad_cb"),
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 403
    assert len(store.append_calls) == 1, (
        "The valid item before the bad created_by must have been flushed"
    )
    assert len(store.append_calls[0]) == 1


# ── tests: permission rejection ───────────────────────────────────────────────


def test_insufficient_permission_rejects_batch_before_append() -> None:
    """When _require_access_and_level raises FORBIDDEN, the batch is rejected
    without calling append at all."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    async def _deny_edit(*args: Any, **kwargs: Any) -> None:
        raise OmnigentError("insufficient permission", code=ErrorCode.FORBIDDEN)

    with patch.object(routes_events_mod, "_require_access_and_level", _deny_edit):
        resp = client.post(
            "/sessions/conv_test/events",
            json=[_assistant_event(source_id="p_1"), _assistant_event(source_id="p_2")],
        )

    assert resp.status_code == 403, (
        f"Expected FORBIDDEN when edit permission denied. Got {resp.status_code}: {resp.text}"
    )
    assert len(store.append_calls) == 0, "No items must be appended when auth fails"


# ── tests: non-OmnigentError mid-run ─────────────────────────────────────────


def test_non_omnigent_error_mid_run_flushes_earlier_items() -> None:
    """A non-OmnigentError while building the 3rd coalesced item must still
    flush the first two already-validated items."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    original_parse = routes_events_mod._parse_external_conversation_item
    call_count = count(1)

    def _flaky_parse(ev: SessionEventInput) -> NewConversationItem:
        if next(call_count) == 3:
            raise RuntimeError("test-induced non-OmnigentError")
        return original_parse(ev)

    batch = [_assistant_event(source_id=f"flaky_{i}") for i in range(5)]
    with patch.object(routes_events_mod, "_parse_external_conversation_item", _flaky_parse):
        resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 500, resp.text
    assert len(store.append_calls) == 1, (
        "Expected the first two validated items to be flushed before the non-OmnigentError"
    )
    assert len(store.append_calls[0]) == 2


# ── tests: audit re-tag ───────────────────────────────────────────────────────


def test_audit_event_type_retagged_after_trailing_coalesced_run() -> None:
    """The trailing coalesced run's flush must re-tag the request audit row
    with external_conversation_item, not the middle event's type."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    original_add_audit_attrs = routes_events_mod.add_audit_attrs
    recorded: list[dict[str, Any]] = []

    def _recording_add_audit_attrs(**kwargs: Any) -> None:
        recorded.append(kwargs)
        original_add_audit_attrs(**kwargs)

    batch = [
        _assistant_event(source_id="tag_pre"),
        _text_delta_event("delta_text"),
        _assistant_event(source_id="tag_post"),
    ]
    with patch.object(routes_events_mod, "add_audit_attrs", _recording_add_audit_attrs):
        resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202, resp.text
    assert recorded, "add_audit_attrs was never called"
    assert recorded[-1].get("event_type") == "external_conversation_item", (
        f"Last audit event_type must be external_conversation_item, got {recorded[-1]}"
    )


# ── tests: slash_command stays on per-entry path ──────────────────────────────


def test_skill_slash_command_item_uses_per_entry_path() -> None:
    """A slash_command item interrupts a coalesced run and goes through
    the per-entry path, producing three separate append calls."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(source_id="skill_pre"),
        _slash_command_event(),
        _assistant_event(source_id="skill_post"),
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202, resp.text
    assert len(store.append_calls) == 3, (
        f"Expected 3 append calls (run, per-entry slash_command, run), "
        f"got {len(store.append_calls)}"
    )
    assert len(store.append_calls[0]) == 1, "first coalesced run"
    assert len(store.append_calls[1]) == 1, "slash_command per-entry"
    assert store.append_calls[1][0].type == "slash_command"
    assert len(store.append_calls[2]) == 1, "second coalesced run"


# ── tests: stable_id only when source_id is present ──────────────────────────


def test_items_without_source_id_have_no_stable_id() -> None:
    """Items without data.source_id keep stable_id=None; items with source_id
    get the uuid5-derived stable_id that the per-entry path computes."""
    store = _RecordingStore(_make_conv())
    client = _make_client(store)

    batch = [
        _assistant_event(response_id="r0"),  # no source_id
        _assistant_event(response_id="r1", source_id="has_src"),
        _assistant_event(response_id="r2"),  # no source_id
    ]
    resp = client.post("/sessions/conv_test/events", json=batch)

    assert resp.status_code == 202, resp.text
    assert len(store.append_calls) == 1
    appended = store.append_calls[0]
    assert appended[0].stable_id is None
    expected = _uuid_mod.uuid5(
        _uuid_mod.NAMESPACE_URL,
        "omnigent-external-item:conv_test:has_src",
    ).hex
    assert appended[1].stable_id == expected
    assert appended[2].stable_id is None
