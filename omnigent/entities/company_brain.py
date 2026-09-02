from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BrainInstallation:
    id: str
    workspace_id: int
    repo_path: str
    repo_url: str | None
    gbrain_state_path: str
    mcp_url: str | None
    mcp_auth_ref: str | None
    status: str
    created_at: int
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class IntegrationConnection:
    id: str
    workspace_id: int
    provider: str
    credential_ciphertext: str | None = field(repr=False)
    account_label: str | None
    granted_scopes: tuple[str, ...]
    status: str
    last_error: str | None
    created_by: str | None
    created_at: int
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class IntegrationSelection:
    id: str
    workspace_id: int
    connection_id: str
    external_resource_id: str
    resource_name: str
    resource_type: str
    source_url: str | None
    transform_profile: str
    visibility_class: str
    rrule: str | None
    timezone: str
    state: str
    last_synced_at: int | None
    page_count: int
    last_error: str | None
    created_at: int
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class IntegrationSyncRun:
    id: str
    workspace_id: int
    connection_id: str
    selection_id: str
    status: str
    trigger: str
    fetched_count: int
    changed_count: int
    deleted_count: int
    skipped_count: int
    commit_sha: str | None
    gbrain_result: dict[str, object] | None
    error: str | None
    scheduled_at: int | None
    started_at: int | None
    finished_at: int | None
    created_at: int
