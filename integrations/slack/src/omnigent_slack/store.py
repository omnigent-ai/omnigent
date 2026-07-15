from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

from omnigent_slack.models import ThreadKey


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_sessions (
                    team_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    omnigent_session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (team_id, channel_id, thread_ts)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS slack_events (
                    event_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL
                )
                """
            )
            await db.commit()

    async def get_session_id(self, key: ThreadKey) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT omnigent_session_id
                FROM thread_sessions
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
                """,
                (key.team_id, key.channel_id, key.thread_ts),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return str(row[0])

    async def upsert_session(self, key: ThreadKey, session_id: str, title: str) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO thread_sessions (
                    team_id, channel_id, thread_ts, omnigent_session_id,
                    title, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
                    omnigent_session_id = excluded.omnigent_session_id,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (key.team_id, key.channel_id, key.thread_ts, session_id, title, now, now),
            )
            await db.commit()

    async def claim_event(self, event_id: str | None, ttl_seconds: int = 7 * 24 * 60 * 60) -> bool:
        if not event_id:
            return True

        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO slack_events (event_id, created_at) VALUES (?, ?)",
                (event_id, now),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            await db.execute("DELETE FROM slack_events WHERE created_at < ?", (now - ttl_seconds,))
            await db.commit()
        return claimed
