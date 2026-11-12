"""Which Claude MCP servers signed in after a claude-native pane started."""

from __future__ import annotations

import pytest

from omnigent.runner.native import claude_mcp_sign_in
from omnigent.runner.native.claude_mcp_sign_in import ClaudeMcpSignInTracker


class _Pane:
    """Stands in for a terminal instance; only its identity matters."""


@pytest.fixture
def statuses(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The host's reported Claude sign-in state, by server name, for the test to edit."""
    reported: dict[str, str] = {}
    monkeypatch.setattr(
        claude_mcp_sign_in,
        "harness_sign_in_by_live_name",
        lambda harness: dict(reported) if harness == "claude" else {},
    )
    return reported


def _launch(tracker: ClaudeMcpSignInTracker, session_id: str) -> _Pane:
    """Start a pane the way the runner does: record, launch, hand the record over."""
    launch = object()
    tracker.note_launching(session_id, launch)
    pane = _Pane()
    tracker.note_launched(session_id, pane, launch=launch)
    return pane


def test_a_server_signed_in_after_launch_is_named(statuses: dict[str, str]) -> None:
    """Only a server that needed sign-in as the pane started, and is connected now, counts.

    The launch's record belongs to its pane, so a sign-in that finished before
    the pane is first checked still counts.
    """
    tracker = ClaudeMcpSignInTracker()
    statuses.update({"linear": "sign_in_required", "plugin:docs:notion": "sign_in_required"})
    pane = _launch(tracker, "conv_a")
    statuses.update({"linear": "connected", "github": "connected"})  # github: not known at launch

    assert tracker.signed_in_since_launch("conv_a", pane) == {"linear"}
    statuses["plugin:docs:notion"] = "connected"
    assert tracker.signed_in_since_launch("conv_a", pane) == {"linear", "plugin:docs:notion"}


def test_a_moved_pane_keeps_its_record(statuses: dict[str, str]) -> None:
    """/clear and fork hand the running pane to a successor session; its record follows."""
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    pane = _launch(tracker, "conv_a")
    statuses["linear"] = "connected"

    tracker.transfer("conv_a", "conv_b")

    assert tracker.signed_in_since_launch("conv_b", pane) == {"linear"}
    # The source keeps nothing, so its later deletion can't drop the successor's record.
    tracker.forget("conv_a")
    assert tracker.signed_in_since_launch("conv_b", pane) == {"linear"}


def test_a_relaunched_pane_starts_afresh(statuses: dict[str, str]) -> None:
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    first = _launch(tracker, "conv_a")
    statuses["linear"] = "connected"
    assert tracker.signed_in_since_launch("conv_a", first) == {"linear"}

    assert tracker.signed_in_since_launch("conv_a", _launch(tracker, "conv_a")) == frozenset()


def test_a_launch_that_finds_its_pane_running_keeps_the_pane_record(
    statuses: dict[str, str],
) -> None:
    """Attaching to a running pane must not reset what that pane started with."""
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    pane = _launch(tracker, "conv_a")
    statuses["linear"] = "connected"

    launch = object()
    tracker.note_launching("conv_a", launch)
    tracker.note_launched("conv_a", pane, launch=launch, previous=pane)

    assert tracker.signed_in_since_launch("conv_a", pane) == {"linear"}


def test_a_launch_without_a_pane_leaves_nothing_for_a_later_pane(
    statuses: dict[str, str],
) -> None:
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    launch = object()
    tracker.note_launching("conv_a", launch)
    tracker.note_launched("conv_a", None, launch=launch)
    statuses["linear"] = "connected"

    assert tracker.signed_in_since_launch("conv_a", _Pane()) == frozenset()


def test_a_pane_the_runner_did_not_start_is_recorded_when_first_checked(
    statuses: dict[str, str],
) -> None:
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    pane = _Pane()
    assert tracker.signed_in_since_launch("conv_a", pane) == frozenset()
    statuses["linear"] = "connected"

    assert tracker.signed_in_since_launch("conv_a", pane) == {"linear"}
    tracker.forget("conv_a")
    assert tracker.signed_in_since_launch("conv_a", pane) == frozenset()


def test_a_launch_that_never_recorded_leaves_another_launch_record(
    statuses: dict[str, str],
) -> None:
    """A caller cancelled while another launch held the lock can't discard that launch's record."""
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    launching, waiting = object(), object()
    tracker.note_launching("conv_a", launching)
    # The waiter is cancelled before it records anything, and finishes with its own token.
    tracker.note_launched("conv_a", None, launch=waiting)
    pane = _Pane()
    tracker.note_launched("conv_a", pane, launch=launching)
    statuses["linear"] = "connected"

    assert tracker.signed_in_since_launch("conv_a", pane) == {"linear"}


def test_a_late_check_for_a_replaced_pane_keeps_the_new_pane_record(
    statuses: dict[str, str],
) -> None:
    """A check still reading for the old pane after a restart keeps the new pane's record."""
    tracker = ClaudeMcpSignInTracker()
    statuses["linear"] = "sign_in_required"
    old_pane = _Pane()
    new_pane = _launch(tracker, "conv_a")
    statuses["linear"] = "connected"

    assert tracker.signed_in_since_launch("conv_a", old_pane) == frozenset()
    assert tracker.signed_in_since_launch("conv_a", new_pane) == {"linear"}
