"""Tests for SqlAlchemyConversationStore."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import (
    ErrorData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    ReasoningData,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

# ── CRUD ──────────────────────────────────────────────


def test_create_and_get(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    assert conv.id.startswith("conv_")

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.id == conv.id


def test_get_nonexistent(conversation_store: SqlAlchemyConversationStore) -> None:
    assert conversation_store.get_conversation("conv_none") is None


def test_get_conversations_bulk(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``get_conversations`` returns one entry per resolvable id, omits
    unknown ids, and carries each row's batched labels — matching what
    a per-id ``get_conversation`` fan-out would produce, which is what
    the ``WS /v1/sessions/updates`` rescan relies on.
    """
    a = conversation_store.create_conversation(title="alpha")
    b = conversation_store.create_conversation(title="beta")
    # Label only one row to prove labels are joined per-id, not smeared
    # across the batch or dropped for the unlabeled row.
    conversation_store.set_labels(a.id, {"omnigent.ui": "terminal"})

    result = conversation_store.get_conversations([a.id, b.id, "conv_missing"])

    # The unknown id is omitted rather than mapped to None — the caller
    # treats absence as "no longer resolves".
    assert set(result) == {a.id, b.id}
    # Titles prove the real rows came back, not placeholder shells.
    assert result[a.id].title == "alpha"
    assert result[b.id].title == "beta"
    # Labels are attached to the row they belong to and only that row.
    assert result[a.id].labels == {"omnigent.ui": "terminal"}
    assert result[b.id].labels == {}


def test_get_conversations_empty_input_skips_query(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Empty id list returns an empty map without a database round-trip."""
    assert conversation_store.get_conversations([]) == {}


def test_list_latest_message_items_for_conversations(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``list_latest_message_items_for_conversations`` returns newest message
    rows per conversation in one batched call.

    The child-session summary route depends on this helper to avoid an
    event-loop-blocking N+1 ``list_items`` fan-out. This test seeds two
    conversations with interleaved item types, asks for two messages per
    conversation, and asserts exact per-conversation ordering and filtering.

    :param conversation_store: SQLAlchemy conversation store fixture.
    """
    conv_a = conversation_store.create_conversation(title="alpha")
    conv_b = conversation_store.create_conversation(title="beta")
    conv_empty = conversation_store.create_conversation(title="empty")

    conversation_store.append(
        conv_a.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_a",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "alpha old"}],
                    agent="worker",
                ),
            ),
            NewConversationItem(
                type="reasoning",
                response_id="resp_a",
                data=ReasoningData(
                    agent="worker",
                    summary=[{"type": "summary_text", "text": "thinking"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_a",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "alpha new"}],
                    agent="worker",
                ),
            ),
        ],
    )
    conversation_store.append(
        conv_b.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_b",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "bravo old"}],
                    agent="worker",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_b",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "bravo middle"}],
                    agent="worker",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_b",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "bravo new"}],
                    agent="worker",
                ),
            ),
        ],
    )

    result = conversation_store.list_latest_message_items_for_conversations(
        [conv_a.id, conv_b.id, conv_empty.id, "conv_missing"],
        per_conversation_limit=2,
    )

    def _texts(conversation_id: str) -> list[str]:
        """Extract assistant text from the returned message items.

        :param conversation_id: Conversation id whose returned messages
            should be inspected.
        :returns: Ordered list of text blocks from that conversation's
            returned message items.
        """
        texts: list[str] = []
        for item in result[conversation_id]:
            assert isinstance(item.data, MessageData)
            texts.append(item.data.content[0]["text"])
        return texts

    assert set(result) == {conv_a.id, conv_b.id, conv_empty.id, "conv_missing"}
    assert _texts(conv_a.id) == ["alpha new", "alpha old"]
    assert _texts(conv_b.id) == ["bravo new", "bravo middle"]
    assert result[conv_empty.id] == []
    assert result["conv_missing"] == []


def test_update_title(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    updated = conversation_store.update_conversation(conv.id, title="Chat 1")
    assert updated is not None
    assert updated.title == "Chat 1"

    assert conversation_store.update_conversation("conv_none", title="x") is None


def test_update_archived_round_trip(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``update_conversation(archived=...)`` persists the flag both ways
    and a fresh conversation defaults to not-archived.

    Re-fetching via ``get_conversation`` (a separate read from the DB)
    proves the column was actually written, not just reflected on the
    in-session ORM object. A failure here means the archive column
    isn't persisted — the sidebar's archive button would appear to do
    nothing after a refresh.
    """
    conv = conversation_store.create_conversation()
    # Default state: a brand-new session is not archived. Read it back
    # from the DB so we exercise the server_default / column mapping.
    assert conversation_store.get_conversation(conv.id).archived is False

    archived = conversation_store.update_conversation(conv.id, archived=True)
    assert archived is not None
    assert archived.archived is True
    # Persisted, not just on the returned object.
    assert conversation_store.get_conversation(conv.id).archived is True

    unarchived = conversation_store.update_conversation(conv.id, archived=False)
    assert unarchived is not None
    assert unarchived.archived is False
    assert conversation_store.get_conversation(conv.id).archived is False


def test_update_archived_none_leaves_unchanged(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``archived=None`` (the default) must not touch the stored flag.

    The PATCH route passes ``archived=body.archived`` on every session
    update — including title-only edits where ``archived`` is ``None``.
    If ``None`` were treated as "set to false", renaming an archived
    session would silently unarchive it. This guards that.
    """
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(conv.id, archived=True)

    # A title-only update (archived defaults to None) must leave the
    # archived flag set.
    conversation_store.update_conversation(conv.id, title="Renamed")
    refetched = conversation_store.get_conversation(conv.id)
    assert refetched.title == "Renamed"
    assert refetched.archived is True, (
        "archived=None must leave the flag unchanged; a title-only edit "
        "unarchived the session, which would lose archive state on rename."
    )


def test_update_archived_bumps_updated_at(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Toggling archived advances ``updated_at`` (like title/effort do).

    The clock is monkeypatched to a fixed, larger value so the bump is
    deterministic regardless of wall-clock resolution. The web client
    relies on this bump being acknowledged (``markConversationSeen``)
    so a self-initiated archive isn't mistaken for new activity — if
    the bump regressed to a no-op, that contract would silently change.
    """
    conv = conversation_store.create_conversation()
    created_at = conversation_store.get_conversation(conv.id).updated_at

    # Pin the clock past created_at so the new updated_at is unambiguous.
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: created_at + 100,
    )
    updated = conversation_store.update_conversation(conv.id, archived=True)
    assert updated is not None
    assert updated.updated_at == created_at + 100, (
        f"archiving should stamp updated_at via now_epoch(); expected "
        f"{created_at + 100}, got {updated.updated_at}. If it equals "
        f"{created_at}, the archive write didn't mark the row changed."
    )


# ── Append & list items ──────────────────────────────


def test_append_and_list_items(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    items = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "Hello"}]),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Hi there!"}],
                    agent="test-agent",
                ),
            ),
        ],
    )
    assert len(items) == 2
    assert items[0].id.startswith("msg_")
    assert items[1].id.startswith("msg_")

    page = conversation_store.list_items(conv.id)
    assert len(page.data) == 2
    assert page.data[0].data.role == "user"
    assert page.data[1].data.role == "assistant"


def test_append_records_human_author_attribution(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A human-authored item round-trips its author identity.

    Analogue of the comment ``created_by`` contract: the actor who
    posted the message is recorded on the persisted item and read back
    on listing.
    """
    conv = conversation_store.create_conversation()
    [persisted] = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_attr",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Please review"}],
                ),
                created_by="alice@example.com",
            )
        ],
    )

    assert persisted.created_by == "alice@example.com"
    [read_back] = conversation_store.list_items(conv.id).data
    assert read_back.created_by == "alice@example.com"


def test_append_leaves_created_by_none_for_agent_items(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Items appended without an actor (agent/tool/system) read back None.

    Keeps agent output distinguishable from human-authored messages.
    """
    conv = conversation_store.create_conversation()
    [persisted] = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_agent",
                data=MessageData(
                    role="assistant",
                    agent="my-agent",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )

    assert persisted.created_by is None
    [read_back] = conversation_store.list_items(conv.id).data
    assert read_back.created_by is None


def test_append_function_call_items(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    items = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_002",
                data=FunctionCallData(
                    agent="test-agent",
                    name="get_weather",
                    arguments='{"city": "SF"}',
                    call_id="call_001",
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="resp_002",
                data=FunctionCallOutputData(
                    call_id="call_001",
                    output='{"temp": 65}',
                ),
            ),
        ],
    )
    assert items[0].id.startswith("fc_")
    assert items[1].id.startswith("fco_")


def test_append_tool_output_with_nul_bytes(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    Tool output containing NUL (0x00) bytes must still persist.

    Reproduces the production failure where a tool returned bytes from
    a binary file: the embedded NUL hit a Postgres text column and
    aborted the whole INSERT (``psycopg.DataError: PostgreSQL text
    fields cannot contain NUL (0x00) bytes``), so the function call
    output never persisted. SQLite tolerates NUL, so the deterministic
    guard below is reading the persisted columns and asserting no raw
    NUL survived — that fails if the store stops stripping NUL.
    """
    conv = conversation_store.create_conversation()
    # "marker_unique_token" is space-delimited from the NUL run so we
    # can later prove it remained FTS-indexable after sanitization.
    nul_output = "binary chunk \x00\x00\x00\x00 marker_unique_token"
    items = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="function_call_output",
                response_id="resp_nul",
                data=FunctionCallOutputData(
                    call_id="call_nul",
                    output=nul_output,
                ),
            ),
        ],
    )
    # append() returning normally (not raising DataError) is the
    # primary reproduction: pre-fix this INSERT aborted on Postgres.
    item_id = items[0].id
    assert item_id.startswith("fco_")

    # The payload round-trips faithfully: NUL is preserved in the data
    # column (json.dumps escapes it to the literal 6-char
    # backslash-u0000 sequence, which Postgres accepts and json.loads
    # decodes back). Stripping is lossy for the FTS index only, never
    # for the stored output.
    page = conversation_store.list_items(conv.id)
    assert page.data[0].data.output == nul_output

    # Deterministic backend-agnostic guard: inspect the persisted
    # columns directly. search_text must have had its raw NUL removed
    # (this is the line that fails if append() stops calling
    # strip_nul_bytes); data must carry no raw NUL either (json
    # escaping keeps it as the literal backslash-u0000 sequence).
    engine = get_or_create_engine(db_uri)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data, search_text FROM conversation_items WHERE id = :id"),
            {"id": item_id},
        ).one()
    assert "\x00" not in row.search_text
    assert "\x00" not in row.data

    # The meaningful, NUL-adjacent token is still searchable, proving
    # sanitization dropped only the NUL and left real tokens indexed.
    results = conversation_store.search("marker_unique_token", conversation_id=conv.id)
    assert [r.id for r in results] == [item_id]


def test_append_reasoning_item(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    items = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="reasoning",
                response_id="resp_003",
                data=ReasoningData(
                    agent="test-agent",
                    summary=[{"type": "summary_text", "text": "Thinking..."}],
                ),
            ),
        ],
    )
    assert items[0].id.startswith("rs_")


def test_append_error_item_round_trips_for_history(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Persisted ``error`` items survive the real SQLAlchemy store path
    and flatten to the same shape ``GET /sessions/{id}/items`` returns.

    A regression here reproduces the web symptom where a live
    ``response.error`` banner appears but disappears after refresh:
    either append/list rejects the new item type, or the API shape no
    longer contains the fields ``itemsToBlocks`` needs to rebuild the
    banner.

    :param conversation_store: SQLAlchemy conversation store fixture.
    """
    conv = conversation_store.create_conversation()
    [persisted] = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="error",
                response_id="resp_failed",
                data=ErrorData(
                    source="execution",
                    code="native_terminal_start_failed",
                    message="Native Codex requires the 'codex' CLI on PATH.",
                ),
            ),
        ],
    )

    assert persisted.id.startswith("err_")
    [read_back] = conversation_store.list_items(conv.id).data
    assert read_back.id == persisted.id
    assert read_back.response_id == "resp_failed"
    assert isinstance(read_back.data, ErrorData)
    assert read_back.data.message == "Native Codex requires the 'codex' CLI on PATH."
    assert read_back.to_api_dict() == {
        "id": persisted.id,
        "response_id": "resp_failed",
        "type": "error",
        "status": "completed",
        "source": "execution",
        "code": "native_terminal_start_failed",
        "message": "Native Codex requires the 'codex' CLI on PATH.",
    }


# ── Ordering & cursors ───────────────────────────────


def test_position_ordering(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_a",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "First"}]),
            ),
        ],
    )
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_b",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "Second"}]),
            ),
        ],
    )
    page = conversation_store.list_items(conv.id)
    assert len(page.data) == 2
    texts = [page.data[i].data.content[0]["text"] for i in range(2)]
    assert texts == ["First", "Second"]


def test_unique_position_constraint(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The (conversation_id, position) pair has a unique index.

    Verify that manually inserting a duplicate position raises
    IntegrityError, confirming the safety net is in place.
    """
    from sqlalchemy.exc import IntegrityError

    from omnigent.db.db_models import SqlConversationItem
    from omnigent.db.utils import generate_item_id

    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_dup",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "first"}],
                ),
            ),
        ],
    )
    # Directly insert a row at position 0 (already taken) to
    # confirm the unique constraint rejects it.
    with pytest.raises(IntegrityError):
        with conversation_store._session() as session:
            session.add(
                SqlConversationItem(
                    id=generate_item_id("message"),
                    conversation_id=conv.id,
                    response_id="resp_dup",
                    created_at=0,
                    status="completed",
                    position=0,  # duplicate
                    type="message",
                    data='{"role":"user","content":[]}',
                    search_text="",
                )
            )


def test_concurrent_appends_do_not_collide_on_position(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Two concurrent ``append()`` calls on the same conversation
    must not collide on position assignment.

    Reproduces the UNIQUE-constraint race observed 2026-04-30 in
    a live REPL session with the user's 20-shell scenario: the
    agent loop's incremental tool-call persist and the steering
    inbox's auto-injection of idle-notification user messages
    both ran ``append()`` for the same conversation_id. Both
    transactions read ``max(position) = N``, both INSERTed at
    position ``N+1``, the loser crashed with
    ``IntegrityError: UNIQUE constraint failed:
    conversation_items.conversation_id, conversation_items.position``.

    The race window came from SQLite starting transactions as
    DEFERRED (no write lock until first DML), so the
    ``select(max(position))`` in :meth:`append` ran without
    holding any lock. Fixed by upgrading
    :meth:`_lock_conversation` from a no-op on SQLite to an
    UPDATE that escalates the transaction to RESERVED.

    What this test proves and what a failure means:

    - Running N appends concurrently on the same conversation
      produces N items with N distinct positions, no
      ``IntegrityError``. If a thread raises, the
      transaction-escalation fix regressed.
    - Final positions are contiguous from 0 to ``items_per_thread *
      threads - 1``. Gaps would mean a write succeeded but its
      position was reused — a worse failure mode than the
      IntegrityError.

    Does NOT use mocks: real :class:`SqlAlchemyConversationStore`,
    real SQLite (the conftest's per-test temp DB), real threads
    so SQLite's actual lock semantics participate.
    """
    import threading

    conv = conversation_store.create_conversation()
    threads_count = 8
    items_per_thread = 5
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def _append_n(thread_idx: int) -> None:
        try:
            for i in range(items_per_thread):
                conversation_store.append(
                    conv.id,
                    [
                        NewConversationItem(
                            type="message",
                            response_id=f"resp_t{thread_idx}",
                            data=MessageData(
                                role="user",
                                content=[
                                    {
                                        "type": "input_text",
                                        "text": f"t{thread_idx}-{i}",
                                    }
                                ],
                            ),
                        ),
                    ],
                )
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_append_n, args=(i,)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    # No thread raised — the lock escalation prevented every
    # IntegrityError. If non-empty, the race regressed and the
    # user-reported "UNIQUE constraint failed" symptom is back.
    assert errors == [], (
        f"Concurrent appends raised {len(errors)} error(s); the "
        f"first was: {errors[0]!r}. The position-race fix in "
        f"_lock_conversation regressed — SQLite transactions are "
        f"running concurrent SELECT max(position) without a "
        f"write lock again."
    )

    # All items persisted with distinct IDs. Position
    # uniqueness is enforced by the
    # ``ix_conversation_items_conversation_id_position`` UNIQUE
    # index on the SQL table, so any race that produced
    # duplicate positions would have raised IntegrityError —
    # caught above. The remaining check here is that no append
    # silently dropped: every (thread, item) pair surfaced as
    # exactly one ConversationItem in the public listing.
    items = conversation_store.list_items(conv.id, limit=1000).data
    expected_count = threads_count * items_per_thread
    assert len(items) == expected_count, (
        f"expected {expected_count} items; got {len(items)}. "
        f"Some appends silently dropped despite not raising — "
        f"that would be a worse regression than the original "
        f"IntegrityError."
    )
    item_ids = {item.id for item in items}
    assert len(item_ids) == expected_count, (
        f"expected {expected_count} distinct item IDs; got "
        f"{len(item_ids)}. Duplicate IDs would mean an item was "
        f"persisted twice or list_items returned duplicates."
    )

    # Positions are contiguous 0..N-1. The UNIQUE index catches *reused*
    # positions (IntegrityError, asserted above), but a counter that
    # over-advances — or an append silently skipped — leaves a *gap* with no
    # error. list_items hides position, so assert on the raw column directly.
    assert _stored_positions(conversation_store, conv.id) == list(range(expected_count)), (
        "concurrent appends must allocate a gap-free 0..N-1 position sequence"
    )


def test_heavy_batch_racing_steering_append_does_not_collide(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Models the exact user-reported race shape from 2026-04-30:
    one path appends a heavy batch (assistant message +
    function_call + function_call_output items, mirroring the
    workflow's incremental tool-call persist via
    ``_handle_executor_event``), while a second path concurrently
    appends a single user message (mirroring the steering
    inbox's auto-injection of an idle notification).

    The unit test ``test_concurrent_appends_do_not_collide_on_position``
    proves the SQL-level race exists with N homogeneous threads.
    This test pins the SHAPE of the user's actual failure: a
    multi-item append (which holds the reserve longer because it
    inserts more rows) racing a single-item append (the steering
    write). If a regression introduces a DIFFERENT race window
    that only surfaces under this asymmetric pattern, this test
    catches it.

    On revert: the heavy-batch thread holds RESERVED while
    inserting message + fc + fco, the steering thread blocks on
    busy_timeout, then re-reads max_pos. With the fix, no
    IntegrityError. Without the fix, the steering thread reads
    max_pos before the heavy batch commits and one of them
    crashes.
    """
    import threading

    conv = conversation_store.create_conversation()
    barrier = threading.Barrier(2, timeout=10.0)
    errors: list[tuple[str, Exception]] = []
    errors_lock = threading.Lock()

    def _heavy_batch() -> None:
        """Mimics ``_handle_executor_event``: 1 message + 1 fc + 1 fco per call."""
        try:
            barrier.wait()
            for i in range(20):
                conversation_store.append(
                    conv.id,
                    [
                        NewConversationItem(
                            type="message",
                            response_id="resp_workflow",
                            data=MessageData(
                                role="assistant",
                                content=[
                                    {
                                        "type": "output_text",
                                        "text": f"calling tool {i}",
                                    }
                                ],
                                agent="agent_test",
                            ),
                        ),
                        NewConversationItem(
                            type="function_call",
                            response_id="resp_workflow",
                            data=FunctionCallData(
                                name="sys_terminal_launch",
                                arguments=f'{{"session": "sh{i}"}}',
                                call_id=f"call_{i}",
                                agent="agent_test",
                            ),
                        ),
                        NewConversationItem(
                            type="function_call_output",
                            response_id="resp_workflow",
                            data=FunctionCallOutputData(
                                call_id=f"call_{i}",
                                output='{"status": "launched"}',
                            ),
                        ),
                    ],
                )
        except Exception as exc:
            with errors_lock:
                errors.append(("heavy_batch", exc))

    def _steering_appends() -> None:
        """Mimics the steering inbox auto-injecting idle notifications."""
        try:
            barrier.wait()
            for i in range(20):
                conversation_store.append(
                    conv.id,
                    [
                        NewConversationItem(
                            type="message",
                            response_id="resp_steering",
                            data=MessageData(
                                role="user",
                                content=[
                                    {
                                        "type": "input_text",
                                        "text": (f"[System: terminal sh{i} is idle]"),
                                    }
                                ],
                            ),
                        ),
                    ],
                )
        except Exception as exc:
            with errors_lock:
                errors.append(("steering", exc))

    heavy = threading.Thread(target=_heavy_batch)
    steering = threading.Thread(target=_steering_appends)
    heavy.start()
    steering.start()
    heavy.join(timeout=30.0)
    steering.join(timeout=30.0)

    # Neither thread crashed — the lock escalation serialized
    # both. Without the fix, one of them raises
    # ``IntegrityError: UNIQUE constraint failed:
    # conversation_items.conversation_id, conversation_items.position``.
    assert errors == [], (
        f"Heavy-batch + steering append race produced {len(errors)} "
        f"error(s). First: {errors[0]!r}. The user-reported "
        f"2026-04-30 IntegrityError is back."
    )

    # Total = 20 batches × 3 items + 20 steering items = 80.
    # Position uniqueness is enforced by the SQL UNIQUE index
    # on (conversation_id, position) — any race that produced a
    # duplicate would have raised above. So count + ID
    # distinctness is sufficient evidence that the lock
    # escalation worked.
    items = conversation_store.list_items(conv.id, limit=1000).data
    assert len(items) == 80, f"expected 80 items; got {len(items)}"
    item_ids = {item.id for item in items}
    assert len(item_ids) == 80, (
        f"expected 80 distinct item IDs; got {len(item_ids)}. "
        f"Duplicate IDs would mean an item was persisted twice."
    )

    # Gap-free 0..79 across both threads: the heavy batch advances the
    # next_position counter by 3 and the steering append by 1, so a counter
    # that mis-advances under this asymmetric race would skip or reuse a
    # position without tripping the UNIQUE index. Assert the raw column.
    assert _stored_positions(conversation_store, conv.id) == list(range(80)), (
        "heavy-batch + steering appends must allocate a gap-free 0..79 sequence"
    )


def _make_5_items(conversation_store: SqlAlchemyConversationStore, conv_id: str):
    """Helper: append 5 messages and return the persisted items."""
    return conversation_store.append(
        conv_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_x",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"msg-{i}"}],
                ),
            )
            for i in range(5)
        ],
    )


def test_list_items_after_cursor(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    items = _make_5_items(conversation_store, conv.id)

    page = conversation_store.list_items(conv.id, after=items[1].id, limit=2)
    assert len(page.data) == 2
    assert page.data[0].id == items[2].id


def test_list_items_desc_order(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    _make_5_items(conversation_store, conv.id)
    page_asc = conversation_store.list_items(conv.id, order="asc")
    page_desc = conversation_store.list_items(conv.id, order="desc")
    assert [it.id for it in page_asc.data] == list(reversed([it.id for it in page_desc.data]))


def test_list_items_desc_with_after_cursor(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """In desc order, 'after' means items with lower position."""
    conv = conversation_store.create_conversation()
    items = _make_5_items(conversation_store, conv.id)
    # desc full page: [4, 3, 2, 1, 0]
    page1 = conversation_store.list_items(conv.id, limit=2, order="desc")
    assert page1.data[0].id == items[4].id
    assert page1.data[1].id == items[3].id
    assert page1.has_more is True

    page2 = conversation_store.list_items(conv.id, limit=2, order="desc", after=page1.last_id)
    assert page2.data[0].id == items[2].id
    assert page2.data[1].id == items[1].id


def test_list_items_before_cursor(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    items = _make_5_items(conversation_store, conv.id)
    # asc order: [0, 1, 2, 3, 4]; before item[3] should give [0, 1, 2]
    page = conversation_store.list_items(conv.id, before=items[3].id, order="asc")
    assert [it.id for it in page.data] == [items[i].id for i in range(3)]


# ── Conversation ID / response ID lookups ────────────


def test_search(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_s1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "weather in Paris"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_s1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "sunny and warm"}],
                    agent="test-agent",
                ),
            ),
        ],
    )
    results = conversation_store.search("Paris")
    assert len(results) == 1
    assert results[0].type == "message"

    results = conversation_store.search("sunny")
    assert len(results) == 1

    assert conversation_store.search("nonexistent") == []


def test_search_scoped_to_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv1 = conversation_store.create_conversation()
    conv2 = conversation_store.create_conversation()
    conversation_store.append(
        conv1.id,
        [
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello world"}],
                ),
            ),
        ],
    )
    conversation_store.append(
        conv2.id,
        [
            NewConversationItem(
                type="message",
                response_id="r2",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello universe"}],
                ),
            ),
        ],
    )
    # Unscoped: both match "hello"
    assert len(conversation_store.search("hello")) == 2

    # Scoped: only one per conversation
    assert len(conversation_store.search("hello", conversation_id=conv1.id)) == 1
    assert len(conversation_store.search("hello", conversation_id=conv2.id)) == 1


def test_search_function_call_item(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """FTS indexes function_call items by name and arguments."""
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_fc",
                data=FunctionCallData(
                    agent="test-agent",
                    name="get_weather",
                    arguments='{"city": "Tokyo"}',
                    call_id="call_1",
                ),
            ),
        ],
    )
    assert len(conversation_store.search("get_weather")) == 1
    assert len(conversation_store.search("Tokyo")) == 1
    assert conversation_store.search("nonexistent") == []


def test_list_conversations_search_query_matches_content(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``list_conversations(search_query=...)`` matches conversations
    whose title OR item content contains the query substring.

    :param conversation_store: The conversation store fixture.
    """
    conv_title = conversation_store.create_conversation()
    conversation_store.update_conversation(conv_title.id, title="deployment runbook")

    conv_content = conversation_store.create_conversation()
    conversation_store.update_conversation(conv_content.id, title="General chat")
    conversation_store.append(
        conv_content.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_gc1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "fix the deployment pipeline"}],
                ),
            ),
        ],
    )

    conv_neither = conversation_store.create_conversation()
    conversation_store.update_conversation(conv_neither.id, title="Unrelated session")

    page = conversation_store.list_conversations(search_query="deployment")
    matched_ids = {c.id for c in page.data}
    assert conv_title.id in matched_ids, "title match should be included"
    assert conv_content.id in matched_ids, "content match should be included"
    assert conv_neither.id not in matched_ids, "non-matching conversation excluded"


def test_list_conversations_search_query_content_only(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A conversation with no title but matching item content is
    returned by ``search_query``.

    :param conversation_store: The conversation store fixture.
    """
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_co1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "the server is healthy"}],
                    agent="test-agent",
                ),
            ),
        ],
    )

    page = conversation_store.list_conversations(search_query="healthy")
    assert len(page.data) == 1
    assert page.data[0].id == conv.id


def test_list_conversations_excludes_archived_by_default(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``list_conversations`` hides archived rows unless
    ``include_archived=True``.

    Two conversations, one archived: the default listing returns only
    the active one; ``include_archived=True`` returns both. This is the
    exact contract the sidebar's default view and "Show archived"
    toggle depend on. A failure means archived sessions either leak
    into the default sidebar (filter not applied) or can never be
    surfaced (toggle has no effect).
    """
    active = conversation_store.create_conversation()
    archived = conversation_store.create_conversation()
    conversation_store.update_conversation(archived.id, archived=True)

    default_ids = {c.id for c in conversation_store.list_conversations().data}
    assert active.id in default_ids, "active session must appear in the default listing"
    assert archived.id not in default_ids, "archived session must be hidden by default"

    all_ids = {c.id for c in conversation_store.list_conversations(include_archived=True).data}
    assert all_ids >= {active.id, archived.id}, (
        f"include_archived=True must return both active and archived sessions; got {all_ids}"
    )


# ── Delete ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_del",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "bye"}]),
            ),
        ],
    )
    assert await conversation_store.delete_conversation(conv.id) is True
    assert conversation_store.get_conversation(conv.id) is None
    assert conversation_store.list_items(conv.id).data == []
    assert await conversation_store.delete_conversation(conv.id) is False


@pytest.mark.asyncio
async def test_delete_conversation_with_items(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Deleting a conversation with items removes the conversation and all its items.

    :param conversation_store: The conversation store fixture.
    """
    conv = conversation_store.create_conversation()
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_x",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hi"}],
                ),
            ),
        ],
    )
    assert await conversation_store.delete_conversation(conv.id) is True
    assert conversation_store.get_conversation(conv.id) is None


# ── List conversations pagination ────────────────────


def test_list_conversations_pagination(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    for _ in range(4):
        conversation_store.create_conversation()

    page1 = conversation_store.list_conversations(limit=2)
    assert len(page1.data) == 2
    assert page1.has_more is True

    page2 = conversation_store.list_conversations(limit=2, after=page1.last_id)
    assert len(page2.data) == 2
    assert page2.has_more is False


def test_list_conversations_order_asc(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    for _ in range(3):
        conversation_store.create_conversation()
    page_desc = conversation_store.list_conversations(order="desc")
    page_asc = conversation_store.list_conversations(order="asc")
    assert [c.id for c in page_asc.data] == list(reversed([c.id for c in page_desc.data]))


def test_list_conversations_asc_with_after_cursor(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    for _ in range(5):
        conversation_store.create_conversation()

    page1 = conversation_store.list_conversations(limit=2, order="asc")
    page2 = conversation_store.list_conversations(limit=2, order="asc", after=page1.last_id)
    page3 = conversation_store.list_conversations(limit=2, order="asc", after=page2.last_id)

    all_ids = [c.id for c in page1.data + page2.data + page3.data]
    full_asc = conversation_store.list_conversations(limit=100, order="asc")
    assert all_ids == [c.id for c in full_asc.data]


# ── list_items type filter ────────────────────────────


def test_list_items_type_filter_returns_only_matching_type(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    list_items(type=...) returns only items of the specified type,
    while list_items() without a filter returns all types.
    """
    from omnigent.entities import CompactionData

    conv = conversation_store.create_conversation()

    # Append a mix of message and compaction items
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            ),
        ],
    )
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="compaction",
                response_id="resp_001",
                data=CompactionData(
                    summary="Summary text",
                    last_item_id="msg_001",
                    model="openai/gpt-4o",
                    token_count=50,
                ),
            ),
        ],
    )
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_002",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "hello"}],
                    agent="test-agent",
                ),
            ),
        ],
    )

    compaction_items = conversation_store.list_items(conv.id, type="compaction")
    message_items = conversation_store.list_items(conv.id, type="message")
    all_items = conversation_store.list_items(conv.id)

    # Only the one compaction item must be returned.
    assert len(compaction_items.data) == 1, (
        f"Expected 1 compaction item, got {len(compaction_items.data)}. "
        "Failure means type filter did not exclude message items."
    )
    assert compaction_items.data[0].type == "compaction"

    # Only message items (2) must be returned.
    assert len(message_items.data) == 2, (
        f"Expected 2 message items, got {len(message_items.data)}. "
        "Failure means type filter did not exclude the compaction item."
    )
    assert all(i.type == "message" for i in message_items.data)

    # No filter returns all 3 items.
    assert len(all_items.data) == 3, (
        f"Expected 3 total items (2 message + 1 compaction), got {len(all_items.data)}."
    )


def test_list_items_type_filter_with_order_and_limit(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    list_items(type="compaction", order="desc", limit=1) returns only
    the most recently appended compaction item.
    """
    from omnigent.entities import CompactionData

    conv = conversation_store.create_conversation()

    # Append two compaction items
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="compaction",
                response_id="resp_001",
                data=CompactionData(
                    summary="First summary",
                    last_item_id="msg_010",
                    model="openai/gpt-4o",
                    token_count=100,
                ),
            ),
        ],
    )
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="compaction",
                response_id="resp_002",
                data=CompactionData(
                    summary="Second summary",
                    last_item_id="msg_020",
                    model="openai/gpt-4o",
                    token_count=120,
                ),
            ),
        ],
    )

    result = conversation_store.list_items(conv.id, type="compaction", order="desc", limit=1)

    # Only one item returned (limit=1).
    assert len(result.data) == 1, f"Expected 1 item with limit=1, got {len(result.data)}."
    # The most recent compaction item (second) should be returned (order=desc).
    assert result.data[0].data.summary == "Second summary", (
        f"Expected the latest compaction item with 'Second summary', "
        f"got: {result.data[0].data.summary!r}. "
        "Failure means order=desc with limit=1 did not return the newest item."
    )


# ── Sub-agent conversation isolation ────────────────


def test_subagent_conversations_are_isolated(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Two sub-agent conversations created independently must have
    fully isolated item sets. list_items on one must never return
    items belonging to the other.

    This is the foundational invariant that prevents sub-agent
    "pollution": each sub-agent writes to its own conversation,
    and the agent loop loads history via
    ``list_items(conversation_id)`` — so items from sibling
    sub-agents are structurally invisible.

    A failure here means the WHERE clause on ``conversation_id``
    in ``list_items`` is broken, which would cause sub-agents to
    see each other's messages and produce incoherent LLM prompts.
    """
    conv_a = conversation_store.create_conversation(kind="sub_agent")
    conv_b = conversation_store.create_conversation(kind="sub_agent")

    # Append distinct items to each conversation.
    conversation_store.append(
        conv_a.id,
        [
            NewConversationItem(
                type="message",
                response_id="task_a",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "alpha input"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="task_a",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "alpha output"}],
                    agent="researcher",
                ),
            ),
        ],
    )
    conversation_store.append(
        conv_b.id,
        [
            NewConversationItem(
                type="message",
                response_id="task_b",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "bravo input"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="task_b",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "bravo output"}],
                    agent="researcher",
                ),
            ),
        ],
    )

    # List items for conv_a — must contain only alpha items.
    page_a = conversation_store.list_items(conv_a.id)
    # 2 items: user + assistant for the alpha sub-agent.
    assert len(page_a.data) == 2, (
        f"Expected 2 items in conv_a, got {len(page_a.data)}. "
        "If > 2, items from conv_b leaked into conv_a's listing."
    )
    texts_a = [item.data.content[0]["text"] for item in page_a.data]
    assert texts_a == ["alpha input", "alpha output"], (
        f"Expected alpha items only in conv_a, got {texts_a}. "
        "If bravo items appear, the conversation_id filter is broken."
    )
    # Every item must carry the correct response_id.
    for item in page_a.data:
        assert item.response_id == "task_a", (
            f"Item {item.id} in conv_a has response_id {item.response_id!r}, expected 'task_a'."
        )

    # List items for conv_b — must contain only bravo items.
    page_b = conversation_store.list_items(conv_b.id)
    # 2 items: user + assistant for the bravo sub-agent.
    assert len(page_b.data) == 2, (
        f"Expected 2 items in conv_b, got {len(page_b.data)}. "
        "If > 2, items from conv_a leaked into conv_b's listing."
    )
    texts_b = [item.data.content[0]["text"] for item in page_b.data]
    assert texts_b == ["bravo input", "bravo output"], (
        f"Expected bravo items only in conv_b, got {texts_b}. "
        "If alpha items appear, the conversation_id filter is broken."
    )
    for item in page_b.data:
        assert item.response_id == "task_b", (
            f"Item {item.id} in conv_b has response_id {item.response_id!r}, expected 'task_b'."
        )

    # Cross-check: item IDs must be disjoint.
    ids_a = {item.id for item in page_a.data}
    ids_b = {item.id for item in page_b.data}
    assert ids_a.isdisjoint(ids_b), (
        f"Item IDs overlap between conversations: "
        f"{ids_a & ids_b}. Each conversation must have "
        "unique item IDs."
    )


# ── updated_at ─────────────────────────────────────────


def test_create_sets_updated_at_equal_to_created_at(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A newly created conversation has updated_at == created_at.
    """
    conv = conversation_store.create_conversation()
    assert conv.updated_at == conv.created_at, (
        f"Expected updated_at ({conv.updated_at}) to equal "
        f"created_at ({conv.created_at}) on a brand-new conversation."
    )


def test_append_bumps_updated_at(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Appending items to a conversation advances updated_at
    to the current time.
    """
    import omnigent.stores.conversation_store.sqlalchemy_store as store_mod

    # Freeze time at creation
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 1000)
    conv = conversation_store.create_conversation()
    assert conv.updated_at == 1000

    # Advance time, then append
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 2000)
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_bump",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hi"}],
                ),
            ),
        ],
    )
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.updated_at == 2000, (
        f"Expected updated_at to advance to 2000 after append, got {fetched.updated_at}."
    )


def test_update_title_bumps_updated_at(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Updating the title of a conversation advances updated_at.
    """
    import omnigent.stores.conversation_store.sqlalchemy_store as store_mod

    monkeypatch.setattr(store_mod, "now_epoch", lambda: 1000)
    conv = conversation_store.create_conversation()
    assert conv.updated_at == 1000

    monkeypatch.setattr(store_mod, "now_epoch", lambda: 3000)
    updated = conversation_store.update_conversation(conv.id, title="New title")
    assert updated is not None
    assert updated.updated_at == 3000, (
        f"Expected updated_at to advance to 3000 after title update, got {updated.updated_at}."
    )


# ── sort_by=updated_at ────────────────────────────────


def test_list_conversations_sort_by_updated_at(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Sorting by updated_at returns conversations in order of
    last activity, not creation order.
    """
    import omnigent.stores.conversation_store.sqlalchemy_store as store_mod

    # Create conv_a at t=100, conv_b at t=200
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 100)
    conv_a = conversation_store.create_conversation()
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 200)
    conv_b = conversation_store.create_conversation()

    # Append to conv_a at t=300, making it the most recently updated
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 300)
    conversation_store.append(
        conv_a.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_sort",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello"}],
                ),
            ),
        ],
    )

    # sort_by=created_at desc → conv_b first (created later)
    by_created = conversation_store.list_conversations(
        sort_by="created_at",
        order="desc",
        kind=None,
    )
    assert by_created.data[0].id == conv_b.id, (
        "Expected conv_b first when sorting by created_at desc."
    )

    # sort_by=updated_at desc → conv_a first (updated more recently)
    by_updated = conversation_store.list_conversations(
        sort_by="updated_at",
        order="desc",
        kind=None,
    )
    assert by_updated.data[0].id == conv_a.id, (
        "Expected conv_a first when sorting by updated_at desc, "
        "because it was updated at t=300 vs conv_b at t=200."
    )


def test_list_conversations_sort_by_updated_at_with_pagination(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cursor-based pagination works correctly when sorting
    by updated_at.
    """
    import omnigent.stores.conversation_store.sqlalchemy_store as store_mod

    # Create 3 conversations with distinct updated_at values
    ids = []
    for t in (100, 200, 300):
        monkeypatch.setattr(store_mod, "now_epoch", lambda _t=t: _t)
        conv = conversation_store.create_conversation()
        ids.append(conv.id)

    # Reverse the update order: bump the oldest conversation last
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 400)
    conversation_store.append(
        ids[0],
        [
            NewConversationItem(
                type="message",
                response_id="resp_pg",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "pg"}],
                ),
            ),
        ],
    )

    # sort_by=updated_at desc: ids[0] (400), ids[2] (300), ids[1] (200)
    page1 = conversation_store.list_conversations(
        limit=2,
        sort_by="updated_at",
        order="desc",
        kind=None,
    )
    # 2 results with has_more=True
    assert len(page1.data) == 2
    assert page1.has_more is True
    assert page1.data[0].id == ids[0]
    assert page1.data[1].id == ids[2]

    page2 = conversation_store.list_conversations(
        limit=2,
        sort_by="updated_at",
        order="desc",
        after=page1.last_id,
        kind=None,
    )
    # 1 result remaining
    assert len(page2.data) == 1
    assert page2.has_more is False
    assert page2.data[0].id == ids[1]


# ─── Phase 4: parent_conversation_id + name uniqueness ──────


def test_create_conversation_with_parent_pointer_and_title(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Setting ``parent_conversation_id`` + ``title`` round-trips through the row."""
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent",
        title="coder:auth",
        parent_conversation_id=parent.id,
    )
    # Both fields surface on the entity — proves the row was
    # populated AND the converter pulls the column. Without the
    # converter update, parent_conversation_id would always be
    # None on the returned entity even after the row stores it.
    fetched = conversation_store.get_conversation(child.id)
    assert fetched is not None
    assert fetched.title == "coder:auth"
    assert fetched.parent_conversation_id == parent.id
    assert fetched.kind == "sub_agent"


def test_create_duplicate_title_under_same_parent_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """G36: partial unique index rejects ``(parent_id, title)`` duplicates."""
    from omnigent.stores.conversation_store import NameAlreadyExistsError

    parent = conversation_store.create_conversation()
    conversation_store.create_conversation(
        kind="sub_agent",
        title="coder:auth",
        parent_conversation_id=parent.id,
    )
    # Without the partial unique index + IntegrityError-to-
    # NameAlreadyExistsError translation, the second create
    # would either succeed silently (creating a duplicate row)
    # or raise a raw sqlalchemy IntegrityError that would leak
    # through to the LLM as an opaque error.
    with pytest.raises(NameAlreadyExistsError):
        conversation_store.create_conversation(
            kind="sub_agent",
            title="coder:auth",
            parent_conversation_id=parent.id,
        )


def test_create_same_title_under_different_parents_succeeds(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The unique constraint is per-parent — ``(p1, "auth")`` and ``(p2, "auth")`` coexist."""
    p1 = conversation_store.create_conversation()
    p2 = conversation_store.create_conversation()
    conversation_store.create_conversation(
        kind="sub_agent", title="coder:auth", parent_conversation_id=p1.id
    )
    # Same title, different parent — no conflict.
    conversation_store.create_conversation(
        kind="sub_agent", title="coder:auth", parent_conversation_id=p2.id
    )
    # Both children must exist; if the unique constraint were
    # global (not partial-by-parent), the second create would
    # raise.
    p1_children = conversation_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=p1.id,
    )
    p2_children = conversation_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=p2.id,
    )
    assert len(p1_children.data) == 1
    assert len(p2_children.data) == 1
    assert p1_children.data[0].title == "coder:auth"
    assert p2_children.data[0].title == "coder:auth"


def test_create_null_parent_allows_duplicate_titles(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Top-level conversations (NULL parent) are NOT subject to the unique constraint."""
    # Both conversations share title=None and parent=None.
    # The partial index excludes NULL parents, so two NULL-NULL
    # rows are valid. Without the WHERE clause on the index,
    # this would raise.
    a = conversation_store.create_conversation()
    b = conversation_store.create_conversation()
    assert a.id != b.id
    assert a.parent_conversation_id is None
    assert b.parent_conversation_id is None


def test_list_conversations_filtered_by_parent_returns_children_only(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``parent_conversation_id`` filter scopes results to one parent's sub-tree."""
    parent_a = conversation_store.create_conversation()
    parent_b = conversation_store.create_conversation()
    conversation_store.create_conversation(
        kind="sub_agent", title="coder:auth", parent_conversation_id=parent_a.id
    )
    conversation_store.create_conversation(
        kind="sub_agent", title="coder:payments", parent_conversation_id=parent_a.id
    )
    conversation_store.create_conversation(
        kind="sub_agent", title="coder:other", parent_conversation_id=parent_b.id
    )

    page = conversation_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=parent_a.id,
    )
    # Exactly 2 children for parent_a — proves the WHERE clause
    # excludes parent_b's child. If the filter were a no-op,
    # all 3 sub-agent rows would appear.
    titles = sorted(c.title for c in page.data if c.title)
    assert titles == ["coder:auth", "coder:payments"]


def test_list_child_conversation_ids_by_parent_groups_direct_subagents(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``list_child_conversation_ids_by_parent`` groups direct sub-agent children.

    The sessions list uses this helper to roll child runner status onto
    parent rows without issuing one ``child_sessions`` query per sidebar
    row. This test proves the helper returns every requested parent key,
    excludes other parents and non-sub-agent children, and does not widen
    from direct children to nested descendants.
    """
    parent_a = conversation_store.create_conversation()
    parent_b = conversation_store.create_conversation()
    child_a1 = conversation_store.create_conversation(
        kind="sub_agent", title="coder:auth", parent_conversation_id=parent_a.id
    )
    child_a2 = conversation_store.create_conversation(
        kind="sub_agent", title="coder:payments", parent_conversation_id=parent_a.id
    )
    child_b = conversation_store.create_conversation(
        kind="sub_agent", title="coder:other", parent_conversation_id=parent_b.id
    )
    default_child = conversation_store.create_conversation(
        kind="default", title="default:child", parent_conversation_id=parent_a.id
    )
    nested = conversation_store.create_conversation(
        kind="sub_agent", title="reviewer:nested", parent_conversation_id=child_a1.id
    )

    result = conversation_store.list_child_conversation_ids_by_parent(
        [parent_a.id, parent_b.id, "conv_missing", parent_a.id]
    )

    assert set(result) == {parent_a.id, parent_b.id, "conv_missing"}
    assert len(result[parent_a.id]) == 2
    assert sorted(result[parent_a.id]) == sorted([child_a1.id, child_a2.id])
    assert result[parent_b.id] == [child_b.id]
    assert result["conv_missing"] == []
    assert default_child.id not in result[parent_a.id]
    assert nested.id not in result[parent_a.id]


# ── agent_id filter on list_conversations ──────────────


def test_list_conversations_filtered_by_agent_id_returns_matching_only(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """
    Powers Omnigent mode ``--continue`` (resume the most-recent
    conversation for *this agent*). Two agents, three
    conversations: agent_alpha owns convs 1+2, agent_beta
    owns conv 3. Filtering by agent_alpha returns exactly the
    two conversations agent_alpha touched.

    The filter uses ``conversations.agent_id`` directly
    (the tasks table has been removed).

    What breaks if this fails: ``--continue`` either resumes a
    conversation belonging to the wrong agent (privacy /
    correctness regression) or returns nothing when prior
    history exists.

    :param conversation_store: The conversation store fixture.
    :param agent_store: The agent store fixture.
    """
    alpha = agent_store.create(agent_id="ag_alpha", name="alpha", bundle_location="ag_alpha/h")
    beta = agent_store.create(agent_id="ag_beta", name="beta", bundle_location="ag_beta/h")
    conv1 = conversation_store.create_conversation(agent_id=alpha.id)
    conv2 = conversation_store.create_conversation(agent_id=alpha.id)
    conv3 = conversation_store.create_conversation(agent_id=beta.id)
    _ = conv3  # ensure conv3 exists but is not returned

    page = conversation_store.list_conversations(agent_id=alpha.id)
    returned_ids = {c.id for c in page.data}
    # Exactly two conversations, both owned by alpha — beta's
    # conversation must be excluded.
    assert returned_ids == {conv1.id, conv2.id}


def test_list_conversations_agent_id_none_disables_filter(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """
    The default (``agent_id=None``) returns every conversation,
    including ones without an agent binding. Pinning this so the filter
    stays opt-in and existing callers (the ``/switch`` slash
    command, list endpoint without filter) keep their
    cross-agent visibility.

    :param conversation_store: The conversation store fixture.
    :param agent_store: The agent store fixture.
    """
    alpha = agent_store.create(agent_id="ag_alpha2", name="alpha2", bundle_location="ag_alpha2/h")
    conv_with_agent = conversation_store.create_conversation(agent_id=alpha.id)
    conv_without_agent = conversation_store.create_conversation()

    page = conversation_store.list_conversations()
    returned_ids = {c.id for c in page.data}
    # Both conversations present — no agent_id filter applied.
    assert conv_with_agent.id in returned_ids
    assert conv_without_agent.id in returned_ids


def test_list_conversations_filter_distinct_by_agent_id(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """
    A conversation bound to an agent appears exactly once in the
    result when filtered by that agent's id.

    The filter now uses ``conversations.agent_id`` directly
    (the tasks table has been removed) — each conversation
    appears at most once by definition.

    :param conversation_store: The conversation store fixture.
    :param agent_store: The agent store fixture.
    """
    alpha = agent_store.create(agent_id="ag_alpha3", name="alpha3", bundle_location="ag_alpha3/h")
    conv = conversation_store.create_conversation(agent_id=alpha.id)

    page = conversation_store.list_conversations(agent_id=alpha.id)
    assert [c.id for c in page.data] == [conv.id]


def test_list_conversations_filter_orders_by_sort_by(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``agent_id`` AND ``sort_by="updated_at"``, the result
    is ordered by the conversation's updated_at. This is what
    ``--continue`` relies on: "the conversation I most recently
    *did anything in*", which is reflected in the conversation's
    own ``updated_at`` (bumped on every append).

    The filter uses ``conversations.agent_id`` directly
    (the tasks table has been removed).

    :param conversation_store: The conversation store fixture.
    :param agent_store: The agent store fixture.
    :param monkeypatch: Pytest monkeypatch for time control.
    """
    import omnigent.stores.conversation_store.sqlalchemy_store as store_mod

    alpha = agent_store.create(agent_id="ag_alpha4", name="alpha4", bundle_location="ag_alpha4/h")

    # Create two conversations at distinct timestamps so
    # ``updated_at`` differs.
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 100)
    older = conversation_store.create_conversation(agent_id=alpha.id)
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 200)
    newer = conversation_store.create_conversation(agent_id=alpha.id)
    _ = newer  # both created; newer has later created_at

    # Bump older's updated_at so it becomes the most recently updated.
    monkeypatch.setattr(store_mod, "now_epoch", lambda: 300)
    conversation_store.append(
        older.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_recency",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "bump"}],
                ),
            ),
        ],
    )

    page = conversation_store.list_conversations(
        agent_id=alpha.id,
        sort_by="updated_at",
        order="desc",
        limit=1,
    )
    # ``older`` is now the most-recently-updated conversation
    # despite being created earlier.
    assert [c.id for c in page.data] == [older.id]


def test_cascade_delete_removes_descendants(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Deleting a parent recursively removes children + grandchildren (FK CASCADE)."""
    import asyncio

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", title="coder:auth", parent_conversation_id=parent.id
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", title="reviewer:nested", parent_conversation_id=child.id
    )
    # Delete the root — both descendants must vanish via the
    # ON DELETE CASCADE on parent_conversation_id.
    asyncio.run(conversation_store.delete_conversation(parent.id))
    assert conversation_store.get_conversation(parent.id) is None
    assert conversation_store.get_conversation(child.id) is None, (
        "Child not cascaded — FK ondelete=CASCADE missing or migration didn't apply it"
    )
    assert conversation_store.get_conversation(grandchild.id) is None, (
        "Grandchild not cascaded — recursive FK cascade missing"
    )


# ── Runner pinning (designs/RUNNER.md §5) ─────────────


def test_runner_id_default_null(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Fresh conversations have no runner pin until first dispatch claims one."""
    conv = conversation_store.create_conversation()
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    # ``runner_id`` is the load-bearing assertion — proves the column flowed
    # all the way from the DB row to the entity. A non-None default would
    # mean the entity dataclass is masking the SQL NULL.
    assert fetched.runner_id is None


def test_replace_runner_id_allows_internal_non_session_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Internal sub-agent conversations can inherit runner bindings."""
    conv = conversation_store.create_conversation()

    updated = conversation_store.replace_runner_id(conv.id, "runner-uuid-1")

    assert updated.runner_id == "runner-uuid-1"
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.runner_id == "runner-uuid-1"


def test_list_conversations_by_runner_id_filters(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only conversations pinned to the queried runner are returned.

    The runner tunnel's connect/disconnect callbacks use this lookup to
    find the sessions whose ``create_session`` handshake (and the
    claude-native terminal bootstrap behind it) must be driven on
    reconnect — a wrong or empty result here means the terminal is
    silently never created.
    """
    bound = conversation_store.create_conversation(runner_id="runner_token_a")
    conversation_store.create_conversation(runner_id="runner_token_b")
    conversation_store.create_conversation()  # unbound

    result = conversation_store.list_conversations_by_runner_id("runner_token_a")

    # Exactly the bound conversation — proves the filter matched on the
    # column rather than returning a superset the caller must re-filter.
    assert [c.id for c in result] == [bound.id]
    assert result[0].runner_id == "runner_token_a"


# ── Host id ─────────────────────────────────────────


def _register_host(db_uri: str, host_id: str) -> None:
    """
    Insert a ``hosts`` row so a conversation can reference ``host_id``.

    ``conversations.host_id`` is an FK to ``hosts.host_id`` (enforced
    under ``PRAGMA foreign_keys=ON``), so the referenced host must
    exist before a conversation can be bound to it. Shares ``db_uri``
    with the ``conversation_store`` fixture so both hit the same DB.

    :param db_uri: SQLite URI shared with the conversation store.
    :param host_id: Host identifier to register, e.g. ``"host_abc123"``.
    """
    HostStore(db_uri).upsert_on_connect(host_id, f"laptop-{host_id}", RESERVED_USER_LOCAL)


def test_host_id_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A freshly created conversation has ``host_id=None``.

    If not None, the entity dataclass or the row→entity converter
    is fabricating a default instead of reflecting the SQL NULL.
    """
    conv = conversation_store.create_conversation()
    assert conv.host_id is None
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.host_id is None


def test_create_conversation_with_host_id(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    Verify that host_id passed to create_conversation is persisted
    and survives a round-trip through the DB.

    Pass ``workspace`` alongside ``host_id`` because the schema's
    ``ck_conversations_workspace_required_for_host`` constraint
    forbids the (host_id NOT NULL, workspace NULL) combination —
    sessions targeting a host always need a path to launch in.
    If the fetched host_id doesn't match, either the INSERT is
    dropping the column or the row→entity converter is skipping it.
    """
    # host_id is an FK to hosts.host_id, so the host must exist first.
    _register_host(db_uri, "host_abc123")
    conv = conversation_store.create_conversation(
        host_id="host_abc123",
        workspace="/Users/corey/projects/myapp",
    )
    assert conv.host_id == "host_abc123"
    assert conv.workspace == "/Users/corey/projects/myapp"

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.host_id == "host_abc123"
    assert fetched.workspace == "/Users/corey/projects/myapp"


def test_create_conversation_with_git_branch(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify git_branch passed to create_conversation persists and
    round-trips through the DB.

    If the fetched git_branch doesn't match, either the INSERT drops
    the column (bad migration / model) or the row→entity converter
    skips it — both would break the delete-dialog cleanup gate, which
    keys off ``git_branch IS NOT NULL``.
    """
    conv = conversation_store.create_conversation(git_branch="feature/login")
    assert conv.git_branch == "feature/login"
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.git_branch == "feature/login"


def test_create_conversation_git_branch_defaults_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify git_branch defaults to None for sessions with no worktree.

    A non-None default would make every session look like a worktree
    session and wrongly surface the "delete local branch" checkbox.
    """
    conv = conversation_store.create_conversation()
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.git_branch is None


def test_set_host_id(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    Verify that set_host_id updates the column and persists.

    The conversation is created with ``workspace`` already set so
    that the post-update row satisfies
    ``ck_conversations_workspace_required_for_host``. Without a
    pre-existing workspace, the UPDATE that introduces ``host_id``
    would be blocked by the check constraint.

    If the fetched value is still None after set_host_id, the UPDATE
    statement is not executing or not committing.
    """
    # host_id is an FK to hosts.host_id, so the host must exist first.
    _register_host(db_uri, "host_def456")
    # Pre-set workspace so the host_id update doesn't violate the
    # workspace-required-for-host check constraint.
    conv = conversation_store.create_conversation(
        workspace="/Users/corey/projects/myapp",
    )
    assert conv.host_id is None
    assert conv.workspace == "/Users/corey/projects/myapp"

    updated = conversation_store.set_host_id(conv.id, "host_def456")
    assert updated.host_id == "host_def456"

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.host_id == "host_def456"
    assert fetched.workspace == "/Users/corey/projects/myapp"


def test_set_host_id_missing_conversation_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify that set_host_id raises ConversationNotFoundError for
    a nonexistent conversation.

    If it silently succeeds, the guard clause is missing and a
    stale host_id could be written to a phantom row.
    """
    from omnigent.stores.conversation_store import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        conversation_store.set_host_id("conv_nonexistent", "host_xyz")


def test_set_host_id_with_workspace_satisfies_constraint(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    Verify set_host_id(host_id, workspace) writes both columns so
    the row satisfies ck_conversations_workspace_required_for_host.

    The launch-runner endpoint binds host_id post-create on rows
    that may have NULL workspace; without the workspace argument,
    the UPDATE would violate the check constraint and 500 the
    request.
    """
    # host_id is an FK to hosts.host_id, so the host must exist first.
    _register_host(db_uri, "host_with_workspace")
    conv = conversation_store.create_conversation()
    assert conv.host_id is None
    assert conv.workspace is None

    updated = conversation_store.set_host_id(
        conv.id,
        "host_with_workspace",
        workspace="/Users/corey/projects/myapp",
    )
    assert updated.host_id == "host_with_workspace"
    assert updated.workspace == "/Users/corey/projects/myapp"


def test_clear_host_binding_nulls_all_binding_fields(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    clear_host_binding NULLs host_id/workspace/git_branch/runner_id together.

    This is the failed-bind rollback primitive: after a worktree launch
    fails and the worktree is removed, the session must not keep pointing
    at the deleted worktree/branch or stay runner-bound. Unlike set_host_id
    (None = leave untouched, so it can't clear git_branch), this fully
    reverts to unbound. A leftover git_branch here would wrongly satisfy
    worktree-cleanup paths (git_branch IS NOT NULL); a leftover runner_id
    would block the picker's retry on the atomic set_runner_id CAS.
    """
    _register_host(db_uri, "host_to_unbind")
    conv = conversation_store.create_conversation()
    # Fully bind it the way a worktree launch would: host + worktree path
    # + branch, then a runner.
    conversation_store.set_host_id(
        conv.id,
        "host_to_unbind",
        workspace="/Users/corey/repo-worktrees/feature-x",
        git_branch="feature/x",
    )
    assert conversation_store.set_runner_id(conv.id, "runner_token_abc") is True
    bound = conversation_store.get_conversation(conv.id)
    assert bound is not None
    # Precondition: every binding field is set (so the assertions below
    # prove clearing, not a no-op on already-empty fields).
    assert bound.host_id == "host_to_unbind"
    assert bound.workspace == "/Users/corey/repo-worktrees/feature-x"
    assert bound.git_branch == "feature/x"
    assert bound.runner_id == "runner_token_abc"

    cleared = conversation_store.clear_host_binding(conv.id)
    assert cleared.host_id is None
    assert cleared.workspace is None
    assert cleared.git_branch is None
    assert cleared.runner_id is None
    # Persisted, not just returned: a re-fetch must agree.
    refetched = conversation_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.host_id is None
    assert refetched.workspace is None
    assert refetched.git_branch is None
    assert refetched.runner_id is None
    # And the session can be re-bound (the CAS sees runner_id IS NULL).
    assert conversation_store.set_runner_id(conv.id, "runner_token_retry") is True


def test_clear_host_binding_missing_conversation_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """clear_host_binding raises for an unknown conversation id."""
    from omnigent.stores.conversation_store import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        conversation_store.clear_host_binding("conv_nonexistent")


def test_create_session_with_agent_records_workspace(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_session_with_agent stores workspace=<value> on the
    conversation row.

    The CLI calls this path with ``workspace=os.getcwd()`` so the
    Web UI can show "running locally in <workspace>" for sessions
    started outside the Web UI. If the column isn't populated, the
    Web UI shows "(no workspace)" and the user can't tell where the
    session is running.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_ws",
        agent_name="cli-test-agent",
        agent_bundle_location="ag_session_ws/bundle1",
        agent_description=None,
        workspace="/Users/corey/projects/cli-launch",
    )

    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    assert fetched.workspace == "/Users/corey/projects/cli-launch"
    assert fetched.host_id is None


def test_create_session_with_agent_workspace_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_session_with_agent leaves workspace NULL when no
    value is passed.

    Headless API callers (no host, no terminal cwd) shouldn't be
    forced to invent a workspace. The check constraint allows
    NULL workspace when host_id is also NULL — this test pins
    that path through the public method.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_no_ws",
        agent_name="no-ws-agent",
        agent_bundle_location="ag_session_no_ws/bundle1",
        agent_description=None,
    )
    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    assert fetched.workspace is None


def test_create_session_with_agent_records_terminal_launch_args(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_session_with_agent persists terminal_launch_args as
    a JSON list that round-trips back to a real ``list[str]``.

    The native wrappers set these at create-time so a daemon- or
    server-launched runner can reconstruct the terminal command. If
    the JSON encode (row builder) or decode (converter) is broken,
    the fetched value would be a raw JSON string or ``None`` instead
    of the list, and the runner would launch with the wrong args.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_tla",
        agent_name="cli-test-agent",
        agent_bundle_location="ag_session_tla/bundle1",
        agent_description=None,
        terminal_launch_args=["--dangerously-skip-permissions", "--model", "opus"],
    )

    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    # Exact list (order + content) proves the value traversed
    # encode→DB→decode intact, not just "something non-null".
    assert fetched.terminal_launch_args == [
        "--dangerously-skip-permissions",
        "--model",
        "opus",
    ]


def test_create_session_with_agent_terminal_launch_args_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_session_with_agent leaves terminal_launch_args NULL
    when no value is passed.

    Non-native sessions (the common case) must not carry launch args.
    A NULL column must decode to ``None`` — not ``[]`` or ``"null"`` —
    so downstream code can distinguish "no native launch" from "native
    launch with zero extra args".
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_no_tla",
        agent_name="no-tla-agent",
        agent_bundle_location="ag_session_no_tla/bundle1",
        agent_description=None,
    )
    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    assert fetched.terminal_launch_args is None


def test_create_session_with_agent_links_parent_and_inherits_root(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_session_with_agent with parent_conversation_id creates
    a sub-agent child that inherits the parent's spawn-tree root and
    runner binding.

    This is the bundle-mode ``sys_session_create`` path: the multipart
    create must produce the same parent/kind/root/runner shape as the
    JSON child create, or tree-scoped reads (``sys_session_get_history``
    matches on ``root_conversation_id``) and runner co-location would
    silently break for bundle-created children.
    """
    parent = conversation_store.create_conversation(runner_id="runner_swa1")
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_child",
        agent_name="bundle-child-agent",
        agent_bundle_location="ag_session_child/bundle1",
        agent_description=None,
        parent_conversation_id=parent.id,
        runner_id=parent.runner_id,
    )

    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    assert fetched.parent_conversation_id == parent.id
    # Child rows are sub-agent kind — sys_session_list filters on this.
    assert fetched.kind == "sub_agent"
    # Root must be the PARENT's root (== parent.id for a top-level
    # parent), not the child's own id — otherwise the child lands in
    # its own one-row tree and tree-scoped tools can't see it.
    assert fetched.root_conversation_id == parent.root_conversation_id
    assert fetched.runner_id == "runner_swa1"


def test_create_session_with_agent_top_level_unchanged(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify the no-parent path still creates a top-level default row.

    The parent support must not disturb the existing multipart create
    contract (CLI ``omnigent run`` sessions): kind stays "default",
    no parent link, and the row roots its own tree.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_top",
        agent_name="top-level-agent",
        agent_bundle_location="ag_session_top/bundle1",
        agent_description=None,
    )
    fetched = conversation_store.get_conversation(created.conversation.id)
    assert fetched is not None
    assert fetched.parent_conversation_id is None
    assert fetched.kind == "default"
    assert fetched.root_conversation_id == created.conversation.id
    assert fetched.runner_id is None


def test_create_session_with_agent_missing_parent_fails_loud(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify a nonexistent parent_conversation_id raises
    ConversationNotFoundError instead of silently creating an orphan.

    The route authorizes the parent before calling the store, but the
    parent can be deleted between the check and the insert — the store
    must fail loud so no half-linked child row (and no orphaned agent
    row) is committed.
    """
    from omnigent.stores.conversation_store import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        conversation_store.create_session_with_agent(
            agent_id="ag_session_orphan",
            agent_name="orphan-agent",
            agent_bundle_location="ag_session_orphan/bundle1",
            agent_description=None,
            parent_conversation_id="conv_does_not_exist",
        )
    # The transaction rolled back: no agent row leaked either.
    assert conversation_store.get_conversation("conv_does_not_exist") is None


def test_create_conversation_records_terminal_launch_args(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_conversation persists terminal_launch_args as a JSON
    list that round-trips back to a real ``list[str]``.

    This is the JSON ``POST /v1/sessions`` path (web permission-mode
    selector), distinct from create_session_with_agent (multipart). If
    the row builder skipped the JSON encode or the converter the decode,
    the fetched value would be ``None`` or a raw string and the runner
    would launch claude without the chosen --permission-mode.
    """
    created = conversation_store.create_conversation(
        terminal_launch_args=["--permission-mode", "bypassPermissions"],
    )
    fetched = conversation_store.get_conversation(created.id)
    assert fetched is not None
    # Exact list (order preserved) proves the value traversed
    # encode→DB→decode intact.
    assert fetched.terminal_launch_args == ["--permission-mode", "bypassPermissions"]


def test_create_conversation_terminal_launch_args_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify create_conversation leaves terminal_launch_args NULL when no
    value is passed.

    Non-native / auto-mode-off sessions (the common case) must not
    carry launch args; a NULL column must decode to ``None``, not
    ``[]``, so the runner distinguishes "no native launch" from
    "native launch with zero extra args".
    """
    created = conversation_store.create_conversation()
    fetched = conversation_store.get_conversation(created.id)
    assert fetched is not None
    assert fetched.terminal_launch_args is None


def test_update_conversation_replaces_terminal_launch_args(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify update_conversation replaces terminal_launch_args wholesale
    (last-write-wins) and that ``None`` leaves the stored value
    unchanged.

    This pins the resume semantics from
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md: a cold resume with new
    flags overwrites the prior set rather than appending to it. If the
    store appended instead of replacing, the second assertion would
    see the concatenation and fail — which is exactly the bug that
    would make repeated resumes accumulate stale/conflicting flags.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_tla_update",
        agent_name="update-agent",
        agent_bundle_location="ag_session_tla_update/bundle1",
        agent_description=None,
        terminal_launch_args=["--model", "opus"],
    )
    conv_id = created.conversation.id

    replaced = conversation_store.update_conversation(
        conv_id,
        terminal_launch_args=["--verbose"],
    )
    assert replaced is not None
    # Replaced, NOT appended: ["--model", "opus", "--verbose"] would
    # mean the store concatenated across launches (the bug).
    assert replaced.terminal_launch_args == ["--verbose"]

    # None must leave the prior value intact (matches the
    # reasoning_effort / model_override "None = unchanged" contract).
    unchanged = conversation_store.update_conversation(conv_id, title="renamed")
    assert unchanged is not None
    assert unchanged.terminal_launch_args == ["--verbose"]


def test_update_conversation_terminal_launch_args_empty_list_distinct_from_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify an explicitly-empty arg list round-trips as ``[]`` and stays
    distinct from the NULL/None "leave unchanged" sentinel.

    The store uses ``None`` to mean "leave unchanged", so an empty
    list must be storable as a real, retrievable ``[]`` — otherwise a
    caller clearing args back to none-extra would be silently ignored.
    """
    created = conversation_store.create_session_with_agent(
        agent_id="ag_session_tla_empty",
        agent_name="empty-agent",
        agent_bundle_location="ag_session_tla_empty/bundle1",
        agent_description=None,
        terminal_launch_args=["--model", "opus"],
    )
    updated = conversation_store.update_conversation(
        created.conversation.id,
        terminal_launch_args=[],
    )
    assert updated is not None
    # "[]" is a truthy JSON string, so the converter must decode it to
    # an empty list — not collapse it to None the way a NULL column does.
    assert updated.terminal_launch_args == []


def test_set_host_id_no_workspace_fails_when_row_has_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Verify that calling set_host_id without a workspace argument
    on a row whose workspace is NULL raises IntegrityError.

    Pins the contract that set_host_id can't bypass the check
    constraint by accident. Callers must pass a workspace when
    binding to a host on a row that doesn't already have one.
    """
    from sqlalchemy.exc import IntegrityError

    conv = conversation_store.create_conversation()
    assert conv.workspace is None

    with pytest.raises(IntegrityError):
        conversation_store.set_host_id(conv.id, "host_no_ws")


# ── Workspace ───────────────────────────────────────


def test_workspace_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A freshly created conversation has ``workspace=None`` when no
    workspace is passed.

    Load-bearing because the entity must mirror the DB's NULL state
    rather than substituting an empty string — empty-string defaults
    on path columns mask the "never set" case and would launch the
    runner in the host daemon's process cwd, which is rarely correct.
    """
    conv = conversation_store.create_conversation()
    assert conv.workspace is None
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.workspace is None


def test_workspace_persists_for_cli_session_without_host_id(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A CLI session can record its starting cwd without a host_id.

    Pairs with the schema-level test in ``tests/db/test_migration_workspace.py``
    that proves the check constraint is one-way (host_id requires
    workspace, not the reverse). This test exercises the same path
    through the store interface to catch regressions where the store
    layer might add a stricter rule than the schema enforces.
    """
    conv = conversation_store.create_conversation(
        workspace="/Users/corey/projects/cli-launched",
    )
    assert conv.host_id is None
    assert conv.workspace == "/Users/corey/projects/cli-launched"

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.host_id is None
    assert fetched.workspace == "/Users/corey/projects/cli-launched"


def test_create_conversation_with_host_id_no_workspace_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Creating a conversation with host_id but no workspace raises
    IntegrityError from the check constraint.

    The store deliberately does NOT add a Python-side guard here:
    the check constraint is the canonical enforcement, and adding a
    duplicate guard at the store would diverge from the schema in
    confusing ways. This test pins the contract: violating the
    constraint surfaces as IntegrityError, callers can catch it,
    and the row is never written.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        conversation_store.create_conversation(host_id="host_abc")


# ── External session id ─────────────────────────────


def test_external_session_id_defaults_to_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A freshly created conversation has ``external_session_id=None``.

    Load-bearing because a non-None default would mean the entity
    dataclass is masking the SQL NULL — the wrapper bridge wouldn't
    be able to tell "not yet observed" from "set to empty".
    """
    conv = conversation_store.create_conversation()
    assert conv.external_session_id is None
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    # Read-back through get_conversation goes through the row→entity
    # converter; this proves the column flows from DB row to entity.
    assert fetched.external_session_id is None


def test_set_external_session_id_first_call_persists(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """First write transitions NULL → value and is visible on read-back."""
    conv = conversation_store.create_conversation()
    updated = conversation_store.set_external_session_id(
        conv.id,
        "a1b2c3d4-1234-5678-9abc-def012345678",
    )
    # The returned entity reflects the write — the route's
    # response builder reads this snapshot rather than issuing a
    # follow-up GET.
    assert updated.external_session_id == "a1b2c3d4-1234-5678-9abc-def012345678"
    # Independent read-back proves the column was actually committed
    # (not just held in the returned dataclass).
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.external_session_id == "a1b2c3d4-1234-5678-9abc-def012345678"


def test_set_external_session_id_same_value_is_idempotent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Re-writing the same value is a no-op and does not raise.

    The wrapper bridge may observe the Claude session id across many
    hook events; the latch in the forwarder limits this to one PATCH
    per process, but a second forwarder process for the same conv
    (server bounce, hot reload) would race-write the same value. The
    store must accept that without surfacing a spurious error.
    """
    conv = conversation_store.create_conversation()
    first = conversation_store.set_external_session_id(conv.id, "sid-1")
    second = conversation_store.set_external_session_id(conv.id, "sid-1")
    assert first.external_session_id == "sid-1"
    assert second.external_session_id == "sid-1"
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.external_session_id == "sid-1"


def test_set_external_session_id_rejects_overwrite_with_different_value(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Attempting to overwrite an existing value raises ValueError.

    A divergent write signals a real bug (the bridge captured a
    different Claude session id than was previously recorded). The
    store surfaces it loudly so the route can return 400 instead of
    silently destroying the prior mapping.
    """
    conv = conversation_store.create_conversation()
    conversation_store.set_external_session_id(conv.id, "sid-1")
    with pytest.raises(ValueError, match="already has external_session_id"):
        conversation_store.set_external_session_id(conv.id, "sid-2")
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    # First-writer-wins — the rejected second call must not have
    # mutated the row. If this assertion fails, the store silently
    # overwrote on conflict (the very bug the ValueError exists to
    # prevent).
    assert fetched.external_session_id == "sid-1"


def test_set_external_session_id_missing_conversation_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Writing to a nonexistent conversation raises ConversationNotFoundError.

    Mirrors replace_runner_id / clear_runner_id — the public PATCH
    routes translate this into a 404, so silently no-oping here would
    let the route return 200 for a write that never happened.
    """
    from omnigent.stores.conversation_store import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        conversation_store.set_external_session_id(
            "conv_does_not_exist",
            "sid-1",
        )


# ── Fork conversation ────────────────────────────────


def test_fork_conversation_copies_items(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Fork creates a new conversation with deep-copied items.

    Items in the fork must have fresh IDs but identical data. The
    source conversation must be untouched — no items removed or
    mutated.
    """
    agent_store.create(
        agent_id="ag_fork_test",
        name="fork-test",
        bundle_location="ag_fork_test/fakehash",
    )
    source = conversation_store.create_conversation(
        agent_id="ag_fork_test",
        title="Original",
    )
    conversation_store.append(
        source.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Hello"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Hi there!"}],
                    agent="test-agent",
                ),
            ),
        ],
    )

    fork = conversation_store.fork_conversation(source.id, title="My Fork")

    # The fork is a new conversation with a different ID.
    assert fork.id != source.id
    assert fork.id.startswith("conv_")
    assert fork.title == "My Fork"
    # Agent binding is copied from the source.
    assert fork.agent_id == "ag_fork_test"

    # Items are deep-copied — same count, different IDs, same data.
    fork_items = conversation_store.list_items(fork.id)
    source_items = conversation_store.list_items(source.id)
    # Both conversations have 2 items.
    assert len(fork_items.data) == 2, (
        f"Fork should have 2 items (same as source), got {len(fork_items.data)}"
    )
    assert len(source_items.data) == 2, (
        f"Source should still have 2 items after fork, got {len(source_items.data)}"
    )

    for src_item, fork_item in zip(source_items.data, fork_items.data, strict=True):
        # IDs must differ (fresh generation).
        assert fork_item.id != src_item.id, "Fork item IDs must be fresh, not reused from source"
        # Type and response_id are preserved.
        assert fork_item.type == src_item.type
        assert fork_item.response_id == src_item.response_id
        # Data content is identical.
        assert fork_item.data == src_item.data


def test_fork_conversation_preserves_created_by(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Forking carries per-item actor attribution into the fork.

    Attribution history travels with the items; cloning the
    conversation does not blank out who authored each message.
    """
    agent_store.create(
        agent_id="ag_fork_attr",
        name="fork-attr",
        bundle_location="ag_fork_attr/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_attr")
    conversation_store.append(
        source.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Hello"}],
                ),
                created_by="alice@example.com",
            ),
            NewConversationItem(
                type="message",
                response_id="resp_001",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Hi!"}],
                    agent="test-agent",
                ),
            ),
        ],
    )

    fork = conversation_store.fork_conversation(source.id)
    fork_items = conversation_store.list_items(fork.id).data

    assert fork_items[0].created_by == "alice@example.com"
    assert fork_items[1].created_by is None


def test_fork_conversation_default_title(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """When no title is given, fork derives one from the source title."""
    agent_store.create(
        agent_id="ag_fork_title",
        name="fork-title",
        bundle_location="ag_fork_title/fakehash",
    )
    source = conversation_store.create_conversation(
        agent_id="ag_fork_title",
        title="Chat about Python",
    )
    fork = conversation_store.fork_conversation(source.id)

    assert fork.title == "Fork of Chat about Python"


def test_fork_conversation_empty_source(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Forking a conversation with no items produces an empty fork."""
    agent_store.create(
        agent_id="ag_fork_empty",
        name="fork-empty",
        bundle_location="ag_fork_empty/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_empty")
    fork = conversation_store.fork_conversation(source.id)

    fork_items = conversation_store.list_items(fork.id)
    assert fork_items.data == [], "Fork of an empty conversation should have no items"
    assert fork.agent_id == "ag_fork_empty"


def test_fork_conversation_nonexistent_raises(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Forking a non-existent conversation raises LookupError."""
    with pytest.raises(LookupError, match="conversation not found"):
        conversation_store.fork_conversation("conv_does_not_exist")


def test_fork_conversation_copies_labels(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Labels on the source conversation are copied to the fork."""
    agent_store.create(
        agent_id="ag_fork_labels",
        name="fork-labels",
        bundle_location="ag_fork_labels/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_labels")
    conversation_store.set_labels(source.id, {"sensitivity": "high", "dept": "eng"})

    fork = conversation_store.fork_conversation(source.id)
    # Both labels must be copied — a mismatch means the store's fork
    # skipped the label-copy step or only copied partial keys.
    assert fork.labels == {"sensitivity": "high", "dept": "eng"}, (
        "Fork should inherit all labels from the source conversation"
    )


def test_fork_conversation_drops_instance_scoped_labels(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """
    Instance-scoped labels are NOT copied to the fork.

    The native bridge-id labels and the context metrics belong to the
    source's running instance. Copying the bridge-id in particular
    pointed a forked claude-native session at the SOURCE's bridge — the
    launched terminal + web injection keyed off it and hit "session no
    longer active after /clear" because the bridge's active-session
    marker wasn't the clone. The fork must drop them (and re-bind its
    own runtime), while ordinary labels still copy.

    The DANGEROUS codex full-bypass directive is in the same set for a
    different reason: a fork is a new session + workspace, so re-arming
    ``--dangerously-bypass-approvals-and-sandbox`` there with no typed
    re-confirmation would violate the "impossible to enable accidentally"
    contract (#657). It must be dropped so the clone opts in afresh.
    """
    agent_store.create(
        agent_id="ag_fork_instance",
        name="fork-instance",
        bundle_location="ag_fork_instance/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_instance")
    conversation_store.set_labels(
        source.id,
        {
            "omnigent.claude_native.bridge_id": source.id,
            "omnigent.codex_native.bridge_id": source.id,
            "omnigent.last_context_tokens": "39903",
            "omnigent.last_context_window": "1000000",
            # The dangerous bypass opt-in must NOT ride into the fork.
            "omnigent.codex_native.bypass_sandbox": "1",
            # An ordinary, non-instance label that SHOULD carry over.
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )

    fork = conversation_store.fork_conversation(source.id)

    # The clone keeps the harness identity (wrapper) but none of the
    # source's per-instance state. A bridge-id here would re-introduce
    # the cross-bridge bug; the metrics would show the source's stale
    # usage.
    assert fork.labels == {"omnigent.wrapper": "claude-code-native-ui"}, (
        f"Fork must drop instance-scoped labels, kept {fork.labels!r}"
    )


def test_fork_conversation_stamps_source_external_session_id(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """
    The source's native session id is stamped on the fork as a one-shot
    resume directive, while the clone's own ``external_session_id`` stays
    NULL.

    A native harness launching the clone uses
    ``omnigent.fork.source_external_session_id`` to resume + branch the
    source's local transcript (Claude Code ``--fork-session``), so the
    clone opens with prior history. The clone is NOT that session, so its
    own ``external_session_id`` must remain unset until it captures its
    own on first launch. Sources without a native session get no label.
    """
    agent_store.create(
        agent_id="ag_fork_ext",
        name="fork-ext",
        bundle_location="ag_fork_ext/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_ext")
    conversation_store.set_external_session_id(source.id, "claude-uuid-abc")

    fork = conversation_store.fork_conversation(source.id)

    # Directive carries the SOURCE's claude uuid for the resume+fork launch.
    assert fork.labels.get("omnigent.fork.source_external_session_id") == "claude-uuid-abc", (
        f"Fork should carry the source's external session id, got {fork.labels!r}"
    )
    # The clone is a fresh session — it has no native session of its own
    # yet. Copying external_session_id would make two Omnigent sessions claim
    # the same Claude session.
    reloaded = conversation_store.get_conversation(fork.id)
    assert reloaded is not None
    assert reloaded.external_session_id is None, (
        "Fork must not inherit the source's external_session_id"
    )


def test_fork_conversation_no_external_session_id_no_directive(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A source with no native session id stamps no fork directive."""
    agent_store.create(
        agent_id="ag_fork_noext",
        name="fork-noext",
        bundle_location="ag_fork_noext/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_noext")

    fork = conversation_store.fork_conversation(source.id)

    assert "omnigent.fork.source_external_session_id" not in fork.labels, (
        f"No source native session → no resume directive, got {fork.labels!r}"
    )


def _append_three_responses(
    conversation_store: SqlAlchemyConversationStore,
    conversation_id: str,
) -> None:
    """
    Append three user/assistant turns under distinct response ids.

    Builds the fixture history for the fork-truncation tests: response
    ``resp_001`` ("Q1"/"A1"), ``resp_002`` ("Q2"/"A2"), and
    ``resp_003`` ("Q3"/"A3"), six items total in chronological order.

    :param conversation_store: Store to append into.
    :param conversation_id: Target conversation id, e.g.
        ``"conv_abc123"``.
    """
    for index in (1, 2, 3):
        conversation_store.append(
            conversation_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_00{index}",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": f"Q{index}"}],
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id=f"resp_00{index}",
                    data=MessageData(
                        role="assistant",
                        content=[{"type": "output_text", "text": f"A{index}"}],
                        agent="test-agent",
                    ),
                ),
            ],
        )


def test_fork_conversation_up_to_response_truncates_items(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """``up_to_response_id`` copies history through that response's last item.

    Forking at the middle response must copy every item up to and
    including the LAST item of that response (never ending mid-turn)
    and drop everything after it, while leaving the source untouched.
    """
    agent_store.create(
        agent_id="ag_fork_trunc",
        name="fork-trunc",
        bundle_location="ag_fork_trunc/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_trunc")
    _append_three_responses(conversation_store, source.id)

    fork = conversation_store.fork_conversation(source.id, up_to_response_id="resp_002")

    fork_texts = [
        part["text"]
        for item in conversation_store.list_items(fork.id).data
        for part in item.data.content  # type: ignore[union-attr]
    ]
    # Both items of resp_002 are included (the cutoff is the response's
    # LAST item) and resp_003 is dropped. A1-only would mean the cutoff
    # used the response's FIRST item; any Q3/A3 means no truncation.
    assert fork_texts == ["Q1", "A1", "Q2", "A2"], (
        f"Fork should contain history through resp_002 only, got {fork_texts}"
    )
    # The source keeps its full 6-item history — fork must never mutate it.
    assert len(conversation_store.list_items(source.id).data) == 6


def test_fork_conversation_truncated_drops_external_session_directive(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A truncated fork omits the native resume directive but keeps carry-history.

    If ``omnigent.fork.source_external_session_id`` were stamped, the
    runner would clone the source's FULL native transcript and resume
    it — resurrecting the truncated turns. The directive must be
    omitted so the runner's carry-history fork-rebuild path
    synthesizes the transcript from the truncated items instead.
    """
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    agent_store.create(
        agent_id="ag_fork_trunc_ext",
        name="fork-trunc-ext",
        bundle_location="ag_fork_trunc_ext/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_trunc_ext")
    conversation_store.set_external_session_id(source.id, "claude-uuid-trunc")
    _append_three_responses(conversation_store, source.id)

    fork = conversation_store.fork_conversation(
        source.id,
        carry_history_into_native=True,
        up_to_response_id="resp_001",
    )

    assert FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY not in fork.labels, (
        f"Truncated fork must not carry the full-transcript resume directive, got {fork.labels!r}"
    )
    # Carry-history still stamps: the runner needs it to rebuild the
    # native transcript from the truncated items rather than launch fresh.
    assert fork.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"


def test_fork_conversation_cross_family_drops_external_session_directive(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """``resume_source_native_session=False`` omits the native resume directive.

    A cross-family fork (e.g. codex-native source → claude-native target)
    must not stamp ``omnigent.fork.source_external_session_id``: the
    source's native transcript is the wrong format for the target harness,
    and the runner's clone path launches FRESH when its clone attempt
    fails — silently losing history. Omitting the directive routes the
    runner to the carry-history rebuild path (native transcript built from
    the copied Omnigent items) instead.
    """
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    agent_store.create(
        agent_id="ag_fork_xfam",
        name="fork-xfam",
        bundle_location="ag_fork_xfam/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_xfam")
    # A codex thread id on the source: resumable only by a codex target.
    conversation_store.set_external_session_id(source.id, "codex-thread-xfam")
    _append_three_responses(conversation_store, source.id)

    fork = conversation_store.fork_conversation(
        source.id,
        carry_history_into_native=True,
        resume_source_native_session=False,
    )

    # Despite a FULL (untruncated) fork of a source with a native session,
    # the directive must be absent — present would mean the cross-family
    # gate regressed and the runner will attempt a doomed transcript clone.
    assert FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY not in fork.labels, (
        f"Cross-family fork must not stamp the source's native session id, got {fork.labels!r}"
    )
    # Carry-history still stamps so the runner rebuilds from the copied items.
    assert fork.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1", (
        f"cross-family fork must stamp carry-history for rebuild, got {fork.labels!r}"
    )


def test_fork_conversation_up_to_last_response_keeps_external_directive(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Truncating at the LAST response is treated as a full fork.

    The copy is equivalent to a full fork, so the resume directive is
    kept — the runner can still clone the source's native transcript
    verbatim (full fidelity) instead of rebuilding from items.
    """
    from omnigent.stores.conversation_store import FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY

    agent_store.create(
        agent_id="ag_fork_trunc_last",
        name="fork-trunc-last",
        bundle_location="ag_fork_trunc_last/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_trunc_last")
    conversation_store.set_external_session_id(source.id, "claude-uuid-last")
    _append_three_responses(conversation_store, source.id)

    fork = conversation_store.fork_conversation(source.id, up_to_response_id="resp_003")

    # All 6 items copied — the cutoff at the last response drops nothing.
    assert len(conversation_store.list_items(fork.id).data) == 6
    assert fork.labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY) == "claude-uuid-last", (
        f"Cutoff at the last response is a full fork and must keep the resume "
        f"directive, got {fork.labels!r}"
    )


def test_fork_conversation_up_to_unknown_response_raises(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """An ``up_to_response_id`` matching no item raises ValueError.

    Silently copying the full history would fork far more context than
    the user selected (stale client state); the store must fail loud
    and create nothing.
    """
    agent_store.create(
        agent_id="ag_fork_trunc_bad",
        name="fork-trunc-bad",
        bundle_location="ag_fork_trunc_bad/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_trunc_bad")
    _append_three_responses(conversation_store, source.id)

    with pytest.raises(ValueError, match="resp_nope"):
        conversation_store.fork_conversation(source.id, up_to_response_id="resp_nope")


def test_fork_clone_agent_is_session_scoped(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A fork that clones an agent creates a session-scoped row, not a built-in.

    The clone must be born with ``session_id`` set so it never appears in
    the built-in agent list (``session_id IS NULL``) that backs the fork
    picker — the regression that surfaced as duplicate "Claude Code" /
    "Codex" entries in the fork dialog.
    """
    agent_store.create(
        agent_id="ag_fork_src",
        name="claude-native-ui",
        bundle_location="ag_fork_src/hash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_src")

    fork = conversation_store.fork_conversation(
        source.id,
        agent_id="ag_clone_ok",
        cloned_agent_name="claude-native-ui (fork ag_clone_o)",
        cloned_agent_bundle_location="ag_fork_src/hash",
        cloned_agent_description=None,
    )

    assert fork.agent_id == "ag_clone_ok"
    cloned = agent_store.get("ag_clone_ok")
    assert cloned is not None
    assert cloned.session_id == fork.id, "clone must be bound to the fork session"
    # The clone is session-scoped, so it must NOT leak into the built-in
    # list (the source built-in is the only template-name row).
    builtin_ids = {a.id for a in agent_store.list(limit=100).data}
    assert "ag_clone_ok" not in builtin_ids
    assert "ag_fork_src" in builtin_ids


def test_fork_clone_agent_failure_leaves_no_orphan(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A failed clone-fork rolls the agent row back — no orphaned built-in.

    Pre-fix the route pre-created the clone in its own committed
    transaction, so a fork failure (here a stale ``up_to_response_id``)
    orphaned a ``session_id IS NULL`` row that polluted the built-in agent
    catalog. Creating the clone inside the fork transaction means the
    failure rolls it back too.
    """
    agent_store.create(
        agent_id="ag_fork_src2",
        name="codex-native-ui",
        bundle_location="ag_fork_src2/hash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_src2")
    _append_three_responses(conversation_store, source.id)

    before = {a.id for a in agent_store.list(limit=100).data}
    with pytest.raises(ValueError, match="resp_nope"):
        conversation_store.fork_conversation(
            source.id,
            agent_id="ag_clone_orphan",
            cloned_agent_name="codex-native-ui (fork ag_clone_o)",
            cloned_agent_bundle_location="ag_fork_src2/hash",
            up_to_response_id="resp_nope",
        )

    # The clone must not exist at all, and the built-in list is unchanged.
    assert agent_store.get("ag_clone_orphan") is None
    after = {a.id for a in agent_store.list(limit=100).data}
    assert after == before


def test_instance_scoped_label_keys_match_harness_constants() -> None:
    """
    The store's instance-scoped denylist matches the harness label keys.

    The store hard-codes the bridge-id literals (to avoid importing
    harness modules into the persistence layer). This guards against
    drift: if a harness renames its bridge-id label key, the literal in
    :data:`_INSTANCE_SCOPED_LABEL_KEYS` would silently stop matching and
    forks would re-inherit the source's bridge. Importing the real
    constants here makes that rename fail loudly at test time.
    """
    from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY
    from omnigent.codex_native_bridge import CODEX_NATIVE_BRIDGE_ID_LABEL_KEY
    from omnigent.stores.conversation_store import _INSTANCE_SCOPED_LABEL_KEYS

    # Each harness's canonical bridge-id key must be in the denylist; a
    # miss means a rename slipped past the store's hard-coded literal.
    assert BRIDGE_ID_LABEL_KEY in _INSTANCE_SCOPED_LABEL_KEYS
    assert CODEX_NATIVE_BRIDGE_ID_LABEL_KEY in _INSTANCE_SCOPED_LABEL_KEYS


def test_fork_conversation_copies_reasoning_effort(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Fork inherits the source's reasoning_effort setting."""
    agent_store.create(
        agent_id="ag_fork_re",
        name="fork-reasoning",
        bundle_location="ag_fork_re/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_re")
    conversation_store.update_conversation(source.id, reasoning_effort="high")

    fork = conversation_store.fork_conversation(source.id)

    # A wrong value means fork_conversation didn't copy the
    # reasoning_effort column — the fork would silently use the
    # default effort level instead of the source's setting.
    assert fork.reasoning_effort == "high", "Fork should inherit reasoning_effort from the source"


def test_fork_conversation_copies_terminal_launch_args(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """Fork inherits the source's terminal_launch_args setting."""
    agent_store.create(
        agent_id="ag_fork_tla",
        name="fork-tla",
        bundle_location="ag_fork_tla/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_tla")
    conversation_store.update_conversation(
        source.id,
        terminal_launch_args=["--dangerously-skip-permissions"],
    )

    fork = conversation_store.fork_conversation(source.id)

    # A wrong value means fork_conversation didn't copy the
    # terminal_launch_args column — a forked native session would
    # silently lose its launch flags (consistent with how the fork
    # copies reasoning_effort / model_override).
    assert fork.terminal_launch_args == ["--dangerously-skip-permissions"], (
        "Fork should inherit terminal_launch_args from the source"
    )


def test_fork_conversation_copy_model_settings_false_resets(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """``copy_model_settings=False`` drops the source's model settings.

    A model id is provider-bound, so a fork that switches to a different
    provider family must NOT inherit the source's ``model_override`` /
    ``reasoning_effort`` — they'd name a model the new provider can't
    serve. Both must reset to ``None`` (the bound agent's defaults), while
    the default (``True``) still copies them.
    """
    agent_store.create(
        agent_id="ag_fork_cms",
        name="fork-cms",
        bundle_location="ag_fork_cms/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_cms")
    conversation_store.update_conversation(
        source.id, reasoning_effort="high", model_override="claude-opus-4"
    )

    fork = conversation_store.fork_conversation(source.id, copy_model_settings=False)

    reloaded = conversation_store.get_conversation(fork.id)
    assert reloaded is not None
    # Both reset — a non-None value means the cross-family reset didn't
    # apply and the fork would launch pointing at an incompatible model.
    assert reloaded.model_override is None, (
        "copy_model_settings=False must drop the source's model_override"
    )
    assert reloaded.reasoning_effort is None, (
        "copy_model_settings=False must drop the source's reasoning_effort"
    )

    # Control: the default still copies, proving the reset is gated on the
    # flag and not a blanket drop.
    fork_default = conversation_store.fork_conversation(source.id)
    reloaded_default = conversation_store.get_conversation(fork_default.id)
    assert reloaded_default is not None
    assert reloaded_default.model_override == "claude-opus-4"
    assert reloaded_default.reasoning_effort == "high"


def test_fork_conversation_model_override_wins_over_copy(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """An explicit ``model_override`` overrides the source's copied model.

    The "restart with model" path: the fork must launch on the requested
    model, not the source's. ``reasoning_effort`` still follows
    ``copy_model_settings`` (a same-family model switch keeps the effort).
    """
    agent_store.create(
        agent_id="ag_fork_mo",
        name="fork-mo",
        bundle_location="ag_fork_mo/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_mo")
    conversation_store.update_conversation(
        source.id, reasoning_effort="high", model_override="databricks-gpt-5-5"
    )

    fork = conversation_store.fork_conversation(
        source.id, model_override="databricks-gpt-5-4-mini"
    )

    reloaded = conversation_store.get_conversation(fork.id)
    assert reloaded is not None
    assert reloaded.model_override == "databricks-gpt-5-4-mini", (
        "An explicit model_override must win over the source's copied model."
    )
    # reasoning_effort still copies (same-family switch keeps the effort).
    assert reloaded.reasoning_effort == "high"


def test_fork_conversation_carry_history_into_native_stamps_label(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """``carry_history_into_native=True`` stamps the carry-history directive.

    The runner reads ``omnigent.fork.carry_history`` to decide whether a
    native target rebuilds its transcript (vs launching fresh). The label
    must be set only when the flag is passed; the default leaves it off so
    a normal fork into a native target doesn't trigger a rebuild from the
    wrong items.
    """
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    agent_store.create(
        agent_id="ag_fork_carry",
        name="fork-carry",
        bundle_location="ag_fork_carry/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_carry")

    carried = conversation_store.fork_conversation(source.id, carry_history_into_native=True)
    assert carried.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1", (
        f"carry_history_into_native=True must stamp the directive, got {carried.labels!r}"
    )

    # Default (False): no directive, so a native target launches fresh.
    plain = conversation_store.fork_conversation(source.id)
    assert FORK_CARRY_HISTORY_LABEL_KEY not in plain.labels, (
        f"Default fork must not stamp the carry-history directive, got {plain.labels!r}"
    )


def test_fork_conversation_agent_id_override(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """When agent_id is passed, the fork binds to the override
    instead of the source's agent.
    """
    agent_store.create(
        agent_id="ag_original",
        name="original-agent",
        bundle_location="ag_original/fakehash",
    )
    agent_store.create(
        agent_id="ag_cloned",
        name="cloned-agent",
        bundle_location="ag_original/fakehash",
    )
    source = conversation_store.create_conversation(agent_id="ag_original")

    fork = conversation_store.fork_conversation(
        source.id,
        agent_id="ag_cloned",
    )

    assert fork.agent_id == "ag_cloned", (
        "Fork should use the overridden agent_id, not the source's"
    )


def test_switch_conversation_agent_cross_family_resets_and_relabels(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """In-place switch deletes the old agent, binds the new, and on a
    cross-family switch resets model settings, clears the native session
    id, and replaces the harness-presentation labels.
    """
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        SWITCH_PREVIOUS_BUILTIN_LABEL_KEY,
    )

    # An instance-scoped label (belongs to the running instance, dropped on a
    # switch). Uses a literal still in _INSTANCE_SCOPED_LABEL_KEYS — the old
    # omnigent.stopped marker was retired upstream.
    instance_label = "omnigent.last_context_tokens"

    # A real session binds a session-scoped agent (agent.session_id == conv).
    created = conversation_store.create_session_with_agent(
        agent_id="ag_switch_old",
        agent_name="claude (switch src)",
        agent_bundle_location="ag_switch_old/hash",
        agent_description="old",
    )
    conv_id = created.conversation.id
    # Give the session model settings, a native session id, and labels that a
    # switch must touch (instance-scoped stopped marker + the old harness's
    # ui/wrapper) so we can assert they're handled correctly.
    conversation_store.update_conversation(
        conv_id, model_override="claude-opus-4-7", reasoning_effort="high"
    )
    conversation_store.set_external_session_id(conv_id, "old-native-uuid")
    conversation_store.set_labels(
        conv_id,
        {
            instance_label: "1",
            # DANGEROUS codex bypass opt-in: in the instance-scoped set so a
            # switch (a new agent/harness context) drops it rather than
            # silently re-arming bypass without a fresh typed confirmation.
            "omnigent.codex_native.bypass_sandbox": "1",
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: "claude-code-native-ui",
        },
    )
    conversation_store.append(
        conv_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )

    target_labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    updated = conversation_store.switch_conversation_agent(
        conv_id,
        new_agent_id="ag_switch_new",
        new_agent_name="codex (switch new)",
        new_agent_bundle_location="ag_switch_new/hash",
        new_agent_description="new",
        copy_model_settings=False,  # cross-family
        carry_history_into_native=True,  # native target
        presentation_labels=target_labels,
        previous_builtin_id="ag_builtin_claude",
    )

    # New agent bound; old session-scoped agent deleted (unique session_id
    # index would otherwise be violated by leaving both).
    assert updated.agent_id == "ag_switch_new"
    assert agent_store.get("ag_switch_old") is None, (
        "old session-scoped agent must be deleted on switch"
    )
    new_agent = agent_store.get("ag_switch_new")
    assert new_agent is not None and new_agent.session_id == conv_id, (
        "new agent must be session-scoped to this conversation"
    )
    # Cross-family → provider-bound model id is meaningless, so both reset.
    assert updated.model_override is None
    assert updated.reasoning_effort is None
    # Native runtime state belongs to the old harness → cleared so the next
    # turn cold-starts and rebuilds from items.
    assert updated.external_session_id is None
    # Labels: target ui/wrapper applied, carry-history + previous-builtin
    # stamped, and the old instance-scoped stopped marker dropped.
    assert updated.labels[UI_MODE_LABEL_KEY] == UI_MODE_TERMINAL_VALUE
    assert updated.labels[WRAPPER_LABEL_KEY] == CODEX_NATIVE_WRAPPER_VALUE
    assert updated.labels[FORK_CARRY_HISTORY_LABEL_KEY] == "1"
    assert updated.labels[SWITCH_PREVIOUS_BUILTIN_LABEL_KEY] == "ag_builtin_claude"
    assert instance_label not in updated.labels, "instance-scoped labels must not survive a switch"
    assert "omnigent.codex_native.bypass_sandbox" not in updated.labels, (
        "the dangerous bypass opt-in must not survive a switch (re-confirm per context)"
    )
    # Transcript is untouched (in place, not copied).
    assert len(conversation_store.list_items(conv_id).data) == 1


def test_switch_conversation_agent_same_family_keeps_model_settings(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A same-family switch keeps model settings; an SDK target (empty
    presentation labels) drops the old ui/wrapper labels and does not stamp
    the carry-history directive.
    """
    from omnigent._wrapper_labels import (
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        SWITCH_PREVIOUS_BUILTIN_LABEL_KEY,
    )

    created = conversation_store.create_session_with_agent(
        agent_id="ag_switch_old2",
        agent_name="claude-native-ui",
        agent_bundle_location="ag_switch_old2/hash",
        agent_description=None,
    )
    conv_id = created.conversation.id
    conversation_store.update_conversation(
        conv_id, model_override="claude-opus-4-7", reasoning_effort="high"
    )
    conversation_store.set_labels(
        conv_id,
        {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: "claude-code-native-ui",
            # A stale previous-builtin pointer from an earlier switch.
            SWITCH_PREVIOUS_BUILTIN_LABEL_KEY: "ag_stale_builtin",
        },
    )

    updated = conversation_store.switch_conversation_agent(
        conv_id,
        new_agent_id="ag_switch_new2",
        new_agent_name="claude (switch new)",
        new_agent_bundle_location="ag_switch_new2/hash",
        new_agent_description=None,
        copy_model_settings=True,  # same family (anthropic native → sdk)
        carry_history_into_native=False,  # SDK target rebuilds nothing
        presentation_labels={},  # SDK → chat mode (drop ui/wrapper)
        previous_builtin_id=None,
    )

    # Same family → model settings carry over unchanged.
    assert updated.model_override == "claude-opus-4-7"
    assert updated.reasoning_effort == "high"
    # SDK target → terminal-first ui/wrapper labels removed (chat mode).
    assert UI_MODE_LABEL_KEY not in updated.labels
    assert WRAPPER_LABEL_KEY not in updated.labels
    # No native rebuild for an SDK target.
    assert FORK_CARRY_HISTORY_LABEL_KEY not in updated.labels
    # Stale previous-builtin pointer dropped (None passed → not re-stamped),
    # so a later "switch back" can't offer a wrong target.
    assert SWITCH_PREVIOUS_BUILTIN_LABEL_KEY not in updated.labels


def test_get_session_connectivity_batches_runner_and_host(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """
    ``get_session_connectivity`` returns runner/host per id.

    This is the bulk read powering the ``/health`` online-dot path: it
    replaced an N+1 fan-out of ``get_conversation`` (plus a labels query
    each). Liveness is now purely "is the tunnel up / is the host
    fresh" — the retired ``omnigent.stopped`` marker is no longer a
    field on the result. The test pins both binding fields the dot
    decision needs, across a mix of bindings in one call:

    - a runner-bound session reports its ``runner_id`` and no host;
    - a host-bound session reports its ``host_id`` and no runner;
    - an unknown id is absent from the result (callers treat that as
      reachable, matching the legacy single-row path).
    """
    _register_host(db_uri, "host_conn")
    runner_bound = conversation_store.create_conversation(runner_id="runner_xyz")
    host_bound = conversation_store.create_conversation(host_id="host_conn", workspace="/tmp/ws")

    result = conversation_store.get_session_connectivity(
        [runner_bound.id, host_bound.id, "conv_unknown"]
    )

    assert set(result) == {runner_bound.id, host_bound.id}
    assert result[runner_bound.id].runner_id == "runner_xyz"
    assert result[runner_bound.id].host_id is None
    assert result[host_bound.id].host_id == "host_conn"
    assert result[host_bound.id].runner_id is None
    # None of these are forks of a coding session, so needs_workspace is
    # off across the board — a True here would wrongly force the online
    # dot off for a normally-bound session.
    assert result[runner_bound.id].needs_workspace is False
    assert result[host_bound.id].needs_workspace is False


def test_get_session_connectivity_reports_needs_workspace_for_fork(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The fork-source label surfaces as ``needs_workspace=True``.

    A fork of a session that had a working directory carries the
    ``omnigent.fork.source_id`` label (set by ``fork_conversation``).
    ``get_session_connectivity`` must report ``needs_workspace=True`` for
    it — that flag is what makes ``_bulk_session_liveness`` mark the
    unbound clone offline so the UI prompts for a directory instead of
    dropping the first message.
    """
    fork = conversation_store.create_conversation()
    conversation_store.set_labels(fork.id, {"omnigent.fork.source_id": "conv_src"})
    plain = conversation_store.create_conversation()

    result = conversation_store.get_session_connectivity([fork.id, plain.id])

    # Fork-source label present → needs_workspace on. A False here means
    # the label SELECT or the flag computation dropped the fork key.
    assert result[fork.id].needs_workspace is True
    # No label → in-process resumable, needs_workspace off. This is the
    # CUJ-2 chat-only fork path that must stay reachable.
    assert result[plain.id].needs_workspace is False


def test_get_session_connectivity_empty_input_skips_query(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``get_session_connectivity([])`` returns ``{}`` without a query.

    The single-session ``/health`` variant and an empty sidebar both
    hit this; it must short-circuit rather than issue an ``IN ()``.
    """
    assert conversation_store.get_session_connectivity([]) == {}


# ── Per-user daily cost rollup ────────────────────────


def test_get_daily_cost_missing_returns_zero(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A (user, day) with no recorded spend reads as ``0.0``."""
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-05") == 0.0


def test_add_daily_cost_accumulates(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Repeated adds for the same (user, day) sum into one total."""
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 1.25)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 0.75)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 2.00)

    # 1.25 + 0.75 + 2.00 = 4.00 proves each delta was added, not overwritten.
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-05") == pytest.approx(
        4.00
    )


def test_add_daily_cost_isolated_by_user_and_day(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Spend is partitioned by both user and UTC day; no cross-bleed."""
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 5.0)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-06", 3.0)
    conversation_store.add_daily_cost("bob@example.com", "2026-06-05", 9.0)

    # Same user, different day: only that day's delta.
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-05") == pytest.approx(
        5.0
    )
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-06") == pytest.approx(
        3.0
    )
    # Different user, same day: isolated from alice.
    assert conversation_store.get_daily_cost("bob@example.com", "2026-06-05") == pytest.approx(9.0)


def test_add_daily_cost_nonpositive_is_noop(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``delta <= 0`` never creates or mutates a row."""
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 0.0)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", -1.0)
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-05") == 0.0

    # A subsequent positive add still works and isn't polluted by the no-ops.
    conversation_store.add_daily_cost("alice@example.com", "2026-06-05", 2.5)
    assert conversation_store.get_daily_cost("alice@example.com", "2026-06-05") == pytest.approx(
        2.5
    )


def test_get_session_owner_returns_highest_level_grantee(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """The owner is the max-``level`` grantee, regardless of grant order."""
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    for user in ("reader@example.com", "alice@example.com", "editor@example.com"):
        # session_permissions.user_id is an FK to users.id.
        perms.ensure_user(user)
    perms.grant("reader@example.com", conv.id, 1)  # read
    perms.grant("alice@example.com", conv.id, 4)  # owner (LEVEL_OWNER)
    perms.grant("editor@example.com", conv.id, 2)  # edit

    # Owner (level 4) outranks the read/edit grants; if this returned a
    # lower-level grantee the ORDER BY level DESC ranking would be wrong.
    assert conversation_store.get_session_owner(conv.id) == "alice@example.com"


def test_get_session_owner_none_when_no_grants(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A session with no permission grants (single-user mode) has no owner."""
    conv = conversation_store.create_conversation()
    assert conversation_store.get_session_owner(conv.id) is None


def test_get_session_owner_excludes_public_sentinel(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """A session with only a public grant (no real owner) returns None."""
    from omnigent.server.auth import RESERVED_USER_PUBLIC
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(RESERVED_USER_PUBLIC)
    perms.grant(RESERVED_USER_PUBLIC, conv.id, 1)  # public read, no owner grant

    # Without the public-sentinel filter this would return "__public__"
    # (the only — hence highest-level — grant); the filter makes a
    # session with no real owner read as None instead.
    assert conversation_store.get_session_owner(conv.id) is None


# ── Per-user daily cost: ask-approved state ───────────


def test_get_daily_cost_state_missing_returns_zeros(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A (user, day) with no row reads as zeros for both fields."""
    state = conversation_store.get_daily_cost_state("alice@example.com", "2026-06-06")
    assert state == {"cost_usd": 0.0, "ask_approved_usd": 0.0}


def test_set_daily_ask_approved_does_not_clobber_cost(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Recording an approved checkpoint leaves accumulated cost intact."""
    conversation_store.add_daily_cost("alice@example.com", "2026-06-06", 3.0)
    conversation_store.set_daily_ask_approved("alice@example.com", "2026-06-06", 2.0)

    state = conversation_store.get_daily_cost_state("alice@example.com", "2026-06-06")
    # cost must survive the approval write (set touches only ask_approved_usd);
    # if it dropped to 0 the UPSERT wrongly overwrote cost_usd.
    assert state["cost_usd"] == pytest.approx(3.0)
    assert state["ask_approved_usd"] == pytest.approx(2.0)


def test_add_daily_cost_does_not_clobber_ask_approved(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Accumulating cost after an approval leaves the approval intact."""
    conversation_store.set_daily_ask_approved("alice@example.com", "2026-06-06", 2.0)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-06", 1.5)

    state = conversation_store.get_daily_cost_state("alice@example.com", "2026-06-06")
    # The increment UPSERT must only touch cost_usd; ask_approved survives.
    assert state["cost_usd"] == pytest.approx(1.5)
    assert state["ask_approved_usd"] == pytest.approx(2.0)


def test_set_daily_ask_approved_creates_row_when_absent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Approving with no prior row inserts a cost=0 row carrying the approval."""
    conversation_store.set_daily_ask_approved("alice@example.com", "2026-06-06", 0.05)
    state = conversation_store.get_daily_cost_state("alice@example.com", "2026-06-06")
    assert state["cost_usd"] == 0.0
    assert state["ask_approved_usd"] == pytest.approx(0.05)


def test_add_daily_cost_stacks_after_ask_approved(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Cost increments stack (not overwrite) even after an approval is set."""
    conversation_store.set_daily_ask_approved("alice@example.com", "2026-06-06", 2.0)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-06", 1.5)
    conversation_store.add_daily_cost("alice@example.com", "2026-06-06", 1.0)

    state = conversation_store.get_daily_cost_state("alice@example.com", "2026-06-06")
    # 1.5 + 1.0 proves the second add incremented rather than overwrote;
    # ask_approved untouched throughout.
    assert state["cost_usd"] == pytest.approx(2.5)
    assert state["ask_approved_usd"] == pytest.approx(2.0)


# ── set_session_state ─────────────────────────────────────────────────────


def test_set_session_state_persists(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """set_session_state writes a JSON-serializable dict to the conversation."""
    conv = conversation_store.create_conversation()
    state = {"cursor": 42, "flags": ["a", "b"]}
    conversation_store.set_session_state(conv.id, state)

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.session_state == state


def test_set_session_state_overwrites(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """set_session_state replaces the entire state dict."""
    conv = conversation_store.create_conversation()
    conversation_store.set_session_state(conv.id, {"v": 1})
    conversation_store.set_session_state(conv.id, {"v": 2, "new_key": True})

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.session_state == {"v": 2, "new_key": True}


def test_set_session_state_empty_dict(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """set_session_state with empty dict clears state."""
    conv = conversation_store.create_conversation()
    conversation_store.set_session_state(conv.id, {"old": True})
    conversation_store.set_session_state(conv.id, {})

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.session_state == {}


# ── set_session_usage ─────────────────────────────────────────────────────


def test_set_session_usage_persists(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """set_session_usage writes token usage to the conversation."""
    conv = conversation_store.create_conversation()
    usage = {"input_tokens": 1500, "output_tokens": 350, "total_tokens": 1850}
    conversation_store.set_session_usage(conv.id, usage)

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.session_usage == usage


def test_set_session_usage_overwrites(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """set_session_usage replaces the entire usage dict."""
    conv = conversation_store.create_conversation()
    conversation_store.set_session_usage(conv.id, {"input_tokens": 100})
    conversation_store.set_session_usage(conv.id, {"input_tokens": 200, "output_tokens": 50})

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.session_usage == {"input_tokens": 200, "output_tokens": 50}


# ── list_conversations_by_host_id ─────────────────────────────────────────


def test_list_conversations_by_host_id_returns_matching(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """list_conversations_by_host_id returns conversations bound to the host."""
    _register_host(db_uri, "host_byhost")
    conv = conversation_store.create_conversation(
        host_id="host_byhost",
        workspace="/tmp/ws",
    )
    conversation_store.create_conversation()  # no host_id

    result = conversation_store.list_conversations_by_host_id("host_byhost")
    assert len(result) == 1
    assert result[0].id == conv.id


def test_list_conversations_by_host_id_empty(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """list_conversations_by_host_id returns empty list when no match."""
    conversation_store.create_conversation()
    result = conversation_store.list_conversations_by_host_id("host_nonexistent")
    assert result == []


# ── next_position counter (write-path MAX(position) scan removal) ──────


def _user_message(text: str, response_id: str = "resp_pos") -> NewConversationItem:
    """A minimal user message item for position-counter tests."""
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _stored_next_position(
    conversation_store: SqlAlchemyConversationStore, conversation_id: str
) -> int | None:
    """Read the raw ``conversations.next_position`` counter for assertions."""
    from omnigent.db.db_models import SqlConversation

    with conversation_store._session() as session:
        row = session.get(SqlConversation, conversation_id)
        assert row is not None
        return row.next_position


def _stored_positions(
    conversation_store: SqlAlchemyConversationStore, conversation_id: str
) -> list[int]:
    """Raw item positions for a conversation, ascending — the source of
    truth ``list_items`` (which hides ``position``) cannot assert on."""
    from sqlalchemy import select

    from omnigent.db.db_models import SqlConversationItem

    with conversation_store._session() as session:
        return sorted(
            session.execute(
                select(SqlConversationItem.position).where(
                    SqlConversationItem.conversation_id == conversation_id
                )
            )
            .scalars()
            .all()
        )


def test_new_conversation_seeds_next_position_zero(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A freshly created conversation starts its position allocator at 0, so
    the first append reads the counter rather than scanning MAX(position)."""
    conv = conversation_store.create_conversation()
    assert _stored_next_position(conversation_store, conv.id) == 0


@pytest.mark.parametrize("batch_sizes", [[1], [1, 1, 1], [3], [2, 1, 4]])
def test_append_allocates_dense_positions_and_advances_counter(
    conversation_store: SqlAlchemyConversationStore,
    batch_sizes: list[int],
) -> None:
    """append() assigns contiguous positions from next_position and advances
    the counter by the batch size, so the stored counter always equals the
    total items appended — across single- and multi-item batches.

    Real store, real SQLite; asserts on the raw position column and counter,
    no mocks.
    """
    conv = conversation_store.create_conversation()
    total = 0
    for batch in batch_sizes:
        conversation_store.append(conv.id, [_user_message(f"m{total + i}") for i in range(batch)])
        total += batch
        assert _stored_positions(conversation_store, conv.id) == list(range(total))
        # The counter points one past the last item — the next position to hand out.
        assert _stored_next_position(conversation_store, conv.id) == total


def test_append_reads_counter_not_max_scan(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """append() allocates from the maintained counter, not a MAX(position)
    scan: advancing the counter past the real max makes the next item land at
    the counter value, which a scan-based implementation could never produce.
    """
    from omnigent.db.db_models import SqlConversation

    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_user_message("a"), _user_message("b")])
    # Real max position is 1; jump the counter ahead to 100.
    with conversation_store._session() as session:
        session.get(SqlConversation, conv.id).next_position = 100

    conversation_store.append(conv.id, [_user_message("c")])

    # Position 100 (counter), not 2 (max + 1) — proves the scan path is unused.
    assert _stored_positions(conversation_store, conv.id) == [0, 1, 100]
    assert _stored_next_position(conversation_store, conv.id) == 101


@pytest.mark.parametrize("preexisting", [0, 1, 3])
def test_append_falls_back_to_scan_when_counter_null(
    conversation_store: SqlAlchemyConversationStore,
    preexisting: int,
) -> None:
    """A conversation written before the counter existed has
    next_position = NULL. The next append falls back to a one-time
    MAX(position) scan to place items correctly, then persists the advanced
    counter so subsequent appends are scan-free.
    """
    from omnigent.db.db_models import SqlConversation

    conv = conversation_store.create_conversation()
    if preexisting:
        conversation_store.append(conv.id, [_user_message(f"pre{i}") for i in range(preexisting)])
    # Simulate a pre-counter row: clear the maintained counter.
    with conversation_store._session() as session:
        session.get(SqlConversation, conv.id).next_position = None
    assert _stored_next_position(conversation_store, conv.id) is None

    conversation_store.append(conv.id, [_user_message("new")])

    # The fallback scan placed the new item right after the existing max...
    assert _stored_positions(conversation_store, conv.id) == list(range(preexisting + 1))
    # ...and the counter is now backfilled, so the next append won't scan.
    assert _stored_next_position(conversation_store, conv.id) == preexisting + 1


def test_fork_seeds_next_position_from_copied_items(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A full fork seeds the clone's allocator from the number of copied items,
    so the clone's first append is scan-free and collision-free."""
    agent_store.create(agent_id="ag_fork_pos", name="fork-pos", bundle_location="ag_fork_pos/h")
    source = conversation_store.create_conversation(agent_id="ag_fork_pos")
    conversation_store.append(
        source.id, [_user_message(f"s{i}", response_id="resp_1") for i in range(3)]
    )

    fork = conversation_store.fork_conversation(source.id, title="fork")

    # 3 items copied (dense positions 0..2) → allocator starts at 3.
    assert _stored_next_position(conversation_store, fork.id) == 3
    conversation_store.append(fork.id, [_user_message("after")])
    assert _stored_positions(conversation_store, fork.id) == [0, 1, 2, 3]


def test_truncated_fork_seeds_next_position_from_copied_items(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
) -> None:
    """A truncated fork seeds the allocator from the count of the *copied*
    items, not the source length, so the shorter clone stays collision-free."""
    agent_store.create(
        agent_id="ag_fork_trunc", name="fork-trunc", bundle_location="ag_fork_trunc/h"
    )
    source = conversation_store.create_conversation(agent_id="ag_fork_trunc")
    conversation_store.append(
        source.id,
        [_user_message("a", "resp_1"), _user_message("b", "resp_1")],
    )
    conversation_store.append(
        source.id,
        [_user_message("c", "resp_2"), _user_message("d", "resp_2")],
    )

    fork = conversation_store.fork_conversation(source.id, up_to_response_id="resp_1")

    # Only resp_1's 2 items are copied → allocator starts at 2.
    assert _stored_positions(conversation_store, fork.id) == [0, 1]
    assert _stored_next_position(conversation_store, fork.id) == 2
    conversation_store.append(fork.id, [_user_message("after")])
    assert _stored_positions(conversation_store, fork.id) == [0, 1, 2]


def test_append_many_batches_stay_contiguous(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """End-to-end: many sequential appends produce a contiguous, gap-free
    position sequence and a counter equal to the item count — the invariant
    the maintained allocator must preserve across a long session (the
    scan-per-write pattern this replaces grew with that length)."""
    conv = conversation_store.create_conversation()
    total = 0
    for turn in range(25):
        conversation_store.append(
            conv.id,
            [_user_message(f"t{turn}-{i}", response_id=f"resp_{turn}") for i in range(3)],
        )
        total += 3

    assert _stored_positions(conversation_store, conv.id) == list(range(total))
    assert _stored_next_position(conversation_store, conv.id) == total
    listed = conversation_store.list_items(conv.id, limit=total)
    assert len(listed.data) == total


# ── Projects (conversation_labels key="omni_project") ───────


def test_list_projects_returns_distinct_names_sorted(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``list_projects`` returns each distinct project name once, ordered
    alphabetically. Sessions with no project label don't create phantom
    projects, and a project shared by two sessions appears a single time."""
    a1 = conversation_store.create_conversation()
    a2 = conversation_store.create_conversation()
    b1 = conversation_store.create_conversation()
    conversation_store.create_conversation()  # unfiled — must not appear

    conversation_store.set_labels(a1.id, {"omni_project": "Sprint 42"})
    conversation_store.set_labels(a2.id, {"omni_project": "Sprint 42"})
    conversation_store.set_labels(b1.id, {"omni_project": "Customer X"})

    # Alphabetical, de-duplicated. A missing DISTINCT would list "Sprint 42"
    # twice.
    assert conversation_store.list_projects() == ["Customer X", "Sprint 42"]


def test_list_projects_empty_when_no_project_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Non-project labels (e.g. guardrail keys) never surface as projects."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1", "sensitivity": "public"})
    assert conversation_store.list_projects() == []


def test_list_projects_excludes_all_archived_projects(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A project whose every member is archived drops out of the list (this is
    what makes "Delete project" — which archives all members — remove the
    folder), while the label is preserved so unarchiving restores it.

    A project with a mix of archived and active members still appears."""
    solo = conversation_store.create_conversation()
    mix_archived = conversation_store.create_conversation()
    mix_active = conversation_store.create_conversation()

    conversation_store.set_labels(solo.id, {"omni_project": "Gone"})
    conversation_store.set_labels(mix_archived.id, {"omni_project": "Mixed"})
    conversation_store.set_labels(mix_active.id, {"omni_project": "Mixed"})

    # "Gone" has one member; archiving it empties the project. "Mixed" keeps a
    # live member, so it stays.
    conversation_store.update_conversation(solo.id, archived=True)
    conversation_store.update_conversation(mix_archived.id, archived=True)

    assert conversation_store.list_projects() == ["Mixed"]

    # Unarchiving the lone member brings its project back — the label was kept.
    conversation_store.update_conversation(solo.id, archived=False)
    assert conversation_store.list_projects() == ["Gone", "Mixed"]


def test_list_projects_scoped_by_accessible_by(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """When ``accessible_by`` is set, only projects on sessions the user has a
    permission row for are returned — mirroring the list_conversations ACL."""
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    mine = conversation_store.create_conversation()
    theirs = conversation_store.create_conversation()
    conversation_store.set_labels(mine.id, {"omni_project": "Mine"})
    conversation_store.set_labels(theirs.id, {"omni_project": "Theirs"})

    perms = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice@example.com", "bob@example.com"):
        perms.ensure_user(user)
    perms.grant("alice@example.com", mine.id, 4)
    perms.grant("bob@example.com", theirs.id, 4)

    # Alice only sees her project; Theirs is invisible to her.
    assert conversation_store.list_projects(accessible_by="alice@example.com") == ["Mine"]


def test_delete_label_removes_only_target_key(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``delete_label`` drops the named key and leaves siblings intact — so
    removing a session from its project doesn't wipe guardrail labels."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"omni_project": "X", "integrity": "1"})

    conversation_store.delete_label(conv.id, "omni_project")

    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    assert got.labels == {"integrity": "1"}


def test_delete_label_is_noop_when_absent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Deleting a label that doesn't exist is a no-op, not an error."""
    conv = conversation_store.create_conversation()
    conversation_store.delete_label(conv.id, "omni_project")  # must not raise
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    assert got.labels == {}


def test_list_conversations_filters_by_project(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``project="X"`` returns only sessions carrying that exact project label."""
    filed = conversation_store.create_conversation()
    other = conversation_store.create_conversation()
    conversation_store.create_conversation()  # unfiled

    conversation_store.set_labels(filed.id, {"omni_project": "X"})
    conversation_store.set_labels(other.id, {"omni_project": "Y"})

    ids = {c.id for c in conversation_store.list_conversations(project="X").data}
    assert ids == {filed.id}


def test_list_conversations_empty_project_returns_unfiled(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``project=""`` returns only sessions with NO project label (Unfiled)."""
    filed = conversation_store.create_conversation()
    unfiled = conversation_store.create_conversation()
    conversation_store.set_labels(filed.id, {"omni_project": "X"})

    ids = {c.id for c in conversation_store.list_conversations(project="").data}
    assert unfiled.id in ids
    assert filed.id not in ids


def test_list_conversations_project_none_disables_filter(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``project=None`` (the default) returns filed and unfiled alike."""
    filed = conversation_store.create_conversation()
    unfiled = conversation_store.create_conversation()
    conversation_store.set_labels(filed.id, {"omni_project": "X"})

    ids = {c.id for c in conversation_store.list_conversations().data}
    assert ids >= {filed.id, unfiled.id}


# ── usage_totals_for_user ─────────────────────────────


def test_usage_totals_for_user_sums_accessible_sessions(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Totals sum session_usage across a user's accessible sessions only."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("alice@example.com")
    perms.ensure_user("bob@example.com")

    a1 = conversation_store.create_conversation()
    a2 = conversation_store.create_conversation()
    b1 = conversation_store.create_conversation()
    conversation_store.set_session_usage(a1.id, {"total_cost_usd": 1.5, "total_tokens": 1000})
    conversation_store.set_session_usage(a2.id, {"total_cost_usd": 0.5, "total_tokens": 250})
    conversation_store.set_session_usage(b1.id, {"total_cost_usd": 9.0, "total_tokens": 9999})
    perms.grant("alice@example.com", a1.id, 4)
    perms.grant("alice@example.com", a2.id, 4)
    perms.grant("bob@example.com", b1.id, 4)

    totals = conversation_store.usage_totals_for_user("alice@example.com")

    # Only alice's two sessions — bob's high-cost session must not leak in.
    assert totals.cost_usd == pytest.approx(2.0)
    assert totals.total_tokens == 1250
    assert totals.session_count == 2


def test_usage_totals_for_user_empty(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A user with no sessions totals to zero."""
    totals = conversation_store.usage_totals_for_user("nobody@example.com")
    assert totals.cost_usd == 0.0
    assert totals.total_tokens == 0
    assert totals.session_count == 0


def test_usage_totals_for_user_handles_missing_usage(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Sessions with no recorded usage contribute zero but are still counted."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("alice@example.com")
    conv = conversation_store.create_conversation()  # no set_session_usage
    perms.grant("alice@example.com", conv.id, 4)

    totals = conversation_store.usage_totals_for_user("alice@example.com")

    assert totals.cost_usd == 0.0
    assert totals.total_tokens == 0
    assert totals.session_count == 1
