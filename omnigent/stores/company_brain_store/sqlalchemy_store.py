from __future__ import annotations

import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import asc, delete, desc, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlBrainInstallation,
    SqlIntegrationConnection,
    SqlIntegrationSelection,
    SqlIntegrationSyncRun,
    SqlOAuthStateNonce,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities import (
    BrainInstallation,
    IntegrationConnection,
    IntegrationSelection,
    IntegrationSyncRun,
)
from omnigent.server.scheduled.rrule import validate_rrule
from omnigent.stores.company_brain_store import UNSET, CompanyBrainStore

_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+|access[_-]?token[=:]\s*|refresh[_-]?token[=:]\s*|client[_-]?secret[=:]\s*)\S+"
)
_SYNC_RUN_STALE_SECONDS = 3600


def _redact_error(value: str | None) -> str | None:
    if value is None:
        return None
    return _SECRET_VALUE.sub(r"\1[redacted]", value).strip()[:500]


def _validate_schedule(rrule: str | None, timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("schedule timezone is not a recognized IANA timezone") from exc
    if rrule is not None:
        validate_rrule(rrule)


def _installation(row: SqlBrainInstallation) -> BrainInstallation:
    return BrainInstallation(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        repo_path=row.repo_path,
        repo_url=row.repo_url,
        gbrain_state_path=row.gbrain_state_path,
        mcp_url=row.mcp_url,
        mcp_auth_ref=row.mcp_auth_ref,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _connection(row: SqlIntegrationConnection) -> IntegrationConnection:
    return IntegrationConnection(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        provider=row.provider,
        credential_ciphertext=row.credential_ciphertext,
        account_label=row.account_label,
        granted_scopes=tuple(json.loads(row.granted_scopes_json)),
        status=row.status,
        last_error=row.last_error,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _selection(row: SqlIntegrationSelection) -> IntegrationSelection:
    return IntegrationSelection(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        connection_id=row.connection_id,
        external_resource_id=row.external_resource_id,
        resource_name=row.resource_name,
        resource_type=row.resource_type,
        source_url=row.source_url,
        transform_profile=row.transform_profile,
        visibility_class=row.visibility_class,
        rrule=row.rrule,
        timezone=row.timezone,
        state=row.state,
        last_synced_at=row.last_synced_at,
        page_count=row.page_count,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _sync_run(row: SqlIntegrationSyncRun) -> IntegrationSyncRun:
    return IntegrationSyncRun(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        connection_id=row.connection_id,
        selection_id=row.selection_id,
        status=row.status,
        trigger=row.trigger,
        fetched_count=row.fetched_count,
        changed_count=row.changed_count,
        deleted_count=row.deleted_count,
        skipped_count=row.skipped_count,
        commit_sha=row.commit_sha,
        gbrain_result=json.loads(row.gbrain_result_json) if row.gbrain_result_json else None,
        error=row.error,
        scheduled_at=row.scheduled_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


class SqlAlchemyCompanyBrainStore(CompanyBrainStore):
    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.company_brain_store",
        )

    def upsert_installation(
        self,
        installation_id: str,
        *,
        repo_path: str,
        repo_url: str | None,
        gbrain_state_path: str,
        mcp_url: str | None,
        mcp_auth_ref: str | None,
        status: str = "ready",
    ) -> BrainInstallation:
        with self._session("upsert_installation") as session:
            row = session.execute(
                select(SqlBrainInstallation).where(
                    SqlBrainInstallation.workspace_id == current_workspace_id()
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlBrainInstallation(
                    id=installation_id,
                    repo_path=repo_path,
                    repo_url=repo_url,
                    gbrain_state_path=gbrain_state_path,
                    mcp_url=mcp_url,
                    mcp_auth_ref=mcp_auth_ref,
                    status=status,
                    created_at=now_epoch(),
                    updated_at=None,
                )
                session.add(row)
            else:
                row.repo_path = repo_path
                row.repo_url = repo_url
                row.gbrain_state_path = gbrain_state_path
                row.mcp_url = mcp_url
                row.mcp_auth_ref = mcp_auth_ref
                row.status = status
                row.updated_at = now_epoch()
            session.flush()
            return _installation(row)

    def get_installation(self) -> BrainInstallation | None:
        with self._session("get_installation") as session:
            row = session.execute(
                select(SqlBrainInstallation).where(
                    SqlBrainInstallation.workspace_id == current_workspace_id()
                )
            ).scalar_one_or_none()
            return _installation(row) if row else None

    def claim_oauth_nonce(self, nonce_sha256: str, *, expires_at: int) -> bool:
        now = now_epoch()
        if expires_at <= now:
            return False
        try:
            with self._session("claim_oauth_nonce") as session:
                session.execute(
                    delete(SqlOAuthStateNonce).where(
                        SqlOAuthStateNonce.workspace_id == current_workspace_id(),
                        SqlOAuthStateNonce.expires_at <= now,
                    )
                )
                session.add(
                    SqlOAuthStateNonce(
                        nonce_sha256=nonce_sha256,
                        expires_at=expires_at,
                        created_at=now,
                    )
                )
                session.flush()
            return True
        except IntegrityError:
            return False

    def create_connection(
        self,
        connection_id: str,
        *,
        provider: str,
        credential_ciphertext: str,
        account_label: str | None,
        granted_scopes: tuple[str, ...],
        created_by: str | None,
    ) -> IntegrationConnection:
        row = SqlIntegrationConnection(
            id=connection_id,
            provider=provider,
            credential_ciphertext=credential_ciphertext,
            account_label=account_label,
            granted_scopes_json=json.dumps(sorted(set(granted_scopes))),
            status="connected",
            last_error=None,
            created_by=created_by,
            created_at=now_epoch(),
            updated_at=None,
        )
        with self._session("create_connection") as session:
            session.add(row)
            session.flush()
            return _connection(row)

    def get_connection(self, connection_id: str) -> IntegrationConnection | None:
        with self._session("get_connection") as session:
            row = session.get(
                SqlIntegrationConnection,
                (current_workspace_id(), connection_id),
            )
            return _connection(row) if row else None

    def list_connections(self) -> list[IntegrationConnection]:
        with self._session("list_connections") as session:
            rows = session.execute(
                select(SqlIntegrationConnection)
                .where(SqlIntegrationConnection.workspace_id == current_workspace_id())
                .order_by(
                    asc(SqlIntegrationConnection.created_at),
                    asc(SqlIntegrationConnection.id),
                )
            ).scalars()
            return [_connection(row) for row in rows]

    def update_connection_credentials(
        self,
        connection_id: str,
        *,
        credential_ciphertext: str,
        granted_scopes: tuple[str, ...],
        account_label: str | None,
    ) -> IntegrationConnection | None:
        with self._session("update_connection_credentials") as session:
            row = session.get(
                SqlIntegrationConnection,
                (current_workspace_id(), connection_id),
            )
            if row is None:
                return None
            row.credential_ciphertext = credential_ciphertext
            row.granted_scopes_json = json.dumps(sorted(set(granted_scopes)))
            row.account_label = account_label
            row.status = "connected"
            row.last_error = None
            row.updated_at = now_epoch()
            session.flush()
            return _connection(row)

    def disconnect_connection(self, connection_id: str) -> IntegrationConnection | None:
        with self._session("disconnect_connection") as session:
            row = session.get(
                SqlIntegrationConnection,
                (current_workspace_id(), connection_id),
            )
            if row is None:
                return None
            row.credential_ciphertext = None
            row.status = "disconnected"
            row.updated_at = now_epoch()
            session.flush()
            return _connection(row)

    def create_selection(
        self,
        selection_id: str,
        *,
        connection_id: str,
        external_resource_id: str,
        resource_name: str,
        resource_type: str,
        source_url: str | None,
        transform_profile: str,
        rrule: str | None,
        timezone: str,
    ) -> IntegrationSelection:
        _validate_schedule(rrule, timezone)
        row = SqlIntegrationSelection(
            id=selection_id,
            connection_id=connection_id,
            external_resource_id=external_resource_id,
            resource_name=resource_name,
            resource_type=resource_type,
            source_url=source_url,
            transform_profile=transform_profile,
            visibility_class="org-shared",
            rrule=rrule,
            timezone=timezone,
            state="active",
            last_synced_at=None,
            page_count=0,
            last_error=None,
            created_at=now_epoch(),
            updated_at=None,
        )
        with self._session("create_selection") as session:
            session.add(row)
            session.flush()
            return _selection(row)

    def get_selection(self, selection_id: str) -> IntegrationSelection | None:
        with self._session("get_selection") as session:
            row = session.get(
                SqlIntegrationSelection,
                (current_workspace_id(), selection_id),
            )
            return _selection(row) if row else None

    def list_selections(self, connection_id: str | None = None) -> list[IntegrationSelection]:
        with self._session("list_selections") as session:
            stmt = select(SqlIntegrationSelection).where(
                SqlIntegrationSelection.workspace_id == current_workspace_id()
            )
            if connection_id is not None:
                stmt = stmt.where(SqlIntegrationSelection.connection_id == connection_id)
            rows = session.execute(
                stmt.order_by(
                    asc(SqlIntegrationSelection.created_at),
                    asc(SqlIntegrationSelection.id),
                )
            ).scalars()
            return [_selection(row) for row in rows]

    def list_scheduled_selections_all_workspaces(self) -> list[IntegrationSelection]:
        with self._session("list_scheduled_selections_all_workspaces") as session:
            rows = session.execute(
                select(SqlIntegrationSelection)
                .where(
                    SqlIntegrationSelection.state == "active",
                    SqlIntegrationSelection.rrule.is_not(None),
                )
                .order_by(
                    asc(SqlIntegrationSelection.workspace_id),
                    asc(SqlIntegrationSelection.created_at),
                    asc(SqlIntegrationSelection.id),
                )
            ).scalars()
            return [_selection(row) for row in rows]

    def update_selection(
        self,
        selection_id: str,
        *,
        state: str | None = None,
        rrule: str | None | Any = UNSET,
        timezone: str | None = None,
        last_synced_at: int | None = None,
        page_count: int | None = None,
        last_error: str | None = None,
    ) -> IntegrationSelection | None:
        if isinstance(rrule, str):
            validate_rrule(rrule)
        if timezone is not None:
            _validate_schedule(None, timezone)
        with self._session("update_selection") as session:
            row = session.get(
                SqlIntegrationSelection,
                (current_workspace_id(), selection_id),
            )
            if row is None:
                return None
            if state is not None:
                row.state = state
            if rrule is not UNSET:
                row.rrule = rrule
            if timezone is not None:
                row.timezone = timezone
            if last_synced_at is not None:
                row.last_synced_at = last_synced_at
            if page_count is not None:
                row.page_count = page_count
            row.last_error = _redact_error(last_error)
            row.updated_at = now_epoch()
            session.flush()
            return _selection(row)

    def create_sync_run(
        self,
        run_id: str,
        *,
        connection_id: str,
        selection_id: str,
        trigger: str,
        scheduled_at: int | None = None,
    ) -> IntegrationSyncRun:
        created_at = now_epoch()
        row = SqlIntegrationSyncRun(
            id=run_id,
            connection_id=connection_id,
            selection_id=selection_id,
            status="pending",
            trigger=trigger,
            fetched_count=0,
            changed_count=0,
            deleted_count=0,
            skipped_count=0,
            commit_sha=None,
            gbrain_result_json=None,
            error=None,
            scheduled_at=scheduled_at,
            started_at=None,
            finished_at=None,
            created_at=created_at,
        )
        with self._session("create_sync_run") as session:
            session.execute(
                update(SqlIntegrationSyncRun)
                .where(
                    SqlIntegrationSyncRun.workspace_id == current_workspace_id(),
                    SqlIntegrationSyncRun.selection_id == selection_id,
                    SqlIntegrationSyncRun.status == "running",
                    SqlIntegrationSyncRun.started_at.is_not(None),
                    SqlIntegrationSyncRun.started_at <= created_at - _SYNC_RUN_STALE_SECONDS,
                )
                .values(
                    status="failed",
                    error="Sync worker lease expired",
                    finished_at=created_at,
                )
            )
            session.add(row)
            session.flush()
            return _sync_run(row)

    def claim_sync_run(self, run_id: str) -> IntegrationSyncRun | None:
        with self._session("claim_sync_run") as session:
            claimed_at = now_epoch()
            result = session.execute(
                update(SqlIntegrationSyncRun)
                .where(
                    SqlIntegrationSyncRun.workspace_id == current_workspace_id(),
                    SqlIntegrationSyncRun.id == run_id,
                    SqlIntegrationSyncRun.status == "pending",
                )
                .values(status="running", started_at=claimed_at)
            )
            if getattr(result, "rowcount", 0) != 1:
                return None
            row = session.get(
                SqlIntegrationSyncRun,
                (current_workspace_id(), run_id),
            )
            return _sync_run(row) if row else None

    def finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        fetched_count: int,
        changed_count: int,
        deleted_count: int,
        skipped_count: int,
        commit_sha: str | None,
        gbrain_result: dict[str, object] | None,
        error: str | None,
    ) -> IntegrationSyncRun | None:
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("sync run terminal status is invalid")
        with self._session("finish_sync_run") as session:
            row = session.get(
                SqlIntegrationSyncRun,
                (current_workspace_id(), run_id),
            )
            if row is None or row.status != "running":
                return None
            row.status = status
            row.fetched_count = fetched_count
            row.changed_count = changed_count
            row.deleted_count = deleted_count
            row.skipped_count = skipped_count
            row.commit_sha = commit_sha
            row.gbrain_result_json = (
                json.dumps(gbrain_result, ensure_ascii=False, sort_keys=True)
                if gbrain_result is not None
                else None
            )
            row.error = _redact_error(error)
            row.finished_at = now_epoch()
            session.flush()
            return _sync_run(row)

    def list_sync_runs(
        self,
        *,
        selection_id: str | None = None,
        limit: int = 100,
    ) -> list[IntegrationSyncRun]:
        with self._session("list_sync_runs") as session:
            stmt = select(SqlIntegrationSyncRun).where(
                SqlIntegrationSyncRun.workspace_id == current_workspace_id()
            )
            if selection_id is not None:
                stmt = stmt.where(SqlIntegrationSyncRun.selection_id == selection_id)
            rows = session.execute(
                stmt.order_by(
                    desc(SqlIntegrationSyncRun.created_at),
                    desc(SqlIntegrationSyncRun.id),
                ).limit(limit)
            ).scalars()
            return [_sync_run(row) for row in rows]
