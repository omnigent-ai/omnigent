"""Transactional reservations for children that do not yet exist in the AP store.

This store does not authorize requests, materialize conversations, or start work.
Its caller must validate the parent scope, agent, attachments, and account before
using the verified identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_authority_scope, require_active_account
from omnigent.db.db_models import (
    SqlAgent,
    SqlConversationMetadata,
    SqlFile,
    SqlPolicy,
    SqlSessionPermission,
    SqlSessionSpawn,
    SqlSessionSpawnEpoch,
    SqlSessionSpawnPayload,
    current_workspace_id,
    uuid_to_bytes,
    workspace_scope,
)
from omnigent.db.enum_codecs import encode_policy_scope, encode_policy_type
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.file import StoredFile
from omnigent.stores.artifact_store import ArtifactStore

MAX_FILES = 100
MAX_CREATE_BYTES = 64 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_TERMINAL_RECEIPTS_PER_PARENT = 1024
MAX_PENDING_PER_PARENT = 64
# Retries never renew this lifetime; admission wins only before expiry claims the row.
PENDING_SPAWN_LIFETIME_SECONDS = 24 * 60 * 60

_logger = logging.getLogger(__name__)


class SpawnConflictError(ValueError):
    """The exact spawn attempt cannot make the requested transition."""


class SpawnNameConflictError(SpawnConflictError):
    """The title was rejected before this attempt reserved any child."""


class SpawnAuthorityError(ValueError):
    """The supplied identity no longer owns this reservation or parent binding."""


class SpawnEpochExpiredError(SpawnConflictError):
    """An unknown attempt in a retired epoch must never be recreated."""


class SpawnState(IntEnum):
    PENDING = 1
    ADMITTED = 2
    ABORTING = 3
    ABORTED = 4


class SpawnPhase(IntEnum):
    PREPARED = 1
    MATERIALIZE = 2
    ACTIVATE = 3
    DISPATCH = 6
    READY = 4
    CLEANUP = 5


@dataclass(frozen=True)
class SpawnIdentity:
    runner_id: str
    parent_id: str
    owner_id: str
    account_generation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_id", _required_uuid(self.parent_id))
        for value, limit in ((self.runner_id, 64), (self.owner_id, 128)):
            if not 0 < len(value) <= limit:
                raise ValueError("Spawn identity exceeds its field limit")
        if self.account_generation is not None and not 0 < len(self.account_generation) <= 32:
            raise ValueError("Invalid account generation")


@dataclass(frozen=True)
class SpawnReceipt:
    nonce: str
    epoch: int
    child_id: str
    identity: SpawnIdentity
    state: SpawnState
    phase: SpawnPhase
    request_digest: bytes = field(repr=False)
    file_mapping: dict[str, str]
    cleanup_blob_keys: tuple[str, ...]
    create_payload: dict[str, Any] | None = field(repr=False)
    input_payload: dict[str, Any] | None = field(repr=False)


@dataclass(frozen=True)
class SpawnActivationCheck:
    runner_id: str
    host_id: str | None
    workspace: str | None
    files: dict[str, bytes]
    owner_level: int | None
    cost_budget: dict[str, Any] | None = None


@dataclass(frozen=True)
class SpawnWorkRef:
    workspace_id: int
    child_id: str
    identity: SpawnIdentity
    nonce: str
    epoch: int


def spawn_file_fingerprint(file: StoredFile | SqlFile) -> bytes:
    metadata = file.source_metadata
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    value = (file.filename, file.bytes, file.content_type, file.blob_key or file.id, metadata)
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).digest()


def _required_uuid(value: str) -> str:
    if not value:
        raise ValueError("A spawn identifier must be nonempty")
    return uuid_to_bytes(value).hex()


def _payload(value: dict[str, Any], limit: int) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > limit:
        raise ValueError("Spawn payload exceeds its byte limit")
    return encoded


class SessionSpawnStore:
    """Store nonce-bound admission and cleanup using the metadata database.

    Payload codecs are mandatory so callers retain the conversation store's
    opaque-content transform. Encoding happens before database locks are taken.
    """

    def __init__(
        self,
        storage_location: str,
        *,
        encode_payload: Callable[[str], str],
        decode_payload: Callable[[str], str],
        check_current_scope: Callable[[Session, str, str], bool],
        artifact_store: ArtifactStore,
        check_current_access: Callable[[Session, SpawnIdentity], bool] | None = None,
    ) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_spawn_store"
        )
        self._write = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_spawn_store", immediate=True
        )
        self._encode = encode_payload
        self._decode = decode_payload
        self._check_current_scope = check_current_scope
        self._artifacts = artifact_store
        self._check_current_access = check_current_access

    def _encode_bounded(self, raw: str) -> bytes:
        encoded = self._encode(json.dumps({"version": 1, "data": json.loads(raw)})).encode()
        if len(encoded) > 4 * MAX_INPUT_BYTES:
            raise ValueError("Encoded spawn payload exceeds its byte limit")
        return encoded

    @staticmethod
    def _artifact_key(payload_id: str) -> str:
        return f"session-spawns/{current_workspace_id()}/{payload_id}"

    @staticmethod
    def _retire_payload(session: Session, payload_id: str | None) -> None:
        if payload_id is None:
            return
        payload = session.get(SqlSessionSpawnPayload, (current_workspace_id(), payload_id))
        if payload is None:
            raise SpawnConflictError("Spawn payload has no ownership record")
        if payload.cleanup_after is None:
            payload.cleanup_after = now_epoch()

    def _stage_payload(
        self,
        session: Session,
        child_id: str,
        raw: str,
        encoded: bytes,
        previous: str | None = None,
    ) -> str:
        self._retire_payload(session, previous)
        payload_id = uuid4().hex
        session.add(
            SqlSessionSpawnPayload(
                id=payload_id,
                child_id=child_id,
                payload_digest=hashlib.sha256(raw.encode()).digest(),
                encoded_digest=hashlib.sha256(encoded).digest(),
                cleanup_after=None,
                cleanup_attempts=0,
            )
        )
        return payload_id

    def _decode_payload(self, payload_id: str, *, pending: bool) -> dict[str, Any] | None:
        with self._session("read_child_spawn_payload_reference") as session:
            payload = session.get(SqlSessionSpawnPayload, (current_workspace_id(), payload_id))
        if payload is None or payload.cleanup_after is not None:
            raise SpawnConflictError("Spawn payload has no active ownership record")
        try:
            encoded = self._artifacts.get(self._artifact_key(payload_id))
        except KeyError as exc:
            if pending:
                return None
            raise SpawnConflictError("Admitted spawn payload is unavailable") from exc
        if (
            len(encoded) > 4 * MAX_INPUT_BYTES
            or hashlib.sha256(encoded).digest() != payload.encoded_digest
        ):
            raise SpawnConflictError("Stored spawn payload integrity check failed")
        envelope = json.loads(self._decode(encoded.decode()))
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("version")) is not int
            or envelope.get("version") != 1
            or not isinstance(envelope.get("data"), dict)
        ):
            raise SpawnConflictError("Unsupported stored spawn payload version")
        raw = _payload(envelope["data"], MAX_INPUT_BYTES)
        if hashlib.sha256(raw.encode()).digest() != payload.payload_digest:
            raise SpawnConflictError("Decoded spawn payload integrity check failed")
        return envelope["data"]

    def _receipt(self, row: SqlSessionSpawn) -> SpawnReceipt:
        return SpawnReceipt(
            nonce=row.nonce,
            epoch=row.epoch,
            child_id=row.child_id,
            identity=SpawnIdentity(
                row.creator_runner_id, row.parent_id, row.owner_id, row.account_generation
            ),
            state=SpawnState(row.state),
            phase=SpawnPhase(row.phase),
            request_digest=row.request_digest,
            file_mapping=json.loads(row.file_mapping) if row.file_mapping else {},
            cleanup_blob_keys=tuple(json.loads(row.cleanup_blob_keys or "[]")),
            create_payload=self._decode_payload(
                row.create_payload, pending=row.state == SpawnState.PENDING
            )
            if row.create_payload is not None
            else None,
            input_payload=self._decode_payload(
                row.input_payload, pending=row.state == SpawnState.PENDING
            )
            if row.input_payload is not None
            else None,
        )

    def _parent(self, session: Session, identity: SpawnIdentity) -> None:
        require_active_account(session, identity.owner_id, generation=identity.account_generation)
        parent = session.scalar(
            select(SqlConversationMetadata)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == identity.parent_id,
            )
            .with_for_update()
        )
        # The injected canonical lookup runs at the mutation boundary, including
        # inherited bindings and creator grants; no cached authorization is used.
        if parent is None or not self._check_current_scope(
            session, identity.runner_id, identity.parent_id
        ):
            raise SpawnAuthorityError("The parent is outside the current runner scope")
        if self._check_current_access is not None and not self._check_current_access(
            session, identity
        ):
            raise SpawnAuthorityError("The creator no longer has edit access to the parent")

    @staticmethod
    def _epoch(session: Session, parent_id: str) -> SqlSessionSpawnEpoch:
        row = session.get(SqlSessionSpawnEpoch, (current_workspace_id(), parent_id))
        if row is None:
            row = SqlSessionSpawnEpoch(parent_id=parent_id, epoch=1)
            session.add(row)
        return row

    @staticmethod
    def _retire_terminal_receipts(session: Session, epoch: SqlSessionSpawnEpoch) -> bool:
        terminal = (SqlSessionSpawn.state == SpawnState.ABORTED) | (
            (SqlSessionSpawn.state == SpawnState.ADMITTED)
            & (SqlSessionSpawn.phase == SpawnPhase.READY)
        )
        bounded = list(
            session.scalars(
                select(SqlSessionSpawn.child_id)
                .where(
                    SqlSessionSpawn.workspace_id == current_workspace_id(),
                    SqlSessionSpawn.parent_id == epoch.parent_id,
                    terminal,
                )
                .limit(MAX_TERMINAL_RECEIPTS_PER_PARENT)
            )
        )
        if len(bounded) < MAX_TERMINAL_RECEIPTS_PER_PARENT:
            return False
        session.execute(
            delete(SqlSessionSpawn).where(
                SqlSessionSpawn.workspace_id == current_workspace_id(),
                SqlSessionSpawn.child_id.in_(bounded),
                terminal,
            )
        )
        epoch.epoch += 1
        return True

    def current_epoch(self, identity: SpawnIdentity) -> int:
        """Get a fresh preparation epoch, retiring a bounded terminal batch if full."""

        def write(session: Session) -> int:
            self._parent(session, identity)
            epoch = self._epoch(session, identity.parent_id)
            self._retire_terminal_receipts(session, epoch)
            return epoch.epoch

        return run_write_transaction(self._write, "refresh_child_spawn_epoch", write)

    @staticmethod
    def _find(
        session: Session, identity: SpawnIdentity, epoch: int, nonce: str
    ) -> SqlSessionSpawn | None:
        row = session.scalar(
            select(SqlSessionSpawn)
            .where(
                SqlSessionSpawn.workspace_id == current_workspace_id(),
                SqlSessionSpawn.creator_runner_id == identity.runner_id,
                SqlSessionSpawn.epoch == epoch,
                SqlSessionSpawn.nonce == nonce,
            )
            .with_for_update()
        )
        if row is not None and (
            row.parent_id != identity.parent_id
            or row.owner_id != identity.owner_id
            or row.account_generation != identity.account_generation
        ):
            raise SpawnAuthorityError("Spawn identity does not match the reservation")
        return row

    def reserve(
        self,
        identity: SpawnIdentity,
        nonce: str,
        *,
        epoch: int,
        title: str,
        create_payload: dict[str, Any],
        source_file_ids: tuple[str, ...] = (),
        max_file_bytes: int,
        source_fingerprints: dict[str, bytes] | None = None,
    ) -> SpawnReceipt:
        """Reserve an ID and stage parent-owned file references in one transaction.

        Name uniqueness covers pending reservations, not ordinary AP writers.
        Materialization must still reconcile an ordinary child-name collision.
        """
        nonce = _required_uuid(nonce)
        source_ids = tuple(_required_uuid(value) for value in source_file_ids)
        if not 0 < len(title) <= 768 or max_file_bytes < 0 or not 1 <= epoch < 2**63:
            raise ValueError("Invalid spawn title or file limit")
        if len(source_ids) > MAX_FILES or len(set(source_ids)) != len(source_ids):
            raise ValueError("Invalid spawn file count or duplicate file IDs")
        raw = _payload(
            {"create": create_payload, "title": title, "files": source_ids}, MAX_CREATE_BYTES
        )
        request_digest = hashlib.sha256(raw.encode()).digest()
        create_raw = _payload(create_payload, MAX_CREATE_BYTES)
        encoded = self._encode_bounded(create_raw)
        child_id = uuid4().hex
        mapping = {source: uuid5(UUID(child_id), source).hex for source in source_ids}
        name_digest = hashlib.sha256(f"{identity.parent_id}\0{title}".encode()).digest()

        def write(session: Session) -> tuple[SqlSessionSpawn | None, str | None]:
            self._parent(session, identity)
            existing = self._find(session, identity, epoch, nonce)
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise SpawnConflictError("Spawn nonce already belongs to another request")
                if existing.state == SpawnState.PENDING:
                    existing.create_payload = self._stage_payload(
                        session, existing.child_id, create_raw, encoded, existing.create_payload
                    )
                    return existing, existing.create_payload
                return existing, None
            current = self._epoch(session, identity.parent_id)
            if epoch != current.epoch:
                raise SpawnEpochExpiredError("Unknown spawn attempt has an expired epoch")
            if self._retire_terminal_receipts(session, current):
                return None, None
            active = list(
                session.scalars(
                    select(SqlSessionSpawn.child_id)
                    .where(
                        SqlSessionSpawn.workspace_id == current_workspace_id(),
                        SqlSessionSpawn.parent_id == identity.parent_id,
                        (SqlSessionSpawn.state.in_((SpawnState.PENDING, SpawnState.ABORTING)))
                        | (
                            (SqlSessionSpawn.state == SpawnState.ADMITTED)
                            & (SqlSessionSpawn.phase != SpawnPhase.READY)
                        ),
                    )
                    .limit(MAX_PENDING_PER_PARENT)
                )
            )
            if len(active) >= MAX_PENDING_PER_PARENT:
                raise SpawnConflictError("Parent active spawn limit reached")
            if (
                session.scalar(
                    select(SqlSessionSpawn.nonce).where(
                        SqlSessionSpawn.workspace_id == current_workspace_id(),
                        SqlSessionSpawn.name_digest == name_digest,
                    )
                )
                is not None
            ):
                raise SpawnNameConflictError("Child name is already reserved")
            sources = list(
                session.scalars(
                    select(SqlFile)
                    .where(
                        SqlFile.workspace_id == current_workspace_id(),
                        SqlFile.session_id == identity.parent_id,
                        SqlFile.id.in_(source_ids),
                    )
                    .order_by(SqlFile.id)
                    .with_for_update()
                )
            )
            if (
                len(sources) != len(source_ids)
                or sum(source.bytes for source in sources) > max_file_bytes
            ):
                raise SpawnConflictError("Source files are missing or exceed the byte limit")
            if (
                source_fingerprints is not None
                and {source.id: spawn_file_fingerprint(source) for source in sources}
                != source_fingerprints
            ):
                raise SpawnConflictError("Source attachments changed before reservation")
            now = now_epoch()
            row = SqlSessionSpawn(
                creator_runner_id=identity.runner_id,
                epoch=epoch,
                nonce=nonce,
                child_id=child_id,
                parent_id=identity.parent_id,
                owner_id=identity.owner_id,
                account_generation=identity.account_generation,
                title=title,
                request_digest=request_digest,
                name_digest=name_digest,
                state=SpawnState.PENDING,
                phase=SpawnPhase.PREPARED,
                create_payload=self._stage_payload(session, child_id, create_raw, encoded),
                file_mapping=json.dumps(mapping).encode(),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            for source in sources:
                session.add(
                    SqlFile(
                        id=mapping[source.id],
                        created_at=now,
                        filename=source.filename,
                        bytes=source.bytes,
                        content_type=source.content_type,
                        session_id=child_id,
                        blob_key=source.blob_key or source.id,
                        source_metadata=source.source_metadata,
                    )
                )
            return row, row.create_payload

        try:
            row, payload_id = run_write_transaction(self._write, "reserve_child_spawn", write)
        except IntegrityError as exc:
            raise SpawnConflictError("Spawn identity or child name is already reserved") from exc
        if row is None:
            raise SpawnEpochExpiredError(
                "Spawn epoch retired before preparation; reconcile the attempt"
            )
        if payload_id is not None:
            self._artifacts.put(self._artifact_key(payload_id), encoded)
            return self.get(identity, nonce, epoch=epoch) or self._receipt(row)
        return self._receipt(row)

    def get(self, identity: SpawnIdentity, nonce: str, *, epoch: int) -> SpawnReceipt | None:
        nonce = _required_uuid(nonce)

        def read(session: Session) -> SqlSessionSpawn | None:
            self._parent(session, identity)
            return self._find(session, identity, epoch, nonce)

        row = run_write_transaction(self._write, "reconcile_child_spawn", read)
        return self._receipt(row) if row is not None else None

    def admit(
        self, identity: SpawnIdentity, nonce: str, input_payload: dict[str, Any], *, epoch: int
    ) -> SpawnReceipt:
        """Persist first input and admission atomically, before any AP/runtime work."""
        nonce = _required_uuid(nonce)
        raw = _payload(input_payload, MAX_INPUT_BYTES)
        digest = hashlib.sha256(raw.encode()).digest()
        encoded = self._encode_bounded(raw)

        def stage(session: Session) -> tuple[SqlSessionSpawn, str | None]:
            self._parent(session, identity)
            row = self._find(session, identity, epoch, nonce)
            if row is None or row.state in (SpawnState.ABORTING, SpawnState.ABORTED):
                raise SpawnConflictError("Spawn is unknown or already aborted")
            if row.state == SpawnState.ADMITTED:
                if row.input_digest != digest:
                    raise SpawnConflictError("Spawn already admitted a different first input")
                return row, None
            if row.input_digest is not None and row.input_digest != digest:
                raise SpawnConflictError("Spawn already staged a different first input")
            row.input_payload = self._stage_payload(
                session, row.child_id, raw, encoded, row.input_payload
            )
            row.input_digest = digest
            row.updated_at = now_epoch()
            return row, row.input_payload

        staged, payload_id = run_write_transaction(self._write, "stage_child_spawn_input", stage)
        if payload_id is None:
            return self._receipt(staged)
        if staged.create_payload is None:
            raise SpawnConflictError("Spawn creation payload is unavailable")
        self._decode_payload(staged.create_payload, pending=False)
        self._artifacts.put(self._artifact_key(payload_id), encoded)

        def admit(session: Session) -> SqlSessionSpawn:
            self._parent(session, identity)
            row = self._find(session, identity, epoch, nonce)
            if row is None or row.state in (SpawnState.ABORTING, SpawnState.ABORTED):
                raise SpawnConflictError("Spawn is unknown or already aborted")
            if row.input_digest != digest:
                raise SpawnConflictError("Spawn already admitted a different first input")
            if row.state == SpawnState.PENDING:
                if row.input_payload != payload_id or row.create_payload != staged.create_payload:
                    raise SpawnConflictError("Spawn preparation changed before admission")
                row.state = SpawnState.ADMITTED
                row.phase = SpawnPhase.MATERIALIZE
                row.updated_at = now_epoch()
            return row

        return self._receipt(run_write_transaction(self._write, "admit_child_spawn_input", admit))

    def abort(self, identity: SpawnIdentity, nonce: str, *, epoch: int) -> SpawnReceipt | None:
        """Claim pending cleanup; admitted/unknown attempts never delete anything."""
        nonce = _required_uuid(nonce)

        def write(session: Session) -> SqlSessionSpawn | None:
            self._parent(session, identity)
            row = self._find(session, identity, epoch, nonce)
            if row is None or row.state != SpawnState.PENDING:
                return row
            return self._claim_pending_abort(session, row)

        row = run_write_transaction(self._write, "claim_child_spawn_abort", write)
        return self._receipt(row) if row is not None else None

    def _claim_pending_abort(self, session: Session, row: SqlSessionSpawn) -> SqlSessionSpawn:
        """Release only a locked pending reservation's staged files and payload claims."""
        if row.state != SpawnState.PENDING:
            raise SpawnConflictError("Only pending reservations can be aborted")
        if (
            session.get(SqlConversationMetadata, (current_workspace_id(), row.child_id))
            is not None
        ):
            raise SpawnConflictError("Pending reservation already has session metadata")
        mapping = json.loads(row.file_mapping or "{}")
        files = list(
            session.scalars(
                select(SqlFile)
                .where(
                    SqlFile.workspace_id == current_workspace_id(),
                    SqlFile.session_id == row.child_id,
                )
                .limit(MAX_FILES + 1)
                .with_for_update()
            )
        )
        if {file.id for file in files} != set(mapping.values()):
            raise SpawnConflictError("Pending child has unexpected file state")
        blob_keys = {file.blob_key or file.id for file in files}
        session.execute(
            delete(SqlFile).where(
                SqlFile.workspace_id == current_workspace_id(),
                SqlFile.session_id == row.child_id,
                SqlFile.id.in_(mapping.values()),
            )
        )
        orphaned = []
        for key in sorted(blob_keys):
            blob_reference = (
                select(SqlFile.id)
                .where(
                    SqlFile.workspace_id == current_workspace_id(),
                    SqlFile.blob_key == key,
                )
                .limit(1)
                .exists()
            )
            legacy_reference = (
                select(SqlFile.id)
                .where(
                    SqlFile.workspace_id == current_workspace_id(),
                    SqlFile.id == key,
                    SqlFile.blob_key.is_(None),
                )
                .limit(1)
                .exists()
            )
            # Both reference forms must be observed in the same statement snapshot.
            if not session.scalar(select(or_(blob_reference, legacy_reference))):
                orphaned.append(key)
        row.state = SpawnState.ABORTING if orphaned else SpawnState.ABORTED
        row.phase = SpawnPhase.CLEANUP
        row.cleanup_blob_keys = json.dumps(orphaned).encode()
        self._retire_payload(session, row.create_payload)
        self._retire_payload(session, row.input_payload)
        row.create_payload = None
        row.input_payload = None
        row.file_mapping = None
        row.name_digest = None
        row.updated_at = now_epoch()
        return row

    def _expire_pending(self, child_id: str, cutoff: int) -> bool:
        """Trusted recovery only: expiry does not depend on the former owner's access."""

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlSessionSpawn)
                .where(
                    SqlSessionSpawn.workspace_id == current_workspace_id(),
                    SqlSessionSpawn.child_id == _required_uuid(child_id),
                )
                .with_for_update()
            )
            if row is None or row.state != SpawnState.PENDING or row.created_at > cutoff:
                return False
            self._claim_pending_abort(session, row)
            return True

        return run_write_transaction(self._write, "expire_pending_child_spawn", write)

    def expire_pending_page(
        self, *, after: tuple[int, int, str] | None = None, limit: int = 100
    ) -> tuple[tuple[int, int, str] | None, list[tuple[int, str, Exception]]]:
        """Expire one indexed global page after a fixed lifetime, without reading payloads."""
        if not 1 <= limit <= 100:
            raise ValueError("Spawn expiry page size must be between 1 and 100")
        cutoff = now_epoch() - PENDING_SPAWN_LIFETIME_SECONDS
        statement = select(
            SqlSessionSpawn.created_at, SqlSessionSpawn.workspace_id, SqlSessionSpawn.child_id
        ).where(SqlSessionSpawn.state == SpawnState.PENDING, SqlSessionSpawn.created_at <= cutoff)
        if after is not None:
            created_at, workspace_id, child_id = after
            statement = statement.where(
                (SqlSessionSpawn.created_at > created_at)
                | (
                    (SqlSessionSpawn.created_at == created_at)
                    & (SqlSessionSpawn.workspace_id > workspace_id)
                )
                | (
                    (SqlSessionSpawn.created_at == created_at)
                    & (SqlSessionSpawn.workspace_id == workspace_id)
                    & (SqlSessionSpawn.child_id > _required_uuid(child_id))
                )
            )
        with self._session("list_expired_child_spawns") as session:
            rows = session.execute(
                statement.order_by(
                    SqlSessionSpawn.created_at,
                    SqlSessionSpawn.workspace_id,
                    SqlSessionSpawn.child_id,
                ).limit(limit)
            ).all()
        failures = []
        for row in rows:
            with workspace_scope(row.workspace_id), account_authority_scope(None, None):
                try:
                    self._expire_pending(row.child_id, cutoff)
                except Exception as exc:
                    _logger.exception(
                        "Pending child expiry failed", extra={"session_id": row.child_id}
                    )
                    failures.append((row.workspace_id, row.child_id, exc))
        cursor = (
            (rows[-1].created_at, rows[-1].workspace_id, rows[-1].child_id)
            if len(rows) == limit
            else None
        )
        return cursor, failures

    def list_work(
        self, state: SpawnState, phase: SpawnPhase, *, after: str | None = None, limit: int = 100
    ) -> list[SpawnReceipt]:
        """Read one bounded outbox page; only trusted coordination code calls this."""
        if not 1 <= limit <= 100:
            raise ValueError("Spawn outbox page size must be between 1 and 100")
        statement = select(SqlSessionSpawn).where(
            SqlSessionSpawn.workspace_id == current_workspace_id(),
            SqlSessionSpawn.state == state,
            SqlSessionSpawn.phase == phase,
        )
        if after is not None:
            statement = statement.where(SqlSessionSpawn.child_id > _required_uuid(after))
        with self._session("list_child_spawn_work") as session:
            rows = list(session.scalars(statement.order_by(SqlSessionSpawn.child_id).limit(limit)))
        return [self._receipt(row) for row in rows]

    def list_global_work(
        self,
        state: SpawnState,
        phase: SpawnPhase,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 100,
    ) -> list[SpawnWorkRef]:
        """Read trusted recovery references without decoding another tenant's payloads."""
        if not 1 <= limit <= 100:
            raise ValueError("Spawn outbox page size must be between 1 and 100")
        statement = select(
            SqlSessionSpawn.workspace_id,
            SqlSessionSpawn.child_id,
            SqlSessionSpawn.creator_runner_id,
            SqlSessionSpawn.parent_id,
            SqlSessionSpawn.owner_id,
            SqlSessionSpawn.account_generation,
            SqlSessionSpawn.nonce,
            SqlSessionSpawn.epoch,
        ).where(SqlSessionSpawn.state == state, SqlSessionSpawn.phase == phase)
        if after is not None:
            workspace_id, child_id = after
            statement = statement.where(
                (SqlSessionSpawn.workspace_id > workspace_id)
                | (
                    (SqlSessionSpawn.workspace_id == workspace_id)
                    & (SqlSessionSpawn.child_id > _required_uuid(child_id))
                )
            )
        with self._session("list_global_child_spawn_work") as session:
            rows = session.execute(
                statement.order_by(SqlSessionSpawn.workspace_id, SqlSessionSpawn.child_id).limit(
                    limit
                )
            ).all()
        return [
            SpawnWorkRef(
                row.workspace_id,
                row.child_id,
                SpawnIdentity(
                    row.creator_runner_id, row.parent_id, row.owner_id, row.account_generation
                ),
                row.nonce,
                row.epoch,
            )
            for row in rows
        ]

    def get_claimed_cleanup(self, child_id: str) -> SpawnReceipt | None:
        """Restore already-authorized cleanup even after its parent authority is revoked."""
        with self._session("read_claimed_child_spawn_cleanup") as session:
            row = session.scalar(
                select(SqlSessionSpawn).where(
                    SqlSessionSpawn.workspace_id == current_workspace_id(),
                    SqlSessionSpawn.child_id == _required_uuid(child_id),
                    SqlSessionSpawn.state.in_((SpawnState.ABORTING, SpawnState.ABORTED)),
                )
            )
        return self._receipt(row) if row is not None else None

    def finish_abort(self, child_id: str, cleaned_blob_keys: tuple[str, ...]) -> None:
        """Acknowledge only artifact keys handled by the trusted cleanup worker."""
        child_id = _required_uuid(child_id)
        cleaned = {_required_uuid(key) for key in cleaned_blob_keys}

        def write(session: Session) -> None:
            row = session.scalar(
                select(SqlSessionSpawn)
                .where(
                    SqlSessionSpawn.workspace_id == current_workspace_id(),
                    SqlSessionSpawn.child_id == child_id,
                )
                .with_for_update()
            )
            if row is None or row.state not in (SpawnState.ABORTING, SpawnState.ABORTED):
                raise SpawnConflictError("Spawn has no claimed cleanup")
            if row.state == SpawnState.ABORTED:
                return
            remaining = set(json.loads(row.cleanup_blob_keys or "[]")) - cleaned
            row.cleanup_blob_keys = json.dumps(sorted(remaining)).encode()
            if not remaining:
                row.state = SpawnState.ABORTED
            row.updated_at = now_epoch()

        run_write_transaction(self._write, "finish_child_spawn_abort", write)

    def retire_deleted_materialization(self, receipt: SpawnReceipt | SpawnWorkRef) -> bool:
        """Release only receipt resources after coordination verifies permanent AP deletion."""

        def write(session: Session) -> bool:
            row = self._find(session, receipt.identity, receipt.epoch, receipt.nonce)
            if row is None or row.state != SpawnState.ADMITTED or row.phase == SpawnPhase.READY:
                return False
            if row.child_id != receipt.child_id:
                raise SpawnConflictError("Deleted materialization does not match the attempt")
            row.phase = SpawnPhase.READY
            row.name_digest = None
            self._retire_payload(session, row.create_payload)
            self._retire_payload(session, row.input_payload)
            row.create_payload = None
            row.input_payload = None
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._write, "retire_deleted_child_spawn", write)

    def advance_materialization(
        self,
        receipt: SpawnReceipt,
        expected: SpawnPhase,
        *,
        activation_check: SpawnActivationCheck | None = None,
    ) -> bool:
        """Advance under the same account and parent locks as admission."""
        next_phase = {
            SpawnPhase.MATERIALIZE: SpawnPhase.ACTIVATE,
            SpawnPhase.ACTIVATE: SpawnPhase.DISPATCH,
            SpawnPhase.DISPATCH: SpawnPhase.READY,
        }
        if expected not in next_phase:
            raise ValueError("Invalid materialization phase")
        if expected == SpawnPhase.ACTIVATE and activation_check is None:
            raise SpawnConflictError("Activation requires explicit validated child state")

        def write(session: Session) -> bool:
            self._parent(session, receipt.identity)
            row = self._find(session, receipt.identity, receipt.epoch, receipt.nonce)
            if row is not None and (
                row.child_id != receipt.child_id or row.request_digest != receipt.request_digest
            ):
                raise SpawnConflictError("Materialization receipt does not match the attempt")
            if row is None or row.state != SpawnState.ADMITTED or row.phase != expected:
                return False
            if expected == SpawnPhase.ACTIVATE:
                assert activation_check is not None
                payload = receipt.create_payload or {}
                agent_id = payload.get("arguments", {}).get("agent_id")
                if agent_id is not None:
                    agent = session.get(
                        SqlAgent, (current_workspace_id(), agent_id), with_for_update=True
                    )
                    if agent is None or (
                        agent.version != payload.get("agent_version")
                        or agent.bundle_location != payload.get("agent_bundle_location")
                    ):
                        raise SpawnConflictError("Child agent revision changed during activation")
                child = session.get(
                    SqlConversationMetadata,
                    (current_workspace_id(), receipt.child_id),
                    with_for_update=True,
                )
                if child is None or (child.runner_id, child.host_id, child.workspace) != (
                    activation_check.runner_id,
                    activation_check.host_id,
                    activation_check.workspace,
                ):
                    raise SpawnConflictError("Child placement changed during activation")
                files = session.scalars(
                    select(SqlFile)
                    .where(
                        SqlFile.workspace_id == current_workspace_id(),
                        SqlFile.session_id == receipt.child_id,
                    )
                    .order_by(SqlFile.created_at, SqlFile.id)
                    .limit(MAX_FILES + 1)
                    .with_for_update()
                )
                if {
                    file.id: spawn_file_fingerprint(file) for file in files
                } != activation_check.files:
                    raise SpawnConflictError("Child attachments changed during activation")
                if activation_check.owner_level is not None:
                    owner = session.get(
                        SqlSessionPermission,
                        (current_workspace_id(), receipt.identity.owner_id, receipt.child_id),
                        with_for_update=True,
                    )
                    if owner is None or owner.level < activation_check.owner_level:
                        raise SpawnAuthorityError("Child owner grant changed during activation")
                if activation_check.cost_budget is not None:
                    policy_id = uuid5(UUID(receipt.child_id), "spawn-cost-budget").hex
                    policy = session.get(SqlPolicy, (current_workspace_id(), policy_id))
                    handler = "omnigent.policies.builtins.cost.subagent_cost_budget"
                    if policy is None:
                        session.add(
                            SqlPolicy(
                                id=policy_id,
                                session_id=receipt.child_id,
                                name="__subagent_cost_budget",
                                created_at=now_epoch(),
                                type=encode_policy_type("python"),
                                scope=encode_policy_scope("session"),
                                handler=handler,
                                factory_params=json.dumps(activation_check.cost_budget),
                                enabled=True,
                            )
                        )
                    elif (
                        policy.session_id != receipt.child_id
                        or policy.name != "__subagent_cost_budget"
                        or policy.handler != handler
                        or policy.type != encode_policy_type("python")
                        or policy.scope != encode_policy_scope("session")
                        or not policy.enabled
                        or json.loads(policy.factory_params or "null")
                        != activation_check.cost_budget
                    ):
                        raise SpawnConflictError("Child cost policy changed before activation")
            row.phase = next_phase[expected]
            row.updated_at = now_epoch()
            if row.phase == SpawnPhase.READY:
                row.name_digest = None
                self._retire_payload(session, row.create_payload)
                self._retire_payload(session, row.input_payload)
                row.create_payload = None
                row.input_payload = None
            return True

        return run_write_transaction(self._write, "advance_child_spawn_materialization", write)

    def cleanup_payloads(
        self, *, limit: int = 100, all_workspaces: bool = False
    ) -> list[tuple[str, Exception]]:
        """Reap due exclusive artifacts, retaining fences against delayed uploaders."""
        if not 1 <= limit <= 100:
            raise ValueError("Spawn payload cleanup page size must be between 1 and 100")
        now = now_epoch()
        statement = select(SqlSessionSpawnPayload.workspace_id, SqlSessionSpawnPayload.id).where(
            SqlSessionSpawnPayload.cleanup_after <= now
        )
        if not all_workspaces:
            statement = statement.where(
                SqlSessionSpawnPayload.workspace_id == current_workspace_id()
            )
        with self._session("list_due_child_spawn_payload_cleanup") as session:
            payloads = session.execute(
                statement.order_by(
                    SqlSessionSpawnPayload.cleanup_after,
                    SqlSessionSpawnPayload.workspace_id,
                    SqlSessionSpawnPayload.id,
                ).limit(limit)
            ).all()
        failures = []
        for workspace_id, payload_id in payloads:
            with workspace_scope(workspace_id):

                def claim(session: Session, claimed_id: str = payload_id) -> bool:
                    payload = session.get(
                        SqlSessionSpawnPayload,
                        (current_workspace_id(), claimed_id),
                        with_for_update=True,
                    )
                    if (
                        payload is None
                        or payload.cleanup_after is None
                        or payload.cleanup_after > now
                    ):
                        return False
                    payload.cleanup_attempts = min(payload.cleanup_attempts + 1, 12)
                    delay = min(60 * 2 ** (payload.cleanup_attempts - 1), 86400)
                    payload.cleanup_after = now + delay
                    return True

                if not run_write_transaction(
                    self._write, "schedule_child_spawn_payload_cleanup", claim
                ):
                    continue
                try:
                    self._artifacts.delete(self._artifact_key(payload_id))
                except Exception as exc:
                    _logger.exception(
                        "Spawn payload cleanup failed", extra={"payload_id": payload_id}
                    )
                    failures.append((payload_id, exc))
        # A put begun before retirement may complete later; deleting the record
        # would lose the only durable cleanup owner for that late artifact.
        return failures
