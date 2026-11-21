"""Tests for the authenticated Databricks Managed Memory REST client."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, cast

import pytest

from omnigent.stores.memory_store.databricks_client import (
    DatabricksManagedMemoryClient,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient


class _RecordingApiClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response: object = {"ok": True}

    def do(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> object:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "body": body,
            }
        )
        return self.response


class _RecordingWorkspaceClient:
    def __init__(self) -> None:
        self.api_client = _RecordingApiClient()


@pytest.fixture()
def client_and_api_client() -> tuple[DatabricksManagedMemoryClient, _RecordingApiClient]:
    workspace_client = _RecordingWorkspaceClient()
    client = DatabricksManagedMemoryClient(
        "support-memory",
        workspace_client=cast("WorkspaceClient", workspace_client),
    )
    return client, workspace_client.api_client


def test_store_and_permission_requests_use_canonical_paths(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client

    assert client.get_store() == {"ok": True}
    assert client.get_effective_permission() == {"ok": True}

    assert api_client.calls == [
        {
            "method": "GET",
            "path": "/api/2.0/agents/memory-stores/support-memory",
            "query": None,
            "body": None,
        },
        {
            "method": "GET",
            "path": "/api/2.0/agents/memory-stores/support-memory/permissions/self",
            "query": None,
            "body": None,
        },
    ]


def test_entry_requests_use_canonical_shapes(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client

    client.create_entry(
        {"actor_id": "alice", "path": "/preferences", "content": "Concise"},
        entry_id="entry one",
    )
    client.get_entry("entry one", view="MANAGED_MEMORY_ENTRY_VIEW_BASIC")
    client.list_entries(
        "alice",
        path_prefix="/preferences",
        page_size=25,
        page_token="next",
        view="MANAGED_MEMORY_ENTRY_VIEW_BASIC",
    )
    client.search_entries(
        "alice",
        "response style",
        path_prefix="/preferences",
        page_size=10,
    )
    client.update_entry(
        "entry one",
        {"content": "Very concise", "description": "Response style"},
        update_mask="content,description",
    )
    client.delete_entry("entry one")

    base = "/api/2.0/agents/memory-stores/support-memory/entries"
    entry_path = f"{base}/entry%20one"
    assert api_client.calls == [
        {
            "method": "POST",
            "path": base,
            "query": {"managed_memory_entry_id": "entry one"},
            "body": {
                "actor_id": "alice",
                "path": "/preferences",
                "content": "Concise",
            },
        },
        {
            "method": "GET",
            "path": entry_path,
            "query": {"view": "MANAGED_MEMORY_ENTRY_VIEW_BASIC"},
            "body": None,
        },
        {
            "method": "GET",
            "path": base,
            "query": {
                "actor_id": "alice",
                "path_prefix": "/preferences",
                "page_size": 25,
                "page_token": "next",
                "view": "MANAGED_MEMORY_ENTRY_VIEW_BASIC",
            },
            "body": None,
        },
        {
            "method": "POST",
            "path": f"{base}:search",
            "query": None,
            "body": {
                "actor_id": "alice",
                "query": "response style",
                "path_prefix": "/preferences",
                "page_size": 10,
            },
        },
        {
            "method": "PATCH",
            "path": entry_path,
            "query": {"update_mask": "content,description"},
            "body": {"content": "Very concise", "description": "Response style"},
        },
        {
            "method": "DELETE",
            "path": entry_path,
            "query": None,
            "body": None,
        },
    ]


def test_unified_auth_selectors_are_forwarded_to_workspace_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: dict[str, object] = {}
    workspace_client = _RecordingWorkspaceClient()

    def workspace_client_factory(**kwargs: object) -> _RecordingWorkspaceClient:
        constructed.update(kwargs)
        return workspace_client

    sdk_module = types.ModuleType("databricks.sdk")
    sdk_module.WorkspaceClient = workspace_client_factory  # type: ignore[attr-defined]
    databricks_module = types.ModuleType("databricks")
    databricks_module.sdk = sdk_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks", databricks_module)
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk_module)

    client = DatabricksManagedMemoryClient(
        "support-memory",
        workspace_host="https://workspace.example.com",
        profile="memory-service",
        auth_type="oauth-m2m",
    )

    assert constructed == {
        "host": "https://workspace.example.com",
        "profile": "memory-service",
        "auth_type": "oauth-m2m",
        "product": "omnigent",
    }
    assert client.get_store() == {"ok": True}


def test_injected_client_cannot_be_combined_with_auth_selectors() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        DatabricksManagedMemoryClient(
            "support-memory",
            workspace_host="https://workspace.example.com",
            workspace_client=cast("WorkspaceClient", _RecordingWorkspaceClient()),
        )


@pytest.mark.parametrize("store_id", ["", "   "])
def test_store_id_is_required(store_id: str) -> None:
    with pytest.raises(ValueError, match="managed_memory_store_id is required"):
        DatabricksManagedMemoryClient(
            store_id,
            workspace_client=cast("WorkspaceClient", _RecordingWorkspaceClient()),
        )


def test_update_mask_is_required(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, _ = client_and_api_client
    with pytest.raises(ValueError, match="update_mask is required"):
        client.update_entry("entry", {"content": "new"}, update_mask=" ")


def test_sdk_errors_are_not_replaced(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client

    class PermissionDeniedError(Exception):
        pass

    error = PermissionDeniedError("credential lacks WRITE")

    def raise_permission_denied(*args: object, **kwargs: object) -> object:
        raise error

    api_client.do = raise_permission_denied  # type: ignore[method-assign]

    with pytest.raises(PermissionDeniedError) as raised:
        client.create_entry({"actor_id": "alice", "path": "/preferences", "content": "Concise"})
    assert raised.value is error


def test_non_object_response_fails_explicitly(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client
    api_client.response = []

    with pytest.raises(RuntimeError, match="non-object response"):
        client.get_store()
