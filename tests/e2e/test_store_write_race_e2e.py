"""
End-to-end regression tests for the conversation-store write races
reported against the conversation store: label seeding and session-state
writes decide "what to write" from a snapshot taken before the write,
so concurrent updates are silently lost, and neither path
distinguishes an absent row from an empty one (phantom usage writes,
orphan label rows).

All four tests drive the REAL production components — the
:class:`SqlAlchemyConversationStore`, :func:`build_policy_engine`, and
:meth:`PolicyEngine.apply_state_updates` — against a real SQLite
database, constructing the exact interleavings from the report
deterministically (no sleep-based timing), so a regression fails every
run rather than flaking:

1. **Label seeding loses a concurrent policy write.** The builder's
   seed path reads the current labels, computes which declared
   ``initial`` values are missing, then upserts those. A store proxy
   lands a real concurrent label write (the normal
   :meth:`ConversationStore.set_labels` path) inside that window; the
   seed's blind upsert then resets the key back to its initial value.
2. **Session-state updates lose concurrent increments.** Two engines
   built from the same pre-write snapshot (the shape of two parallel
   tool calls on one session — each ``policies/evaluate`` call builds
   its own engine) each INCREMENT one counter by 1; the second
   whole-blob write clobbers the first, persisting +1 instead of +2.
3. **``increment_session_usage`` phantom-writes on an absent row.**
   With the conversation deleted, the UPDATE matches nothing but the
   mutated total is returned as if persisted — the caller is never
   told nothing was written.
4. **Label seeding against a deleted conversation leaves orphan
   rows.** ``conversation_labels`` carries no foreign key, so seeding
   a conversation that no longer exists inserts rows nothing can ever
   read back (and reports a snapshot of them).

These tests intentionally sit at the store/engine layer because the
failures have NO user-observable surface: they are silent lost writes
under concurrency (confirmed by the reporter on the issue thread).
The unit/integration suites cover the fixed primitives once they
exist; this file is the end-to-end regression gate for the four
user-invisible data-loss shapes themselves.
"""

from __future__ import annotations

import contextlib
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from omnigent.db.db_models import SqlConversationLabel
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, StateUpdate, StateUpdateAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def store() -> SqlAlchemyConversationStore:
    """
    A production store over a fresh on-disk SQLite database.

    On-disk (not ``:memory:``) so the store's real engine cache /
    session-factory wiring — including the ``BEGIN IMMEDIATE``
    immediate-session factory the fix relies on — behaves exactly as
    in a deployed server.

    :returns: A :class:`SqlAlchemyConversationStore` on a unique
        temp-file database.
    """
    tmp = Path(tempfile.mkdtemp(prefix="store-write-race-"))
    return SqlAlchemyConversationStore(f"sqlite:///{tmp}/ap-{uuid.uuid4().hex}.db")


@pytest.fixture()
def seeded_spec(tmp_path: Path) -> AgentSpec:
    """
    An agent spec declaring one label with an ``initial`` value.

    ``integrity: "1"`` is the declared default the builder seeds; the
    race tests overwrite it concurrently with ``"0"``.

    :param tmp_path: Pytest-provided fresh directory for the spec.
    :returns: The parsed :class:`AgentSpec`.
    """
    (tmp_path / "config.yaml").write_text(
        """
spec_version: 1
name: seeded
guardrails:
  labels:
    integrity: "1"
"""
    )
    return parse(tmp_path)


class _SeedWindowRacingStore:
    """
    Store proxy that lands a competing label write inside the seed window.

    The builder's seeding helper reads the current labels (its
    snapshot), decides which declared initials are missing, and then
    calls the store's label-write primitive. This proxy intercepts
    that first write call — i.e. the instant AFTER the snapshot was
    taken but BEFORE the seed lands — and commits a real concurrent
    write through the normal :meth:`set_labels` path, exactly the
    interleaving from the report. Everything else passes through to
    the real store.
    """

    def __init__(self, inner: SqlAlchemyConversationStore, conversation_id: str) -> None:
        self._inner = inner
        self._conversation_id = conversation_id
        self.raced = False

    def __getattr__(self, name: str) -> Any:
        # Intercept the current seeding primitive (set_labels on HEAD)
        # and any insert-if-absent successor, so the constructed race
        # keeps racing the seed write itself after a fix renames it.
        if name in ("set_labels", "seed_labels_if_absent"):
            real = getattr(self._inner, name)

            def hooked(conversation_id: str, updates: Any, *args: Any, **kwargs: Any) -> Any:
                if not self.raced and conversation_id == self._conversation_id:
                    self.raced = True
                    # The concurrent policy write: a real caller sets
                    # integrity="0" via the normal label-write path,
                    # after the seeder's snapshot read.
                    self._inner.set_labels(conversation_id, {"integrity": "0"})
                return real(conversation_id, updates, *args, **kwargs)

            return hooked
        return getattr(self._inner, name)


def test_label_seeding_preserves_concurrent_policy_write(
    store: SqlAlchemyConversationStore,
    seeded_spec: AgentSpec,
) -> None:
    """
    A policy write landing between the seed's snapshot read and its
    upsert must survive — the seed must not reset the key back to the
    spec's declared initial value.

    On the buggy build the seed's read-then-upsert overwrites the
    concurrent ``integrity="0"`` back to ``"1"``: a silently lost
    guardrail write (an integrity downgrade written by a policy is
    undone with no error and no log).
    """
    conv = store.create_conversation()
    racing = _SeedWindowRacingStore(store, conv.id)

    build_policy_engine(
        spec=seeded_spec,
        conversation_id=conv.id,
        conversation_store=racing,  # type: ignore[arg-type]
    )

    assert racing.raced, (
        "test harness failure: the seeding path never called the store's "
        "label-write primitive, so the concurrent write was never injected "
        "(did seeding move off set_labels/seed_labels_if_absent?)"
    )
    after = store.get_conversation(conv.id)
    assert after is not None
    assert after.labels.get("integrity") == "0", (
        f"label seeding LOST a concurrent policy write: a caller set "
        f"integrity='0' after the seed's snapshot read, but the persisted "
        f"value is {after.labels.get('integrity')!r} — the seed's blind "
        f"upsert reset it to the declared initial (facet 1: seed clobbers concurrent write)."
    )


def test_concurrent_session_state_increments_both_persist(
    store: SqlAlchemyConversationStore,
    seeded_spec: AgentSpec,
) -> None:
    """
    Two overlapping policy evaluations that each INCREMENT the same
    session-state counter by 1 must persist a total of 2.

    Each ``policies/evaluate`` request builds its own engine from the
    state persisted at build time, so two engines built before either
    write is the exact shape of two parallel tool calls on one
    session. On the buggy build each engine persists its own full
    in-memory blob, so the second write overwrites the first and the
    persisted total is 1 — a silently lost increment.
    """
    conv = store.create_conversation()
    engine_a = build_policy_engine(
        spec=seeded_spec, conversation_id=conv.id, conversation_store=store
    )
    engine_b = build_policy_engine(
        spec=seeded_spec, conversation_id=conv.id, conversation_store=store
    )

    increment = [StateUpdate(key="calls", action=StateUpdateAction.INCREMENT, value=1)]
    engine_a.apply_state_updates(increment)
    engine_b.apply_state_updates(increment)

    after = store.get_conversation(conv.id)
    assert after is not None
    assert after.session_state.get("calls") == 2, (
        f"session-state write LOST a concurrent increment: two policy "
        f"evaluations each incremented 'calls' by 1, but the persisted "
        f"total is {after.session_state.get('calls')!r}, not 2 — the second "
        f"whole-blob write clobbered the first (facet 2: whole-blob state write)."
    )


@pytest.mark.asyncio
async def test_increment_session_usage_absent_row_is_not_a_phantom_write(
    store: SqlAlchemyConversationStore,
) -> None:
    """
    Incrementing usage for a conversation that no longer exists must
    not report success while writing nothing.

    On the buggy build the absent row is treated as empty: the UPDATE
    matches nothing, yet the mutated total is returned as if
    persisted. Acceptable fixed behaviours are either raising (the
    row-missing contract) or actually persisting — silently returning
    an unpersisted total is the bug.
    """
    conv = store.create_conversation()
    deleted_id = conv.id
    assert await store.delete_conversation(deleted_id)
    assert store.get_conversation(deleted_id) is None

    try:
        returned = store.increment_session_usage(deleted_id, {"total_tokens": 5})
    except Exception:
        # Raising on an absent row is the correct contract.
        return

    # No exception: then the returned total must actually be readable
    # back — otherwise the caller was told a write happened that didn't.
    after = store.get_conversation(deleted_id)
    persisted = dict(after.session_usage) if after is not None else None
    assert persisted == returned, (
        f"increment_session_usage PHANTOM-WROTE: it returned {returned!r} "
        f"as the persisted usage for a deleted conversation, but the row "
        f"does not exist (read-back: {persisted!r}) — the UPDATE matched "
        f"nothing and the caller was never told (facet 3: phantom usage write)."
    )


@pytest.mark.asyncio
async def test_label_seeding_absent_row_leaves_no_orphans(
    store: SqlAlchemyConversationStore,
    seeded_spec: AgentSpec,
) -> None:
    """
    Seeding declared initial labels for a conversation that no longer
    exists must not insert rows.

    ``conversation_labels`` has no foreign key to ``conversations``,
    so on the buggy build a build-engine call racing a delete inserts
    label rows for the dead conversation — orphans nothing can read
    back, growing unboundedly with every such race.
    """
    conv = store.create_conversation()
    deleted_id = conv.id
    assert await store.delete_conversation(deleted_id)
    assert store.get_conversation(deleted_id) is None

    # Refusing to build for a deleted conversation is acceptable fixed
    # behaviour; the invariant below must hold either way.
    with contextlib.suppress(Exception):
        build_policy_engine(
            spec=seeded_spec,
            conversation_id=deleted_id,
            conversation_store=store,
        )

    with store._conv_session("test_orphan_label_check") as session:
        orphans = (
            session.execute(
                select(SqlConversationLabel.key, SqlConversationLabel.value).where(
                    SqlConversationLabel.conversation_id == deleted_id
                )
            )
            .tuples()
            .all()
        )
    assert orphans == [], (
        f"label seeding created ORPHAN rows for a deleted conversation: "
        f"{orphans!r} were inserted into conversation_labels for "
        f"{deleted_id!r}, which no longer exists — no FK, no existence "
        f"check in the same transaction as the write (facet 4: orphan label rows)."
    )
