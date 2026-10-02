"""Account-locked device registrations and conditional outbox operations."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import (
    current_account_user,
    lock_account,
    require_active_account,
)
from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlMobilePushDevice,
    SqlMobilePushOutbox,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.enum_codecs import encode_session_live_status
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.mobile_push_content import Platform, PushKind


@dataclass(frozen=True)
class Device:
    installation_id: str
    user_id: str
    platform: Platform
    token: str = field(repr=False)
    generation: str
    account_generation: str | None


@dataclass(frozen=True)
class Delivery:
    id: str
    session_id: str
    user_id: str
    installation_id: str
    device_generation: str
    kind: PushKind
    reason: str | None
    lease: str
    attempts: int


@dataclass(frozen=True)
class PreparedDelivery:
    device: Device
    title: str


def _device(row: SqlMobilePushDevice) -> Device:
    return Device(
        row.installation_id,
        row.user_id,
        cast(Platform, row.platform),
        row.fcm_token,
        row.generation,
        row.account_generation,
    )


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


class MobilePushStore:
    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._workspace_cursor: int | None = None
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.mobile_push_store"
        )
        self._writer = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.mobile_push_store", immediate=True
        )

    def _devices(
        self, session: Session, installation_id: str, token_hash: str
    ) -> list[SqlMobilePushDevice]:
        return list(
            session.scalars(
                select(SqlMobilePushDevice)
                .where(
                    SqlMobilePushDevice.workspace_id == current_workspace_id(),
                    or_(
                        SqlMobilePushDevice.installation_id == installation_id,
                        SqlMobilePushDevice.token_hash == token_hash,
                    ),
                )
                .execution_options(populate_existing=True)
            )
        )

    def register(
        self,
        installation_id: str,
        *,
        user_id: str,
        platform: Platform,
        fcm_token: str,
        now: int | None = None,
    ) -> Device:
        token_hash = hashlib.sha256(fcm_token.encode()).hexdigest()
        stamp = _now(now)

        def write(session: Session) -> Device:
            rows = self._devices(session, installation_id, token_hash)
            locked_users = {row.user_id for row in rows} | {user_id}
            generation = require_active_account(
                session,
                user_id,
                related_accounts={
                    row.user_id: account.account_generation
                    if (account := session.get(SqlUser, (current_workspace_id(), row.user_id)))
                    else None
                    for row in rows
                },
            )
            rows = self._devices(session, installation_id, token_hash)
            if any(row.user_id not in locked_users for row in rows):
                raise OmnigentError("Device registration changed; retry", code=ErrorCode.CONFLICT)
            for row in rows:
                if (
                    row.installation_id == installation_id
                    and row.user_id != user_id
                    and row.fcm_token != fcm_token
                ):
                    raise OmnigentError(
                        "Installation belongs to another user", code=ErrorCode.CONFLICT
                    )
            for row in rows:
                session.execute(
                    delete(SqlMobilePushOutbox).where(
                        SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                        SqlMobilePushOutbox.installation_id == row.installation_id,
                    )
                )
                session.delete(row)
            session.execute(
                delete(SqlMobilePushDevice).where(
                    SqlMobilePushDevice.workspace_id == current_workspace_id(),
                    or_(
                        SqlMobilePushDevice.installation_id == installation_id,
                        SqlMobilePushDevice.token_hash == token_hash,
                    ),
                )
            )
            row = SqlMobilePushDevice(
                installation_id=installation_id,
                user_id=user_id,
                platform=platform,
                fcm_token=fcm_token,
                token_hash=token_hash,
                generation=uuid4().hex,
                account_generation=generation,
                expires_at=stamp + 30 * 86400,
            )
            session.add(row)
            return _device(row)

        try:
            return run_write_transaction(self._writer, "register_authenticated_device", write)
        except SQLAlchemyError:
            raise OmnigentError(
                "Device registration could not be saved", code=ErrorCode.INTERNAL_ERROR
            ) from None

    def delete_device(self, installation_id: str, user_id: str) -> bool:
        def write(session: Session) -> bool:
            require_active_account(session, user_id)
            row = session.get(SqlMobilePushDevice, (current_workspace_id(), installation_id))
            if row is None or row.user_id != user_id:
                return False
            session.execute(
                delete(SqlMobilePushOutbox).where(
                    SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                    SqlMobilePushOutbox.installation_id == installation_id,
                    SqlMobilePushOutbox.user_id == user_id,
                )
            )
            session.delete(row)
            return True

        return run_write_transaction(self._writer, "unregister_owned_device", write)

    def devices_for_user(self, user_id: str, *, now: int | None = None) -> list[Device]:
        with self._session("list_live_user_devices") as session:
            return [
                _device(row)
                for row in session.scalars(
                    select(SqlMobilePushDevice).where(
                        SqlMobilePushDevice.workspace_id == current_workspace_id(),
                        SqlMobilePushDevice.user_id == user_id,
                        SqlMobilePushDevice.expires_at > _now(now),
                    )
                )
            ]

    def _root(self, session: Session, session_id: str) -> str | None:
        conversation = session.get(SqlConversation, (current_workspace_id(), session_id))
        return conversation.root_conversation_id if conversation else None

    def _recipient_ids(self, session: Session, session_id: str) -> list[str]:
        return list(
            session.scalars(
                select(SqlSessionPermission.user_id)
                .outerjoin(
                    SqlUser,
                    (SqlUser.workspace_id == SqlSessionPermission.workspace_id)
                    & (SqlUser.id == SqlSessionPermission.user_id),
                )
                .where(
                    SqlSessionPermission.workspace_id == current_workspace_id(),
                    SqlSessionPermission.conversation_id == session_id,
                    SqlSessionPermission.level >= 1,
                    SqlSessionPermission.user_id.not_in(("__public__", "__all__", "local")),
                    SqlUser.deleted_at.is_(None),
                )
            )
        )

    def enqueue(
        self, session_id: str, kind: PushKind, reason: str | None = None, *, now: int | None = None
    ) -> None:
        stamp = _now(now)

        def write(session: Session) -> None:
            target_id = self._root(session, session_id) if kind == "needs_input" else session_id
            if target_id is None:
                return
            recipients = self._recipient_ids(session, target_id)
            devices = list(
                session.scalars(
                    select(SqlMobilePushDevice).where(
                        SqlMobilePushDevice.workspace_id == current_workspace_id(),
                        SqlMobilePushDevice.user_id.in_(recipients),
                        SqlMobilePushDevice.expires_at > stamp,
                    )
                )
            )
            users = {device.user_id for device in devices}
            if actor := current_account_user():
                users.add(actor)
            accounts = {user: lock_account(session, user) for user in sorted(users)}
            require_active_account(session, None)
            for device in devices:
                account = accounts[device.user_id]
                if account is not None and account.deleted_at is not None:
                    continue
                if device.account_generation is not None and (
                    account is None or account.account_generation != device.account_generation
                ):
                    continue
                identity = (
                    SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                    SqlMobilePushOutbox.session_id == target_id,
                    SqlMobilePushOutbox.user_id == device.user_id,
                    SqlMobilePushOutbox.installation_id == device.installation_id,
                    SqlMobilePushOutbox.device_generation == device.generation,
                    SqlMobilePushOutbox.kind == kind,
                )
                session.execute(
                    delete(SqlMobilePushOutbox).where(
                        *identity, SqlMobilePushOutbox.expires_at <= stamp
                    )
                )
                existing = session.scalar(select(SqlMobilePushOutbox.id).where(*identity))
                if existing is None:
                    session.add(
                        SqlMobilePushOutbox(
                            id=uuid4().hex,
                            session_id=target_id,
                            user_id=device.user_id,
                            installation_id=device.installation_id,
                            device_generation=device.generation,
                            kind=kind,
                            reason=reason,
                            not_before=stamp + (0 if kind == "needs_input" else 10),
                            expires_at=stamp + 3600,
                            lease_until=0,
                            attempts=0,
                            delivered=False,
                        )
                    )

        run_write_transaction(self._writer, "queue_notification_intents", write)

    def cancel(
        self, session_id: str, *, prompts_only: bool = False, terminal_only: bool = False
    ) -> None:
        def write(session: Session) -> None:
            root_id = self._root(session, session_id)
            rows = list(
                session.scalars(
                    select(SqlMobilePushOutbox).where(
                        SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                        or_(
                            SqlMobilePushOutbox.session_id == session_id,
                            (SqlMobilePushOutbox.session_id == root_id)
                            & (SqlMobilePushOutbox.kind == "needs_input"),
                        ),
                    )
                )
            )
            users = sorted({row.user_id for row in rows})
            for user in users:
                require_active_account(session, user)
            for row in rows:
                if terminal_only and row.kind == "needs_input":
                    continue
                if not prompts_only or (
                    row.kind == "needs_input" and not self._has_prompts(session, row.session_id)
                ):
                    session.delete(row)

        run_write_transaction(self._writer, "cancel_obsolete_notification_intents", write)

    def pending_workspaces(self, *, now: int | None = None, limit: int = 32) -> list[int]:
        stamp = _now(now)
        pending = []
        visited = set()
        with self._session("discover_outbox_tenants") as session:
            next_workspace = (
                select(SqlMobilePushOutbox.workspace_id)
                .where(SqlMobilePushOutbox.delivered.is_(False))
                .order_by(SqlMobilePushOutbox.workspace_id)
                .limit(1)
            )
            for _ in range(limit):
                cursor = self._workspace_cursor
                workspace = session.scalar(
                    next_workspace.where(SqlMobilePushOutbox.workspace_id > cursor)
                    if cursor is not None
                    else next_workspace
                )
                if workspace is None and cursor is not None:
                    self._workspace_cursor = None
                    workspace = session.scalar(next_workspace)
                if workspace is None or workspace in visited:
                    break
                visited.add(workspace)
                self._workspace_cursor = workspace
                earliest = session.scalar(
                    select(SqlMobilePushOutbox.not_before)
                    .where(
                        SqlMobilePushOutbox.workspace_id == workspace,
                        SqlMobilePushOutbox.delivered.is_(False),
                    )
                    .order_by(SqlMobilePushOutbox.not_before)
                    .limit(1)
                )
                if earliest is not None and earliest <= stamp:
                    pending.append(workspace)
        return pending

    def purge_expired(self, *, now: int | None = None, limit: int = 50) -> int:
        stamp = _now(now)

        def write(session: Session) -> int:
            devices = list(
                session.scalars(
                    select(SqlMobilePushDevice)
                    .where(SqlMobilePushDevice.expires_at <= stamp)
                    .order_by(
                        SqlMobilePushDevice.expires_at,
                        SqlMobilePushDevice.workspace_id,
                        SqlMobilePushDevice.installation_id,
                    )
                    .limit(limit)
                )
            )
            expired = list(
                session.scalars(
                    select(SqlMobilePushOutbox)
                    .where(SqlMobilePushOutbox.expires_at <= stamp)
                    .order_by(
                        SqlMobilePushOutbox.expires_at,
                        SqlMobilePushOutbox.workspace_id,
                        SqlMobilePushOutbox.id,
                    )
                    .limit(limit)
                )
            )
            owners = {(row.workspace_id, row.user_id) for row in expired + devices}
            for workspace, user in sorted(owners):
                with workspace_scope(workspace):
                    lock_account(session, user)
            live_expired_devices = []
            for candidate in devices:
                device = session.scalar(
                    select(SqlMobilePushDevice)
                    .where(
                        SqlMobilePushDevice.workspace_id == candidate.workspace_id,
                        SqlMobilePushDevice.installation_id == candidate.installation_id,
                        SqlMobilePushDevice.generation == candidate.generation,
                        SqlMobilePushDevice.expires_at <= stamp,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if device is not None:
                    live_expired_devices.append(device)
            devices = live_expired_devices
            dependent = (
                list(
                    session.scalars(
                        select(SqlMobilePushOutbox)
                        .where(
                            tuple_(
                                SqlMobilePushOutbox.workspace_id,
                                SqlMobilePushOutbox.installation_id,
                            ).in_(
                                [
                                    (device.workspace_id, device.installation_id)
                                    for device in devices
                                ]
                            )
                        )
                        .limit(limit)
                    )
                )
                if devices
                else []
            )
            rows = {(row.workspace_id, row.id): row for row in expired + dependent}
            for row in rows.values():
                session.delete(row)
            removed = len(rows)
            for device in devices:
                remaining = session.scalar(
                    select(SqlMobilePushOutbox.id)
                    .where(
                        SqlMobilePushOutbox.workspace_id == device.workspace_id,
                        SqlMobilePushOutbox.installation_id == device.installation_id,
                    )
                    .limit(1)
                )
                if remaining is None:
                    session.delete(device)
                    removed += 1
            return removed

        return run_write_transaction(self._writer, "purge_expired_push_data", write)

    def claim(self, *, now: int | None = None, limit: int = 50) -> list[Delivery]:
        stamp = _now(now)

        def write(session: Session) -> list[Delivery]:
            rows = list(
                session.scalars(
                    select(SqlMobilePushOutbox)
                    .where(
                        SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                        SqlMobilePushOutbox.delivered.is_(False),
                        SqlMobilePushOutbox.not_before <= stamp,
                    )
                    .order_by(SqlMobilePushOutbox.not_before)
                    .limit(limit)
                )
            )
            for user in sorted({row.user_id for row in rows}):
                lock_account(session, user)
            claimed = []
            for row in rows:
                if row.expires_at <= stamp or row.attempts >= 5:
                    session.execute(
                        update(SqlMobilePushOutbox)
                        .where(
                            SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                            SqlMobilePushOutbox.id == row.id,
                            SqlMobilePushOutbox.delivered.is_(False),
                            or_(
                                SqlMobilePushOutbox.expires_at <= stamp,
                                SqlMobilePushOutbox.attempts >= 5,
                            ),
                        )
                        .values(delivered=True)
                        .execution_options(synchronize_session=False)
                    )
                    continue
                lease = uuid4().hex
                result = cast(
                    CursorResult,
                    session.execute(
                        update(SqlMobilePushOutbox)
                        .where(
                            SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                            SqlMobilePushOutbox.id == row.id,
                            SqlMobilePushOutbox.lease_until <= stamp,
                            SqlMobilePushOutbox.delivered.is_(False),
                            SqlMobilePushOutbox.attempts == row.attempts,
                        )
                        .values(
                            lease=lease,
                            lease_until=stamp + 30,
                            not_before=stamp + 30,
                            attempts=row.attempts + 1,
                        )
                        .execution_options(synchronize_session=False)
                    ),
                )
                if result.rowcount:
                    claimed.append(
                        Delivery(
                            row.id,
                            row.session_id,
                            row.user_id,
                            row.installation_id,
                            row.device_generation,
                            cast(PushKind, row.kind),
                            row.reason,
                            lease,
                            row.attempts + 1,
                        )
                    )
            return claimed

        return run_write_transaction(self._writer, "lease_due_notification_intents", write)

    def _has_prompts(self, session: Session, root_id: str) -> bool:
        return (
            session.scalar(
                select(SqlConversationMetadata.id)
                .join(
                    SqlConversation,
                    (SqlConversation.workspace_id == SqlConversationMetadata.workspace_id)
                    & (SqlConversation.id == SqlConversationMetadata.id),
                )
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversation.root_conversation_id == root_id,
                    SqlConversationMetadata.pending_elicitation_count > 0,
                )
                .limit(1)
            )
            is not None
        )

    def prepare(self, delivery: Delivery, *, now: int | None = None) -> PreparedDelivery | None:
        with self._session("revalidate_notification_authority") as session:
            row = session.get(SqlMobilePushOutbox, (current_workspace_id(), delivery.id))
            device = session.get(
                SqlMobilePushDevice, (current_workspace_id(), delivery.installation_id)
            )
            conversation = session.get(
                SqlConversation, (current_workspace_id(), delivery.session_id)
            )
            if (
                row is None
                or row.lease != delivery.lease
                or row.lease_until <= _now(now)
                or row.delivered
                or row.expires_at <= _now(now)
                or device is None
                or device.generation != delivery.device_generation
                or device.expires_at <= _now(now)
                or device.user_id != delivery.user_id
                or conversation is None
                or delivery.user_id not in self._recipient_ids(session, delivery.session_id)
            ):
                return None
            account = session.get(SqlUser, (current_workspace_id(), delivery.user_id))
            if device.account_generation is not None and (
                account is None or account.account_generation != device.account_generation
            ):
                return None
            if delivery.kind == "needs_input":
                if not self._has_prompts(session, delivery.session_id):
                    return None
            else:
                metadata = session.get(
                    SqlConversationMetadata, (current_workspace_id(), delivery.session_id)
                )
                expected = "idle" if delivery.kind == "completed" else "failed"
                if metadata is None or metadata.live_status != encode_session_live_status(
                    expected
                ):
                    return None
            return PreparedDelivery(_device(device), conversation.title)

    def acknowledge(
        self,
        delivery: Delivery,
        outcome: Literal["sent", "prune", "retry", "discard"],
        *,
        retry_after: int = 0,
        now: int | None = None,
    ) -> bool:
        stamp = _now(now)

        def write(session: Session) -> bool:
            lock_account(session, delivery.user_id)
            row = session.scalar(
                select(SqlMobilePushOutbox)
                .where(
                    SqlMobilePushOutbox.workspace_id == current_workspace_id(),
                    SqlMobilePushOutbox.id == delivery.id,
                    SqlMobilePushOutbox.lease == delivery.lease,
                )
                .with_for_update()
            )
            if row is None or row.delivered or row.lease_until <= stamp:
                return False
            if outcome == "prune":
                session.execute(
                    delete(SqlMobilePushDevice).where(
                        SqlMobilePushDevice.workspace_id == current_workspace_id(),
                        SqlMobilePushDevice.installation_id == delivery.installation_id,
                        SqlMobilePushDevice.user_id == delivery.user_id,
                        SqlMobilePushDevice.generation == delivery.device_generation,
                    )
                )
                row.delivered = True
            elif outcome == "retry" and row.attempts < 5:
                row.not_before = stamp + max(min(retry_after, 3600), min(300, 2**row.attempts * 5))
                row.lease_until = 0
                row.lease = None
            else:
                row.delivered = True
            return True

        return run_write_transaction(self._writer, "acknowledge_leased_notification", write)
