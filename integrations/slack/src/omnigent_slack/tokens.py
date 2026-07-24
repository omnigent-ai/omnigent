"""Storage for delegated Omnigent tokens.

Each Slack user who authenticates via the device flow gets a delegated
access + refresh token for their Omnigent server (see
``designs/DEVICE_AUTH.md``). Those are bearer credentials that let
this process act as that user.

Two backends implement the same :class:`TokenStore` protocol:

- :class:`EncryptedTokenStore` — persisted to SQLite, encrypted with a
  Fernet key held only in the environment, so a stolen database file
  alone cannot be used to impersonate anyone. Used when
  ``OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY`` is configured.
- :class:`InMemoryTokenStore` — tokens live only in process memory and
  are lost on restart (users re-authenticate). The fallback when no
  encryption key is set: we never write bearer credentials to disk in
  the clear, but the integration still works.

Both are keyed by ``(team_id, user_id, server_url)``: the bot targets one
operator-fixed server, but keying on it keeps tokens strictly scoped to the
server that issued them (and cleanly separated if the operator ever
repoints the bot).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """A stored delegated-token pair for one (user, server)."""

    access_token: str
    refresh_token: str
    updated_at: int


class TokenStore(Protocol):
    """Common interface over the encrypted and in-memory backends."""

    async def initialize(self) -> None: ...

    async def get(self, team_id: str, user_id: str, server_url: str) -> TokenRecord | None: ...

    async def list_for_user(self, team_id: str, user_id: str) -> list[tuple[str, TokenRecord]]: ...

    async def put(
        self,
        team_id: str,
        user_id: str,
        server_url: str,
        *,
        access_token: str,
        refresh_token: str,
    ) -> None: ...

    async def delete(self, team_id: str, user_id: str, server_url: str) -> None: ...


class EncryptedTokenStore:
    """Fernet-encrypted, SQLite-persisted store for delegated tokens.

    :param path: SQLite file (shared with :class:`SQLiteStore` or its
        own file — only this class touches the ``oauth_tokens`` table).
    :param encryption_key: A urlsafe-base64 Fernet key. Tokens are
        encrypted with it before they touch disk.
    """

    def __init__(self, path: Path, encryption_key: str) -> None:
        self._path = path
        self._fernet = Fernet(encryption_key.encode("utf-8"))

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    team_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    access_token_enc BLOB NOT NULL,
                    refresh_token_enc BLOB NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (team_id, user_id, server_url)
                )
                """
            )
            await db.commit()

    async def get(self, team_id: str, user_id: str, server_url: str) -> TokenRecord | None:
        server_url = server_url.rstrip("/")
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT access_token_enc, refresh_token_enc, updated_at
                FROM oauth_tokens
                WHERE team_id = ? AND user_id = ? AND server_url = ?
                """,
                (team_id, user_id, server_url),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        try:
            access = self._fernet.decrypt(row[0]).decode("utf-8")
            refresh = self._fernet.decrypt(row[1]).decode("utf-8")
        except InvalidToken:
            # Key rotated or DB tampered — treat as no token so the user
            # is prompted to re-authenticate rather than crashing.
            return None
        return TokenRecord(access_token=access, refresh_token=refresh, updated_at=int(row[2]))

    async def list_for_user(self, team_id: str, user_id: str) -> list[tuple[str, TokenRecord]]:
        """Return ``(server_url, record)`` for every token the user holds.

        Used by logout to revoke each server's grant. Undecryptable rows
        (wrong key) are skipped — they can't be revoked but are cleared
        by the accompanying delete.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT server_url, access_token_enc, refresh_token_enc, updated_at
                FROM oauth_tokens
                WHERE team_id = ? AND user_id = ?
                """,
                (team_id, user_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        out: list[tuple[str, TokenRecord]] = []
        for row in rows:
            try:
                access = self._fernet.decrypt(row[1]).decode("utf-8")
                refresh = self._fernet.decrypt(row[2]).decode("utf-8")
            except InvalidToken:
                continue
            out.append(
                (
                    str(row[0]),
                    TokenRecord(
                        access_token=access, refresh_token=refresh, updated_at=int(row[3])
                    ),
                )
            )
        return out

    async def put(
        self,
        team_id: str,
        user_id: str,
        server_url: str,
        *,
        access_token: str,
        refresh_token: str,
    ) -> None:
        server_url = server_url.rstrip("/")
        now = int(time.time())
        access_enc = self._fernet.encrypt(access_token.encode("utf-8"))
        refresh_enc = self._fernet.encrypt(refresh_token.encode("utf-8"))
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO oauth_tokens (
                    team_id, user_id, server_url,
                    access_token_enc, refresh_token_enc, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, user_id, server_url) DO UPDATE SET
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    updated_at = excluded.updated_at
                """,
                (team_id, user_id, server_url, access_enc, refresh_enc, now),
            )
            await db.commit()

    async def delete(self, team_id: str, user_id: str, server_url: str) -> None:
        server_url = server_url.rstrip("/")
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                DELETE FROM oauth_tokens
                WHERE team_id = ? AND user_id = ? AND server_url = ?
                """,
                (team_id, user_id, server_url),
            )
            await db.commit()


class InMemoryTokenStore:
    """Process-memory store for delegated tokens — never written to disk.

    The fallback when no encryption key is configured. Delegated tokens
    are bearer credentials, so writing them to disk in the clear is not
    acceptable; keeping them in memory lets the integration work while
    bounding exposure to the process lifetime. Tokens are lost on
    restart, so users re-authenticate — a deliberate trade rather than
    disabling the integration.
    """

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str, str], TokenRecord] = {}

    async def initialize(self) -> None:
        return None

    async def get(self, team_id: str, user_id: str, server_url: str) -> TokenRecord | None:
        return self._tokens.get((team_id, user_id, server_url.rstrip("/")))

    async def list_for_user(self, team_id: str, user_id: str) -> list[tuple[str, TokenRecord]]:
        return [
            (server, record)
            for (team, user, server), record in self._tokens.items()
            if team == team_id and user == user_id
        ]

    async def put(
        self,
        team_id: str,
        user_id: str,
        server_url: str,
        *,
        access_token: str,
        refresh_token: str,
    ) -> None:
        self._tokens[(team_id, user_id, server_url.rstrip("/"))] = TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            updated_at=int(time.time()),
        )

    async def delete(self, team_id: str, user_id: str, server_url: str) -> None:
        self._tokens.pop((team_id, user_id, server_url.rstrip("/")), None)
