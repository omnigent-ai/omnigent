"""Tests for the batch path in POST /v1/sessions/{id}/events.

When a batch contains consecutive batchable external_conversation_item entries
(other than user messages, slash_command items, and entries with created_by or
tools), the route authorizes once and calls conversation_store.append once per
run instead of once per item.
"""

from __future__ import annotations

import threading
import uuid as _uuid_mod
from itertools import count
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import omnigent.server.routes._sessions.orchestration as orchestration_mod
import omnigent.server.routes.sessions.routes_events as routes_events_mod
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import Conversation, ConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import create_sessions_router

_SESSION_ID = "conv_test"

# ── helpers ───────────────────────────────────────────────────────────────────

_id_counter = count(1)


def _next_id() -> str:
    return f"item_{next(_id_counter)}"


def _stable_id(source_id: str) -> str:
    """The dedupe key the code derives from ``data.source_id``."""
    return _uuid_mod.uuid5(
        _uuid_mod.NAMESPACE_URL,
        f"omnigent-external-item:{_SESSION_ID}:{source_id}",
    ).hex


def _make_conv(session_id: str = _SESSION_ID, *, title: str = "Test conv") -> Conversation:
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
    """Conversation-store stand-in recording get_conversation/append calls,
    with optional dedupe and an optional Nth-call append failure."""

    def __init__(
        self,
        conv: Conversation,
        *,
        dedup_item_ids: set[str] | None = None,
        raise_on_append: int | None = None,
    ) -> None:
        self._conv = conv
        self._dedup_ids: set[str] = dedup_item_ids or set()
        self._raise_on_append = raise_on_append
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
            call_number = len(self.append_calls) + 1
            if call_number == self._raise_on_append:
                # Not recorded: the failing call has no observable effect.
                raise RuntimeError("test-induced append failure")
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


class _SideEffectRecorder:
    """Records the publish-side-effect calls both persistence paths make
    (they live on orchestration_mod, shared by the batched and per-entry code)."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.driven: list[str] = []

    def publish(
        self,
        session_id: str,
        item: ConversationItem,
        *,
        message_id: str | None = None,
        cleared_pending_id: str | None = None,
    ) -> None:
        self.published.append(item.id)

    def drive(self, session_id: str, persisted: ConversationItem) -> None:
        self.driven.append(persisted.id)

    def patch(self):
        return patch.multiple(
            orchestration_mod,
            _publish_external_conversation_item=self.publish,
            _drive_terminal_resolved_elicitation=self.drive,
        )


# ── client factory ────────────────────────────────────────────────────────────


def _make_client(store: _RecordingStore) -> TestClient:
    """FastAPI test client with auth and permissions disabled."""
    router = create_sessions_router(
        conversation_store=store,  # type: ignore[arg-type]
        agent_store=MagicMock(),
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


def _post(body: Any, *, store: _RecordingStore | None = None) -> tuple[Any, _RecordingStore]:
    """POST ``body`` to the events endpoint; returns ``(response, store)``."""
    store = store if store is not None else _RecordingStore(_make_conv())
    resp = _make_client(store).post(f"/sessions/{_SESSION_ID}/events", json=body)
    return resp, store


# ── event body builders ───────────────────────────────────────────────────────


def _assistant_event(
    *,
    response_id: str = "resp_1",
    source_id: str | None = None,
    created_by: str | None = None,
) -> dict:
    data: dict[str, Any] = {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
            "agent": "claude-3.7",
        },
        "response_id": response_id,
    }
    if source_id is not None:
        data["source_id"] = source_id
    event: dict[str, Any] = {"type": "external_conversation_item", "data": data}
    if created_by is not None:
        event["created_by"] = created_by
    return event


def _tool_call_event(
    call_id: str, *, response_id: str = "resp_1", source_id: str | None = None
) -> dict:
    data: dict[str, Any] = {
        "item_type": "function_call",
        "item_data": {
            "name": "bash",
            "call_id": call_id,
            "arguments": "{}",
            "agent": "claude-3.7",
        },
        "response_id": response_id,
    }
    if source_id is not None:
        data["source_id"] = source_id
    return {"type": "external_conversation_item", "data": data}


def _user_event(text: str = "hello", *, response_id: str = "resp_1") -> dict:
    return {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "item_data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            "response_id": response_id,
        },
    }


def _text_delta_event(delta: str = "hi") -> dict:
    """A non-external_conversation_item event the per-entry path accepts."""
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


# ── tests: one append + one auth ─────────────────────────────────────────────


def test_100_item_batch_one_append_one_auth() -> None:
    """A 100-item batch of assistant/tool events produces exactly one
    conversation_store.append call and one get_conversation call."""
    batch = [_assistant_event(response_id=f"r{i}", source_id=f"src_{i}") for i in range(50)]
    batch += [_tool_call_event(f"call_{i}", response_id=f"r{50 + i}") for i in range(50)]
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert len(resp.json()) == 100
    assert len(store.get_calls) == 1, f"Expected one authorization, got {len(store.get_calls)}"
    assert len(store.append_calls) == 1, f"Expected one append call, got {len(store.append_calls)}"
    assert len(store.append_calls[0]) == 100


def test_ack_shape_matches_per_entry_contract() -> None:
    """Each ack has queued=False and an item_id, matching the per-entry shape."""
    batch = [_assistant_event(source_id=f"src_{i}") for i in range(3)]
    resp, _store = _post(batch)

    assert resp.status_code == 202
    for ack in resp.json():
        assert ack.get("queued") is False, ack
        assert "item_id" in ack, ack
        assert isinstance(ack["item_id"], str)


def test_input_order_preserved_in_append() -> None:
    """Items arrive at append in the same order they appear in the batch."""
    batch = [
        _assistant_event(source_id="src_a"),
        _assistant_event(source_id="src_b"),
        _assistant_event(source_id="src_c"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 202
    assert len(store.append_calls) == 1
    appended = store.append_calls[0]
    assert appended[0].stable_id == _stable_id("src_a")
    assert appended[1].stable_id == _stable_id("src_b")
    assert appended[2].stable_id == _stable_id("src_c")


# ── tests: middle events split runs ──────────────────────────────────────────


def test_user_message_in_middle_splits_run() -> None:
    """A user message interrupts a coalesced run: pre-items are flushed, the
    user message goes through the per-entry path, post-items form a new run."""
    batch = (
        [_assistant_event(source_id=f"pre_{i}") for i in range(5)]
        + [_user_event("hello")]
        + [_assistant_event(source_id=f"post_{i}") for i in range(5)]
    )
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert len(resp.json()) == 11
    assert len(store.append_calls) == 3, (
        f"Expected 3 append calls (pre + user + post), got {len(store.append_calls)}"
    )
    assert len(store.append_calls[0]) == 5
    assert len(store.append_calls[1]) == 1
    assert len(store.append_calls[2]) == 5


def test_slash_command_in_middle_splits_run() -> None:
    """A slash_command item interrupts a coalesced run."""
    batch = [
        _assistant_event(source_id="pre"),
        _slash_command_event(),
        _assistant_event(source_id="post"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert len(resp.json()) == 3
    assert [len(c) for c in store.append_calls] == [1, 1, 1]


def test_other_event_type_in_middle_splits_run() -> None:
    """A non-external_conversation_item event type (no item persisted) splits the run."""
    batch = [
        _assistant_event(source_id="pre"),
        _text_delta_event(),
        _assistant_event(source_id="post"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    acks = resp.json()
    assert len(acks) == 3
    assert "item_id" not in acks[1]
    assert [len(c) for c in store.append_calls] == [1, 1]


# ── tests: deduplication ──────────────────────────────────────────────────────


def test_deduplicated_items_not_published() -> None:
    """Items the store returns as deduplicated must not trigger a publish call."""
    src = "dup_src"
    store = _RecordingStore(_make_conv(), dedup_item_ids={_stable_id(src)})
    recorder = _SideEffectRecorder()
    with recorder.patch():
        resp, _ = _post([_assistant_event(source_id=src)], store=store)

    assert resp.status_code == 202
    assert recorder.published == [], "Deduplicated item must not be re-published"
    assert "item_id" in resp.json()[0]


def test_dedup_items_not_elicitation_driven() -> None:
    """_drive_terminal_resolved_elicitation fires for non-deduplicated items only."""
    src_a, src_b, src_c = "src_elicit_a", "src_elicit_b", "src_elicit_c"
    store = _RecordingStore(_make_conv(), dedup_item_ids={_stable_id(src_b)})
    recorder = _SideEffectRecorder()

    batch = [
        _assistant_event(source_id=src_a),
        _assistant_event(source_id=src_b),  # dedup — must NOT drive
        _assistant_event(source_id=src_c),
    ]
    with recorder.patch():
        resp, _ = _post(batch, store=store)

    assert resp.status_code == 202
    assert len(recorder.driven) == 2
    assert _stable_id(src_b) not in recorder.driven


# ── tests: invalid source_id ──────────────────────────────────────────────────


def test_invalid_source_id_returns_error() -> None:
    """An empty source_id must produce an INVALID_INPUT error mentioning source_id."""
    resp, _store = _post([_assistant_event(source_id="")])

    assert resp.status_code in (400, 422), resp.text
    assert "source_id" in str(resp.json())


def test_invalid_source_id_after_valid_items_partial_apply() -> None:
    """Non-atomic contract: valid items before an invalid source_id are applied."""
    batch = [
        _assistant_event(source_id="ok_1"),
        _assistant_event(source_id="ok_2"),
        _assistant_event(source_id=""),  # invalid
    ]
    resp, store = _post(batch)

    assert resp.status_code in (400, 422), resp.text
    assert "source_id" in str(resp.json())
    assert sum(len(c) for c in store.append_calls) == 2


# ── tests: non-OmnigentError mid-batch ───────────────────────────────────────


def test_non_omnigent_error_mid_batch_flushes_earlier_items() -> None:
    """A non-OmnigentError raised while building the 3rd item still flushes
    the first two already-built items in one append call."""
    original_parse = orchestration_mod._parse_external_conversation_item
    calls = count(1)

    def _flaky_parse(event: Any) -> NewConversationItem:
        if next(calls) == 3:
            raise RuntimeError("test-induced non-OmnigentError")
        return original_parse(event)

    batch = [_assistant_event(source_id=f"flaky_{i}") for i in range(5)]
    with patch.object(orchestration_mod, "_parse_external_conversation_item", _flaky_parse):
        resp, store = _post(batch)

    assert resp.status_code == 500, resp.text
    assert len(store.append_calls) == 1
    assert len(store.append_calls[0]) == 2


# ── tests: created_by authority ───────────────────────────────────────────────


def test_created_by_without_authority_alone_is_forbidden() -> None:
    """A single batch entry with ``created_by`` but no runner token is rejected 403
    with no append calls (goes through the per-entry path)."""
    resp, store = _post([_assistant_event(created_by="runner-user@example.com", source_id="bad")])

    assert resp.status_code == 403, resp.text
    assert len(store.append_calls) == 0


def test_created_by_without_authority_after_valid_item_applies_earlier() -> None:
    """A valid batchable item before a created_by event is applied; the created_by
    event hits the per-entry path and is rejected 403."""
    batch = [
        _assistant_event(source_id="ok_before"),
        _assistant_event(created_by="runner-user@example.com", source_id="bad"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 403, resp.text
    assert len(store.append_calls) == 1


def test_empty_created_by_after_valid_item_applies_earlier() -> None:
    """created_by="" is not None: it is still treated as a non-batchable event
    and the FORBIDDEN guard fires via the per-entry path."""
    batch = [
        _assistant_event(source_id="ok_before2"),
        _assistant_event(created_by="", source_id="empty_cb"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 403, resp.text
    assert len(store.append_calls) == 1


# ── tests: permission rejection ───────────────────────────────────────────────


def test_permission_denied_rejects_batch_before_any_append() -> None:
    """When _require_access_and_level raises FORBIDDEN, the batch is rejected
    without calling append at all."""
    batch = [_assistant_event(source_id="p_1"), _assistant_event(source_id="p_2")]

    async def _deny_edit(*args: Any, **kwargs: Any) -> None:
        raise OmnigentError("insufficient permission", code=ErrorCode.FORBIDDEN)

    with patch.object(routes_events_mod, "_require_access_and_level", _deny_edit):
        resp, store = _post(batch)

    assert resp.status_code == 403, resp.text
    assert len(store.append_calls) == 0


# ── tests: append failure ─────────────────────────────────────────────────────


def test_append_error_mid_batch_leaves_earlier_runs_applied() -> None:
    """When append raises on the Nth call, earlier flushes remain applied."""
    # call 1: first batchable run → succeeds
    # call 2: user_message via _post_event_impl → succeeds
    # call 3: final batchable run → RAISES
    store = _RecordingStore(_make_conv(), raise_on_append=3)
    batch = [
        _assistant_event(source_id="c1"),
        _user_event("hello"),
        _assistant_event(source_id="c2"),
    ]
    resp, store = _post(batch, store=store)

    assert resp.status_code != 202, "Server error should propagate as non-202"
    assert len(store.append_calls) == 2
    assert sum(len(c) for c in store.append_calls) == 2


# ── tests: audit re-tag ───────────────────────────────────────────────────────


def test_audit_event_type_retagged_after_trailing_batchable_run() -> None:
    """The trailing batchable run must re-tag the audit row with
    external_conversation_item, not the middle event's type."""
    recorded: list[dict[str, Any]] = []
    original_add_audit_attrs = routes_events_mod.add_audit_attrs

    def _recording_add_audit_attrs(**kwargs: Any) -> None:
        recorded.append(kwargs)
        original_add_audit_attrs(**kwargs)

    batch = [
        _assistant_event(source_id="tag_pre"),
        _text_delta_event(),
        _assistant_event(source_id="tag_post"),
    ]
    with patch.object(routes_events_mod, "add_audit_attrs", _recording_add_audit_attrs):
        resp, _store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert recorded, "add_audit_attrs was never called"
    assert recorded[-1].get("event_type") == "external_conversation_item", recorded[-1]


# ── tests: array vs individual parity ────────────────────────────────────────


def test_array_post_matches_sequential_single_posts() -> None:
    """The array-body batching path and the classic one-event-per-POST path
    must persist and broadcast the same items for the same input — proving
    the two paths, which now share _new_external_conversation_item and
    _publish_persisted_external_item, stay behaviorally identical."""
    events = [
        _assistant_event(source_id="mix_a1"),
        _tool_call_event("mix_call", source_id="mix_fc1"),
        _assistant_event(source_id="mix_dup"),  # deduplicated on both paths
        _assistant_event(),  # no source_id -> stable_id None
        _user_event("mid user"),  # non-batchable, splits the run
        _slash_command_event(),  # non-batchable, splits the run
    ]
    dedup_ids = {_stable_id("mix_dup")}
    # None where the persisted id is a fresh counter value, incomparable
    # across the two runs; the uuid5-derived ids are comparable.
    deterministic_ids = [
        _stable_id("mix_a1"),
        _stable_id("mix_fc1"),
        _stable_id("mix_dup"),
        None,
        None,
        None,
    ]

    batch_store = _RecordingStore(_make_conv(), dedup_item_ids=dedup_ids)
    batch_recorder = _SideEffectRecorder()
    with batch_recorder.patch():
        batch_resp, _ = _post(events, store=batch_store)
    assert batch_resp.status_code == 202, batch_resp.text
    batch_acks = batch_resp.json()

    individual_store = _RecordingStore(_make_conv(), dedup_item_ids=dedup_ids)
    individual_recorder = _SideEffectRecorder()
    individual_acks = []
    with individual_recorder.patch():
        for event in events:
            resp, _ = _post(event, store=individual_store)
            assert resp.status_code == 202, resp.text
            individual_acks.append(resp.json())

    # Both paths append the same NewConversationItems, in the same order.
    batch_appended = [item for call in batch_store.append_calls for item in call]
    individual_appended = [item for call in individual_store.append_calls for item in call]
    assert len(batch_appended) == len(events)
    assert len(individual_appended) == len(events)
    for i, (b_item, i_item) in enumerate(zip(batch_appended, individual_appended, strict=True)):
        assert b_item.type == i_item.type, f"item {i}: type mismatch"
        assert b_item.data == i_item.data, f"item {i}: data mismatch"
        assert b_item.stable_id == i_item.stable_id, f"item {i}: stable_id mismatch"
        assert b_item.created_by == i_item.created_by, f"item {i}: created_by mismatch"

    # Acks match where the id is deterministic; elsewhere only shape is comparable.
    assert len(batch_acks) == len(events)
    assert len(individual_acks) == len(events)
    for i, expected in enumerate(deterministic_ids):
        assert "item_id" in batch_acks[i]
        assert "item_id" in individual_acks[i]
        if expected is not None:
            assert batch_acks[i]["item_id"] == expected, f"batch ack {i}"
            assert individual_acks[i]["item_id"] == expected, f"individual ack {i}"

    # Publish/drive fire for every non-deduplicated item (5 of 6) on both paths.
    assert len(batch_recorder.published) == 5
    assert len(individual_recorder.published) == 5
    assert len(batch_recorder.driven) == 5
    assert len(individual_recorder.driven) == 5
    dup_id = _stable_id("mix_dup")
    assert dup_id not in batch_recorder.published
    assert dup_id not in individual_recorder.published
    for expected in (_stable_id("mix_a1"), _stable_id("mix_fc1")):
        assert expected in batch_recorder.published
        assert expected in individual_recorder.published
        assert expected in batch_recorder.driven
        assert expected in individual_recorder.driven


# ── tests: slash_command stays on per-entry path ──────────────────────────────


def test_skill_slash_command_item_uses_per_entry_path() -> None:
    """A slash_command item takes the per-entry path, producing three separate
    append calls."""
    batch = [
        _assistant_event(source_id="skill_pre"),
        _slash_command_event(),
        _assistant_event(source_id="skill_post"),
    ]
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert len(store.append_calls) == 3, (
        f"Expected 3 append calls (run, per-entry slash_command, run), "
        f"got {len(store.append_calls)}"
    )
    assert len(store.append_calls[0]) == 1
    assert store.append_calls[1][0].type == "slash_command"
    assert len(store.append_calls[2]) == 1


# ── tests: stable_id only when source_id is present ──────────────────────────


def test_items_without_source_id_have_no_stable_id() -> None:
    """Items without data.source_id keep stable_id=None; items with source_id
    get the uuid5-derived stable_id."""
    batch = [
        _assistant_event(response_id="r0"),  # no source_id
        _assistant_event(response_id="r1", source_id="has_src"),
        _assistant_event(response_id="r2"),  # no source_id
    ]
    resp, store = _post(batch)

    assert resp.status_code == 202, resp.text
    assert len(store.append_calls) == 1
    appended = store.append_calls[0]
    assert appended[0].stable_id is None
    assert appended[1].stable_id == _stable_id("has_src")
    assert appended[2].stable_id is None
