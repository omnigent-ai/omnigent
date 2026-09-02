"""Event routing and turn lifecycle.

Routing is where the Discord integration diverges most from Slack: a guild
mention moves the conversation into a thread, a thread the bot owns needs no
further mentions, and a DM is a standing session because Discord DMs have no
threads at all.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fakes import (
    FakeGuild,
    FakeOmnigent,
    FakePool,
    FakeRole,
    FakeThread,
    FakeUser,
    IncomingMessage,
    RecordingChannel,
    delta_event,
    elicitation_event,
    resolved_event,
    status_event,
)
from omnigent_bot_core.events import SessionActivity
from omnigent_bot_core.omnigent import (
    AuthRequiredError,
    HostUnavailableError,
    ServerUnreachableError,
)
from omnigent_discord.models import ChannelKey, UserConfig
from omnigent_discord.notifications import DiscordNotifier
from omnigent_discord.service import DiscordOmnigentService, thread_name_for
from omnigent_discord.store import SQLiteStore

SERVER = "https://omnigent.example.com"
BOT_ID = "42"
OWNER = FakeUser("1001", name="owner")
STRANGER = FakeUser("1002", name="stranger")
BOT = FakeUser(BOT_ID, bot=True, name="omnigent")
GUILD = FakeGuild("900")
LOGGER = logging.getLogger("test")

ANSWER_STREAM = [
    status_event("running", "resp_1"),
    delta_event("Here is the answer.", "m1"),
    status_event("idle", "resp_1"),
]


class Harness:
    """A service wired to fakes, plus the helpers every routing test needs."""

    def __init__(
        self,
        store: SQLiteStore,
        client: FakeOmnigent,
        service: DiscordOmnigentService,
        dm_channel: RecordingChannel,
    ) -> None:
        self.store = store
        self.client = client
        self.service = service
        self.dm_channel = dm_channel

    async def deliver(self, message: IncomingMessage) -> None:
        """Handle a gateway message and wait for any spawned turn to finish."""
        await self.service.handle_message(message)
        await self.drain()

    async def drain(self) -> None:
        while self.service._turn_tasks:
            await asyncio.gather(*list(self.service._turn_tasks), return_exceptions=True)


@pytest.fixture
async def harness(tmp_path: Path) -> Harness:
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    await store.upsert_user_config(
        str(OWNER.id),
        UserConfig(agent_id="ag_1", agent_name="debby", workspace="/srv/work", host_id="h1"),
    )
    client = FakeOmnigent(list(ANSWER_STREAM))
    dm_channel = RecordingChannel("700")

    async def dm_resolver(_user_id: str) -> RecordingChannel:
        return dm_channel

    service = DiscordOmnigentService(
        store=store,
        pool=FakePool(client),  # type: ignore[arg-type]
        notifier=DiscordNotifier(server_url=SERVER, logger=LOGGER, dm_resolver=dm_resolver),
        server_url=SERVER,
        bot_user_id=BOT_ID,
        stream_edit_interval_seconds=0.0,
        elicitation_timeout_seconds=0.05,
    )
    return Harness(store, client, service, dm_channel)


def dm(text: str, author: FakeUser = OWNER, channel: RecordingChannel | None = None):
    return IncomingMessage(content=text, author=author, channel=channel or RecordingChannel("600"))


def in_channel(
    text: str,
    author: FakeUser = OWNER,
    channel: RecordingChannel | None = None,
    *,
    mention: bool = True,
):
    return IncomingMessage(
        content=text,
        author=author,
        channel=channel or RecordingChannel("500"),
        guild=GUILD,
        mentions=[BOT] if mention else [],
    )


# ── thread names ──────────────────────────────────────────────────────────


def test_thread_name_is_the_prompt_collapsed_to_one_line() -> None:
    assert thread_name_for("  fix   the\nbuild ") == "fix the build"


def test_long_thread_name_fits_discords_cap() -> None:
    assert len(thread_name_for("x" * 500)) == 100


def test_empty_prompt_still_names_the_thread() -> None:
    assert thread_name_for("   ") == "Omnigent session"


# ── who the bot answers ───────────────────────────────────────────────────


async def test_dm_runs_without_a_mention(harness: Harness) -> None:
    message = dm("what broke the build?")
    await harness.deliver(message)
    assert harness.client.submitted == ["what broke the build?"]
    assert "Here is the answer." in message.channel.answer


async def test_channel_message_without_a_mention_is_ignored(harness: Harness) -> None:
    # Guild channels are shared human space; the bot joins only when addressed.
    message = in_channel("just chatting", mention=False)
    await harness.deliver(message)
    assert harness.client.submitted == []
    assert message.channel.sent == []


async def test_bots_own_messages_are_ignored(harness: Harness) -> None:
    await harness.deliver(dm("echo", author=BOT))
    assert harness.client.submitted == []


async def test_message_from_a_blocked_guild_is_ignored(tmp_path: Path) -> None:
    # A Discord invite can be used by anyone with Manage Server, so the operator
    # can pin the bot to the guilds they actually run in.
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    client = FakeOmnigent(list(ANSWER_STREAM))
    service = DiscordOmnigentService(
        store=store,
        pool=FakePool(client),  # type: ignore[arg-type]
        notifier=DiscordNotifier(server_url=SERVER, logger=LOGGER),
        server_url=SERVER,
        bot_user_id=BOT_ID,
        guild_allowed=lambda guild_id: guild_id == "999",
    )
    message = in_channel("hello")
    await service.handle_message(message)
    assert client.submitted == []
    assert message.channel.sent == []


async def test_duplicate_message_is_handled_once(harness: Harness) -> None:
    # The gateway replays events after a resumed session.
    message = dm("run it")
    await harness.deliver(message)
    await harness.deliver(message)
    assert harness.client.submitted == ["run it"]


async def test_unconfigured_user_is_nudged_into_setup(harness: Harness) -> None:
    await harness.deliver(dm("hello", author=STRANGER))
    assert harness.client.submitted == []
    assert "/omnigent config" in harness.dm_channel.texts[0]


# ── the guild → thread hand-off ───────────────────────────────────────────


async def test_channel_mention_moves_the_session_into_a_thread(harness: Harness) -> None:
    # A streaming answer would otherwise take over the channel.
    message = in_channel("@bot inspect the failure")
    await harness.deliver(message)
    assert message.thread is not None
    assert message.channel.sent == []  # nothing posted in the channel itself
    assert "Here is the answer." in message.thread.answer
    record = await harness.store.get_session(
        ChannelKey(channel_id=str(message.thread.id), guild_id="900")
    )
    assert record is not None and record.owner_user_id == str(OWNER.id)


async def test_thread_is_named_after_the_prompt(harness: Harness) -> None:
    message = in_channel(f"<@{BOT_ID}> why did the deploy fail?")
    await harness.deliver(message)
    assert message.thread is not None
    assert message.thread.name == "why did the deploy fail?"  # type: ignore[attr-defined]


async def test_bot_mention_is_stripped_from_the_prompt(harness: Harness) -> None:
    await harness.deliver(in_channel(f"<@{BOT_ID}> inspect the failure"))
    assert harness.client.submitted == ["inspect the failure"]


async def test_bare_mention_asks_for_a_message(harness: Harness) -> None:
    message = in_channel(f"<@{BOT_ID}>")
    await harness.deliver(message)
    assert harness.client.submitted == []
    assert "Mention me with a message" in message.channel.texts[0]


async def test_missing_thread_permission_is_explained(harness: Harness) -> None:
    # The one failure a moderator can actually fix.
    message = in_channel("@bot inspect the failure")
    message.thread_error = RuntimeError("Missing Permissions")
    await harness.deliver(message)
    assert "Create Public Threads" in message.channel.texts[0]
    assert harness.client.submitted == []


async def test_failed_thread_release_lets_the_user_retry(harness: Harness) -> None:
    message = in_channel("@bot inspect the failure")
    message.thread_error = RuntimeError("Missing Permissions")
    await harness.deliver(message)
    # The dedup claim was released, so a redelivery is not swallowed.
    assert await harness.store.claim_event(str(message.id)) is True


# ── inside a session thread ───────────────────────────────────────────────


async def _session_thread(harness: Harness) -> FakeThread:
    """Run one turn from a channel mention and return the resulting thread."""
    message = in_channel("@bot start")
    await harness.deliver(message)
    assert message.thread is not None
    harness.client.submitted.clear()
    return message.thread


async def test_owner_continues_a_thread_without_mentioning_the_bot(
    harness: Harness,
) -> None:
    # The thread exists for the session, so demanding a mention every time
    # would be noise.
    thread = await _session_thread(harness)
    await harness.deliver(dm("and now the logs", channel=thread))
    assert harness.client.submitted == ["and now the logs"]


async def test_bystander_chatting_in_the_thread_is_left_alone(harness: Harness) -> None:
    thread = await _session_thread(harness)
    sent_before = len(thread.sent)
    await harness.deliver(
        IncomingMessage(content="nice", author=STRANGER, channel=thread, guild=GUILD)
    )
    assert harness.client.submitted == []
    assert len(thread.sent) == sent_before  # no notice, no noise


async def test_bystander_who_mentions_the_bot_gets_an_explanation(
    harness: Harness,
) -> None:
    thread = await _session_thread(harness)
    await harness.deliver(
        IncomingMessage(
            content=f"<@{BOT_ID}> me too",
            author=STRANGER,
            channel=thread,
            guild=GUILD,
            mentions=[BOT],
        )
    )
    assert harness.client.submitted == []
    assert "belongs to whoever started it" in harness.dm_channel.texts[-1]


async def test_stranger_cannot_adopt_a_thread_whose_session_was_lost(
    harness: Harness,
) -> None:
    # A restart can drop the session record; Discord still knows who started the
    # thread, so ownership survives.
    thread = await _session_thread(harness)
    await harness.store.clear_session(ChannelKey(channel_id=str(thread.id), guild_id="900"))
    await harness.deliver(
        IncomingMessage(
            content=f"<@{BOT_ID}> mine now",
            author=STRANGER,
            channel=thread,
            guild=GUILD,
            mentions=[BOT],
        )
    )
    assert harness.client.submitted == []
    assert "belongs to whoever started it" in harness.dm_channel.texts[-1]


async def test_starter_can_restart_a_thread_whose_session_was_lost(
    harness: Harness,
) -> None:
    thread = await _session_thread(harness)
    await harness.store.clear_session(ChannelKey(channel_id=str(thread.id), guild_id="900"))
    await harness.deliver(
        IncomingMessage(
            content=f"<@{BOT_ID}> carry on",
            author=OWNER,
            channel=thread,
            guild=GUILD,
            mentions=[BOT],
        )
    )
    assert harness.client.submitted == ["carry on"]


# ── concurrency and ownership ─────────────────────────────────────────────


async def test_second_message_while_streaming_is_deflected_not_queued(
    harness: Harness,
) -> None:
    channel = RecordingChannel("600")
    gate = asyncio.Event()

    async def hold(_event: dict[str, object]) -> None:
        await gate.wait()

    harness.client.on_event = hold
    first = asyncio.ensure_future(harness.service.handle_message(dm("first", channel=channel)))
    await asyncio.wait_for(harness.client.turn_started.wait(), timeout=5)
    await harness.service.handle_message(dm("second", channel=channel))
    assert harness.client.submitted == ["first"]
    assert "still working on your previous message" in harness.dm_channel.texts[-1]
    gate.set()
    await first
    await harness.drain()


async def test_message_while_the_server_is_busy_elsewhere_is_deflected(
    harness: Harness,
) -> None:
    # The web UI may be driving the same session; the server is authoritative.
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    harness.client.activity = SessionActivity(status="running", pending_elicitation=False)
    await harness.deliver(dm("second", channel=channel))
    assert harness.client.submitted == ["first"]
    assert "still working" in harness.dm_channel.texts[-1]


async def test_message_while_a_request_is_pending_says_to_answer_it(
    harness: Harness,
) -> None:
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    harness.client.activity = SessionActivity(status="idle", pending_elicitation=True)
    await harness.deliver(dm("second", channel=channel))
    assert "waiting on your response" in harness.dm_channel.texts[-1]


async def test_idle_session_just_continues(harness: Harness) -> None:
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    await harness.deliver(dm("second", channel=channel))
    assert harness.client.submitted == ["first", "second"]


# ── the turn itself ───────────────────────────────────────────────────────


async def test_new_session_posts_its_config_summary_before_the_answer(
    harness: Harness,
) -> None:
    message = dm("hello")
    await harness.deliver(message)
    texts = [m.content for m in message.channel.live]
    assert "debby" in texts[0] and "claude-native" in texts[0]
    assert "Here is the answer." in texts[1]


async def test_session_is_created_with_the_users_config(harness: Harness) -> None:
    await harness.deliver(dm("hello"))
    assert harness.client.created[0][0] == "ag_1"
    assert harness.client.launched[0]["workspace"] == "/srv/work"
    assert harness.client.launched[0]["host_id"] == "h1"


async def test_session_title_is_a_discord_permalink(harness: Harness) -> None:
    message = dm("hello")
    await harness.deliver(message)
    assert harness.client.created[0][1] == f"Discord: {message.jump_url}"


async def test_existing_session_is_reused_without_a_second_launch(
    harness: Harness,
) -> None:
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    await harness.deliver(dm("second", channel=channel))
    assert len(harness.client.created) == 1
    assert len(harness.client.launched) == 1


async def test_plan_update_is_posted_once_and_edited_in_place(harness: Harness) -> None:
    harness.client.events = [
        status_event("running", "r1"),
        {"type": "session.todos", "todos": [{"content": "Step one", "status": "pending"}]},
        delta_event("Working.", "m1"),
        {"type": "session.todos", "todos": [{"content": "Step one", "status": "completed"}]},
        status_event("idle", "r1"),
    ]
    message = dm("plan it")
    await harness.deliver(message)
    plans = [m for m in message.channel.live if m.content and "Plan" in m.content]
    assert len(plans) == 1
    assert "✅ Step one" in plans[0].content


async def test_produced_file_is_announced(harness: Harness) -> None:
    harness.client.events = [
        status_event("running", "r1"),
        delta_event("Done.", "m1"),
        {"type": "response.output_file.done", "file_id": "f1", "filename": "report.md"},
        status_event("idle", "r1"),
    ]
    message = dm("make a report")
    await harness.deliver(message)
    assert any("report.md" in (m.content or "") for m in message.channel.live)


async def test_policy_denial_is_surfaced(harness: Harness) -> None:
    harness.client.events = [
        status_event("running", "r1"),
        {"type": "response.policy_denied", "reason": "writes are not allowed here"},
        status_event("idle", "r1"),
    ]
    message = dm("delete everything")
    await harness.deliver(message)
    assert any("writes are not allowed here" in (m.content or "") for m in message.channel.live)


async def test_committed_only_answer_is_recovered(harness: Harness) -> None:
    # A turn that streamed nothing still has an answer on the server.
    harness.client.events = [status_event("running", "r1"), status_event("idle", "r1")]
    harness.client.latest_after_turn = ("item_9", "The committed answer.")
    message = dm("hello")
    await harness.deliver(message)
    assert "The committed answer." in message.channel.answer


async def test_prior_turns_answer_is_not_resurrected(harness: Harness) -> None:
    # Comparing against the pre-turn baseline stops a no-answer turn (a denied
    # approval, say) from re-posting the previous reply.
    channel = RecordingChannel("600")
    harness.client.events = [status_event("running", "r1"), status_event("idle", "r1")]
    harness.client.latest = ("item_1", "An older answer.")
    await harness.deliver(dm("hello", channel=channel))
    assert "An older answer." not in channel.answer
    assert any("completed without returning" in (m.content or "") for m in channel.live)


async def test_in_band_error_shows_the_generic_failure_not_the_detail(
    harness: Harness,
) -> None:
    # A server error body can carry a stack trace or an internal path, and the
    # channel is visible to everyone in it.
    harness.client.events = [
        status_event("running", "r1"),
        {"type": "response.error", "error": {"message": "/opt/secret/path exploded"}},
        status_event("idle", "r1"),
    ]
    message = dm("hello")
    await harness.deliver(message)
    body = " ".join(m.content or "" for m in message.channel.live)
    assert "/opt/secret/path" not in body
    assert "Something went wrong on the Omnigent server" in body


async def test_answer_plus_an_error_keeps_both(harness: Harness) -> None:
    harness.client.events = [
        status_event("running", "r1"),
        delta_event("Partial answer.", "m1"),
        {"type": "response.error", "error": {"message": "boom"}},
        status_event("idle", "r1"),
    ]
    message = dm("hello")
    await harness.deliver(message)
    body = [m.content for m in message.channel.live]
    assert any("Partial answer." in (t or "") for t in body)
    assert any("Something went wrong" in (t or "") for t in body)


# ── error handling ────────────────────────────────────────────────────────


async def test_unreachable_server_tells_the_user_to_reconfigure(
    harness: Harness,
) -> None:
    harness.client.create_error = ServerUnreachableError("no route")
    message = dm("hello")
    await harness.deliver(message)
    assert "/omnigent config" in message.channel.texts[-1]
    assert "no route" not in message.channel.texts[-1]


async def test_no_online_host_shows_the_command_to_start_one(harness: Harness) -> None:
    harness.client.create_error = HostUnavailableError("no hosts")
    message = dm("hello")
    await harness.deliver(message)
    assert f"omni host --server {SERVER}" in message.channel.texts[-1]


async def test_unexpected_startup_failure_is_generic(harness: Harness) -> None:
    harness.client.create_error = RuntimeError("Traceback: /opt/omnigent/app.py line 9")
    message = dm("hello")
    await harness.deliver(message)
    assert "/opt/omnigent" not in message.channel.texts[-1]
    assert "Something went wrong starting your Omnigent session" in message.channel.texts[-1]


async def test_expired_login_is_reported_privately_leaving_no_placeholder(
    harness: Harness,
) -> None:
    # The channel is shared; the fix is a command only that one user can run.
    harness.client.create_error = AuthRequiredError("401")
    message = dm("hello")
    await harness.deliver(message)
    assert message.channel.live == []
    assert "login has expired" in harness.dm_channel.texts[-1]


async def test_mid_stream_failure_leaves_no_stale_placeholder(harness: Harness) -> None:
    harness.client.turn_error = ServerUnreachableError("dropped")
    message = dm("hello")
    await harness.deliver(message)
    assert all((m.content or "") != "_Working on it…_" for m in message.channel.live)


# ── elicitations in a turn ────────────────────────────────────────────────


async def test_approval_card_sorts_between_the_answer_segments(
    harness: Harness,
) -> None:
    # Discord orders by send time, so the answer must be sealed before the card
    # or later text would keep appearing above it.
    harness.client.events = [
        status_event("running", "r1"),
        delta_event("Before. ", "m1"),
        elicitation_event("el_1"),
        resolved_event("el_1"),
        delta_event("After.", "m2"),
        status_event("idle", "r1"),
    ]
    message = dm("do it")
    await harness.deliver(message)
    live = message.channel.live
    kinds = [("card" if m.embed is not None else "text") for m in live]
    # session info, "Before.", the card, "After."
    assert kinds == ["text", "text", "card", "text"]
    assert "Before." in (live[1].content or "")
    assert "After." in (live[3].content or "")


async def test_unanswered_card_is_declined_when_the_turn_ends(
    harness: Harness,
) -> None:
    harness.client.events = [
        status_event("running", "r1"),
        elicitation_event("el_1"),
        status_event("idle", "r1"),
    ]
    await harness.deliver(dm("do it"))
    assert harness.client.resolved[-1]["accepted"] is False


# ── /omnigent new ─────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, channel: RecordingChannel, user: FakeUser, guild: FakeGuild | None):
        self.channel = channel
        self.user = user
        self.guild = guild
        self.response = FakeResponse()


async def test_new_clears_the_session_so_the_next_message_starts_fresh(
    harness: Harness,
) -> None:
    # A Discord DM has no threads, so without this it would stay bound to one
    # session forever.
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    interaction = FakeInteraction(channel, OWNER, None)
    await harness.service.start_new_session(interaction)
    assert "Started fresh" in interaction.response.messages[0][0]
    await harness.deliver(dm("second", channel=channel))
    assert len(harness.client.created) == 2


async def test_new_stops_a_stuck_turn_instead_of_refusing(harness: Harness) -> None:
    # A turn that never terminates would otherwise hold the channel forever —
    # and in a DM (no threads) this command is the only reset there is, so
    # refusing here strands the user with no way back but a bot restart.
    channel = RecordingChannel("600")
    gate = asyncio.Event()

    async def hold(_event: dict[str, object]) -> None:
        await gate.wait()

    harness.client.on_event = hold
    running = asyncio.ensure_future(
        harness.service.handle_message(dm("wedge me", channel=channel))
    )
    await asyncio.wait_for(harness.client.turn_started.wait(), timeout=5)

    interaction = FakeInteraction(channel, OWNER, None)
    await harness.service.start_new_session(interaction)
    said = interaction.response.messages[0][0]
    assert "Started fresh" in said
    assert "stopped the turn" in said
    # The channel is released, so the next message runs rather than deflecting.
    assert ChannelKey(channel_id="600") not in harness.service._active_channels
    gate.set()
    await running
    await harness.drain()


async def test_new_refuses_for_someone_elses_session(harness: Harness) -> None:
    channel = RecordingChannel("600")
    await harness.deliver(dm("first", channel=channel))
    interaction = FakeInteraction(channel, STRANGER, None)
    await harness.service.start_new_session(interaction)
    assert "belongs to whoever started it" in interaction.response.messages[0][0]


async def test_new_in_a_fresh_conversation_says_there_is_nothing_to_reset(
    harness: Harness,
) -> None:
    interaction = FakeInteraction(RecordingChannel("601"), OWNER, None)
    await harness.service.start_new_session(interaction)
    assert "no Omnigent session here yet" in interaction.response.messages[0][0]


async def test_shutdown_cancels_in_flight_turns(harness: Harness) -> None:
    gate = asyncio.Event()

    async def hold(_event: dict[str, object]) -> None:
        await gate.wait()

    harness.client.on_event = hold
    task = asyncio.ensure_future(harness.service.handle_message(dm("slow")))
    await asyncio.sleep(0)
    await task
    await harness.service.shutdown()
    assert harness.service._turn_tasks == set()
    gate.set()


async def test_unconfigured_mention_leaves_no_empty_thread(harness: Harness) -> None:
    # Creating the thread first would litter the channel with a thread no
    # session will ever use.
    message = in_channel("@bot hello", author=STRANGER)
    await harness.deliver(message)
    assert message.thread is None
    assert harness.client.submitted == []
    assert "/omnigent config" in harness.dm_channel.texts[-1]


async def test_thread_whose_starter_cannot_be_read_is_not_adoptable(
    harness: Harness,
) -> None:
    # Fail-closed: if Discord won't tell us who created the thread, refusing
    # costs the requester a new thread, while granting hands them someone
    # else's — and locks the original owner out of their own conversation.
    thread = await _session_thread(harness)
    await harness.store.clear_session(ChannelKey(channel_id=str(thread.id), guild_id="900"))
    thread.starter_message = None
    thread.owner_id = None  # type: ignore[attr-defined]

    async def refuse(_id: int) -> None:
        raise RuntimeError("403 Missing Access")

    thread.fetch_message = refuse  # type: ignore[attr-defined]
    await harness.deliver(
        IncomingMessage(
            content=f"<@{BOT_ID}> mine now",
            author=STRANGER,
            channel=thread,
            guild=GUILD,
            mentions=[BOT],
        )
    )
    assert harness.client.submitted == []


async def test_thread_ownership_uses_the_gateway_owner_id(harness: Harness) -> None:
    # ``Thread.owner_id`` arrives on the gateway payload, so the common case
    # needs no API call and has no failure mode to fall open through.
    thread = await _session_thread(harness)
    await harness.store.clear_session(ChannelKey(channel_id=str(thread.id), guild_id="900"))
    thread.owner_id = int(OWNER.id)  # type: ignore[attr-defined]

    async def never(_id: int) -> None:  # pragma: no cover - must not be reached
        raise AssertionError("owner_id should have answered without a fetch")

    thread.fetch_message = never  # type: ignore[attr-defined]
    await harness.deliver(
        IncomingMessage(
            content=f"<@{BOT_ID}> carry on",
            author=OWNER,
            channel=thread,
            guild=GUILD,
            mentions=[BOT],
        )
    )
    assert harness.client.submitted == ["carry on"]


async def test_mention_of_the_bots_own_role_counts_as_addressing_it(
    harness: Harness,
) -> None:
    # Discord auto-creates a managed role named after the bot and offers it in
    # autocomplete beside the bot itself, so users routinely pick the role.
    # Observed live: the message arrives with mentions=[] and role_mentions=[…],
    # and the bot answered nothing.
    own_role = FakeRole("700000000000000007", bot_id=BOT_ID)
    message = IncomingMessage(
        content=f"<@&{own_role.id}> what files are in this repo?",
        author=OWNER,
        channel=RecordingChannel("500"),
        guild=GUILD,
        role_mentions=[own_role],
    )
    await harness.deliver(message)
    assert harness.client.submitted == ["what files are in this repo?"]
    assert message.thread is not None


async def test_unrelated_role_mention_is_not_an_address_to_the_bot(
    harness: Harness,
) -> None:
    # A role the bot merely holds — or any other role — is not someone talking
    # to the bot, so it must not start a turn.
    other = FakeRole("999", bot_id=None)
    await harness.deliver(
        IncomingMessage(
            content=f"<@&{other.id}> deploy please",
            author=OWNER,
            channel=RecordingChannel("500"),
            guild=GUILD,
            role_mentions=[other],
        )
    )
    assert harness.client.submitted == []


async def test_another_bots_managed_role_is_not_an_address_to_this_bot(
    harness: Harness,
) -> None:
    someone_else = FakeRole("888", bot_id="9999")
    await harness.deliver(
        IncomingMessage(
            content=f"<@&{someone_else.id}> hello",
            author=OWNER,
            channel=RecordingChannel("500"),
            guild=GUILD,
            role_mentions=[someone_else],
        )
    )
    assert harness.client.submitted == []
