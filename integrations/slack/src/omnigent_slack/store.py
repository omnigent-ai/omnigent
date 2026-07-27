from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

from omnigent_slack.models import SessionRecord, ThreadKey, UserConfig


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
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (team_id, user_id)
                )
                """
            )
            await db.commit()

    async def get_session(self, key: ThreadKey) -> SessionRecord | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT omnigent_session_id, owner_user_id, host_id, workspace
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
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO thread_sessions (
                    team_id, channel_id, thread_ts, omnigent_session_id,
                    title, owner_user_id, host_id, workspace,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
                    omnigent_session_id = excluded.omnigent_session_id,
                    title = excluded.title,
                    owner_user_id = excluded.owner_user_id,
                    host_id = excluded.host_id,
                    workspace = excluded.workspace,
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
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_user_config(self, team_id: str, user_id: str) -> UserConfig | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT agent_id, agent_name, workspace, host_id, host_name
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
        )

    async def upsert_user_config(self, team_id: str, user_id: str, config: UserConfig) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO user_configs (
                    team_id, user_id, agent_id, agent_name,
                    workspace, host_id, host_name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, user_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agent_name = excluded.agent_name,
                    workspace = excluded.workspace,
                    host_id = excluded.host_id,
                    host_name = excluded.host_name,
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
