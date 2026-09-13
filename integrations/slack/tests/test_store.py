from pathlib import Path

import aiosqlite
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.store import SQLiteStore


async def test_store_persists_thread_session(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    assert await store.get_session(key) is None

    await store.upsert_session(
        key,
        "conv_1",
        "title",
        owner_user_id="U1",
        host_id="host_a",
    )
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_1"
    assert record.owner_user_id == "U1"
    assert record.host_id == "host_a"

    await store.upsert_session(key, "conv_2", "title", owner_user_id="U1")
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_2"


async def test_store_user_config_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.get_user_config("T1", "U1") is None

    config = UserConfig(
        agent_id="ag_1",
        agent_name="Helper",
        workspace="/home/me/project",
        host_id="host_a",
        host_name="Host A",
    )
    await store.upsert_user_config("T1", "U1", config)
    assert await store.get_user_config("T1", "U1") == config

    # Upsert overwrites and host may be cleared back to "any".
    updated = UserConfig(
        agent_id="ag_2",
        agent_name="Other",
        workspace="/tmp/ws",
    )
    await store.upsert_user_config("T1", "U1", updated)
    assert await store.get_user_config("T1", "U1") == updated
    # A different user in the same workspace is isolated.
    assert await store.get_user_config("T1", "U2") is None


async def test_store_round_trips_managed_host_type(tmp_path: Path) -> None:
    # A managed session carries no host id and no workspace path — the server
    # chooses both — so host_type is the only record of where it runs.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    config = UserConfig(agent_id="ag_1", agent_name="Helper", workspace="", host_type="managed")
    await store.upsert_user_config("T1", "U1", config)
    assert await store.get_user_config("T1", "U1") == config

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "t", owner_user_id="U1", host_type="managed")
    record = await store.get_session(key)
    assert record is not None
    assert record.host_type == "managed"
    assert record.host_id is None

    # Switching a user back to their own host is recorded as external again.
    await store.upsert_user_config(
        "T1", "U1", UserConfig("ag_1", "Helper", "/home/me", host_id="h1")
    )
    reread = await store.get_user_config("T1", "U1")
    assert reread is not None and reread.host_type == "external"


async def test_store_adds_host_type_to_a_pre_existing_database(tmp_path: Path) -> None:
    # A store written before host_type existed keeps the old table shape, and
    # every query naming the column would fail. initialize() must add it in place
    # and read existing rows as "external" — the behavior they were saved with.
    path = tmp_path / "store.sqlite3"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE user_configs (
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                workspace TEXT,
                host_id TEXT,
                host_name TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (team_id, user_id)
            )
            """
        )
        await db.execute(
            "INSERT INTO user_configs VALUES ('T1','U1','ag_1','Helper','/ws','h1','H',1,1)"
        )
        await db.commit()

    store = SQLiteStore(path)
    await store.initialize()

    config = await store.get_user_config("T1", "U1")
    assert config is not None
    assert config.host_type == "external"
    assert config.host_id == "h1"
    # Idempotent: a second initialize on the upgraded file must not fail.
    await store.initialize()
    assert await store.get_user_config("T1", "U1") == config


async def test_store_claim_event_dedupes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.claim_event("Ev1") is True
    assert await store.claim_event("Ev1") is False
    assert await store.claim_event(None) is True


async def test_store_unclaim_event_allows_reclaim(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.claim_event("Ev1") is True
    # Releasing the claim lets the same event id be processed again.
    await store.unclaim_event("Ev1")
    assert await store.claim_event("Ev1") is True
    # A no-op without an id, and harmless on an unknown id.
    await store.unclaim_event(None)
    await store.unclaim_event("never-seen")
