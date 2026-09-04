"""E2E regression test: a pending approval must survive a server restart.

A pending approval prompt used to live only in RAM. After the Omnigent
server restarts, the ``pending_elicitations`` index starts empty while the
``omnigent_conversation_metadata.pending_elicitation_count`` row still
carries the pre-restart count, so the sidebar badge stays lit
(``max(0, 1) == 1``) while ``GET /v1/sessions/{id}`` replays nothing —
the user sees one approval waiting and nothing to answer, and the parked
runner turn is silently denied a day later.

These tests exercise the whole journey at the module level (``publish`` →
restart → read back), against a real SQLite-backed store, and assert the
*correct* behavior: after a restart the prompt is still counted, still
replayed, and still answerable — the badge and the approval card agree.

On a tree without durable elicitation storage they fail with the observed
bug (the prompt is gone while the persisted badge count still claims one is
waiting).

Usage::

    pytest tests/e2e/test_pending_elicitation_restart_e2e.py -v
"""

from __future__ import annotations

from pathlib import Path

from omnigent.runtime import pending_elicitations, session_stream

# A realistic session id: conversation ids are 32-hex UUIDs (the DB column
# stores them as 16 raw bytes), so a non-hex placeholder would not persist.
_CONV = "conv_3f2a9c1d5e6b47a8b9c0d1e2f3a4b5c6"
_ELICIT_ID = "elicit_e2e_restart_survival"
_EVENT: dict[str, object] = {
    "type": "response.elicitation_request",
    "elicitation_id": _ELICIT_ID,
    "params": {"message": "Approve `rm -rf /tmp/x`?"},
}


def _wire_durable_store(tmp_path: Path) -> None:
    """Wire the durable elicitation store the way ``omnigent server`` does.

    On a tree without durable elicitation storage this is a no-op — which is
    exactly the defect: there is nowhere for a parked prompt to land, so the
    restart takes it along and the assertions below observe the loss.
    """
    try:
        from omnigent.stores.elicitation_store.sqlalchemy_store import (
            SqlAlchemyElicitationStore,
        )
    except ImportError:
        return
    uri = f"sqlite:///{tmp_path / 'elicitations_e2e.db'}"
    pending_elicitations.set_store(SqlAlchemyElicitationStore(uri))


def _simulate_server_restart(tmp_path: Path) -> None:
    """Stand in for a server restart, then serve the first session read.

    The real sequence is: server process dies → the in-memory index is
    garbage-collected → a new process starts with an empty index, rewires its
    stores at startup, and the first ``GET /v1/sessions/{id}`` reloads the
    session's outstanding prompts from durable storage. ``reset_for_tests()``
    produces the identical observable state without forking a new process;
    the store rewire plus the cold-load read are the startup parts that
    matter here. On a tree with no durable storage both are no-ops — the
    defect these tests observe.
    """
    pending_elicitations.reset_for_tests()
    _wire_durable_store(tmp_path)
    restore = getattr(pending_elicitations, "restore_for", None)
    if restore is not None:
        restore(_CONV)


def test_pending_approval_survives_server_restart(tmp_path: Path) -> None:
    """The parked prompt is still counted, replayed, and answerable.

    This is the sidebar/chat agreement the user observes: the badge says one
    approval is waiting *and* opening the session renders the approval card.
    A tree where the prompt lives only in RAM fails all three reads — the
    badge (fed by the persisted count) keeps claiming an approval that the
    chat can no longer render or resolve.
    """
    pending_elicitations.reset_for_tests()
    _wire_durable_store(tmp_path)
    try:
        session_stream.publish(_CONV, _EVENT)
        assert pending_elicitations.count_for(_CONV) == 1  # pre-condition
        assert len(pending_elicitations.snapshot_for(_CONV)) == 1  # pre-condition

        _simulate_server_restart(tmp_path)

        snapshot = pending_elicitations.snapshot_for(_CONV)
        assert [e["elicitation_id"] for e in snapshot] == [_ELICIT_ID], (
            "a parked approval must still be replayed after a server restart; "
            f"got {snapshot!r} — the prompt died with the in-memory index while "
            "the persisted badge count still claims one is waiting, so the "
            "sidebar shows an approval the chat cannot render or answer"
        )
        assert pending_elicitations.count_for(_CONV) == 1, (
            "the restored prompt must be counted so the sidebar badge and the approval card agree"
        )
        found = pending_elicitations.lookup(_ELICIT_ID)
        assert found is not None and found[0] == _CONV, (
            "the standalone approval page resolves by lookup; None means the "
            "page tells the user a still-pending approval was already handled"
        )
    finally:
        pending_elicitations.reset_for_tests()


def test_resolved_approval_stays_resolved_across_restart(tmp_path: Path) -> None:
    """An answered prompt must not come back after a restart.

    The other half of correctness: durability must not resurrect prompts.
    Once resolved, a restart leaves the session with nothing pending — no
    stuck badge, no card for a question whose awaiter no longer exists.
    """
    pending_elicitations.reset_for_tests()
    _wire_durable_store(tmp_path)
    try:
        session_stream.publish(_CONV, _EVENT)
        pending_elicitations.resolve(_CONV, _ELICIT_ID)
        assert pending_elicitations.count_for(_CONV) == 0  # pre-condition

        _simulate_server_restart(tmp_path)

        assert pending_elicitations.snapshot_for(_CONV) == []
        assert pending_elicitations.count_for(_CONV) == 0, (
            "a resolved approval must not be restored — the badge would claim "
            "an approval that was already answered"
        )
    finally:
        pending_elicitations.reset_for_tests()
