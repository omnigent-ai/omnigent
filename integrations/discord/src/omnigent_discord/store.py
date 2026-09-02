from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

from omnigent_discord.models import ChannelKey, SessionRecord, UserConfig


class SQLiteStore:
    """Channel→session mapping, per-user config, and message de-duplication.

    Keys are bare Discord snowflakes: a channel id identifies a conversation
    globally and a user id identifies a person globally, so neither needs the
    guild as a prefix. ``guild_id`` is stored alongside a session for
    operability (which community a session came from), never as part of a key.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_sessions (
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
                """
                CREATE TABLE IF NOT EXISTS discord_events (
                    event_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_configs (
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
            await db.commit()

    async def get_session(self, key: ChannelKey) -> SessionRecord | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT omnigent_session_id, owner_user_id, host_id, workspace
                FROM channel_sessions
                WHERE channel_id = ?
                """,
                (key.channel_id,),
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
        key: ChannelKey,
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
                INSERT INTO channel_sessions (
                    channel_id, guild_id, omnigent_session_id,
                    title, owner_user_id, host_id, workspace,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    omnigent_session_id = excluded.omnigent_session_id,
                    title = excluded.title,
                    owner_user_id = excluded.owner_user_id,
                    host_id = excluded.host_id,
                    workspace = excluded.workspace,
                    updated_at = excluded.updated_at
                """,
                (
                    key.channel_id,
                    key.guild_id,
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

    async def clear_session(self, key: ChannelKey) -> bool:
        """Forget a channel's session so the next message starts a fresh one.

        Backs ``/omnigent new`` — the DM equivalent of starting a new thread,
        since a Discord DM has no threads and would otherwise be pinned to one
        session forever. Returns whether a mapping was actually removed.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "DELETE FROM channel_sessions WHERE channel_id = ?", (key.channel_id,)
            )
            removed = cursor.rowcount > 0
            await cursor.close()
            await db.commit()
        return removed

    async def get_user_config(self, user_id: str) -> UserConfig | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT agent_id, agent_name, workspace, host_id, host_name
                FROM user_configs
                WHERE user_id = ?
                """,
                (user_id,),
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

    async def upsert_user_config(self, user_id: str, config: UserConfig) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO user_configs (
                    user_id, agent_id, agent_name,
                    workspace, host_id, host_name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agent_name = excluded.agent_name,
                    workspace = excluded.workspace,
                    host_id = excluded.host_id,
                    host_name = excluded.host_name,
                    updated_at = excluded.updated_at
                """,
                (
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

    async def clear_user_data(self, user_id: str) -> None:
        """Delete a user's saved config and every session channel they own.

        Backs ``/omnigent logout``: after this the user is fully reset — their
        agent/host/workspace choice is gone and their threads/DMs no longer map
        to any Omnigent session, so a later message starts fresh (once they
        reconfigure).
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM user_configs WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM channel_sessions WHERE owner_user_id = ?", (user_id,))
            await db.commit()

    async def claim_event(self, event_id: str | None, ttl_seconds: int = 7 * 24 * 60 * 60) -> bool:
        """Claim a message id exactly once, so a redelivery is a no-op.

        The gateway replays events after a resumed session, so the same message
        can arrive twice. Returns whether THIS call won the claim.
        """
        if not event_id:
            return True

        now = int(time.time())
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO discord_events (event_id, created_at) VALUES (?, ?)",
                (event_id, now),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            await db.execute(
                "DELETE FROM discord_events WHERE created_at < ?", (now - ttl_seconds,)
            )
            await db.commit()
        return claimed

    async def unclaim_event(self, event_id: str | None) -> None:
        """Release a previously claimed event so it can be processed again.

        Called when handling a claimed event fails before the turn is underway:
        the claim would otherwise permanently swallow the message. Dropping the
        marker lets a redelivery — or the user re-sending — be processed. No-op
        without an id.
        """
        if not event_id:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM discord_events WHERE event_id = ?", (event_id,))
            await db.commit()
