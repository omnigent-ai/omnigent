"""Durable local cache and outbox for explicitly connected preference clients.

Callers supply the authenticated HTTP client and choose when to synchronize.
Nothing here imports YAML, changes launch defaults, or starts a background task.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4

import httpx
from filelock import FileLock

from omnigent.preference_sync import (
    MAX_CONFLICTS,
    MAX_KEYS,
    MAX_RECORD_BYTES,
    PREFERENCE_PAGE_SIZE,
    Name,
    PreferenceMutation,
    PreferenceNamespace,
    PreferencePage,
    PreferenceResult,
    PreferenceSnapshot,
    ProtocolModel,
    SharedPreferenceEntry,
    SharedPreferenceKey,
    Value,
)

_PATH = "/v1/preferences/shared"
_CACHE_VERSION = 2
MAX_PENDING_OPERATIONS = 512
_Result = TypeVar("_Result")


def _encode(value: ProtocolModel) -> bytes:
    payload = value.model_dump_json().encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("Preference cache record exceeds the byte limit")
    return payload


async def _disk_call(function: Callable[..., _Result], *args: object) -> _Result:
    """Keep the synchronizer lock until its worker has stopped, even on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                task.result()
                raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def read_preference_snapshot(
    client: httpx.AsyncClient,
    *,
    identity: PreferenceNamespace | None = None,
    page_size: int = PREFERENCE_PAGE_SIZE,
) -> PreferenceSnapshot:
    """Collect one revision atomically; HTTP/cursor failures discard partial pages.

    Callers with an established binding must supply it. An unbound caller may
    inspect the authenticated namespace, but must confirm it before adoption.
    """
    if type(page_size) is not int or not 1 <= page_size <= PREFERENCE_PAGE_SIZE:
        raise ValueError("Invalid preference page size")
    entries, conflicts = [], []
    entry_keys, conflict_ids, cursors = set(), set(), set()
    cursor = None
    revision = None
    for _ in range(MAX_KEYS + MAX_CONFLICTS + 1):
        params = {"limit": str(page_size)}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(_PATH, params=params)
        response.raise_for_status()
        page = PreferencePage.model_validate_json(response.content)
        if identity is None:
            identity = page.identity
        if page.identity != identity:
            raise ValueError("Preference snapshot belongs to another identity")
        if revision is None:
            revision = page.revision
        if page.revision != revision:
            raise ValueError("Preference revision changed between pages")
        count = len(page.entries) + len(page.conflicts)
        if count > page_size or (page.next_cursor is not None and count == 0):
            raise ValueError("Invalid preference page boundary")
        for entry in page.entries:
            key = entry.key.storage_key()
            if key in entry_keys:
                raise ValueError("Duplicate preference entry across pages")
            entry_keys.add(key)
            entries.append(entry)
        for conflict in page.conflicts:
            if conflict.operation_id in conflict_ids:
                raise ValueError("Duplicate preference conflict across pages")
            if conflict.identity != identity:
                raise ValueError("Preference conflict belongs to another identity")
            conflict_ids.add(conflict.operation_id)
            conflicts.append(conflict)
        if len(entries) > MAX_KEYS or len(conflicts) > MAX_CONFLICTS:
            raise ValueError("Preference snapshot exceeds limits")
        if page.next_cursor is None:
            return PreferenceSnapshot(
                identity=identity, revision=revision, entries=entries, conflicts=conflicts
            )
        if page.next_cursor in cursors:
            raise ValueError("Preference cursor did not advance")
        cursors.add(page.next_cursor)
        cursor = page.next_cursor
    raise ValueError("Preference pagination did not terminate")


class _Binding(ProtocolModel):
    identity: PreferenceNamespace
    installation_id: Name


class PreferenceClient:
    """One identity-bound SQLite cache, shared safely by local processes.

    ``enqueue`` acknowledges a local durable write, not a server save. ``sync``
    flushes a bounded batch and refreshes the cache; failures retain unsent or
    uncertain operations. Use a separate file for each account/workspace.
    """

    def __init__(self, path: Path, *, identity: PreferenceNamespace, installation_id: str) -> None:
        self.path = path
        self._binding = _Binding(identity=identity, installation_id=installation_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone():
                    raise ValueError("Unversioned preference cache; preserve it for recovery")
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "binding BLOB NOT NULL CHECK(length(binding) <= 16000), "
                    "revision INTEGER, revision_floor INTEGER NOT NULL, "
                    "next_sequence INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE entries (key VARCHAR(64) PRIMARY KEY NOT NULL "
                    "CHECK(length(key) = 64), payload BLOB CHECK(length(payload) <= 16000), "
                    "acknowledged BLOB CHECK(length(acknowledged) <= 16000)) WITHOUT ROWID"
                )
                connection.execute(
                    "CREATE TABLE conflicts (operation_id BLOB PRIMARY KEY NOT NULL "
                    "CHECK(length(operation_id) = 16), "
                    "payload BLOB NOT NULL CHECK(length(payload) <= 16000)) WITHOUT ROWID"
                )
                connection.execute(
                    "CREATE TABLE outbox (sequence BIGINT PRIMARY KEY NOT NULL, "
                    "key VARCHAR(64) NOT NULL CHECK(length(key) = 64), "
                    "operation BLOB NOT NULL CHECK(length(operation) <= 16000), "
                    "predecessor BLOB CHECK(length(predecessor) = 16), "
                    "sent INTEGER NOT NULL CHECK(sent IN (0, 1))) WITHOUT ROWID"
                )
                connection.execute("CREATE INDEX outbox_key_sequence ON outbox(key, sequence)")
                connection.execute("CREATE UNIQUE INDEX outbox_predecessor ON outbox(predecessor)")
                connection.execute(
                    "INSERT INTO state (id, binding, revision, revision_floor, next_sequence) "
                    "VALUES (1, ?, NULL, 0, 1)",
                    (_encode(self._binding),),
                )
                connection.execute("PRAGMA user_version = 1")
                version = 1
            if version == 1:
                connection.execute(
                    "CREATE TABLE machine (key VARCHAR(64) PRIMARY KEY NOT NULL "
                    "CHECK(length(key) = 64), payload BLOB NOT NULL "
                    "CHECK(length(payload) <= 16000)) WITHOUT ROWID"
                )
                connection.execute(
                    "CREATE TABLE import_entries (operation_id BLOB PRIMARY KEY NOT NULL "
                    "CHECK(length(operation_id) = 16), "
                    "previous BLOB NOT NULL CHECK(length(previous) <= 16000), "
                    "result BLOB CHECK(length(result) <= 16000)) WITHOUT ROWID"
                )
                connection.execute("PRAGMA user_version = 2")
            elif version != _CACHE_VERSION:
                raise ValueError(f"Unsupported preference cache version: {version}")
            binding = connection.execute("SELECT binding FROM state WHERE id = 1").fetchone()[0]
            if _Binding.model_validate_json(binding) != self._binding:
                raise ValueError("Preference cache belongs to another identity or installation")

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cached(self) -> PreferenceSnapshot | None:
        """Return the last complete server snapshot; pending edits stay separate."""
        with self._transaction(write=False) as connection:
            revision = connection.execute("SELECT revision FROM state WHERE id = 1").fetchone()[0]
            if revision is None:
                return None
            entries = connection.execute(
                "SELECT payload FROM entries ORDER BY key LIMIT ?", (MAX_KEYS + 1,)
            ).fetchall()
            conflicts = connection.execute(
                "SELECT payload FROM conflicts ORDER BY operation_id LIMIT ?", (MAX_CONFLICTS + 1,)
            ).fetchall()
        if len(entries) > MAX_KEYS or len(conflicts) > MAX_CONFLICTS:
            raise ValueError("Preference cache exceeds limits")
        return PreferenceSnapshot(
            identity=self._binding.identity,
            revision=revision,
            entries=[
                SharedPreferenceEntry.model_validate_json(row[0])
                for row in entries
                if row[0] is not None
            ],
            conflicts=[PreferenceMutation.model_validate_json(row[0]) for row in conflicts],
        )

    def pending(self) -> list[PreferenceMutation]:
        """List durable edits in submission order, including uncertain deliveries."""
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT operation FROM outbox ORDER BY sequence LIMIT ?",
                (MAX_PENDING_OPERATIONS + 1,),
            ).fetchall()
        if len(rows) > MAX_PENDING_OPERATIONS:
            raise ValueError("Preference outbox exceeds limits")
        return [PreferenceMutation.model_validate_json(row[0]) for row in rows]

    def discard(self, operation_id: UUID) -> PreferenceMutation:
        """Explicitly abandon the first queued edit, allowing later work to sync.

        Inspect the HTTP error and ``pending()`` first. Discarding never undoes
        a server commit; uncertain deliveries may already have applied. Successors
        retain their versions so they cannot silently overwrite that value.
        """
        with (
            FileLock(str(self.path) + ".sync.lock", timeout=0),
            self._transaction() as connection,
        ):
            row = connection.execute(
                "SELECT sequence, operation FROM outbox ORDER BY sequence LIMIT 1"
            ).fetchone()
            operation = PreferenceMutation.model_validate_json(row[1]) if row else None
            if operation is None or operation.operation_id != operation_id:
                raise ValueError("Only the first queued preference operation can be discarded")
            connection.execute(
                "UPDATE outbox SET predecessor = NULL WHERE predecessor = ?",
                (operation_id.bytes,),
            )
            connection.execute("DELETE FROM outbox WHERE sequence = ?", (row[0],))
        return operation

    def enqueue(
        self, key: SharedPreferenceKey, value: Value, *, resolves: tuple[UUID, ...] = ()
    ) -> UUID:
        """Save an explicit edit locally, without making a network request.

        Successive offline edits to one key form a causal chain. Only an
        unattempted successor may inherit its predecessor's acknowledged version.
        """
        with self._transaction() as connection:
            revision, sequence = connection.execute(
                "SELECT revision, next_sequence FROM state WHERE id = 1"
            ).fetchone()
            if revision is None:
                raise ValueError("Synchronize once to verify identity before queuing preferences")
            count = connection.execute(
                "SELECT COUNT(*) FROM (SELECT sequence FROM outbox LIMIT ?)",
                (MAX_PENDING_OPERATIONS,),
            ).fetchone()[0]
            if count >= MAX_PENDING_OPERATIONS:
                raise ValueError("Preference outbox is full; synchronize before adding edits")
            previous = connection.execute(
                "SELECT operation FROM outbox WHERE key = ? ORDER BY sequence DESC LIMIT 1",
                (key.storage_key(),),
            ).fetchone()
            predecessor = PreferenceMutation.model_validate_json(previous[0]) if previous else None
            raw = connection.execute(
                "SELECT COALESCE(acknowledged, payload) FROM entries WHERE key = ?",
                (key.storage_key(),),
            ).fetchone()
            version = SharedPreferenceEntry.model_validate_json(raw[0]).version if raw else 0
            operation = PreferenceMutation(
                protocol_version=1,
                identity=self._binding.identity,
                installation_id=self._binding.installation_id,
                operation_id=uuid4(),
                key=key,
                expected_version=version,
                value=value,
                resolves=list(resolves),
            )
            connection.execute(
                "INSERT INTO outbox (sequence, key, operation, predecessor, sent) "
                "VALUES (?, ?, ?, ?, 0)",
                (
                    sequence,
                    key.storage_key(),
                    _encode(operation),
                    predecessor.operation_id.bytes if predecessor else None,
                ),
            )
            connection.execute("UPDATE state SET next_sequence = ? WHERE id = 1", (sequence + 1,))
        return operation.operation_id

    def _accept_snapshot(self, snapshot: PreferenceSnapshot) -> None:
        if snapshot.identity != self._binding.identity:
            raise ValueError("Server preference identity does not match this cache")
        if len(snapshot.entries) > MAX_KEYS or len(snapshot.conflicts) > MAX_CONFLICTS:
            raise ValueError("Preference snapshot exceeds limits")
        with self._transaction() as connection:
            floor = connection.execute("SELECT revision_floor FROM state WHERE id = 1").fetchone()[
                0
            ]
            if snapshot.revision < floor:
                raise ValueError("Server preference snapshot is older than acknowledged state")
            incoming = {entry.key.storage_key(): entry for entry in snapshot.entries}
            if len(incoming) != len(snapshot.entries):
                raise ValueError("Server preference snapshot has duplicate keys")
            for key, raw in connection.execute(
                "SELECT key, COALESCE(acknowledged, payload) FROM entries ORDER BY key LIMIT ?",
                (MAX_KEYS + 1,),
            ):
                known = SharedPreferenceEntry.model_validate_json(raw)
                entry = incoming.get(key)
                if entry is None or entry.version < known.version:
                    raise ValueError("Server preference snapshot lost an acknowledged version")
                if entry.version == known.version and entry != known:
                    raise ValueError("Server preference value changed without a version")
            connection.executemany(
                "INSERT OR REPLACE INTO entries (key, payload, acknowledged) VALUES (?, ?, NULL)",
                [(key, _encode(entry)) for key, entry in incoming.items()],
            )
            connection.execute(
                "DELETE FROM conflicts WHERE operation_id IN "
                "(SELECT operation_id FROM conflicts LIMIT ?)",
                (MAX_CONFLICTS + 1,),
            )
            connection.executemany(
                "INSERT INTO conflicts (operation_id, payload) VALUES (?, ?)",
                [(item.operation_id.bytes, _encode(item)) for item in snapshot.conflicts],
            )
            connection.execute(
                "UPDATE state SET revision = ?, revision_floor = ? WHERE id = 1",
                (snapshot.revision, snapshot.revision),
            )

    def _next(self) -> tuple[int, PreferenceMutation] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT sequence, operation, predecessor FROM outbox ORDER BY sequence LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            if row[2] is not None:
                raise ValueError("Preference outbox has a missing predecessor")
            connection.execute("UPDATE outbox SET sent = 1 WHERE sequence = ?", (row[0],))
            return row[0], PreferenceMutation.model_validate_json(row[1])

    def _acknowledge(
        self, sequence: int, operation: PreferenceMutation, result: PreferenceResult
    ) -> None:
        if result.operation_id != operation.operation_id or result.entry.key != operation.key:
            raise ValueError("Preference receipt does not match the submitted operation")
        if result.status != "conflict" and result.entry.value != operation.value:
            raise ValueError("Preference receipt did not acknowledge the submitted value")
        with self._transaction() as connection:
            raw = connection.execute(
                "SELECT COALESCE(acknowledged, payload) FROM entries WHERE key = ?",
                (operation.key.storage_key(),),
            ).fetchone()
            known = SharedPreferenceEntry.model_validate_json(raw[0]) if raw else None
            if known is None or result.entry.version > known.version:
                connection.execute(
                    "INSERT INTO entries (key, payload, acknowledged) VALUES (?, NULL, ?) "
                    "ON CONFLICT(key) DO UPDATE SET acknowledged = excluded.acknowledged",
                    (operation.key.storage_key(), _encode(result.entry)),
                )
            connection.execute(
                "UPDATE state SET revision_floor = MAX(revision_floor, ?) WHERE id = 1",
                (result.revision,),
            )
            for successor_id, payload, sent in connection.execute(
                "SELECT sequence, operation, sent FROM outbox WHERE predecessor = ? LIMIT 1",
                (operation.operation_id.bytes,),
            ).fetchall():
                if sent:
                    raise ValueError("Cannot rebase an attempted preference operation")
                successor = PreferenceMutation.model_validate_json(payload)
                # Conflict receipts never authorize overwriting the competing value.
                if result.status != "conflict":
                    successor = successor.model_copy(
                        update={"expected_version": result.entry.version}
                    )
                connection.execute(
                    "UPDATE outbox SET operation = ?, predecessor = NULL WHERE sequence = ?",
                    (_encode(successor), successor_id),
                )
            connection.execute(
                "UPDATE import_entries SET result = ? WHERE operation_id = ?",
                (_encode(result), operation.operation_id.bytes),
            )
            connection.execute("DELETE FROM outbox WHERE sequence = ?", (sequence,))

    async def sync(
        self,
        client: httpx.AsyncClient,
        *,
        max_operations: int = 100,
        page_size: int = PREFERENCE_PAGE_SIZE,
    ) -> PreferenceSnapshot:
        """Verify identity, deliver a bounded batch, then fetch authoritative state.

        HTTP errors, cancellation, and malformed replies leave uncertain edits
        queued with the same operation ID/body. The caller owns retry scheduling,
        authentication, URL, and HTTP timeouts. No database lock spans HTTP I/O.
        """
        if max_operations < 0:
            raise ValueError("max_operations must be nonnegative")
        # Serialize sync across processes; enqueue remains available during HTTP I/O.
        with FileLock(str(self.path) + ".sync.lock", timeout=0):

            async def refresh() -> PreferenceSnapshot:
                snapshot = await read_preference_snapshot(
                    client, identity=self._binding.identity, page_size=page_size
                )
                await _disk_call(self._accept_snapshot, snapshot)
                return snapshot

            snapshot = await refresh()
            delivered = False
            for _ in range(max_operations):
                pending = await _disk_call(self._next)
                if pending is None:
                    break
                sequence, operation = pending
                response = await client.post(
                    _PATH + "/operations", json=operation.model_dump(mode="json")
                )
                response.raise_for_status()
                result = PreferenceResult.model_validate_json(response.content)
                await _disk_call(self._acknowledge, sequence, operation, result)
                delivered = True
            return await refresh() if delivered else snapshot
