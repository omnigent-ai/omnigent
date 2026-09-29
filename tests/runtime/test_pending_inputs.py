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

    dropped = pending_inputs.resolve("conv_a", drop)
    assert dropped is not None
    assert (dropped.pending_id, dropped.content) == (drop, [_text_block("drop")])
    assert [e["pending_id"] for e in pending_inputs.snapshot_for("conv_a")] == [keep]
    # Idempotent — resolving an already-removed id does nothing and returns None.
    assert pending_inputs.resolve("conv_a", drop) is None
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


def test_record_evicts_the_oldest_entry_beyond_the_per_conversation_cap() -> None:
    """The queue is bounded: recording past the cap drops the oldest entry."""
    cap = pending_inputs._MAX_ENTRIES_PER_CONVERSATION
    ids = [pending_inputs.record("conv_a", [_text_block(f"m{i}")]) for i in range(cap + 2)]

    snapshot = pending_inputs.snapshot_for("conv_a")

    assert len(snapshot) == cap
    assert [entry["pending_id"] for entry in snapshot] == ids[2:]


def test_held_entries_keep_their_slot_while_the_queue_refills() -> None:
    """A drain with ``hold`` leaves the entry in place; refilling evicts unheld ones only."""
    cap = pending_inputs._MAX_ENTRIES_PER_CONVERSATION
    ids = [pending_inputs.record("conv_a", [_text_block(f"m{i}")]) for i in range(cap)]

    held = pending_inputs.resolve_oldest("conv_a", hold=True)
    assert held is not None and held.pending_id == ids[0]
    # Other drains skip the held entry.
    assert pending_inputs.resolve_matching_text("conv_a", "m0").matched is None
    newer = [pending_inputs.record("conv_a", [_text_block(f"n{i}")]) for i in range(2)]
    # Only unheld entries count against the cap: one refill fits, the second
    # evicts the oldest UNHELD entry; the held head is still first.
    after_refill = [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")]
    assert after_refill == [ids[0], *ids[2:], *newer]
    assert len(after_refill) == cap + 1

    pending_inputs.restore("conv_a", held)

    restored = [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")]
    assert restored == after_refill
    assert pending_inputs.resolve_oldest("conv_a") is not None  # unheld again


def test_release_drops_a_held_entry() -> None:
    """Settling a held entry removes it; releasing twice is harmless."""
    first = pending_inputs.record("conv_a", [_text_block("first")])
    second = pending_inputs.record("conv_a", [_text_block("second")])

    held = pending_inputs.resolve_matching_text("conv_a", "first", hold=True)
    assert held.matched is not None and held.matched.pending_id == first
    pending_inputs.release("conv_a", held.matched)
    pending_inputs.release("conv_a", held.matched)

    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [second]


def test_resolve_matching_text_drops_only_generated_leading_markers() -> None:
    """A message with a file block matches behind its generated marker; typed text is kept."""
    with_image = pending_inputs.record(
        "conv_a", [{"type": "input_image", "url": "img://1"}, _text_block("look at this")]
    )
    literal = pending_inputs.record("conv_a", [_text_block("explain [Attached: example]")])

    first = pending_inputs.resolve_matching_text(
        "conv_a", "[Attached: /tmp/x.png]\n\nlook at this"
    )
    second = pending_inputs.resolve_matching_text("conv_a", "explain [Attached: example]")

    assert first.matched is not None and first.matched.pending_id == with_image
    assert first.skipped == []
    assert second.matched is not None and second.matched.pending_id == literal
    assert second.skipped == []
    assert pending_inputs.snapshot_for("conv_a") == []


def test_record_keeps_a_new_entry_when_every_other_entry_is_held() -> None:
    """A fresh record is never the eviction victim, even with the whole cap held."""
    cap = pending_inputs._MAX_ENTRIES_PER_CONVERSATION
    ids = [pending_inputs.record("conv_a", [_text_block(f"m{i}")]) for i in range(cap)]
    held = pending_inputs.resolve_matching_text("conv_a", f"m{cap - 1}", hold=True)
    assert held.matched is not None and len(held.skipped) == cap - 1

    newest = pending_inputs.record("conv_a", [_text_block("newest")])

    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [
        *ids,
        newest,
    ]
    found = pending_inputs.resolve_matching_text("conv_a", "newest")
    assert found.matched is not None and found.matched.pending_id == newest


def test_resolve_matching_text_prefers_an_exact_match_over_marker_stripping() -> None:
    """Two messages differing only in a typed leading marker stay distinct."""
    first = pending_inputs.record("conv_a", [_text_block("[Attached: literal-a] same")])
    second = pending_inputs.record("conv_a", [_text_block("[Attached: literal-b] same")])

    drained = pending_inputs.resolve_matching_text("conv_a", "[Attached: literal-b] same")

    assert drained.matched is not None and drained.matched.pending_id == second
    assert [entry.pending_id for entry in drained.skipped] == [first]


def test_resolve_matching_text_keeps_a_typed_marker_distinct_from_a_generated_one() -> None:
    """A typed ``[Attached: …]`` phrase never stands in for the executor's marker line."""
    typed = pending_inputs.record("conv_a", [_text_block("[Attached: literal] same")])
    with_image = pending_inputs.record(
        "conv_a", [{"type": "input_image", "url": "img://1"}, _text_block("same")]
    )

    drained = pending_inputs.resolve_matching_text("conv_a", "[Attached: /tmp/x.png]\n\nsame")

    assert drained.matched is not None and drained.matched.pending_id == with_image
    assert [entry.pending_id for entry in drained.skipped] == [typed]


def test_resolve_matching_text_identical_texts_drain_in_queue_order() -> None:
    """Identical texts are indistinguishable, so the oldest one takes the mirror."""
    first = pending_inputs.record("conv_a", [_text_block("yes")])
    second = pending_inputs.record("conv_a", [_text_block("yes")])

    drained = pending_inputs.resolve_matching_text("conv_a", "yes")

    assert drained.matched is not None and drained.matched.pending_id == first
    assert drained.skipped == []
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")] == [second]


def test_resolve_matching_text_reports_at_most_a_cap_of_skipped_entries() -> None:
    """A drain surfaces at most a cap's worth of skipped entries and leaves the rest queued."""
    cap = pending_inputs._MAX_ENTRIES_PER_CONVERSATION
    first_wave = [pending_inputs.record("conv_a", [_text_block(f"a{i}")]) for i in range(cap)]
    held = pending_inputs.resolve_matching_text("conv_a", f"a{cap - 1}", hold=True)
    assert held.matched is not None
    second_wave = [pending_inputs.record("conv_a", [_text_block(f"b{i}")]) for i in range(cap)]
    # The append failed: everything is unheld again, twice the cap in queue order.
    for entry in [*held.skipped, held.matched]:
        pending_inputs.restore("conv_a", entry)
    assert len(pending_inputs.snapshot_for("conv_a")) == 2 * cap

    drained = pending_inputs.resolve_matching_text("conv_a", f"b{cap - 1}")

    assert drained.matched is not None and drained.matched.pending_id == second_wave[-1]
    assert [entry.pending_id for entry in drained.skipped] == first_wave
    remaining = [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_a")]
    assert remaining == second_wave[:-1]


def test_pending_id_for_stable_id_finds_only_live_entries() -> None:
    """
    A queued entry is found by the web client's stable id so a resend can be
    answered without a second forward; a settled or unknown id finds nothing.
    """
    stable_id = "7f3a9c1e5b2d4f6a8c0e1d2b3a4f5c6d"
    content = [{"type": "input_text", "text": "hello"}]
    assert pending_inputs.pending_id_for_stable_id("conv_a", stable_id) is None

    pending_id = pending_inputs.record("conv_a", content, stable_id=stable_id)
    assert pending_inputs.pending_id_for_stable_id("conv_a", stable_id) == pending_id
    # Scoped to the conversation and to entries that carry a stable id.
    assert pending_inputs.pending_id_for_stable_id("conv_b", stable_id) is None
    pending_inputs.record("conv_a", content)
    assert pending_inputs.pending_id_for_stable_id("conv_a", stable_id) == pending_id

    pending_inputs.resolve("conv_a", pending_id)
    assert pending_inputs.pending_id_for_stable_id("conv_a", stable_id) is None


def test_mark_uncertain_keeps_jumped_over_entries_out_of_the_undelivered_set() -> None:
    """Entries queued during a positional drain are later drained as uncertain, not skipped."""
    first = pending_inputs.record("conv_a", [_text_block("first")])
    second = pending_inputs.record("conv_a", [_text_block("second")])

    # A reformatted mirror matched nothing and drained the oldest entry.
    drained = pending_inputs.resolve_oldest("conv_a", hold=True)
    assert drained is not None and drained.pending_id == first
    pending_inputs.mark_uncertain("conv_a")
    pending_inputs.release("conv_a", drained)
    third = pending_inputs.record("conv_a", [_text_block("third")])

    matched = pending_inputs.resolve_matching_text("conv_a", "third")

    assert matched.matched is not None and matched.matched.pending_id == third
    assert matched.skipped == []
    assert [entry.pending_id for entry in matched.uncertain] == [second]
    assert pending_inputs.snapshot_for("conv_a") == []
