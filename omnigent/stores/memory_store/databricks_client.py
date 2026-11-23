"""Authenticated REST client for Databricks Managed Memory.

The client uses generated AgentKit methods with Databricks unified authentication.
Raw GET requests cover effective permissions and page-preserving listing, which
the generated surface does not expose. MemoryStore adaptation and user-to-actor
mapping live above this transport layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.agentkit import (
        ManagedMemoryEntry,
        ManagedMemoryEntrySearchResult,
        ManagedMemoryEntryView,
        ManagedMemoryStore,
    )


def _ensure_databricks_sdk() -> None:
    """Raise an actionable error when the optional SDK is unavailable."""
    try:
        import databricks.sdk  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "DatabricksManagedMemoryClient requires the 'databricks' extra. "
            "Install with: pip install 'omnigent[databricks]'"
        ) from exc


def _required_string(value: str, *, name: str) -> str:
    """Validate one required identifier."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _path_segment(value: str, *, name: str) -> str:
    """Validate and URL-encode one resource path segment."""
    return quote(_required_string(value, name=name), safe="")


def _without_none(values: dict[str, object | None]) -> dict[str, object]:
    """Drop optional query fields the caller did not supply."""
    return {key: value for key, value in values.items() if value is not None}


class DatabricksManagedMemoryClient:
    """Call one Managed Memory store with Databricks SDK authentication.

    By default, the SDK resolves credentials through its unified authentication
    chain. ``workspace_host``, ``profile``, and ``auth_type`` constrain that
    resolution without moving secrets into Omnigent configuration. A caller may
    instead inject a preconfigured ``WorkspaceClient``.

    :param managed_memory_store_id: Bare store identifier, without the
        ``memory-stores/`` resource prefix.
    :param workspace_host: Optional Databricks workspace URL.
    :param profile: Optional Databricks SDK profile.
    :param auth_type: Optional Databricks SDK authentication type, such as
        ``oauth-m2m`` or a supported workload identity strategy.
    :param workspace_client: Optional preconfigured SDK client. Authentication
        selectors cannot be combined with an injected client.
    """

    def __init__(
        self,
        managed_memory_store_id: str,
        *,
        workspace_host: str | None = None,
        profile: str | None = None,
        auth_type: str | None = None,
        workspace_client: WorkspaceClient | None = None,
    ) -> None:
        encoded_store_id = _path_segment(managed_memory_store_id, name="managed_memory_store_id")
        if workspace_client is not None and any((workspace_host, profile, auth_type)):
            raise ValueError(
                "workspace_client cannot be combined with workspace_host, profile, or auth_type"
            )

        if workspace_client is None:
            _ensure_databricks_sdk()
            from databricks.sdk import WorkspaceClient

            workspace_client = WorkspaceClient(
                host=workspace_host,
                profile=profile,
                auth_type=auth_type,
                product="omnigent",
            )

        self._workspace_client = workspace_client
        self._store_name = f"memory-stores/{encoded_store_id}"
        self._store_path = f"/api/2.0/agents/{self._store_name}"

    def get_store(self) -> ManagedMemoryStore:
        """Return configured store metadata and fail if it is unavailable."""
        return self._workspace_client.agent_kit.get_memory_store(self._store_name)

    def get_effective_permission(self) -> dict[str, object]:
        """Return this credential's effective permission on the store."""
        return self._get(f"{self._store_path}/permissions/self")

    def create_entry(
        self,
        entry: ManagedMemoryEntry,
        *,
        entry_id: str | None = None,
    ) -> ManagedMemoryEntry:
        """Create an entry, optionally using a caller-selected entry ID."""
        if entry_id is not None:
            _required_string(entry_id, name="entry_id")
        return self._workspace_client.agent_kit.create_memory(
            self._store_name,
            entry,
            managed_memory_entry_id=entry_id,
        )

    def get_entry(
        self, entry_id: str, *, view: ManagedMemoryEntryView | None = None
    ) -> ManagedMemoryEntry:
        """Read one entry by its ID."""
        return self._workspace_client.agent_kit.get_memory(
            self._entry_name(entry_id),
            view=view,
        )

    def list_entries(
        self,
        actor_id: str,
        *,
        path: str | None = None,
        path_prefix: str | None = None,
        session_id: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        view: ManagedMemoryEntryView | None = None,
    ) -> dict[str, object]:
        """List entries for one actor using canonical pagination fields."""
        # The generated iterator hides next_page_token and has no exact-path filter.
        return self._get(
            f"{self._store_path}/entries",
            query=_without_none(
                {
                    "actor_id": actor_id,
                    "path": path,
                    "path_prefix": path_prefix,
                    "session_id": session_id,
                    "page_size": page_size,
                    "page_token": page_token,
                    "view": view.value if view is not None else None,
                }
            ),
        )

    def search_entries(
        self,
        actor_id: str,
        query: str,
        *,
        path_prefix: str | None = None,
        session_id: str | None = None,
        page_size: int | None = None,
        view: ManagedMemoryEntryView | None = None,
    ) -> list[ManagedMemoryEntrySearchResult]:
        """Search one actor's entries and return ranked results."""
        return list(
            self._workspace_client.agent_kit.search_memories(
                self._store_name,
                actor_id,
                query,
                path_prefix=path_prefix,
                session_id=session_id,
                page_size=page_size,
                view=view,
            )
        )

    def update_entry(
        self,
        entry_id: str,
        entry: ManagedMemoryEntry,
        *,
        update_mask: str,
    ) -> ManagedMemoryEntry:
        """Update selected mutable fields on one entry."""
        if not update_mask.strip():
            raise ValueError("update_mask is required")
        from databricks.sdk.common.types.fieldmask import FieldMask

        mask = FieldMask()
        mask.FromJsonString(update_mask)
        return self._workspace_client.agent_kit.update_memory(
            self._entry_name(entry_id),
            entry,
            mask,
        )

    def delete_entry(self, entry_id: str) -> None:
        """Delete one entry by its ID."""
        self._workspace_client.agent_kit.delete_memory(self._entry_name(entry_id))

    def _entry_name(self, entry_id: str) -> str:
        encoded_entry_id = _path_segment(entry_id, name="entry_id")
        return f"{self._store_name}/entries/{encoded_entry_id}"

    def _get(
        self,
        path: str,
        *,
        query: dict[str, object] | None = None,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        workspace_id = self._workspace_client.config.workspace_id
        if workspace_id:
            headers["X-Databricks-Workspace-Id"] = workspace_id
        response = self._workspace_client.api_client.do(
            "GET",
            path,
            query=query,
            headers=headers,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Databricks Managed Memory returned a non-object response")
        return cast(dict[str, object], response)
