from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
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


async def test_initialize_is_safe_to_run_twice(tmp_path: Path) -> None:
    # The migration checks for its column, so a second start must not fail on
    # a duplicate ALTER.
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    await store.initialize()
    assert await store.get_user_config("nobody") is None


async def test_store_round_trips_managed_host_type(store: SQLiteStore) -> None:
    """A managed choice survives a write/read on both tables.

    A managed session carries no host or workspace — the server picks both — so
    the flag is the only thing that says a later turn must not launch a runner.
    """
    await store.upsert_user_config(
        "U1",
        UserConfig(
            agent_id="ag_1",
            agent_name="debby",
            workspace="",
            host_id=None,
            host_name=None,
            host_type="managed",
        ),
    )
    config = await store.get_user_config("U1")
    assert config is not None
    assert config.host_type == "managed"

    await store.upsert_session(KEY, "sess_1", "Title", host_type="managed")
    record = await store.get_session(KEY)
    assert record is not None
    assert record.host_type == "managed"


async def test_store_defaults_host_type_to_external(store: SQLiteStore) -> None:
    """Callers that never mention a host type keep the pre-existing behavior."""
    await store.upsert_session(KEY, "sess_1", "Title", host_id="h1", workspace="/ws")
    record = await store.get_session(KEY)
    assert record is not None
    assert record.host_type == "external"


async def test_store_adds_host_type_to_a_pre_existing_database(tmp_path: Path) -> None:
    # A store written before host_type existed keeps the old table shape, and
    # every query naming the column would fail. initialize() must add it in place
    # and read existing rows as "external" — the behavior they were saved with.
    path = tmp_path / "bot.sqlite3"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE user_configs (
                user_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                workspace TEXT,
                host_id TEXT,
                host_name TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO user_configs VALUES ('U1','ag_1','debby','/ws','h1','Host One',1,1)"
        )
        await db.execute(
            """
            CREATE TABLE channel_sessions (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                omnigent_session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                owner_user_id TEXT,
                host_id TEXT,
                workspace TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO channel_sessions VALUES ('500','900','sess_1','T','U1','h1','/ws',1,1)"
        )
        await db.commit()

    store = SQLiteStore(path)
    await store.initialize()

    config = await store.get_user_config("U1")
    assert config is not None
    assert config.host_type == "external"
    assert config.host_id == "h1"
    record = await store.get_session(KEY)
    assert record is not None
    assert record.host_type == "external"
    # Idempotent: a second initialize on the upgraded file must not fail.
    await store.initialize()
    assert await store.get_user_config("U1") == config
