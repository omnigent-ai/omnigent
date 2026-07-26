"""Turn-end stamping: which status edges record ``omnigent.last_turn_at``.

A caller deciding whether to continue a session or start a fresh one needs
to know how long ago it stopped working — a recently-finished session still
has a warm cached prefix, an hour-old one does not. ``_publish_status`` is
the one place both relay and terminal-backed harnesses report a turn
ending, so the stamp is written there.

The edge selection is the whole substance: relays re-publish a steady
``idle``, so stamping on every publish rather than on the transition would
keep reporting a long-quiet session as having just finished — exactly
inverting the decision the timestamp exists to inform.
"""

from __future__ import annotations

import pytest

from omnigent.server import session_live_state
from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import _publish_status

_SID = "conv_turn_stamp_test"


@pytest.fixture()
def stamped(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record turn-end stamps instead of writing them, and isolate cache state."""
    calls: list[str] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_last_turn_at",
        lambda session_id: calls.append(session_id),
    )
    _sessions_mod._session_status_cache.pop(_SID, None)
    yield calls
    _sessions_mod._session_status_cache.pop(_SID, None)


def test_running_to_idle_stamps_turn_end(stamped: list[str]) -> None:
    """The ordinary turn-completion edge records the timestamp."""
    _publish_status(_SID, "running")
    _publish_status(_SID, "idle")
    assert stamped == [_SID]


def test_running_to_failed_stamps_turn_end(stamped: list[str]) -> None:
    """A turn that errored still ended — the session stopped working."""
    _publish_status(_SID, "running")
    _publish_status(_SID, "failed")
    assert stamped == [_SID]


def test_mid_turn_edges_do_not_stamp(stamped: list[str]) -> None:
    """``running`` / ``waiting`` are not turn ends.

    ``waiting`` means blocked on an approval prompt with the turn still
    open; stamping there would report the session as finished while it is
    mid-flight.
    """
    _publish_status(_SID, "running")
    _publish_status(_SID, "waiting")
    assert stamped == []


def test_repeated_idle_stamps_once(stamped: list[str]) -> None:
    """Re-publishing a settled ``idle`` must not advance the stamp.

    The PTY-activity watcher emits trailing ``idle`` edges long after the
    turn ended; each one re-stamping would make an idle session look
    perpetually just-finished and its cached prefix perpetually warm.
    """
    _publish_status(_SID, "running")
    _publish_status(_SID, "idle")
    _publish_status(_SID, "idle")
    _publish_status(_SID, "idle")
    assert stamped == [_SID]


def test_sticky_failed_swallows_trailing_idle(stamped: list[str]) -> None:
    """A trailing ``idle`` after ``failed`` neither clears the error nor re-stamps.

    ``failed`` is terminal and already stamped; the follow-on quiescence
    ``idle`` returns early, so the turn end keeps the time it actually
    happened.
    """
    _publish_status(_SID, "running")
    _publish_status(_SID, "failed")
    _publish_status(_SID, "idle")
    assert stamped == [_SID]


def test_next_turn_stamps_again(stamped: list[str]) -> None:
    """A new turn produces a new stamp — the value tracks the latest turn."""
    _publish_status(_SID, "running")
    _publish_status(_SID, "idle")
    _publish_status(_SID, "running")
    _publish_status(_SID, "idle")
    assert stamped == [_SID, _SID]
