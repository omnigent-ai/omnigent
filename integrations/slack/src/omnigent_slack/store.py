from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

from omnigent_slack.events import HostType
from omnigent_slack.models import SessionRecord, ThreadKey, UserConfig

# Columns added to a table after it was first created, as
# ``(table, column, definition)``. ``CREATE TABLE IF NOT EXISTS`` leaves an
# existing table alone, so a database written by an earlier build keeps the old
# shape and every query naming a newer column fails. ``initialize`` adds each
# missing one in place. A definition must carry a default, since SQLite requires
# one to add a NOT NULL column to a populated table.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("thread_sessions", "host_type", "TEXT NOT NULL DEFAULT 'external'"),
    ("user_configs", "host_type", "TEXT NOT NULL DEFAULT 'external'"),
)


def _host_type(value: Any) -> HostType:
    """Narrow a stored ``host_type`` to the literal, defaulting to external.

    A row written before the column existed reads as its ``'external'``
    default, and anything unrecognized would be a hand-edited database. Both
    land on ``"external"`` — the behavior every pre-existing row had.
    """
    return "managed" if value == "managed" else "external"


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
                    owner_user_id TEXT,
                    host_id TEXT,
                    workspace TEXT,
                    host_type TEXT NOT NULL DEFAULT 'external',
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_configs (
                    team_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    workspace TEXT,
                    host_id TEXT,
                    host_name TEXT,
                    host_type TEXT NOT NULL DEFAULT 'external',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (team_id, user_id)
                )
                """
            )
            await self._add_missing_columns(db)
            await db.commit()

    @staticmethod
    async def _add_missing_columns(db: aiosqlite.Connection) -> None:
        """Bring an older database's tables up to the current column set.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``, so read each table's live
        columns and add only what :data:`_ADDED_COLUMNS` says is missing. That
        keeps ``initialize`` idempotent on a fresh file and on one written
        before the column existed.
        """
        for table, column, definition in _ADDED_COLUMNS:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            if any(row[1] == column for row in rows):
                continue
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def get_session(self, key: ThreadKey) -> SessionRecord | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT omnigent_session_id, owner_user_id, host_id, workspace, host_type
                FROM thread_sessions
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
                """,
                (key.team_id, key.channel_id, key.thread_ts),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return SessionRecord(
            session_id=str(row[0]),
            owner_user_id=str(row[1]) if row[1] is not None else None,
            host_id=str(row[2]) if row[2] is not None else None,
            workspace=str(row[3]) if row[3] is not None else None,
            host_type=_host_type(row[4]),
        )

    async def upsert_session(
        self,
        key: ThreadKey,
        session_id: str,
        title: str,
        *,
        owner_user_id: str | None = None,
        host_id: str | None = None,
        workspace: str | None = None,
        host_type: HostType = "external",
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO thread_sessions (
                    team_id, channel_id, thread_ts, omnigent_session_id,
                    title, owner_user_id, host_id, workspace, host_type,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
                    omnigent_session_id = excluded.omnigent_session_id,
                    title = excluded.title,
                    owner_user_id = excluded.owner_user_id,
                    host_id = excluded.host_id,
                    workspace = excluded.workspace,
                    host_type = excluded.host_type,
                    updated_at = excluded.updated_at
                """,
                (
                    key.team_id,
                    key.channel_id,
                    key.thread_ts,
                    session_id,
                    title,
                    owner_user_id,
                    host_id,
                    workspace,
                    host_type,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_user_config(self, team_id: str, user_id: str) -> UserConfig | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT agent_id, agent_name, workspace, host_id, host_name, host_type
                FROM user_configs
                WHERE team_id = ? AND user_id = ?
                """,
                (team_id, user_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return UserConfig(
            agent_id=str(row[0]),
            agent_name=str(row[1]),
            workspace=str(row[2]) if row[2] is not None else "",
            host_id=str(row[3]) if row[3] is not None else None,
            host_name=str(row[4]) if row[4] is not None else None,
            host_type=_host_type(row[5]),
        )

    async def upsert_user_config(self, team_id: str, user_id: str, config: UserConfig) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO user_configs (
                    team_id, user_id, agent_id, agent_name, workspace,
                    host_id, host_name, host_type, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, user_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agent_name = excluded.agent_name,
                    workspace = excluded.workspace,
                    host_id = excluded.host_id,
                    host_name = excluded.host_name,
                    host_type = excluded.host_type,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id,
                    user_id,
                    config.agent_id,
                    config.agent_name,
                    config.workspace,
                    config.host_id,
                    config.host_name,
                    config.host_type,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def clear_user_data(self, team_id: str, user_id: str) -> None:
        """Delete a user's saved config and every session thread they own.

        Backs ``/omnigent logout``: after this the user is fully reset —
        their agent/host/workspace choice is gone and their channel/DM
        threads no longer map to any Omnigent session, so a later message
        starts fresh (once they reconfigure).
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM user_configs WHERE team_id = ? AND user_id = ?",
                (team_id, user_id),
            )
            await db.execute(
                "DELETE FROM thread_sessions WHERE team_id = ? AND owner_user_id = ?",
                (team_id, user_id),
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

    async def unclaim_event(self, event_id: str | None) -> None:
        """Release a previously claimed event so it can be processed again.

        Called when handling a claimed event fails before the turn is underway:
        Bolt has already auto-acked, so Slack won't redeliver, and the claim would
        otherwise permanently swallow the message. Dropping the marker lets a
        redelivery — or the user re-sending — be processed. No-op without an id.
        """
        if not event_id:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM slack_events WHERE event_id = ?", (event_id,))
            await db.commit()
