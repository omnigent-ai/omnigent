from __future__ import annotations

from omnigent_discord.models import ChannelKey, SessionRecord, UserConfig


def test_channel_key_without_a_guild_is_a_dm() -> None:
    assert ChannelKey(channel_id="7").is_dm is True


def test_channel_key_with_a_guild_is_not_a_dm() -> None:
    assert ChannelKey(channel_id="7", guild_id="9").is_dm is False


def test_channel_key_display_names_the_guild_or_dm() -> None:
    assert ChannelKey("7", "9").display() == "9:7"
    assert ChannelKey("7").display() == "dm:7"


def test_channel_keys_are_hashable_so_they_can_guard_concurrency() -> None:
    assert {ChannelKey("7", "9"), ChannelKey("7", "9")} == {ChannelKey("7", "9")}


def test_user_config_defaults_host_fields_to_none() -> None:
    config = UserConfig(agent_id="a", agent_name="A", workspace="/w")
    assert (config.host_id, config.host_name) == (None, None)


def test_session_record_carries_the_owner() -> None:
    record = SessionRecord(session_id="s", owner_user_id="u", host_id=None, workspace="/w")
    assert record.owner_user_id == "u"
