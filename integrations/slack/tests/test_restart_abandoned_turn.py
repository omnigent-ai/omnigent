"""A bot restart mid-turn must not leave a Slack thread misleadingly stuck.

A bot restart (deploy, crash, rollout) runs ``SlackOmnigentService.shutdown()``
— the teardown ``app.run``'s finally invokes — which cancels every in-flight
turn task. A thread whose turn was mid-flight must not be left in a permanently
misleading state:

1. the "_Working on it…_" ack placeholder must be deleted, edited, or followed
   by an honest notice — never left as the thread's permanent last word; and
2. the thread→session record survives the restart (SQLite) while the server
   still reports the abandoned turn's session as ``running`` (shutdown only
   severed the bot's own stream reader, not the server-side turn), so a
   follow-up message in the same thread must not be deflected with "I'm still
   working on your previous message … send this again once I've replied" — a
   reply the restarted bot is no longer listening for and will never deliver.

Both tests assert the DESIRED user-visible behavior, so they fail on the buggy
build and become the regression guard once the fix lands.

Same harness as ``test_service.py`` / ``test_integration.py``: the real
service + real ``SQLiteStore``, a recording Slack client from ``fakes``, and
an in-file Omnigent client whose turn hangs mid-flight (the moment a restart
interrupts).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fakes import RecordingSlackClient
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import SessionActivity, SessionInfo
from omnigent_slack.service import _ACK_TEXT, SlackOmnigentService
from omnigent_slack.store import SQLiteStore


class HangingOmnigentClient:
    """An Omnigent client whose turn never finishes within the test.

    ``run_turn`` signals ``turn_underway`` then blocks forever — the turn is
    mid-flight when the bot process shuts down, exactly the state a deploy
    interrupts. ``route_status`` is what the server reports for the session at
    route time (after a restart the abandoned turn's session still reads
    ``running``: the bot's shutdown severed its own stream reader, not the
    server-side turn).
    """

    def __init__(self, *, route_status: str = "idle", pending_action: bool = False) -> None:
        self.turns: list[tuple[str, str]] = []
        self.turn_underway = asyncio.Event()
        self.route_status = route_status
        self.pending_action = pending_action
        self._never = asyncio.Event()  # never set: the turn outlives the test

    async def get_session_activity(self, session_id: str) -> SessionActivity:
        return SessionActivity(status=self.route_status, pending_elicitation=self.pending_action)

    async def get_session_info(self, session_id: str) -> SessionInfo:
        return SessionInfo(harness="claude-native", agent_name="debby")

    async def create_session(
        self, agent_id: str, title: str, *, host_type: str = "external"
    ) -> str:
        return "conv_1"

    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        return "runner_1"

    async def latest_assistant_message(self, session_id: str) -> tuple[str, str] | None:
        return None

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
        host_type: str = "external",
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        self.turn_underway.set()
        await self._never.wait()
        yield {}  # pragma: no cover - the turn is cancelled before any event


class CompletingOmnigentClient(HangingOmnigentClient):
    """A turn that streams a short answer and finishes cleanly."""

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
        host_type: str = "external",
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        self.turn_underway.set()
        yield {"type": "response.output_text.delta", "delta": "done"}
        yield {"type": "response.completed", "response": {"status": "completed"}}


class _Pool:
    """Returns the same client for every server URL (see test_service.FakePool)."""

    def __init__(self, client: HangingOmnigentClient) -> None:
        self._client = client

    async def get(self, server_url: str, user_id: str = "") -> HangingOmnigentClient:
        return self._client


class _Setup:
    """Inert setup flow: the user in these scenarios is already configured."""

    async def prompt_unconfigured(
        self,
        client: Any,
        user_id: str,
        *,
        channel: str,
        thread_ts: str | None,
        in_channel: bool,
    ) -> None:
        raise AssertionError("setup prompt for a configured user")

    async def prompt_relogin(
        self,
        client: Any,
        user_id: str,
        *,
        channel: str,
        thread_ts: str | None,
        in_channel: bool,
    ) -> bool:
        raise AssertionError("re-login prompt outside an auth scenario")


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


def _service(store: SQLiteStore, omnigent: HangingOmnigentClient) -> SlackOmnigentService:
    return SlackOmnigentService(
        store=store,
        pool=_Pool(omnigent),  # type: ignore[arg-type]
        setup=_Setup(),  # type: ignore[arg-type]
        server_url="http://omnigent.test",
    )


async def _restart_mid_turn(tmp_path: Path, slack: RecordingSlackClient) -> SQLiteStore:
    """Drive the reported journey up to the restart.

    mention the bot in a channel → session created, "_Working on it…_" ack
    posted, turn mid-flight → the bot process shuts down (a deploy/restart).
    Returns the store, which — like the real bot's SQLite file — survives the
    restart.
    """
    store = await _store(tmp_path)
    await store.upsert_user_config(
        "T1",
        "U1",
        UserConfig(
            agent_id="ag_1",
            agent_name="debby",
            workspace="/tmp/workspace",
            host_id="h1",
            host_type="external",
        ),
    )
    omnigent = HangingOmnigentClient()
    service = _service(store, omnigent)

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> summarize the build"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await asyncio.wait_for(omnigent.turn_underway.wait(), timeout=5)
    assert omnigent.turns == [("conv_1", "summarize the build")]
    assert slack.acks, "precondition: the Working-on-it ack was posted before the restart"

    # The deploy/restart teardown (app.run's finally) while the turn is mid-flight.
    await service.shutdown()
    return store


async def test_restart_mid_turn_does_not_strand_the_working_ack(tmp_path: Path) -> None:
    """Facet 1: after a mid-turn shutdown the thread must not end on the bare ack.

    The fix shape (per the report) is a best-effort "replace the ack with an
    honest notice" during the shutdown grace period — so the desired observable
    is: the ack is deleted, edited to something else, or followed by a notice.
    Today none of that happens and the ack is the thread's permanent last word.
    """
    slack = RecordingSlackClient()
    await _restart_mid_turn(tmp_path, slack)

    ack_ts = slack.acks[0]["ts"]
    # RecordingSlackClient mirrors Slack's thread state: chat_delete removes a
    # post, chat_update edits it in place, and ``posts`` stays chronological.
    idx = next((i for i, p in enumerate(slack.posts) if p.get("ts") == ack_ts), None)
    ack_still_bare = idx is not None and slack.posts[idx].get("text") == _ACK_TEXT
    notice_after_ack = idx is not None and idx < len(slack.posts) - 1
    assert (not ack_still_bare) or notice_after_ack, (
        "Restart mid-turn stranded the thread on the bare '_Working on it…_' ack: "
        "shutdown() cancelled the turn without deleting/replacing the placeholder "
        f"or posting a notice after it (thread posts: {[p.get('text') for p in slack.posts]})"
    )


async def test_follow_up_after_restart_is_not_deflected_as_still_working(
    tmp_path: Path,
) -> None:
    """Facet 2: a follow-up in the abandoned thread must not get a dead promise.

    After the restart the store still maps the thread to the session and the
    server still reports it ``running``, so today the follow-up is deflected
    with "I'm still working on your previous message … send this again once
    I've replied" — but the restarted bot is not listening to that turn and
    will never reply. The desired observable: the follow-up either runs (a
    fresh/retried turn) or gets an honest notice — anything but the
    still-working deflection.
    """
    slack = RecordingSlackClient()
    store = await _restart_mid_turn(tmp_path, slack)

    # The bot process comes back up: a fresh service on the SAME persisted
    # store, with the abandoned turn's session still reported busy server-side.
    omnigent = HangingOmnigentClient(route_status="running")
    service = _service(store, omnigent)

    posts_before = len(slack.posts)
    ephemerals_before = len(slack.ephemerals)
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "ts": "200.2",
            "thread_ts": "100.1",
            "parent_user_id": "U1",
            "user": "U1",
            "text": "<@B1> any update?",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )

    # The follow-up must produce SOME reaction (a turn, a post, or a notice) —
    # guards against this test passing vacuously if the event were dropped.
    reacted = (
        bool(omnigent.turns)
        or len(slack.posts) > posts_before
        or len(slack.ephemerals) > ephemerals_before
    )
    assert reacted, "the follow-up message was silently dropped"

    deflections = [
        e
        for e in slack.ephemerals
        if "still working on your previous" in str(e.get("text", "")).lower()
    ]
    assert omnigent.turns or not deflections, (
        "Follow-up in a restart-abandoned thread was deflected with the "
        "'still working … send this again once I've replied' notice, but the "
        "bot abandoned that turn at shutdown and will never reply "
        f"(ephemerals: {[e.get('text') for e in slack.ephemerals]})"
    )


async def test_clean_turn_clears_the_marker_so_busy_elsewhere_still_deflects(
    tmp_path: Path,
) -> None:
    """The restart bypass keys off the abandoned-turn marker, not busy alone.

    After a turn ends cleanly, a later busy report (say the web UI driving the
    session) is a promise someone can keep, so the follow-up must still get the
    still-working deflection rather than double-running the thread.
    """
    slack = RecordingSlackClient()
    store = await _store(tmp_path)
    await store.upsert_user_config(
        "T1",
        "U1",
        UserConfig(
            agent_id="ag_1",
            agent_name="debby",
            workspace="/tmp/workspace",
            host_id="h1",
            host_type="external",
        ),
    )
    omnigent = CompletingOmnigentClient()
    service = _service(store, omnigent)
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> summarize the build"},
        client=slack,
        context={"bot_user_id": "B1"},
    )

    # The turn runs in the background: wait until it has mapped the thread and
    # finished — a clean end must leave the inflight marker cleared.
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await asyncio.wait_for(omnigent.turn_underway.wait(), timeout=5)
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        record = await store.get_session(key)
        if record is not None and not record.turn_inflight:
            break
        assert asyncio.get_running_loop().time() < deadline, (
            "the completed turn never cleared its inflight marker"
        )
        await asyncio.sleep(0.01)

    # Same store, new process, session busy from elsewhere: deflect as before.
    follow = HangingOmnigentClient(route_status="running")
    service = _service(store, follow)
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "ts": "200.2",
            "thread_ts": "100.1",
            "parent_user_id": "U1",
            "user": "U1",
            "text": "<@B1> any update?",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    assert not follow.turns, "a busy session with no abandoned turn must not run a follow-up"
    assert any(
        "still working on your previous" in str(e.get("text", "")).lower()
        for e in slack.ephemerals
    ), f"expected the busy deflection (ephemerals: {[e.get('text') for e in slack.ephemerals]})"


async def test_pending_elicitation_still_deflects_after_restart(tmp_path: Path) -> None:
    """A restart-abandoned thread parked on an elicitation keeps its deflection.

    Answering the pending request works across a restart (the card's buttons
    and the web UI talk to the server, not to the dead stream), so "respond to
    the request above" is a promise the bot can keep — the follow-up must not
    run a turn over the park.
    """
    slack = RecordingSlackClient()
    store = await _restart_mid_turn(tmp_path, slack)

    omnigent = HangingOmnigentClient(route_status="running", pending_action=True)
    service = _service(store, omnigent)
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "ts": "200.2",
            "thread_ts": "100.1",
            "parent_user_id": "U1",
            "user": "U1",
            "text": "<@B1> any update?",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    assert not omnigent.turns, "a parked session must not get a new turn run over it"
    notices = [str(e.get("text", "")) for e in slack.ephemerals]
    assert any("waiting on your response" in text for text in notices), (
        f"expected the needs-action deflection (ephemerals: {notices})"
    )
