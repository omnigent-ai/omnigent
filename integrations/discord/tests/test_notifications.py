from __future__ import annotations

import logging

from fakes import RecordingChannel
from omnigent_bot_core.events import OutputFile
from omnigent_discord.models import ChannelKey
from omnigent_discord.notifications import (
    DiscordNotifier,
    format_output_file,
    format_policy_denied,
    format_todos,
)

KEY = ChannelKey(channel_id="500", guild_id="900")
SERVER = "https://omnigent.example.com"
LOGGER = logging.getLogger("test")


def _notifier(dm: RecordingChannel | None = None, *, dm_fails: bool = False) -> DiscordNotifier:
    async def resolve(_user_id: str) -> RecordingChannel | None:
        if dm_fails:
            raise RuntimeError("cannot DM")
        return dm

    return DiscordNotifier(server_url=SERVER, logger=LOGGER, dm_resolver=resolve)


# ── formatters ────────────────────────────────────────────────────────────


def test_todos_render_with_real_unicode_not_shortcodes() -> None:
    # Discord sends message content verbatim, so ``:white_check_mark:`` would
    # appear literally rather than as an emoji.
    text = format_todos(
        [
            {"content": "Read the code", "status": "completed"},
            {"content": "Fix it", "activeForm": "Fixing it", "status": "in_progress"},
            {"content": "Ship it", "status": "pending"},
        ]
    )
    assert text is not None
    assert ":white_check_mark:" not in text
    assert "✅ Read the code" in text
    # The in-progress item uses the gerund, as Claude Code presents it.
    assert "⏳ Fixing it" in text
    assert "⬜ Ship it" in text


def test_empty_todos_render_as_nothing() -> None:
    assert format_todos([]) is None
    assert format_todos([{"content": "  ", "status": "pending"}]) is None


def test_output_file_notice_prefers_the_filename() -> None:
    assert "report.md" in format_output_file(OutputFile(file_id="f1", filename="report.md"))
    assert "f1" in format_output_file(OutputFile(file_id="f1"))


def test_policy_denied_notice_carries_the_reason() -> None:
    assert "not allowed here" in format_policy_denied("not allowed here")


# ── private notices ───────────────────────────────────────────────────────


async def test_private_notice_prefers_a_dm() -> None:
    channel, dm = RecordingChannel(), RecordingChannel("700")
    await _notifier(dm).post_private(channel, KEY, "u1", "just for you")
    assert dm.texts == ["just for you"]
    assert channel.sent == []


async def test_private_notice_falls_back_to_a_self_deleting_mention() -> None:
    # DMs closed to server members is common and not an error, but the user
    # still has to learn why nothing happened.
    channel = RecordingChannel()
    await _notifier(None).post_private(channel, KEY, "u1", "just for you")
    assert channel.sent[0].content == "<@u1> just for you"
    assert channel.sent[0].delete_after is not None


async def test_failing_dm_resolver_still_reaches_the_user() -> None:
    channel = RecordingChannel()
    await _notifier(dm_fails=True).post_private(channel, KEY, "u1", "just for you")
    assert channel.sent[0].content.endswith("just for you")


async def test_notice_that_cannot_be_delivered_never_raises() -> None:
    # A failed side-channel post must not abort turn handling.
    channel = RecordingChannel()
    channel.fail_next_send = True
    await _notifier(None).post_private(channel, KEY, "u1", "just for you")


async def test_non_owner_notice_explains_how_to_get_a_session() -> None:
    dm = RecordingChannel("700")
    await _notifier(dm).notify_non_owner(RecordingChannel(), KEY, "u1")
    assert "belongs to whoever started it" in dm.texts[0]


async def test_busy_notice_points_at_the_pending_request() -> None:
    dm = RecordingChannel("700")
    await _notifier(dm).notify_busy(
        RecordingChannel(), KEY, "u1", needs_action=True, session_id="conv_1"
    )
    assert "waiting on your response" in dm.texts[0]
    assert "/c/conv_1" in dm.texts[0]


async def test_busy_notice_says_to_wait_when_the_server_is_working() -> None:
    dm = RecordingChannel("700")
    await _notifier(dm).notify_busy(
        RecordingChannel(), KEY, "u1", needs_action=False, session_id="conv_1"
    )
    assert "still working on your previous message" in dm.texts[0]


# ── replies ───────────────────────────────────────────────────────────────


async def test_long_reply_is_split_across_messages() -> None:
    channel = RecordingChannel()
    await _notifier().post_reply(channel, "word " * 600)
    assert len(channel.sent) == 2
    assert all(len(m.content or "") <= 2000 for m in channel.sent)


async def test_session_info_message_links_to_the_web_ui() -> None:
    channel = RecordingChannel()
    await _notifier().post_session_info(
        channel,
        KEY,
        harness="claude-native",
        agent_name="debby",
        workspace="/srv/work",
        session_id="conv_1",
    )
    text = channel.texts[0]
    assert "debby" in text and "claude-native" in text
    assert "/srv/work" in text
    assert f"{SERVER}/c/conv_1" in text


async def test_failed_session_info_post_never_raises() -> None:
    channel = RecordingChannel()
    channel.fail_next_send = True
    await _notifier().post_session_info(
        channel, KEY, harness=None, agent_name=None, workspace=None, session_id="conv_1"
    )


# ── the plan message ──────────────────────────────────────────────────────


async def test_plan_is_posted_once_then_edited_in_place() -> None:
    channel = RecordingChannel()
    notifier = _notifier()
    todos = [{"content": "Step one", "status": "pending"}]
    message = await notifier.post_or_update_todos(channel, KEY, todos, None)
    again = await notifier.post_or_update_todos(
        channel, KEY, [{"content": "Step one", "status": "completed"}], message
    )
    assert again is message
    assert len(channel.sent) == 1
    assert "✅ Step one" in channel.sent[0].content


async def test_empty_plan_posts_nothing() -> None:
    channel = RecordingChannel()
    assert await _notifier().post_or_update_todos(channel, KEY, [], None) is None
    assert channel.sent == []


# ── the web link ──────────────────────────────────────────────────────────


def test_web_link_targets_the_conversation_page() -> None:
    assert _notifier().session_web_link("conv_1") == f"{SERVER}/c/conv_1"


def test_databricks_api_mount_maps_to_the_spa_mount() -> None:
    # The API proxy mount answers JSON; the UI lives on the workspace SPA mount.
    notifier = DiscordNotifier(
        server_url="https://ws.cloud.databricks.com/api/2.0/omnigent?o=123",
        logger=LOGGER,
    )
    link = notifier.session_web_link("conv_1")
    assert link == "https://ws.cloud.databricks.com/omnigent/c/conv_1?o=123"


async def test_deliberate_ping_is_scoped_to_that_one_user() -> None:
    # Every other message denies mentions wholesale (the client's default), so
    # this fallback carries its own narrow allowance rather than re-opening the
    # door for agent-authored text.
    channel = RecordingChannel()
    await _notifier(None).post_private(channel, KEY, "1001", "just for you")
    allowance = channel.sent[0].allowed_mentions
    assert allowance is not None
    assert allowance.everyone is False
    assert allowance.roles is False
    assert [u.id for u in allowance.users] == [1001]


async def test_unusable_user_id_degrades_to_no_ping_not_no_notice() -> None:
    # Losing the highlight is acceptable; losing the message is not.
    channel = RecordingChannel()
    await _notifier(None).post_private(channel, KEY, "not-a-snowflake", "just for you")
    assert channel.sent[0].content.endswith("just for you")
    assert channel.sent[0].allowed_mentions is None


def test_generic_failure_uses_a_real_emoji_not_a_shortcode() -> None:
    # Discord sends content verbatim — a shortcode would render as literal text
    # in the one message users see most often when something breaks.
    from omnigent_discord.text import GENERIC_FAILURE_TEXT

    assert ":warning:" not in GENERIC_FAILURE_TEXT
    assert GENERIC_FAILURE_TEXT.startswith("⚠️")
