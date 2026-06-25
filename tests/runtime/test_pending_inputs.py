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
