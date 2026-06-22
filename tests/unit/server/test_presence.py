"""Tests for the session presence registry (``omnigent/server/presence.py``).

Broadcasts are observed through a real ``session_stream.subscribe``
collector — the same pub/sub path the SSE route consumes — so every
test exercises the actual publish pipeline, not a mock. Each
``session.presence`` event carries the FULL viewer list (the
protocol's self-healing contract), so assertions compare whole
payloads.

Presence is scoped to a session tree's ROOT conversation: viewers of
the root and of any sub-agent conversation under it share one viewer
list, and broadcasts fan out to every viewed stream in the tree, each
stamped with that stream's own ``conversation_id``. The single-
conversation tests pass the same id for both (a top-level session is
its own root); the sub-agent tests exercise the cross-stream fan-out.

The leave-grace timer is the registry's core flap-protection (the
Databricks Apps ingress drops every stream ~5 minutes, and the grace
window is what keeps avatars from flickering on each reconnect);
tests shrink ``_LEAVE_GRACE_S`` via monkeypatch the same way
``test_session_updates_ws.py`` shrinks its rescan cadence.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server import presence
from tests.unit.server.helpers import start_session_stream_collector

pytestmark = pytest.mark.asyncio

CONV = "conv_presence_test"
# A session tree for the sub-agent scoping tests: CHILD is a sub-agent
# conversation whose ``root_conversation_id`` is ROOT.
ROOT = "conv_presence_root"
CHILD = "conv_presence_child"
ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture(autouse=True)
def _reset_presence() -> Any:
    """Isolate the module-global registry (and its timers) per test."""
    presence.reset_for_tests()
    yield
    presence.reset_for_tests()


def _viewer_ids(event: dict[str, Any]) -> list[str]:
    """
    Extract viewer user ids from a presence event, preserving order.

    :param event: A ``session.presence`` event dict.
    :returns: User ids, e.g. ``["alice@example.com"]``.
    """
    return [viewer["user_id"] for viewer in event["viewers"]]


async def test_connect_broadcasts_full_state_join() -> None:
    """The 0→1 connection edge broadcasts the complete viewer list."""
    collector = await start_session_stream_collector(CONV)
    try:
        presence.connect(CONV, CONV, ALICE, idle=False)
        event = await collector.next_event()
        # Full-state contract: type, conversation, and the complete
        # viewer payload — a delta-shaped or empty event here means
        # clients can no longer replace state wholesale.
        assert event["type"] == "session.presence"
        assert event["conversation_id"] == CONV
        assert _viewer_ids(event) == [ALICE]
        assert event["viewers"][0]["idle"] is False
        # joined_at is a concrete ISO-Z timestamp, not a placeholder.
        assert event["viewers"][0]["joined_at"].endswith("Z")
        # snapshot() (the snapshot-on-connect payload) must agree with
        # the broadcast — same builder, same self-healing contract.
        assert presence.snapshot(CONV, CONV)["viewers"] == event["viewers"]
    finally:
        await collector.stop()


async def test_second_tab_same_user_is_silent() -> None:
    """A 1→2 connection edge neither broadcasts nor duplicates the viewer."""
    collector = await start_session_stream_collector(CONV)
    try:
        presence.connect(CONV, CONV, ALICE, idle=False)
        await collector.next_event()
        presence.connect(CONV, CONV, ALICE, idle=False)
        # No broadcast: same user, unchanged idle aggregate. A frame
        # here means multi-tab users spam co-viewers with no-op events.
        await collector.assert_no_event(within=0.2)
        assert _viewer_ids(presence.snapshot(CONV, CONV)) == [ALICE]
    finally:
        await collector.stop()


async def test_idle_aggregate_flips_when_only_active_tab_closes() -> None:
    """User idle = AND over tabs; closing the sole active tab greys them."""
    collector = await start_session_stream_collector(CONV)
    try:
        active_token = presence.connect(CONV, CONV, ALICE, idle=False)
        join = await collector.next_event()
        assert join["viewers"][0]["idle"] is False
        # Second, idle tab: aggregate stays active (one tab visible).
        presence.connect(CONV, CONV, ALICE, idle=True)
        await collector.assert_no_event(within=0.2)
        assert presence.snapshot(CONV, CONV)["viewers"][0]["idle"] is False
        # Close the active tab: only the idle tab remains → aggregate
        # flips → broadcast. Silence here means a user who minimizes
        # their last visible window never greys out.
        presence.disconnect(CONV, ALICE, active_token)
        flipped = await collector.next_event()
        assert _viewer_ids(flipped) == [ALICE]
        assert flipped["viewers"][0]["idle"] is True
    finally:
        await collector.stop()


async def test_leave_broadcasts_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last disconnect defers the leave broadcast by the grace window."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)
    collector = await start_session_stream_collector(CONV)
    try:
        token = presence.connect(CONV, CONV, ALICE, idle=False)
        await collector.next_event()
        presence.disconnect(CONV, ALICE, token)
        # Inside the grace window the viewer is still present (frozen),
        # so co-viewers and snapshot-on-connect joiners don't see a
        # flicker on what may be a transparent reconnect.
        assert _viewer_ids(presence.snapshot(CONV, CONV)) == [ALICE]
        # After the timer fires: full-state leave broadcast with the
        # user gone. No event here = ghost viewers that never clear.
        leave = await collector.next_event()
        assert leave["type"] == "session.presence"
        assert leave["viewers"] == []
        assert presence.snapshot(CONV, CONV)["viewers"] == []
    finally:
        await collector.stop()


async def test_reconnect_within_grace_cancels_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect inside the grace window is invisible to co-viewers."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.3)
    collector = await start_session_stream_collector(CONV)
    try:
        token = presence.connect(CONV, CONV, ALICE, idle=False)
        join = await collector.next_event()
        original_joined_at = join["viewers"][0]["joined_at"]
        presence.disconnect(CONV, ALICE, token)
        presence.connect(CONV, CONV, ALICE, idle=False)
        # Past the grace deadline: neither a leave nor a rejoin frame.
        # A leave event here means the reconnect failed to cancel the
        # timer — every ~5-min ingress reconnect would flicker avatars.
        await collector.assert_no_event(within=0.6)
        snapshot = presence.snapshot(CONV, CONV)
        assert _viewer_ids(snapshot) == [ALICE]
        # joined_at survives the reconnect: the entry was reused, not
        # recreated (recreation would reset everyone's "since" times).
        assert snapshot["viewers"][0]["joined_at"] == original_joined_at
    finally:
        await collector.stop()


async def test_reconnect_with_changed_idle_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle flip arrives as a reconnect carrying the new flag."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.3)
    collector = await start_session_stream_collector(CONV)
    try:
        token = presence.connect(CONV, CONV, ALICE, idle=False)
        await collector.next_event()
        presence.disconnect(CONV, ALICE, token)
        presence.connect(CONV, CONV, ALICE, idle=True)
        # The reconnect changed the idle aggregate, so it must
        # broadcast — this is the wire path for "went idle" (there is
        # no separate set-idle endpoint). Silence means co-viewers
        # never see anyone grey out.
        flipped = await collector.next_event()
        assert _viewer_ids(flipped) == [ALICE]
        assert flipped["viewers"][0]["idle"] is True
    finally:
        await collector.stop()


async def test_disconnect_unknown_token_is_noop() -> None:
    """Stale/foreign tokens (and unknown users) change nothing."""
    collector = await start_session_stream_collector(CONV)
    try:
        presence.connect(CONV, CONV, ALICE, idle=False)
        await collector.next_event()
        presence.disconnect(CONV, ALICE, "bogus-token")
        presence.disconnect(CONV, BOB, "bogus-token")
        # Either call broadcasting (or mutating the registry) would
        # mean a disconnect race could evict a live connection.
        await collector.assert_no_event(within=0.2)
        assert _viewer_ids(presence.snapshot(CONV, CONV)) == [ALICE]
    finally:
        await collector.stop()


async def test_two_users_full_state_ordered_by_join() -> None:
    """Each join rebroadcasts everyone, ordered by join time."""
    collector = await start_session_stream_collector(CONV)
    try:
        presence.connect(CONV, CONV, ALICE, idle=False)
        first = await collector.next_event()
        assert _viewer_ids(first) == [ALICE]
        presence.connect(CONV, CONV, BOB, idle=True)
        second = await collector.next_event()
        # Bob's join carries BOTH viewers (full state), earlier joiner
        # first, each with their own idle flag. A single-viewer payload
        # here means the protocol regressed to deltas.
        assert _viewer_ids(second) == [ALICE, BOB]
        assert second["viewers"][0]["idle"] is False
        assert second["viewers"][1]["idle"] is True
    finally:
        await collector.stop()


async def test_root_and_subagent_viewers_share_one_list() -> None:
    """Viewers on different conversations of one tree see each other."""
    root_collector = await start_session_stream_collector(ROOT)
    child_collector = await start_session_stream_collector(CHILD)
    try:
        presence.connect(ROOT, ROOT, ALICE, idle=False)
        await root_collector.next_event()
        # Bob opens the SUB-AGENT conversation's stream. This is the
        # regression path: with per-conversation scoping his join never
        # reached Alice's root stream, so her avatar row stayed empty.
        presence.connect(ROOT, CHILD, BOB, idle=False)
        root_event = await root_collector.next_event()
        # Alice (on the root page) sees both viewers; the event rides
        # her own stream, so it's stamped with the root conversation.
        assert root_event["conversation_id"] == ROOT
        assert _viewer_ids(root_event) == [ALICE, BOB]
        child_event = await child_collector.next_event()
        # Bob (on the sub-agent page) gets the SAME tree-wide list,
        # stamped with the child conversation his client guards on — a
        # root-stamped event here would be dropped by the web client.
        assert child_event["conversation_id"] == CHILD
        assert _viewer_ids(child_event) == [ALICE, BOB]
        # Snapshot-on-connect for a sub-agent stream reports the full
        # tree, so a fresh joiner on the child page sees root viewers.
        assert _viewer_ids(presence.snapshot(ROOT, CHILD)) == [ALICE, BOB]
    finally:
        await root_collector.stop()
        await child_collector.stop()


async def test_subagent_leave_reaches_remaining_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent viewer's departure updates viewers on other streams."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)
    root_collector = await start_session_stream_collector(ROOT)
    try:
        presence.connect(ROOT, ROOT, ALICE, idle=False)
        await root_collector.next_event()
        token = presence.connect(ROOT, CHILD, BOB, idle=False)
        await root_collector.next_event()
        presence.disconnect(ROOT, BOB, token)
        # After the grace window, Alice's root stream gets the leave —
        # no event here means a sub-agent viewer's avatar would linger
        # on the root page forever after they close the tab.
        leave = await root_collector.next_event()
        assert leave["conversation_id"] == ROOT
        assert _viewer_ids(leave) == [ALICE]
    finally:
        await root_collector.stop()


async def test_navigation_between_tree_conversations_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving root → sub-agent within one tree never flickers presence."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.3)
    root_collector = await start_session_stream_collector(ROOT)
    try:
        presence.connect(ROOT, ROOT, ALICE, idle=False)
        await root_collector.next_event()
        root_token = presence.connect(ROOT, ROOT, BOB, idle=False)
        join = await root_collector.next_event()
        original_joined_at = join["viewers"][1]["joined_at"]
        # Bob navigates from the root page to a sub-agent page: the
        # client tears down the root stream and opens the child's
        # inside the grace window — exactly a same-tree reconnect.
        presence.disconnect(ROOT, BOB, root_token)
        presence.connect(ROOT, CHILD, BOB, idle=False)
        # Past the grace deadline: neither a leave nor a rejoin frame.
        # An event here means in-session navigation flickers avatars
        # for every co-viewer.
        await root_collector.assert_no_event(within=0.6)
        snapshot = presence.snapshot(ROOT, ROOT)
        assert _viewer_ids(snapshot) == [ALICE, BOB]
        # joined_at survives the move: the entry was reused, not
        # recreated, so "since" times don't reset on every navigation.
        assert snapshot["viewers"][1]["joined_at"] == original_joined_at
    finally:
        await root_collector.stop()


async def test_reset_cancels_pending_leave_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset_for_tests`` cancels grace timers so they can't fire later."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)
    collector = await start_session_stream_collector(CONV)
    try:
        token = presence.connect(CONV, CONV, ALICE, idle=False)
        await collector.next_event()
        presence.disconnect(CONV, ALICE, token)
        presence.reset_for_tests()
        # A broadcast after reset means a cancelled timer still fired —
        # exactly the cross-test leak the conftest reset must prevent.
        await collector.assert_no_event(within=0.2)
        assert presence.snapshot(CONV, CONV)["viewers"] == []
    finally:
        await collector.stop()
