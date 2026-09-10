import asyncio
import time
from pathlib import Path

import aiosqlite
import omnigent_slack.store as store_module
import pytest
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


async def test_store_thread_marks_only_ever_move_forward(tmp_path: Path) -> None:
    # The marks are how a thread catches up across mentions. Moving one BACKWARDS
    # re-opens ground a later turn covered, so the next mention re-quotes the
    # whole span — which an unconditional UPDATE does on a delayed commit.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "title", owner_user_id="U1")

    record = await store.get_session(key)
    assert record is not None
    # A session predating the marks: NULL, which reads as "no floor".
    assert (record.context_read_ts, record.context_delivered_ts) == (None, None)

    await store.advance_thread_marks(key, read_ts="100.3000", delivered_ts="100.3000")
    await store.advance_thread_marks(key, read_ts="100.2000", delivered_ts="100.2000")
    record = await store.get_session(key)
    assert record is not None
    assert (record.context_read_ts, record.context_delivered_ts) == ("100.3000", "100.3000")

    # Ordered as timestamps, not as strings: "1000000000.1" is NEWER than
    # "999999999.9" even though it sorts earlier.
    await store.advance_thread_marks(key, read_ts="999999999.900000")
    await store.advance_thread_marks(key, read_ts="1000000000.100000")
    await store.advance_thread_marks(key, read_ts="999999999.900000")
    record = await store.get_session(key)
    assert record is not None
    assert record.context_read_ts == "1000000000.100000"

    # Each mark moves on its own; ``None`` leaves the other alone.
    await store.advance_thread_marks(key, delivered_ts="1000000001.000000")
    record = await store.get_session(key)
    assert record is not None
    assert record.context_read_ts == "1000000000.100000"
    assert record.context_delivered_ts == "1000000001.000000"


async def test_store_thread_marks_survive_a_session_upsert(tmp_path: Path) -> None:
    # A thread whose session is replaced (a re-created session on the same
    # thread) must keep its read position, or the new session re-quotes the
    # whole thread from the top.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "title", owner_user_id="U1")
    await store.advance_thread_marks(key, read_ts="100.5", delivered_ts="100.5")

    await store.upsert_session(key, "conv_2", "title", owner_user_id="U1")
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_2"
    assert (record.context_read_ts, record.context_delivered_ts) == ("100.5", "100.5")


async def test_store_advancing_marks_on_an_unknown_thread_is_a_no_op(tmp_path: Path) -> None:
    # A logout between the read and the acceptance deletes the row. Committing
    # must not resurrect it as a session-less mark holder, and must not raise.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")

    await store.advance_thread_marks(key, read_ts="100.5", delivered_ts="100.5")

    assert await store.get_session(key) is None


async def test_store_adds_thread_marks_to_a_pre_existing_database(tmp_path: Path) -> None:
    # A store predating the marks keeps a table shape every query naming them
    # would fail on. They are added in place, read as NULL on existing rows (the
    # bounded window, never a whole-thread backfill), and added idempotently.
    path = tmp_path / "store.sqlite3"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE thread_sessions (
                team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                omnigent_session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                owner_user_id TEXT,
                host_id TEXT,
                workspace TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (team_id, channel_id, thread_ts)
            )
            """
        )
        await db.execute(
            "INSERT INTO thread_sessions "
            "VALUES ('T1','C1','100.1','conv_1','t','U1','h1','/ws',1,1)"
        )
        await db.commit()

    store = SQLiteStore(path)
    await store.initialize()
    # Idempotent: a second initialize on the upgraded file must not fail.
    await store.initialize()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_1"
    assert record.host_type == "external"
    assert (record.context_read_ts, record.context_delivered_ts) == (None, None)

    # And the upgraded row takes marks normally from here on.
    await store.advance_thread_marks(key, read_ts="100.5", delivered_ts="100.5")
    record = await store.get_session(key)
    assert record is not None
    assert (record.context_read_ts, record.context_delivered_ts) == ("100.5", "100.5")


async def test_store_thread_marks_serialize_under_concurrent_writers(tmp_path: Path) -> None:
    # Two turns on one thread can finish at the same moment, and each commit is a
    # read-compare-write. Without ``BEGIN IMMEDIATE`` both read the same stored
    # value and the later write wins on arrival order rather than on timestamp,
    # so an older mention can drop the mark back.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "title", owner_user_id="U1")

    # Interleaved so the newest is neither first nor last to be scheduled.
    marks = ["100.0300", "100.0900", "100.0100", "100.0700", "100.0500"]
    await asyncio.gather(
        *(store.advance_thread_marks(key, read_ts=ts, delivered_ts=ts) for ts in marks)
    )

    record = await store.get_session(key)
    assert record is not None
    assert (record.context_read_ts, record.context_delivered_ts) == ("100.0900", "100.0900")


async def test_store_thread_marks_wait_out_a_held_write_lock(tmp_path: Path) -> None:
    # A commit contending with another writer must wait for the lock, not fail
    # on contact. The wait is the module's stated ``busy_timeout``: shorten it
    # below how long the lock is held and the same commit gives up instead.
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "title", owner_user_id="U1")

    async def hold_the_write_lock(seconds: float) -> None:
        async with aiosqlite.connect(tmp_path / "store.sqlite3") as blocker:
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute("UPDATE thread_sessions SET title = 'held' WHERE team_id = 'T1'")
            await asyncio.sleep(seconds)
            await blocker.rollback()

    held = asyncio.create_task(hold_the_write_lock(0.3))
    await asyncio.sleep(0.05)
    await store.advance_thread_marks(key, read_ts="100.5", delivered_ts="100.5")
    await held

    record = await store.get_session(key)
    assert record is not None
    assert (record.context_read_ts, record.context_delivered_ts) == ("100.5", "100.5")


async def test_store_thread_marks_give_up_after_the_stated_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the contract: the wait is bounded and it is OURS. With
    # the pragma removed the driver's own five-second default would carry this
    # commit through, so the short timeout below is what makes it give up.
    monkeypatch.setattr(store_module, "_BUSY_TIMEOUT_MS", 50)
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_1", "title", owner_user_id="U1")

    release = asyncio.Event()

    async def hold_the_write_lock() -> None:
        async with aiosqlite.connect(tmp_path / "store.sqlite3") as blocker:
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute("UPDATE thread_sessions SET title = 'held' WHERE team_id = 'T1'")
            await release.wait()
            await blocker.rollback()

    held = asyncio.create_task(hold_the_write_lock())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    with pytest.raises(aiosqlite.OperationalError, match="locked"):
        await store.advance_thread_marks(key, read_ts="100.5", delivered_ts="100.5")
    waited = time.monotonic() - started
    release.set()
    await held

    # Bounded by the stated timeout, nowhere near the five-second default.
    assert waited < 1.0
    record = await store.get_session(key)
    assert record is not None
    assert (record.context_read_ts, record.context_delivered_ts) == (None, None)
