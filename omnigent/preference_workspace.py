"""Explicit workspace edits, import previews and recovery above the durable client."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from pydantic import Field

from omnigent.preference_client import MAX_PENDING_OPERATIONS, PreferenceClient, _encode
from omnigent.preference_sync import (
    MAX_KEYS,
    PreferenceMutation,
    PreferenceNamespace,
    PreferenceResult,
    PreferenceSnapshot,
    ProtocolModel,
    SharedPreferenceEntry,
    SharedPreferenceKey,
    Value,
)
from omnigent.preferences import (
    PluginIdentity,
    PreferenceContext,
    PreferenceKey,
    PreferenceLayer,
    PreferenceScope,
    PreferenceValue,
    preferences_from_config,
)


class ImportChange(ProtocolModel):
    previous: SharedPreferenceEntry
    operation: PreferenceMutation


class ImportPreview(ProtocolModel):
    """Reviewed values and versions; applying never silently refreshes their bases."""

    identity: PreferenceNamespace
    changes: list[ImportChange] = Field(max_length=MAX_KEYS)


def shared_key(key: PreferenceKey) -> SharedPreferenceKey:
    name = key.name
    return SharedPreferenceKey(
        kind=key.kind,
        name={"ecosystem": name.ecosystem, "source": name.source, "name": name.name}
        if isinstance(name, PluginIdentity)
        else name,
    )


def preview_import(
    snapshot: PreferenceSnapshot,
    *,
    installation_id: str,
    values: Mapping[PreferenceKey, PreferenceValue | None],
) -> ImportPreview:
    """Preview a patch: absent keys inherit; only explicit None values clear."""
    current = {entry.key.as_key(): entry for entry in snapshot.entries}
    changes = []
    for key, value in values.items():
        encoded_key = shared_key(key)
        previous = current.get(key, SharedPreferenceEntry(key=encoded_key, version=0, value=None))
        wire_value = list(value) if isinstance(value, tuple) else value
        if previous.value == wire_value:
            continue
        changes.append(
            ImportChange(
                previous=previous,
                operation=PreferenceMutation(
                    protocol_version=1,
                    identity=snapshot.identity,
                    operation_id=uuid4(),
                    installation_id=installation_id,
                    key=encoded_key,
                    expected_version=previous.version,
                    value=wire_value,
                ),
            )
        )
    return ImportPreview(identity=snapshot.identity, changes=changes)


class PreferenceWorkspace:
    """User intent bound to one local cache; no implicit global-file migration."""

    def __init__(self, client: PreferenceClient) -> None:
        self.client = client
        self.identity = client._binding.identity
        self.installation_id = client._binding.installation_id

    @property
    def context(self) -> PreferenceContext:
        return PreferenceContext(self.identity.as_identity(), self.installation_id)

    def machine_layer(self) -> PreferenceLayer:
        with self.client._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT payload FROM machine ORDER BY key LIMIT ?", (MAX_KEYS + 1,)
            ).fetchall()
        if len(rows) > MAX_KEYS:
            raise ValueError("Machine preferences exceed the key limit")
        values = {}
        for row in rows:
            entry = SharedPreferenceEntry.model_validate_json(row[0])
            value = tuple(entry.value) if isinstance(entry.value, list) else entry.value
            if value is None:
                raise ValueError("Machine overrides cannot contain a tombstone")
            values[entry.key.as_key()] = value
        return PreferenceLayer(
            PreferenceScope.MACHINE,
            self.identity.as_identity(),
            values,
            installation_id=self.installation_id,
        )

    def set_machine(self, key: SharedPreferenceKey, value: Value) -> None:
        """None removes the override; False and [] remain explicit local choices."""
        validated = PreferenceMutation(
            protocol_version=1,
            identity=self.identity,
            installation_id=self.installation_id,
            operation_id=uuid4(),
            key=key,
            expected_version=0,
            value=value,
        )
        with self.client._transaction() as connection:
            if value is None:
                connection.execute("DELETE FROM machine WHERE key = ?", (key.storage_key(),))
                return
            exists = connection.execute(
                "SELECT key FROM machine WHERE key = ?", (key.storage_key(),)
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM (SELECT key FROM machine LIMIT ?)", (MAX_KEYS,)
            ).fetchone()[0]
            if exists is None and count >= MAX_KEYS:
                raise ValueError("Machine preference key limit reached")
            entry = SharedPreferenceEntry(
                key=key,
                version=0,
                value=validated.value,
                installation_id=self.installation_id,
            )
            connection.execute(
                "INSERT OR REPLACE INTO machine (key, payload) VALUES (?, ?)",
                (key.storage_key(), _encode(entry)),
            )

    def preview_config(self, config: Mapping[str, object]) -> ImportPreview:
        """Extract only portable YAML fields; commands and credentials stay local."""
        snapshot = self.client.cached()
        if snapshot is None:
            raise ValueError("Synchronize before previewing an import")
        return preview_import(
            snapshot,
            installation_id=self.installation_id,
            values=preferences_from_config(config),
        )

    def apply_import(self, preview: ImportPreview) -> list[UUID]:
        """Queue the reviewed patch and its restore record in one transaction.

        Imports require an empty outbox. The previous import's restore record is
        replaced only when a nonempty patch is queued. Shared writes remain per key.
        """
        if preview.identity != self.identity:
            raise ValueError("Import belongs to another identity")
        keys = set()
        operation_ids = set()
        for change in preview.changes:
            operation, previous = change.operation, change.previous
            if (
                operation.identity != self.identity
                or operation.installation_id != self.installation_id
                or operation.key != previous.key
                or operation.expected_version != previous.version
                or operation.resolves
                or operation.key.storage_key() in keys
                or operation.operation_id in operation_ids
            ):
                raise ValueError("Invalid import preview")
            keys.add(operation.key.storage_key())
            operation_ids.add(operation.operation_id)
            _encode(operation)
            _encode(previous)
        if not preview.changes:
            return []
        with self.client._transaction() as connection:
            if connection.execute("SELECT sequence FROM outbox LIMIT 1").fetchone():
                raise ValueError("Synchronize or discard pending edits before importing")
            revision, sequence = connection.execute(
                "SELECT revision, next_sequence FROM state WHERE id = 1"
            ).fetchone()
            if revision is None:
                raise ValueError("Synchronize before importing")
            if len(preview.changes) > MAX_PENDING_OPERATIONS:
                raise ValueError("Import exceeds the outbox limit")
            connection.execute(
                "DELETE FROM import_entries WHERE operation_id IN "
                "(SELECT operation_id FROM import_entries LIMIT ?)",
                (MAX_KEYS,),
            )
            for change in preview.changes:
                operation = change.operation
                connection.execute(
                    "INSERT INTO outbox (sequence, key, operation, predecessor, sent) "
                    "VALUES (?, ?, ?, NULL, 0)",
                    (sequence, operation.key.storage_key(), _encode(operation)),
                )
                connection.execute(
                    "INSERT INTO import_entries (operation_id, previous, result) "
                    "VALUES (?, ?, NULL)",
                    (operation.operation_id.bytes, _encode(change.previous)),
                )
                sequence += 1
            connection.execute("UPDATE state SET next_sequence = ? WHERE id = 1", (sequence,))
        return [change.operation.operation_id for change in preview.changes]

    def preview_restore(self) -> ImportPreview:
        """Undo only acknowledged imports, using their versions to expose later edits."""
        with self.client._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT previous, result FROM import_entries ORDER BY operation_id LIMIT ?",
                (MAX_KEYS + 1,),
            ).fetchall()
        if len(rows) > MAX_KEYS:
            raise ValueError("Import history exceeds the key limit")
        changes = []
        for previous_raw, result_raw in rows:
            if result_raw is None:
                raise ValueError(
                    "Import has unacknowledged or discarded edits; resolve them first"
                )
            previous = SharedPreferenceEntry.model_validate_json(previous_raw)
            result = PreferenceResult.model_validate_json(result_raw)
            if result.status != "applied":
                continue
            changes.append(
                ImportChange(
                    previous=result.entry,
                    operation=PreferenceMutation(
                        protocol_version=1,
                        identity=self.identity,
                        installation_id=self.installation_id,
                        operation_id=uuid4(),
                        key=previous.key,
                        expected_version=result.entry.version,
                        value=previous.value,
                    ),
                )
            )
        return ImportPreview(identity=self.identity, changes=changes)
