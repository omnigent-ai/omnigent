"""Authenticated REST client for Databricks Managed Memory.

The client uses the Databricks SDK's unified authentication and raw API client.
Credentials stay in the server's SDK configuration, where OAuth tokens can be
renewed for long-running processes. MemoryStore adaptation and user-to-actor
mapping live above this transport layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient


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
        self._store_path = f"/api/2.0/agents/memory-stores/{encoded_store_id}"

    def get_store(self) -> dict[str, object]:
        """Return configured store metadata and fail if it is unavailable."""
        return self._request("GET", self._store_path)

    def get_effective_permission(self) -> dict[str, object]:
        """Return this credential's effective permission on the store."""
        return self._request("GET", f"{self._store_path}/permissions/self")

    def create_entry(
        self,
        entry: dict[str, object],
        *,
        entry_id: str | None = None,
    ) -> dict[str, object]:
        """Create an entry, optionally using a caller-selected entry ID."""
        query: dict[str, object] | None = None
        if entry_id is not None:
            query = {"managed_memory_entry_id": _required_string(entry_id, name="entry_id")}
        return self._request(
            "POST",
            f"{self._store_path}/entries",
            query=query,
            body=entry,
        )

    def get_entry(self, entry_id: str, *, view: str | None = None) -> dict[str, object]:
        """Read one entry by its ID."""
        return self._request(
            "GET",
            self._entry_path(entry_id),
            query=_without_none({"view": view}),
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
        view: str | None = None,
    ) -> dict[str, object]:
        """List entries for one actor using canonical pagination fields."""
        return self._request(
            "GET",
            f"{self._store_path}/entries",
            query=_without_none(
                {
                    "actor_id": actor_id,
                    "path": path,
                    "path_prefix": path_prefix,
                    "session_id": session_id,
                    "page_size": page_size,
                    "page_token": page_token,
                    "view": view,
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
        view: str | None = None,
    ) -> dict[str, object]:
        """Search one actor's entries and return ranked results."""
        body = _without_none(
            {
                "actor_id": actor_id,
                "query": query,
                "path_prefix": path_prefix,
                "session_id": session_id,
                "page_size": page_size,
                "view": view,
            }
        )
        return self._request(
            "POST",
            f"{self._store_path}/entries:search",
            body=body,
        )

    def update_entry(
        self,
        entry_id: str,
        entry: dict[str, object],
        *,
        update_mask: str,
    ) -> dict[str, object]:
        """Update selected mutable fields on one entry."""
        if not update_mask.strip():
            raise ValueError("update_mask is required")
        return self._request(
            "PATCH",
            self._entry_path(entry_id),
            query={"update_mask": update_mask},
            body=entry,
        )

    def delete_entry(self, entry_id: str) -> None:
        """Delete one entry by its ID."""
        self._request("DELETE", self._entry_path(entry_id))

    def _entry_path(self, entry_id: str) -> str:
        encoded_entry_id = _path_segment(entry_id, name="entry_id")
        return f"{self._store_path}/entries/{encoded_entry_id}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response = self._workspace_client.api_client.do(
            method,
            path,
            query=query,
            body=body,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Databricks Managed Memory returned a non-object response")
        return cast(dict[str, object], response)
