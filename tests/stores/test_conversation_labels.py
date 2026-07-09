"""
Tests for the ``conversation_labels`` table + store API
(POLICIES.md §6, Phase 1 of the guardrails implementation).

Covers the store-level contract (CRUD, batched UPSERT
semantics, survival across conversation_items compaction,
cascade delete). Schema validation (``values`` / ``monotonic``
checks) lives in the runtime engine, not the store — those
tests land in Phase 3+.
"""

from __future__ import annotations

import pytest

from omnigent.entities import (
    MessageData,
    NewConversationItem,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── Happy-path CRUD ────────────────────────────────────


def test_new_conversation_has_empty_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A fresh conversation has no labels — empty dict, not
    None. If this regresses, downstream code that iterates
    ``conv.labels.items()`` would crash instead of no-op."""
    conv = conversation_store.create_conversation()
    got = conversation_store.get_conversation(conv.id)
    # Exact equality (not truthiness) — regression guard
    # against returning None or sentinel.
    assert got is not None
    assert got.labels == {}


def test_set_labels_persists_values(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Batched UPSERT writes every key; subsequent get reads
    them back. If this fails, the dialect-aware UPSERT path
    isn't running."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(
        conv.id,
        {"integrity": "1", "sensitivity": "public"},
    )
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # Both keys must be present with exact values — the
    # assertion proves the string coercion + UPSERT round-
    # tripped without corrupting either payload.
    assert got.labels == {"integrity": "1", "sensitivity": "public"}


def test_set_labels_overwrites_existing(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Second set_labels on the same key overwrites rather
    than errors or appends. Overwrite semantics are required
    by POLICIES.md §4 last-writer-wins composition."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1"})
    conversation_store.set_labels(conv.id, {"integrity": "0"})
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # The later "0" won; if this reads "1", overwrite
    # semantics broke and the UPSERT turned into INSERT-only.
    assert got.labels == {"integrity": "0"}


def test_set_labels_leaves_unmentioned_keys_untouched(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Keys not in the current update remain unchanged.
    If this regresses (e.g. a DELETE+INSERT implementation),
    every ``set_labels`` call would clobber the full state."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1", "sensitivity": "public"})
    # Only update integrity; sensitivity must survive.
    conversation_store.set_labels(conv.id, {"integrity": "0"})
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # sensitivity still has its original value = proof the
    # UPSERT touched only the keys in the update dict.
    assert got.labels == {"integrity": "0", "sensitivity": "public"}


def test_set_labels_empty_dict_is_noop(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Empty update is a no-op: no transaction, no state
    change. Guards against accidentally erasing labels when
    a policy returns no writes."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"x": "1"})
    conversation_store.set_labels(conv.id, {})
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    assert got.labels == {"x": "1"}


def test_set_labels_clamps_overlong_value_to_column_width(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Values longer than the column width are clamped at the store
    chokepoint so no writer can raise ``DataError`` on PostgreSQL.

    SQLite (used here) doesn't enforce ``VARCHAR`` length, so this proves
    the Python-side clamp in ``_upsert_labels`` — not the DB — does the
    trimming, which is exactly what protects the Postgres production path.
    """
    from omnigent.db.db_models import LABEL_VALUE_MAX_LEN

    conv = conversation_store.create_conversation()
    overlong = "z" * (LABEL_VALUE_MAX_LEN + 50)
    conversation_store.set_labels(conv.id, {"omnigent.last_task_error_message": overlong})
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    stored = got.labels["omnigent.last_task_error_message"]
    assert len(stored) == LABEL_VALUE_MAX_LEN
    # Head preserved (the clamp keeps the front, not a tail or empty string).
    assert stored == overlong[:LABEL_VALUE_MAX_LEN]
    # Every label writer (client ``body.labels`` on create, policy writes,
    # session error labels) funnels through ``_upsert_labels``, so clamping
    # here is the single guarantee that the column can never overflow.


def test_set_labels_many_keys_atomic(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """All keys land in a single transaction. Concurrent
    readers should never see a partial write (not tested
    directly here — see concurrency test in Phase 2).
    Covers: N > 2 keys upserted together."""
    conv = conversation_store.create_conversation()
    updates = {f"key_{i}": str(i) for i in range(20)}
    conversation_store.set_labels(conv.id, updates)
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # All 20 keys present — partial commits would leave some
    # missing; transaction rollback would leave none.
    assert got.labels == updates


# ── Survival across conversation_items churn ───────────


def test_labels_survive_item_appends(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Appending conversation_items does not touch labels.
    If this fails, a label set in turn 1 would vanish when
    the agent replies in turn 2 — critical invariant."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1"})
    # Simulate a few turns of conversation.
    for i in range(5):
        conversation_store.append(
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{i}",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": f"turn {i}"}],
                    ),
                ),
            ],
        )
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # Label state unchanged after 5 appends — the two tables
    # are truly independent.
    assert got.labels == {"integrity": "1"}


def test_labels_survive_item_compaction_proxy(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Deleting (not re-appending) all conversation_items
    leaves labels intact. This is the Phase 1 proxy for
    POLICIES.md §6.3 compaction-survival: compaction
    rewrites items (effectively DELETE + INSERT), so
    verifying labels survive a bulk item delete is the
    same property at a lower layer.

    If this fails, ``conversation_labels`` has a
    non-obvious dependency on item rows — a hidden trigger,
    a cascade pointed the wrong way, or someone routed
    ``delete_items`` through the label table by mistake.
    """
    from omnigent.db.db_models import SqlConversationItem

    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1", "confidentiality": "0"})
    # Add items, then nuke them to simulate compaction.
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_0",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "content"}],
                ),
            ),
        ],
    )
    # Raw-SQL delete through the store's engine — matches
    # what a compaction implementation would do, minus the
    # summary-item replacement. Using the store's internal
    # session for test plumbing only; production code should
    # not reach past the public API.
    from sqlalchemy import delete

    with conversation_store._session() as session:
        session.execute(
            delete(SqlConversationItem).where(
                SqlConversationItem.conversation_id == conv.id,
            )
        )
    got = conversation_store.get_conversation(conv.id)
    assert got is not None
    # Labels persisted across the item-table wipe — matches
    # the §6.3 guarantee.
    assert got.labels == {"integrity": "1", "confidentiality": "0"}


# ── Cascade delete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conversation_cascades_to_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When the conversation goes away, its labels go too
    (FK ON DELETE CASCADE). Without cascade, the labels
    table would accumulate orphaned rows forever."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1"})
    # Pre-condition: label is present.
    assert (conversation_store.get_conversation(conv.id) or conv).labels == {
        "integrity": "1",
    }
    # Act: delete the conversation (async, returns True on
    # existence).
    deleted = await conversation_store.delete_conversation(conv.id)
    assert deleted is True
    # Post-condition: no conversation → no labels to fetch.
    # We verify via a direct count rather than reading via
    # get_conversation (which now returns None and gives us
    # no label snapshot).
    from sqlalchemy import func, select

    from omnigent.db.db_models import SqlConversationLabel

    with conversation_store._session() as session:
        count = session.execute(
            select(func.count())
            .select_from(SqlConversationLabel)
            .where(
                SqlConversationLabel.conversation_id == conv.id,
            )
        ).scalar()
    # FK cascade dropped the rows along with the parent; if
    # this reads > 0, the FK was not declared ON DELETE
    # CASCADE (see the migration).
    assert count == 0


# ── Isolation across conversations ─────────────────────


def test_labels_isolated_across_conversations(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Writes on one conversation do not leak to another.
    Guards against a missing ``conversation_id`` filter in
    either the read path or the write path."""
    a = conversation_store.create_conversation()
    b = conversation_store.create_conversation()
    conversation_store.set_labels(a.id, {"integrity": "0"})
    conversation_store.set_labels(b.id, {"integrity": "1"})
    got_a = conversation_store.get_conversation(a.id)
    got_b = conversation_store.get_conversation(b.id)
    assert got_a is not None and got_b is not None
    # Each conversation has its own label state — proves
    # both SELECT and UPSERT filter by conversation_id.
    assert got_a.labels == {"integrity": "0"}
    assert got_b.labels == {"integrity": "1"}


# ── get_conversation semantics ─────────────────────────


def test_get_conversation_missing_returns_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """get_conversation on a non-existent ID returns None,
    not a zero-value Conversation. Must not attempt the
    label fetch for a missing parent (no SELECT error)."""
    got = conversation_store.get_conversation("conv_does_not_exist")
    assert got is None


# ── list_conversations + update_conversation parity ──


def test_list_conversations_populates_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`list_conversations` populates the `labels` field on
    each returned Conversation. Without this, UI code paging
    through conversations would see empty labels where they
    exist (silent data loss). Verifies the bulk-fetch path."""
    a = conversation_store.create_conversation()
    b = conversation_store.create_conversation()
    c = conversation_store.create_conversation()
    conversation_store.set_labels(a.id, {"integrity": "0"})
    conversation_store.set_labels(c.id, {"confidentiality": "1"})
    # b has no labels.
    page = conversation_store.list_conversations(limit=10)
    by_id = {conv.id: conv for conv in page.data}
    # Every returned conversation has its labels populated
    # correctly; conversations with no labels get empty
    # dicts (not None). Proves the bulk fetcher handles
    # sparse label rows.
    assert by_id[a.id].labels == {"integrity": "0"}
    assert by_id[b.id].labels == {}
    assert by_id[c.id].labels == {"confidentiality": "1"}


def test_update_conversation_returns_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """After update_conversation, the returned Conversation
    has labels populated — parity with get_conversation.
    Callers mixing update_conversation and label inspection
    must not get stale empty labels."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1"})
    updated = conversation_store.update_conversation(conv.id, title="Renamed")
    assert updated is not None
    assert updated.title == "Renamed"
    # Proves the label fetch runs in the update path too —
    # not just in get_conversation.
    assert updated.labels == {"integrity": "1"}


# ── Caller-supplied updated_at ─────────────────────────


def test_set_labels_honors_caller_timestamp(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When the caller passes an explicit `updated_at`, the
    store records that exact value. Required for the policy
    engine to align audit timestamps with evaluation time
    rather than wall-clock drift between evaluate() and the
    DB write (POLICIES.md §6.3).

    If this regresses, the timestamp would come from
    `now_epoch()` regardless, and replay / audit would
    show inconsistent stamps.
    """
    from sqlalchemy import select

    from omnigent.db.db_models import SqlConversationLabel

    conv = conversation_store.create_conversation()
    caller_stamp = 1_700_000_042  # arbitrary historical epoch
    conversation_store.set_labels(conv.id, {"integrity": "1"}, updated_at=caller_stamp)
    with conversation_store._session() as session:
        row = session.execute(
            select(SqlConversationLabel.updated_at).where(
                SqlConversationLabel.conversation_id == conv.id,
                SqlConversationLabel.key == "integrity",
            )
        ).scalar()
    # Store persisted the caller's timestamp verbatim — if
    # this shows `now_epoch()`, the override isn't wired.
    assert row == caller_stamp


def test_upsert_refreshes_timestamp_on_overwrite(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """UPSERT must refresh the timestamp column on re-write
    even when the value is unchanged — matches the
    SqlConversationLabel docstring invariant + POLICIES.md
    audit requirement. Using caller-supplied timestamps to
    deterministically prove the refresh (wall-clock epoch-
    second resolution would let a broken UPSERT pass this
    test if both writes landed in the same second).

    If this regresses, the ``on_conflict_do_update`` ``set_``
    clause dropped ``updated_at`` and old stamps would
    persist forever — audit timelines would silently skew.
    """
    from sqlalchemy import select

    from omnigent.db.db_models import SqlConversationLabel

    conv = conversation_store.create_conversation()
    # Two deliberate timestamps far enough apart that any
    # bug failing to update `updated_at` would leave the
    # earlier value behind.
    first_stamp = 1_700_000_000
    second_stamp = 1_700_000_999
    conversation_store.set_labels(
        conv.id,
        {"x": "1"},
        updated_at=first_stamp,
    )
    conversation_store.set_labels(
        conv.id,
        {"x": "2"},
        updated_at=second_stamp,
    )
    with conversation_store._session() as session:
        stamp = session.execute(
            select(SqlConversationLabel.updated_at).where(
                SqlConversationLabel.conversation_id == conv.id,
                SqlConversationLabel.key == "x",
            )
        ).scalar()
    # Must be the LATER stamp — proves UPSERT updated both
    # `value` and `updated_at`. If this reads first_stamp,
    # the `set_` clause forgot `updated_at`.
    assert stamp == second_stamp
