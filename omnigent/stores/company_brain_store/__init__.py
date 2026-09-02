from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import (
    BrainInstallation,
    IntegrationConnection,
    IntegrationSelection,
    IntegrationSyncRun,
)

UNSET: Any = object()


class CompanyBrainStore(ABC):
    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
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
    ) -> BrainInstallation: ...

    @abstractmethod
    def get_installation(self) -> BrainInstallation | None: ...

    @abstractmethod
    def claim_oauth_nonce(self, nonce_sha256: str, *, expires_at: int) -> bool: ...

    @abstractmethod
    def create_connection(
        self,
        connection_id: str,
        *,
        provider: str,
        credential_ciphertext: str,
        account_label: str | None,
        granted_scopes: tuple[str, ...],
        created_by: str | None,
    ) -> IntegrationConnection: ...

    @abstractmethod
    def get_connection(self, connection_id: str) -> IntegrationConnection | None: ...

    @abstractmethod
    def list_connections(self) -> list[IntegrationConnection]: ...

    @abstractmethod
    def update_connection_credentials(
        self,
        connection_id: str,
        *,
        credential_ciphertext: str,
        granted_scopes: tuple[str, ...],
        account_label: str | None,
    ) -> IntegrationConnection | None: ...

    @abstractmethod
    def disconnect_connection(self, connection_id: str) -> IntegrationConnection | None: ...

    @abstractmethod
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
    ) -> IntegrationSelection: ...

    @abstractmethod
    def get_selection(self, selection_id: str) -> IntegrationSelection | None: ...

    @abstractmethod
    def list_selections(self, connection_id: str | None = None) -> list[IntegrationSelection]: ...

    @abstractmethod
    def list_scheduled_selections_all_workspaces(self) -> list[IntegrationSelection]: ...

    @abstractmethod
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
    ) -> IntegrationSelection | None: ...

    @abstractmethod
    def create_sync_run(
        self,
        run_id: str,
        *,
        connection_id: str,
        selection_id: str,
        trigger: str,
        scheduled_at: int | None = None,
    ) -> IntegrationSyncRun: ...

    @abstractmethod
    def claim_sync_run(self, run_id: str) -> IntegrationSyncRun | None: ...

    @abstractmethod
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
    ) -> IntegrationSyncRun | None: ...

    @abstractmethod
    def list_sync_runs(
        self,
        *,
        selection_id: str | None = None,
        limit: int = 100,
    ) -> list[IntegrationSyncRun]: ...
