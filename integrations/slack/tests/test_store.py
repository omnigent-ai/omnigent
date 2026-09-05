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


async def test_store_pending_message_round_trip(tmp_path: Path) -> None:
    # The message a user sent before setup, kept so it can be replayed. It must
    # come back with the thread it arrived in, so the answer lands there.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.take_pending_message("T1", "U1") is None

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_pending_message("U1", key, "what is the deploy status?", in_channel=True)

    pending = await store.take_pending_message("T1", "U1")
    assert pending is not None
    assert pending.key == key
    assert pending.text == "what is the deploy status?"
    assert pending.in_channel is True
    # Taking it removes it: a second setup submission must not replay the turn.
    assert await store.take_pending_message("T1", "U1") is None


async def test_store_pending_message_keeps_only_the_newest(tmp_path: Path) -> None:
    # Someone who asks a second thing while setup is still open wants THAT
    # answered — not both. The newer message replaces the older one, wherever
    # it arrived from.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    channel = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    dm = ThreadKey(team_id="T1", channel_id="D1", thread_ts="200.2")
    await store.upsert_pending_message("U1", channel, "first", in_channel=True)
    await store.upsert_pending_message("U1", dm, "second", in_channel=False)

    pending = await store.take_pending_message("T1", "U1")
    assert pending is not None
    assert pending.text == "second"
    assert pending.key == dm
    assert pending.in_channel is False
    # Another user's stash is untouched by either write.
    assert await store.take_pending_message("T1", "U2") is None


async def test_store_logout_drops_a_pending_message(tmp_path: Path) -> None:
    # Logout is a full reset. A message stashed before it must not be replayed
    # into the next setup the user completes.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_pending_message("U1", key, "hello", in_channel=True)
    await store.clear_user_data("T1", "U1")

    assert await store.take_pending_message("T1", "U1") is None


async def test_store_adds_pending_messages_to_a_pre_existing_database(tmp_path: Path) -> None:
    # A database written before pending_messages existed has only the old
    # tables. initialize() must create the new one in place, leaving the
    # existing rows alone.
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

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_pending_message("U1", key, "hello", in_channel=False)
    pending = await store.take_pending_message("T1", "U1")
    assert pending is not None and pending.key == key
    # The pre-existing config survived the upgrade.
    config = await store.get_user_config("T1", "U1")
    assert config is not None and config.agent_id == "ag_1"


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
