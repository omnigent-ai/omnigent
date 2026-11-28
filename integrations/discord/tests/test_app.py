"""Gateway wiring: intents, command registration, and its first-run traps.

Two things break a first run silently unless the bot says something useful: the
privileged message-content intent, and a `bot`-only invite that connects to the
gateway perfectly well and then 403s on command registration.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import pytest
from omnigent_discord.app import OmnigentClient, build_intents, register_commands
from omnigent_discord.config import Settings


class _FakeResponse:
    status = 403
    reason = "Forbidden"


def _settings(command_guild_ids: str = "") -> Settings:
    # Fields carry validation aliases, so construction uses the env-var names —
    # the same shape a real deployment supplies.
    return Settings(  # type: ignore[call-arg]
        OMNIGENT_DISCORD_BOT_TOKEN="tok",
        OMNIGENT_SERVER_URL="https://omnigent.example.com",
        OMNIGENT_DISCORD_COMMAND_GUILD_IDS=command_guild_ids,
    )


def _client(command_guild_ids: str = "") -> OmnigentClient:
    # The service is only touched by on_message / on_ready, which these tests
    # drive directly or not at all.
    return OmnigentClient(_settings(command_guild_ids), service=object())  # type: ignore[arg-type]


# ── intents ───────────────────────────────────────────────────────────────


def test_message_content_intent_is_requested() -> None:
    # Without it the gateway hands every message an empty ``content`` and the
    # bot silently answers nothing.
    assert build_intents().message_content is True


def test_intents_the_bot_needs_are_on() -> None:
    intents = build_intents()
    assert intents.guilds is True
    assert intents.guild_messages is True
    assert intents.dm_messages is True


def test_no_privileged_member_or_presence_intents_are_requested() -> None:
    # The bot never needs a member list, and asking for privileged intents it
    # doesn't use is both a review burden and a refusal risk.
    intents = build_intents()
    assert intents.members is False
    assert intents.presences is False


# ── command registration ──────────────────────────────────────────────────


def test_command_group_carries_all_three_subcommands() -> None:
    client = _client()
    register_commands(client, setup=object(), service=object())  # type: ignore[arg-type]
    group = client.tree.get_command("omnigent")
    assert group is not None
    assert {c.name for c in group.commands} == {"config", "new", "logout"}  # type: ignore[attr-defined]


def test_command_is_usable_in_dms() -> None:
    # DMs are a first-class entry point, so /omnigent must work outside a guild.
    client = _client()
    register_commands(client, setup=object(), service=object())  # type: ignore[arg-type]
    group = client.tree.get_command("omnigent")
    assert group is not None
    assert group.allowed_contexts is not None
    assert group.allowed_contexts.dm_channel is True


async def test_bot_only_invite_is_reported_with_the_fix_not_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Registering a guild command needs `applications.commands` in that guild.
    # A bot-only invite connects to the gateway fine and then 403s here, so the
    # log has to name the scope rather than dumping a Forbidden traceback.
    client = _client("900")

    async def refuse(**_kwargs: Any) -> None:
        raise discord.Forbidden(_FakeResponse(), "Missing Access")

    client.tree.sync = refuse  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await client._sync_commands()

    message = caplog.text
    assert "applications.commands" in message
    assert "OMNIGENT_DISCORD_GUILD_IDS" in message
    # The bot keeps answering mentions and DMs; say so rather than implying it
    # is dead in the water.
    assert "keep working" in message


async def test_refused_guild_is_named(caplog: pytest.LogCaptureFixture) -> None:
    client = _client("900000000000000001")

    async def refuse(**_kwargs: Any) -> None:
        raise discord.Forbidden(_FakeResponse(), "Missing Access")

    client.tree.sync = refuse  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await client._sync_commands()
    assert "900000000000000001" in caplog.text


async def test_unexpected_sync_failure_still_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client("900")

    async def boom(**_kwargs: Any) -> None:
        raise RuntimeError("gateway exploded")

    client.tree.sync = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await client._sync_commands()
    assert "Could not register" in caplog.text
    assert "gateway exploded" in caplog.text


async def test_failed_sync_never_stops_the_bot(caplog: pytest.LogCaptureFixture) -> None:
    # The bot answers mentions and DMs without the slash command, so a refused
    # registration must not take the process down.
    client = _client("900")

    async def refuse(**_kwargs: Any) -> None:
        raise discord.Forbidden(_FakeResponse(), "Missing Access")

    client.tree.sync = refuse  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await client._sync_commands()


async def test_opting_into_guild_registration_syncs_only_those_guilds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The fast-iteration path: a guild command appears in seconds.
    client = _client("900 901")
    synced: list[Any] = []

    async def record(**kwargs: Any) -> None:
        synced.append(kwargs.get("guild"))

    client.tree.sync = record  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        await client._sync_commands()
    assert {g.id for g in synced} == {900, 901}
    assert "2 guild(s)" in caplog.text
    # It is DM-blind, and that is easy to trip over — say so loudly.
    assert "NOT appear in DMs" in caplog.text


async def test_command_registers_globally_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Global is the default because Discord routes DM interactions only to
    # globally-registered commands, and /omnigent new is the ONLY way to end a
    # DM session (a Discord DM has no threads). Verified live: with a guild-only
    # registration, /omnigent does not autocomplete in a DM.
    client = _client()
    synced: list[Any] = []

    async def record(**kwargs: Any) -> None:
        synced.append(kwargs.get("guild"))

    client.tree.sync = record  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        await client._sync_commands()
    assert synced == [None]
    assert "globally" in caplog.text
