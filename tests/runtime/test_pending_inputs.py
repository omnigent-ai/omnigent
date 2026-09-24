"""
Unit tests for :mod:`omnigent.runtime.pending_inputs`.

The pending-inputs index holds web-composer user messages on
native-terminal sessions that haven't yet round-tripped back through
the transcript forwarder. It backs the optimistic "queued message"
bubble across a client re-bind by replaying un-consumed messages into
the session snapshot. Tests here pin its core invariants directly:

* :func:`record` assigns a unique id and :func:`snapshot_for` replays
  entries in FIFO (insertion) order with their content verbatim.
* :func:`resolve_oldest` drains the oldest entry (FIFO) and returns its
  id, regardless of the persisted message's text — the transcript
  reformats text (reply quotes, attachment markers), so order is the
  only reliable correlation signal. Returns ``None`` when empty.
* :func:`resolve` removes an entry by id (the forward-failed rollback
  path) and is idempotent.
* Stale entries are evicted after :data:`pending_inputs._TTL_S` — the
  ghost-cleanup backstop for a message the TUI never accepted.

The wire-up between the route layer and the index (record on POST,
drain at persist, replay in the snapshot) is covered by the server
route tests; this file tests the module in isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.runtime import pending_inputs


@pytest.fixture(autouse=True)
def _clean_pending_inputs_index() -> Iterator[None]:
    """
    Reset the module-global pending-inputs dict between tests.

    The index is process-global; without this fixture a leaked entry
    would change the snapshot/match behavior of every later test.
    """
    pending_inputs.reset_for_tests()
    yield
    pending_inputs.reset_for_tests()


def _text_block(text: str) -> dict[str, object]:
    """
    Build a minimal ``input_text`` content block.

    :param text: The message text, e.g. ``"hello"``.
    :returns: A content block dict, e.g.
        ``{"type": "input_text", "text": "hello"}``.
    """
    return {"type": "input_text", "text": text}


def test_record_then_snapshot_preserves_order_and_content() -> None:
    """
    Snapshot replays recorded messages FIFO with content verbatim.

    Proves a (re)connecting client re-hydrates exactly what it posted,
    in submission order. A failure here means the snapshot lost an
    entry, reordered them, or mangled the content blocks — the bubble
    would render wrong or vanish on re-bind.
    """
    first = pending_inputs.record("conv_a", [_text_block("first")])
    second = pending_inputs.record("conv_a", [_text_block("second")])

    snap = pending_inputs.snapshot_for("conv_a")
    # Two distinct ids in insertion order — not deduped, not reordered.
    assert [e["pending_id"] for e in snap] == [first, second]
    assert first != second
    # Content round-trips verbatim (real file ids / text survive replay).
    assert snap[0]["content"] == [_text_block("first")]
    assert snap[1]["content"] == [_text_block("second")]


def test_snapshot_returns_deep_copies() -> None:
    """
    Mutating a snapshot entry must not corrupt the stored content.

    The snapshot is serialized onto the wire; a shallow copy would let
    a caller's mutation leak back into the index and poison a later
    replay. Asserts the stored content is unchanged after mutation.
    """
    pending_inputs.record("conv_a", [_text_block("orig")])
    snap = pending_inputs.snapshot_for("conv_a")
    snap[0]["content"][0]["text"] = "mutated"

    # Re-read: the index still holds the original text, not "mutated".
    assert pending_inputs.snapshot_for("conv_a")[0]["content"] == [_text_block("orig")]


def test_resolve_oldest_drains_fifo_and_returns_entry() -> None:
    """
    A persisted message drains the oldest pending entry (FIFO).

    This is the dedupe that stops the now-committed item from
    double-rendering next to its stale optimistic bubble. Per-session
    SSE ordering means the i-th persisted user message is the i-th
    queued one, so draining is oldest-first. Asserts the first recorded
    entry drains first (with its id + content) and the second remains.
    """
    first = pending_inputs.record("conv_a", [_text_block("first")])
    second = pending_inputs.record("conv_a", [_text_block("second")])

    drained = pending_inputs.resolve_oldest("conv_a")
    # Oldest entry drains first; its id is echoed back so the client can
    # drop that bubble by id, and its content lets the caller fold file
    # blocks into the durable item.
    assert drained is not None
    assert drained.pending_id == first
    assert drained.content == [_text_block("first")]
    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [second]
    # Then the next-oldest.
    assert pending_inputs.resolve_oldest("conv_a").pending_id == second  # type: ignore[union-attr]
    assert pending_inputs.snapshot_for("conv_a") == []


def test_resolve_oldest_returns_none_when_empty() -> None:
    """
    Draining with nothing pending returns ``None``.

    A message typed directly in the TUI on a session with no queued web
    messages has no pending entry; the caller then renders it as a plain
    committed item (``cleared_pending_id`` is ``None``).
    """
    assert pending_inputs.resolve_oldest("conv_a") is None


def test_resolve_oldest_drains_regardless_of_reformatted_text() -> None:
    """
    Regression: a queued message drains even when the transcript
    reformats its text (reply-quote / attachment markers / whitespace).

    The bug: matching the pending entry to the persisted item *by text*
    broke when the native transcript reformatted the message — e.g. a
    reply-quote POSTed as ``"> quoted\\n\\nmy question"`` round-tripped
    back as differently-formatted text. The text match then failed, the
    entry never drained, and the message double-rendered (committed
    bubble + stranded pending bubble) and survived reload until the TTL.

    FIFO draining is immune: it ignores the content entirely. Here the
    stored content (with blockquote markers) is drained by order even
    though the persisted text it corresponds to looks nothing like it.
    """
    quoted = [_text_block("> modeling a crash where set_offline never ran)\n\nIs this the only?")]
    pid = pending_inputs.record("conv_a", quoted)

    # The persisted/round-tripped text is irrelevant to draining — order
    # is the only signal. The entry drains and is gone (no ghost).
    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None and drained.pending_id == pid
    assert pending_inputs.snapshot_for("conv_a") == []


def test_resolve_oldest_returns_content_with_file_blocks() -> None:
    """
    The drained entry carries its file blocks for durable merge.

    Native transcript items are text-only, so the persist site folds the
    drained entry's image/file blocks into the durable item to keep the
    image in history. That only works if :func:`resolve_oldest` hands
    back the original content (with real ``file_id``s), not just the id.
    """
    content = [
        {"type": "input_image", "file_id": "file_real", "filename": "a.png"},
        _text_block("look"),
    ]
    pending_inputs.record("conv_a", content)

    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None
    # The image block survives the drain so the caller can re-attach it.
    assert drained.content == content


def test_resolve_matching_text_skips_older_unmatched_entries() -> None:
    """Kiro can match the accepted prompt and identify older failed inputs."""
    first = pending_inputs.record(
        "conv_a", [_text_block("!!!! XOXOX !!!!")], created_by="alice@example.com"
    )
    second = pending_inputs.record("conv_a", [_text_block("tell me a joke")])

    drained = pending_inputs.resolve_matching_text("conv_a", "tell me a joke")

    assert drained.matched is not None
    assert drained.matched.pending_id == second
    assert drained.matched.content == [_text_block("tell me a joke")]
    assert [entry.pending_id for entry in drained.skipped] == [first]
    assert drained.skipped[0].content == [_text_block("!!!! XOXOX !!!!")]
    assert drained.skipped[0].created_by == "alice@example.com"
    assert pending_inputs.snapshot_for("conv_a") == []


def test_resolve_matching_text_leaves_entries_when_no_text_matches() -> None:
    """A direct Kiro TUI prompt must not consume unrelated web pending entries."""
    first = pending_inputs.record("conv_a", [_text_block("web input")])

    drained = pending_inputs.resolve_matching_text("conv_a", "typed in terminal")

    assert drained.matched is None
    assert drained.skipped == []
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [first]


def test_resolve_matching_text_does_not_match_on_unanchored_suffix() -> None:
    """A short pending entry must not be matched by an unrelated prompt that
    merely ends with its text (e.g. queued "ok" vs. an accepted "...still ok"),
    or that entry's file attachments would be merged into the wrong message."""
    short_entry = pending_inputs.record(
        "conv_a", [_text_block("ok"), {"type": "input_image", "url": "img://1"}]
    )

    drained = pending_inputs.resolve_matching_text("conv_a", "Let's continue - ok")

    assert drained.matched is None
    assert drained.skipped == []
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [
        short_entry
    ]


def test_resolve_removes_entry_idempotently() -> None:
    """
    :func:`resolve` drops an entry by id (forward-failed rollback).

    When the runner forward fails the route rolls back the record so a
    never-delivered message leaves no ghost bubble. Asserts the entry
    is removed and a second resolve of the same id is a harmless no-op.
    """
    keep = pending_inputs.record("conv_a", [_text_block("keep")])
    drop = pending_inputs.record("conv_a", [_text_block("drop")])

    pending_inputs.resolve("conv_a", drop)
    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [keep]
    # Idempotent — resolving an already-removed id does nothing.
    pending_inputs.resolve("conv_a", drop)
    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [keep]


def test_entries_are_scoped_per_conversation() -> None:
    """
    One conversation's pending messages never leak into another's.

    A multi-user server holds many sessions in the same process; a
    snapshot for conv B must never replay conv A's queued bubble.
    """
    a = pending_inputs.record("conv_a", [_text_block("for a")])
    pending_inputs.record("conv_b", [_text_block("for b")])

    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [a]
    # conv_b's snapshot doesn't contain conv_a's entry.
    assert all(e["pending_id"] != a for e in pending_inputs.snapshot_for("conv_b"))


def test_created_by_round_trips_through_drain() -> None:
    """
    :func:`resolve_oldest` returns the ``created_by`` stored at record time.

    The persist site applies the drained author to the ``NewConversationItem``
    so ``session.input.consumed`` broadcasts the correct identity to all
    clients. A failure here means collaborators (who never saw the optimistic
    bubble) receive ``created_by=None`` and the author label never appears for
    them on the committed message.
    """
    pending_inputs.record(
        "conv_a", [_text_block("alice's message")], created_by="alice@example.com"
    )

    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None
    assert drained.created_by == "alice@example.com"


def test_created_by_none_when_not_provided() -> None:
    """
    Entries recorded without ``created_by`` drain with ``None``.

    Covers callers that don't provide an author (e.g. pre-attribution
    code or unknown actor). The persist site guards on ``drained.created_by
    is not None`` before applying it, so ``None`` is a safe no-op.
    """
    pending_inputs.record("conv_a", [_text_block("anonymous")])

    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None
    assert drained.created_by is None


def test_created_by_in_snapshot() -> None:
    """
    :func:`snapshot_for` includes ``created_by`` when present.

    A collaborator who reconnects while a message is still in-flight
    re-hydrates the optimistic bubble from the snapshot. Without
    ``created_by`` in the snapshot payload the frontend cannot stamp
    the correct author on the bubble; the collaborator would either see
    their own email (wrong) or no label at all.
    """
    pending_inputs.record("conv_a", [_text_block("hi")], created_by="alice@example.com")

    snap = pending_inputs.snapshot_for("conv_a")
    assert len(snap) == 1
    assert snap[0]["created_by"] == "alice@example.com"


def test_created_by_absent_from_snapshot_when_none() -> None:
    """
    ``created_by`` is omitted from the snapshot dict when not set.

    Keeps the wire payload backward-compatible: clients that pre-date
    this field see no unknown key rather than an explicit ``null``.
    """
    pending_inputs.record("conv_a", [_text_block("hi")])

    snap = pending_inputs.snapshot_for("conv_a")
    assert "created_by" not in snap[0]


def test_stale_entries_evicted_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A never-drained entry is evicted once it ages past the TTL.

    This is the ghost-cleanup backstop for a message the vendor TUI
    never accepted (runner crash, dropped keystrokes): with no matching
    persist to drain it, it must not replay forever. Drive the clock via
    the ``_now`` seam so no real sleep is needed.

    Asserts the entry is present just under the TTL and gone just over
    it. A failure means eviction never fires (permanent ghost bubble)
    or fires too eagerly (a slow-but-valid round-trip loses its bubble).
    """
    clock = {"t": 1000.0}
    # Patch the module's own _now seam (not time.monotonic globally) so
    # only this index sees the advanced clock — see testing rule 14.
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])

    pid = pending_inputs.record("conv_a", [_text_block("ghost")])

    # Just under the TTL: a slow transcript round-trip still finds it.
    clock["t"] = 1000.0 + pending_inputs._TTL_S - 0.1
    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [pid]

    # Past the TTL: the lazy sweep on the next access evicts the ghost.
    clock["t"] = 1000.0 + pending_inputs._TTL_S + 0.1
    assert pending_inputs.snapshot_for("conv_a") == []


def test_restore_returns_a_drained_entry_to_the_front() -> None:
    """A restored entry reclaims the head of the FIFO.

    Compensation for a drain whose persist deduplicated (the entry
    belongs to the NEXT user message): the entry was the oldest when
    drained, so it must come back ahead of everything queued after it.
    """
    first = pending_inputs.record("conv_r", [_text_block("first")])
    second = pending_inputs.record("conv_r", [_text_block("second")])

    drained = pending_inputs.resolve_oldest("conv_r")
    assert drained is not None and drained.pending_id == first

    pending_inputs.restore("conv_r", drained)
    order = [e["pending_id"] for e in pending_inputs.snapshot_for("conv_r")]
    assert order == [first, second]

    # The restored entry drains again as the oldest.
    redrained = pending_inputs.resolve_oldest("conv_r")
    assert redrained is not None and redrained.pending_id == first


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("matched", [False, True])
def test_title_preference_survives_drain_and_restore(enabled: bool, matched: bool) -> None:
    content = [{"type": "input_text", "text": "investigate timeout"}]
    pending_inputs.record("conv_title", content, background_titles_enabled=enabled)
    drained = (
        pending_inputs.resolve_matching_text("conv_title", "investigate timeout").matched
        if matched
        else pending_inputs.resolve_oldest("conv_title")
    )
    assert drained is not None
    assert drained.background_titles_enabled is enabled
    pending_inputs.restore("conv_title", drained)
    restored = pending_inputs.resolve_oldest("conv_title")
    assert restored is not None
    assert restored.background_titles_enabled is enabled


def test_has_pending_tracks_parked_messages() -> None:
    assert pending_inputs.has_pending("conv_hp") is False
    pending_id = pending_inputs.record("conv_hp", [_text_block("hello")])
    assert pending_inputs.has_pending("conv_hp") is True
    pending_inputs.resolve("conv_hp", pending_id)
    assert pending_inputs.has_pending("conv_hp") is False


def test_record_with_same_stable_id_returns_existing_pending_id() -> None:
    """A retry POST carrying the same stable_id does not create a new entry."""
    stable = "ab" * 16
    first = pending_inputs.record("conv_dedup", [_text_block("hi")], stable_id=stable)
    second = pending_inputs.record("conv_dedup", [_text_block("hi")], stable_id=stable)
    assert first == second
    # Only one entry in the queue — the runner is not re-dispatched.
    assert len(pending_inputs.snapshot_for("conv_dedup")) == 1


def test_record_without_stable_id_always_creates_new_entry() -> None:
    """Messages without stable_id are never deduplicated."""
    first = pending_inputs.record("conv_nodedup", [_text_block("hello")])
    second = pending_inputs.record("conv_nodedup", [_text_block("hello")])
    assert first != second
    assert len(pending_inputs.snapshot_for("conv_nodedup")) == 2


def test_stable_id_dedup_scoped_per_conversation() -> None:
    """Same stable_id in different conversations does not collide."""
    stable = "cd" * 16
    id_a = pending_inputs.record("conv_scope_a", [_text_block("x")], stable_id=stable)
    id_b = pending_inputs.record("conv_scope_b", [_text_block("x")], stable_id=stable)
    assert id_a != id_b


def test_submission_for_resolves_a_live_entry_with_its_identity() -> None:
    """
    A client re-send resolves to the live entry recorded under its stable id.

    The route answers a duplicate POST with the first delivery's pending id
    instead of forwarding again, and needs the content and author the entry
    was posted with to refuse a different message reusing the id. The lookup
    matches the stable id exactly and returns nothing for an unknown or
    discarded one.
    """
    stable = "a" * 32
    pid = pending_inputs.record(
        "conv_a", [_text_block("hi")], created_by="alice@example.com", stable_id=stable
    )

    assert pending_inputs.submission_for("conv_a", stable) == pending_inputs.RecordedSubmission(
        content=[_text_block("hi")], created_by="alice@example.com", pending_id=pid
    )
    assert pending_inputs.submission_for("conv_a", "b" * 32) is None
    assert pending_inputs.submission_for("conv_other", stable) is None

    pending_inputs.resolve_oldest("conv_a")  # discarded (/clear), not persisted
    assert pending_inputs.submission_for("conv_a", stable) is None


def test_committed_submission_is_remembered_until_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A drained submission resolves to its committed item until the TTL passes.

    After the forwarder drains the entry, a client retry of the same stable
    id must find the persisted item (so the prompt is not pasted twice) for
    as long as a still-open tab could plausibly retry, and no longer. The
    posted content and author travel with it.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])
    stable = "c" * 32
    drained = pending_inputs.DrainedInput(
        pending_id="pending_x",
        content=[_text_block("hi")],
        created_by="alice@example.com",
        stable_id=stable,
    )

    assert pending_inputs.submission_for("conv_a", stable) is None
    pending_inputs.remember_committed("conv_a", drained, "item_1")
    assert pending_inputs.submission_for("conv_a", stable) == pending_inputs.RecordedSubmission(
        content=[_text_block("hi")], created_by="alice@example.com", item_id="item_1"
    )
    assert pending_inputs.submission_for("conv_other", stable) is None

    clock["t"] = 1000.0 + pending_inputs._COMMITTED_TTL_S - 0.1
    assert pending_inputs.submission_for("conv_a", stable).item_id == "item_1"

    clock["t"] = 1000.0 + pending_inputs._COMMITTED_TTL_S + 0.1
    assert pending_inputs.submission_for("conv_a", stable) is None


def test_persisting_submission_resolves_to_its_pending_id_until_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Between drain and commit a submission is still known — by its pending id.

    Draining the entry and appending the mirrored item are separated by an
    await. A re-send in that window must not be told the message is committed
    (the append may still fail), so it gets the pending id; a failed append
    restores the entry to the queue, a successful one commits it. A marker
    nobody settles is dropped with the pending-input TTL.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])
    stable = "d" * 32
    pid = pending_inputs.record("conv_a", [_text_block("hi")], stable_id=stable)

    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None
    pending_inputs.begin_persist("conv_a", drained)
    known = pending_inputs.submission_for("conv_a", stable)
    assert known is not None and (known.pending_id, known.item_id) == (pid, None)
    assert pending_inputs.snapshot_for("conv_a") == []

    pending_inputs.restore("conv_a", drained)  # the append failed
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [pid]
    assert pending_inputs.submission_for("conv_a", stable).pending_id == pid

    drained = pending_inputs.resolve_oldest("conv_a")
    assert drained is not None
    pending_inputs.begin_persist("conv_a", drained)
    pending_inputs.remember_committed("conv_a", drained, "item_1")  # the retry landed
    assert pending_inputs.submission_for("conv_a", stable).item_id == "item_1"
    assert pending_inputs._persisting.get("conv_a") is None

    pending_inputs.begin_persist(
        "conv_b", pending_inputs.DrainedInput(pending_id="pending_y", content=[], stable_id=stable)
    )
    clock["t"] += pending_inputs._TTL_S + 1
    assert pending_inputs.submission_for("conv_b", stable) is None


def test_committed_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Committed submissions are swept on write: by age and by a per-conversation cap.

    The cap is a memory bound far above a day of one conversation's messages;
    within it, only age evicts, so a retry inside the window always resolves.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])

    def drained(stable: str) -> pending_inputs.DrainedInput:
        return pending_inputs.DrainedInput(pending_id="p", content=[], stable_id=stable)

    pending_inputs.remember_committed("conv_a", drained("a" * 32), "item_a")
    clock["t"] = 1000.0 + pending_inputs._COMMITTED_TTL_S + 1
    pending_inputs.remember_committed(
        "conv_a", drained("b" * 32), "item_b"
    )  # sweeps the stale one
    assert pending_inputs.submission_for("conv_a", "a" * 32) is None
    assert pending_inputs.submission_for("conv_a", "b" * 32).item_id == "item_b"

    cap = pending_inputs._COMMITTED_MAX_PER_CONVERSATION
    assert cap >= 4096
    for i in range(cap):
        pending_inputs.remember_committed("conv_cap", drained(f"{i:032x}"), f"item_{i}")
    assert pending_inputs.submission_for("conv_cap", f"{0:032x}").item_id == "item_0"
    pending_inputs.remember_committed("conv_cap", drained(f"{cap:032x}"), "item_over")
    assert pending_inputs.submission_for("conv_cap", f"{0:032x}") is None
    assert pending_inputs.submission_for("conv_cap", f"{1:032x}").item_id == "item_1"


def test_dispatched_memory_tracks_delivery_not_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Only a forward the runner accepted counts as done, and only until the TTL.

    The SDK path persists the item before forwarding, so the store's dedup
    alone would answer a retry of a rejected forward with success. Nothing is
    recorded for a failed forward, so that retry dispatches again.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])
    stable = "b" * 32

    assert pending_inputs.dispatch_done("conv_a", stable) is False
    pending_inputs.mark_dispatched("conv_a", stable)
    assert pending_inputs.dispatch_done("conv_a", stable) is True
    assert pending_inputs.dispatch_done("conv_other", stable) is False
    clock["t"] = 1000.0 + pending_inputs._COMMITTED_TTL_S + 1
    assert pending_inputs.dispatch_done("conv_a", stable) is False


def test_snapshot_carries_the_web_stable_id() -> None:
    """A reloading client matches its un-acked send to the snapshot entry by identity."""
    pending_inputs.record("conv_a", [_text_block("mine")], stable_id="a" * 32)
    pending_inputs.record("conv_a", [_text_block("typed in the terminal")])
    snapshot = pending_inputs.snapshot_for("conv_a")
    assert [entry.get("stable_id") for entry in snapshot] == ["a" * 32, None]
    assert "stable_id" not in snapshot[1]


def test_dispatched_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Successful sends are swept on write: by age and by a per-conversation cap.

    Ordinary sends are never queried again, so eviction cannot rely on reads.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(pending_inputs, "_now", lambda: clock["t"])

    pending_inputs.mark_dispatched("conv_a", "a" * 32)
    clock["t"] = 1000.0 + pending_inputs._COMMITTED_TTL_S + 1
    pending_inputs.mark_dispatched("conv_a", "b" * 32)  # the write sweeps the stale one
    assert pending_inputs.dispatch_done("conv_a", "a" * 32) is False
    assert pending_inputs.dispatch_done("conv_a", "b" * 32) is True

    for i in range(pending_inputs._COMMITTED_MAX_PER_CONVERSATION + 5):
        pending_inputs.mark_dispatched("conv_cap", f"{i:032x}")
    assert pending_inputs.dispatch_done("conv_cap", f"{0:032x}") is False
    assert pending_inputs.dispatch_done("conv_cap", f"{5:032x}") is True
    assert (
        pending_inputs.dispatch_done(
            "conv_cap", f"{pending_inputs._COMMITTED_MAX_PER_CONVERSATION + 4:032x}"
        )
        is True
    )
