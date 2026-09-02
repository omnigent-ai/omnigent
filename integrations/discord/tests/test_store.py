from __future__ import annotations

import time
from pathlib import Path

import pytest
from omnigent_discord.models import ChannelKey, UserConfig
from omnigent_discord.store import SQLiteStore

KEY = ChannelKey(channel_id="500", guild_id="900")
DM_KEY = ChannelKey(channel_id="600")


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    return store


async def test_initialize_creates_the_parent_directory(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nested" / "dir" / "bot.sqlite3")
    await store.initialize()
    assert (tmp_path / "nested" / "dir" / "bot.sqlite3").exists()


async def test_missing_session_is_none(store: SQLiteStore) -> None:
    assert await store.get_session(KEY) is None


async def test_session_round_trips(store: SQLiteStore) -> None:
    await store.upsert_session(
        KEY, "conv_1", "title", owner_user_id="u1", host_id="h1", workspace="/w"
    )
    record = await store.get_session(KEY)
    assert record is not None
    assert (record.session_id, record.owner_user_id, record.host_id, record.workspace) == (
        "conv_1",
        "u1",
        "h1",
        "/w",
    )


async def test_upsert_replaces_the_session_for_a_channel(store: SQLiteStore) -> None:
    await store.upsert_session(KEY, "conv_1", "t", owner_user_id="u1")
    await store.upsert_session(KEY, "conv_2", "t", owner_user_id="u1")
    record = await store.get_session(KEY)
    assert record is not None and record.session_id == "conv_2"


async def test_dm_channel_keys_its_own_session(store: SQLiteStore) -> None:
    await store.upsert_session(KEY, "conv_guild", "t", owner_user_id="u1")
    await store.upsert_session(DM_KEY, "conv_dm", "t", owner_user_id="u1")
    guild_record = await store.get_session(KEY)
    dm_record = await store.get_session(DM_KEY)
    assert guild_record is not None and guild_record.session_id == "conv_guild"
    assert dm_record is not None and dm_record.session_id == "conv_dm"


async def test_clear_session_forgets_only_that_channel(store: SQLiteStore) -> None:
    await store.upsert_session(KEY, "conv_1", "t", owner_user_id="u1")
    await store.upsert_session(DM_KEY, "conv_2", "t", owner_user_id="u1")
    assert await store.clear_session(DM_KEY) is True
    assert await store.get_session(DM_KEY) is None
    assert await store.get_session(KEY) is not None


async def test_clear_session_reports_when_there_was_nothing(store: SQLiteStore) -> None:
    assert await store.clear_session(KEY) is False


async def test_user_config_round_trips(store: SQLiteStore) -> None:
    config = UserConfig(
        agent_id="ag", agent_name="debby", workspace="/w", host_id="h1", host_name="Host"
    )
    await store.upsert_user_config("u1", config)
    assert await store.get_user_config("u1") == config


async def test_user_config_is_shared_across_guilds(store: SQLiteStore) -> None:
    # A Discord user id is global, so one setup covers every guild and DM —
    # unlike Slack, where the same person is a different id per workspace.
    await store.upsert_user_config("u1", UserConfig("ag", "debby", "/w"))
    assert await store.get_user_config("u1") is not None
    assert await store.get_user_config("u2") is None


async def test_clear_user_data_removes_config_and_owned_sessions(store: SQLiteStore) -> None:
    await store.upsert_user_config("u1", UserConfig("ag", "debby", "/w"))
    await store.upsert_session(KEY, "conv_1", "t", owner_user_id="u1")
    await store.upsert_session(DM_KEY, "conv_2", "t", owner_user_id="u2")
    await store.clear_user_data("u1")
    assert await store.get_user_config("u1") is None
    assert await store.get_session(KEY) is None
    # Another user's session is untouched.
    assert await store.get_session(DM_KEY) is not None


async def test_claim_event_is_won_once(store: SQLiteStore) -> None:
    assert await store.claim_event("m1") is True
    assert await store.claim_event("m1") is False


async def test_claim_event_without_an_id_always_proceeds(store: SQLiteStore) -> None:
    assert await store.claim_event(None) is True


async def test_unclaim_lets_a_failed_handle_retry(store: SQLiteStore) -> None:
    assert await store.claim_event("m1") is True
    await store.unclaim_event("m1")
    assert await store.claim_event("m1") is True


async def test_claim_event_prunes_entries_past_the_ttl(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent_discord.store as store_module

    await store.claim_event("old")
    # Jump a week ahead: the next claim's prune sweeps the stale marker, so the
    # dedup table can't grow without bound.
    now = int(time.time())
    monkeypatch.setattr(store_module.time, "time", lambda: now + 8 * 24 * 60 * 60)
    await store.claim_event("new")
    assert await store.claim_event("old") is True
