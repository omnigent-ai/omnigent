from pathlib import Path

from omnigent_slack.models import ThreadKey
from omnigent_slack.store import SQLiteStore


async def test_store_persists_thread_session(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    assert await store.get_session_id(key) is None

    await store.upsert_session(key, "conv_1", "title")
    assert await store.get_session_id(key) == "conv_1"

    await store.upsert_session(key, "conv_2", "title")
    assert await store.get_session_id(key) == "conv_2"


async def test_store_claim_event_dedupes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.claim_event("Ev1") is True
    assert await store.claim_event("Ev1") is False
    assert await store.claim_event(None) is True
